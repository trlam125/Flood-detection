"""Camera supervision independent of browser connections and model throughput."""
from __future__ import annotations

import copy
from collections import deque
import threading
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .events import EventDecision, EventPolicy, EventTracker
from .inference import analyze_frame, annotate_frame
from .regions import rasterize_roi
from .recovery import restore_checkpoint
from .severity import SeverityThresholds
from .sources import CameraSource


def status_image(message: str) -> bytes:
    image = np.full((360, 640, 3), (24, 16, 9), np.uint8)
    cv2.putText(image, message, (24, 175), cv2.FONT_HERSHEY_SIMPLEX, .65, (190, 200, 215), 1, cv2.LINE_AA)
    return cv2.imencode(".jpg", image)[1].tobytes()


class LiveMonitor:
    def __init__(self, camera_id: str, run_id: str, configuration: dict, source: CameraSource,
                 database, output_dir: Path, provider: Callable, inference_lock, persist: Callable,
                 capture_factory: Callable, clock=time.monotonic, executor=None, recovery=None, capacity_check=None):
        self.camera_id, self.id = camera_id, run_id
        self.configuration, self.source = copy.deepcopy(configuration), source
        self.db, self.output_dir, self.provider = database, output_dir, provider
        self.inference_lock, self.persist, self.capture_factory = inference_lock, persist, capture_factory
        self.clock = clock
        self.capacity_check = capacity_check
        self.storage_paused = False
        self.latencies = deque(maxlen=600)
        self.skipped_busy = self.stale_results = self.analysis_failures = 0
        self.execute = executor or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.started_mono = clock()
        self.started_at_unix = configuration["started_at_unix"]
        self.policy = EventPolicy.model_validate(configuration["event_policy"])
        self.thresholds = SeverityThresholds(**configuration["thresholds"])
        if source.analysis_interval_sec >= min(source.stale_after_sec, self.policy.max_gap_sec):
            raise ValueError("Live analysis interval must be shorter than both the observation gap and stale-frame limit")
        self.tracker = EventTracker(self.thresholds, self.policy)
        self.decision = EventDecision("UNKNOWN", "UNKNOWN", None, 0, 0, (), None)
        self.lock = threading.RLock()
        self._finalized = False
        self._persistence_failed = False
        self.recovery_count = 0
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self.capture = None
        self.generation = 0
        self.opened_mono = self.started_mono
        self.next_retry = self.started_mono
        self.next_analysis = self.started_mono
        self.failures = self.reconnects = 0
        self.stable_since = None
        self.lifecycle, self.connection = "starting", "connecting"
        self.connection_error = self.analysis_error = None
        self.preview_error = self.evidence_error = None
        self.latest_frame = None
        self.annotated = None
        self.last_analysis_mono = self.last_frame_mono = None
        self.last_completed_unix = None
        self._inferring = False
        self.samples = 0
        self.ratio_sum = self.max_ratio = 0.0
        self.last_metrics = None
        self.model_info = {}
        self.previous_ratio = self.previous_time = None
        self.roi_shape = self.roi = None
        if recovery is None:
            self.db.start_analysis(run_id, f"live:{camera_id}", self.configuration, self.checkpoint())
        else:
            restored, self.tracker, self.decision = restore_checkpoint(configuration, recovery)
            self.started_mono = clock() - restored["elapsed_sec"]
            self.opened_mono = self.next_retry = self.next_analysis = clock()
            self.samples, self.ratio_sum, self.max_ratio = restored["samples"], restored["ratio_sum"], restored["max_ratio"]
            self.model_info, self.recovery_count = restored["model_info"], restored["recovery_count"] + 1
            self._persist(self.decision.transitions)

    @property
    def running(self):
        return self.lifecycle not in {"stopped", "error"}

    def start(self):
        self.thread = threading.Thread(target=self.run, name=f"camera-{self.camera_id[:8]}", daemon=True)
        self.thread.start()

    def _elapsed(self, now):
        return max(0, now - self.started_mono, self.tracker.last_update or 0)

    def checkpoint(self):
        return {"version": 1, "tracker": self.tracker.checkpoint(), "elapsed_sec": self._elapsed(self.clock()),
                "samples": self.samples, "ratio_sum": self.ratio_sum, "max_ratio": self.max_ratio,
                "model_info": self.model_info, "recovery_count": self.recovery_count}

    def _persist(self, transitions, final_result=None):
        try:
            return self.persist(transitions, checkpoint=self.checkpoint(), final_result=final_result)
        except Exception:
            self._persistence_failed = True
            self.lifecycle, self.connection = "error", "stopped"
            self.connection_error = None
            self.analysis_error = "Cannot save monitoring state; monitoring stopped. Check database storage and restart the server."
            self.decision = EventDecision("UNKNOWN", "UNKNOWN", None, 0, 0, (), None)
            self.annotated = None
            self.stop_flag.set()
            raise

    def _unknown(self, now, force=False):
        if self.tracker.confirmed == "UNKNOWN" and not force:
            return
        self.decision = self.tracker.update(self._elapsed(now), None)
        self._persist(self.decision.transitions)
        self.previous_ratio = self.previous_time = None
        self.annotated = None

    def _disconnect(self, now, reason):
        handle, self.capture = self.capture, None
        self.generation += 1
        with self.lock:
            self.connection, self.connection_error = "reconnecting", reason
            self._unknown(self.clock(), force=True)
            self.failures += 1
            self.reconnects += 1
            self.next_retry = self.clock() + min(30, 2 ** min(self.failures - 1, 5))
            self.stable_since = None
        if handle is not None:
            handle.close()
            with self.lock:
                self.next_retry = self.clock() + min(30, 2 ** min(self.failures - 1, 5))

    def step(self, now=None):
        now = self.clock() if now is None else now
        if self.stop_flag.is_set():
            return
        self.lifecycle = "running"
        if self.capture is None and now >= self.next_retry:
            try:
                self.capture = self.capture_factory()
                self.opened_mono = self.clock()
                self.connection = "connecting" if self.reconnects == 0 else "reconnecting"
                self.generation += 1
            except Exception:
                self._disconnect(now, "Cannot start the camera capture process")
        if self.capture is not None:
            polled = self.capture.poll()
            now = self.clock()
            if polled.failed or not polled.alive:
                self._disconnect(now, "Camera connection or image read failed")
            elif polled.frame is not None:
                frame = polled.frame
                if frame.received_mono <= now + .1 and now - frame.received_mono <= self.source.stale_after_sec:
                    with self.lock:
                        self.latest_frame, self.last_frame_mono = frame, frame.received_mono
                        self.connection, self.connection_error = "connected", None
                        if self.stable_since is None:
                            self.stable_since = now
                        if now - self.stable_since >= 30:
                            self.failures = 0
                else:
                    self._disconnect(now, "Received camera image is stale")
            elif self.last_frame_mono is None or self.last_frame_mono < self.opened_mono:
                if now - self.opened_mono > self.source.open_timeout_sec:
                    self._disconnect(now, "Camera open timed out")
            elif now - self.last_frame_mono > self.source.stale_after_sec:
                self._disconnect(now, "No fresh camera images received")
        with self.lock:
            if self.capacity_check is not None:
                try:
                    self.capacity_check()
                    self.storage_paused = False
                except OSError:
                    if not self.storage_paused:
                        self._unknown(now, force=True)
                    self.storage_paused = True
                    return
            if self.last_analysis_mono is not None and now - self.last_analysis_mono > self.policy.max_gap_sec:
                self._unknown(now)
            frame = self.latest_frame
            if (self.connection == "connected" and frame is not None and not self._inferring
                and now >= self.next_analysis and now - frame.received_mono <= min(self.source.stale_after_sec, self.policy.max_gap_sec)
                and (self.last_analysis_mono is None or frame.received_mono > self.last_analysis_mono)):
                self._inferring = True
                self.next_analysis = now + self.source.analysis_interval_sec
                generation = self.generation
                self.execute(lambda: self._infer(frame, generation))

    def _infer(self, packet, generation):
        acquired = self.inference_lock.acquire(blocking=False)
        inference_started = time.perf_counter()
        try:
            if not acquired or self.stop_flag.is_set():
                if not acquired:
                    self.skipped_busy += 1
                return
            with self.lock:
                if generation != self.generation or self.connection != "connected":
                    return
            frame = cv2.imdecode(np.frombuffer(packet.jpeg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError("Camera JPEG could not be decoded")
            with self.lock:
                if self.roi_shape != frame.shape[:2]:
                    camera = self.configuration["camera"]
                    self.roi = None if camera["roi"] is None else rasterize_roi(camera["roi"], frame.shape[1], frame.shape[0],
                        camera["reference_width"], camera["reference_height"])
                    self.roi_shape = frame.shape[:2]
                previous, previous_time = self.previous_ratio, self.previous_time
                roi = self.roi
            at = packet.received_mono - self.started_mono
            elapsed = None if previous_time is None else at - previous_time
            if elapsed is not None and elapsed > self.policy.max_gap_sec:
                previous = None
            if elapsed is not None and elapsed <= 0:
                return
            segmenter = self.provider()
            mask, metrics = analyze_frame(segmenter, frame, self.thresholds, previous, roi,
                elapsed_sec=elapsed, ema_time_constant_sec=self.policy.ema_time_constant_sec)
            model_info = segmenter.info()
            now = self.clock()
            with self.lock:
                if (self.stop_flag.is_set() or generation != self.generation or self.connection != "connected"
                    or now - packet.received_mono > min(self.source.stale_after_sec, self.policy.max_gap_sec)
                    or (self.tracker.last_update is not None and at < self.tracker.last_update)):
                    self.stale_results += 1
                    return
                decision = self.tracker.update(at, metrics.water_ratio)
                self.preview_error = None
                try:
                    annotated = annotate_frame(frame, mask, roi)
                    ok, jpeg = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 88])
                    if not ok:
                        raise ValueError("JPEG encoding failed")
                    self.annotated = jpeg.tobytes()
                except Exception:
                    self.annotated = packet.jpeg
                    self.preview_error = "Overlay unavailable; displaying the original camera image"
                self.decision, self.last_metrics = decision, metrics
                self.last_analysis_mono = packet.received_mono
                self.last_completed_unix = time.time()
                self.previous_ratio, self.previous_time = metrics.water_ratio, at
                self.model_info = model_info
                self.samples += 1
                self.latencies.append((time.perf_counter() - inference_started) * 1000)
                self.ratio_sum += metrics.water_ratio
                self.max_ratio = max(self.max_ratio, metrics.water_ratio)
                self.analysis_error = None
                for transition in decision.transitions:
                    if transition["notify"]:
                        try:
                            folder = self.output_dir / "live_evidence"
                            folder.mkdir(exist_ok=True)
                            filename = f"{transition['event_id']}_{transition['id']}.jpg"
                            (folder / filename).write_bytes(self.annotated)
                            transition["snapshot_url"] = f"/media/outputs/live_evidence/{filename}"
                            transition["event"]["latest_snapshot_url"] = transition["snapshot_url"]
                            for event in self.tracker.events:
                                if event["id"] == transition["event_id"]:
                                    event["latest_snapshot_url"] = transition["snapshot_url"]
                            self.evidence_error = None
                        except OSError:
                            self.evidence_error = "Alert snapshot could not be saved; check output storage"
                            transition["evidence_error"] = self.evidence_error
                self._persist(decision.transitions)
        except Exception as exc:
            with self.lock:
                if self.stop_flag.is_set() or generation != self.generation:
                    return
                self.analysis_failures += 1
                self.analysis_error = f"Camera analysis failed: {exc}"
                self._unknown(self.clock(), force=True)
        finally:
            if acquired:
                self.inference_lock.release()
            with self.lock:
                self._inferring = False

    def run(self):
        try:
            while not self.stop_flag.wait(.1):
                self.step()
        except Exception:
            self.lifecycle = "error"
            self.analysis_error = "Camera supervisor failed; restart monitoring"
        finally:
            self.stop_flag.set()
            if self.capture is not None:
                self.capture.close()
                self.capture = None
            if not self._persistence_failed:
                try:
                    self.finish("supervisor_error" if self.lifecycle == "error" else "monitor_stopped")
                except Exception:
                    pass  # _persist reports UNKNOWN and leaves the durable checkpoint intact.

    def finish(self, reason="monitor_stopped"):
        with self.lock:
            if self._finalized or self._persistence_failed:
                return
            self.stop_flag.set()
            transitions = self.tracker.finish(self._elapsed(self.clock()), reason=reason)
            result = {"analysis_id": self.id, "video_id": f"live:{self.camera_id}", "output_url": None,
                "analysis_mode": "live_camera", "configuration": self.configuration,
                "frames": self.samples, "analyzed_frames": self.samples, "duration_sec": self._elapsed(self.clock()),
                "highest_severity": self.tracker.highest_confirmed, "max_water_ratio": self.max_ratio,
                "mean_water_ratio": self.ratio_sum / max(1, self.samples), "events": self.tracker.events,
                "model": self.model_info, "event_count": len(self.tracker.events)}
            self._persist(transitions, final_result=result)
            self._finalized = True
            self.connection = "stopped"
            self.lifecycle = "stopped" if reason == "monitor_stopped" else "error"
            self.decision = EventDecision("UNKNOWN", "UNKNOWN", None, 0, 0, (), None)

    def stop(self):
        self.lifecycle = "stopping" if self.running else self.lifecycle
        self.stop_flag.set()
        if self.thread is not None:
            self.thread.join(timeout=4)
        else:
            if self.capture is not None:
                self.capture.close()
                self.capture = None
            if not self.tracker._finished:
                self.finish()

    def state(self):
        now = self.clock()
        with self.lock:
            return {"camera_id": self.camera_id, "run_id": self.id,
                "camera_name": self.configuration["camera"]["name"], "camera_revision": self.configuration["camera"]["revision"],
                "lifecycle": self.lifecycle, "connection": self.connection, **self.decision.state(),
                "source_revision": self.configuration["source_revision"], "started_at_unix": self.started_at_unix,
                "recovery_count": self.recovery_count,
                "requires_server_restart": self._persistence_failed,
                "storage_paused": self.storage_paused,
                "fresh_frame_limit_sec": self.source.stale_after_sec,
                "fresh_analysis_limit_sec": self.policy.max_gap_sec,
                "inference_ms": {"samples": len(self.latencies), "last": self.latencies[-1] if self.latencies else None,
                    "p50": float(np.percentile(self.latencies, 50)) if self.latencies else None,
                    "p95": float(np.percentile(self.latencies, 95)) if self.latencies else None},
                "skipped_busy": self.skipped_busy, "stale_results": self.stale_results, "analysis_failures": self.analysis_failures,
                "last_frame_at_unix": None if self.last_frame_mono is None else self.started_at_unix + self.last_frame_mono - self.started_mono,
                "last_analysis_at_unix": self.last_completed_unix,
                "frame_age_sec": None if self.last_frame_mono is None else max(0, now - self.last_frame_mono),
                "analysis_age_sec": None if self.last_analysis_mono is None else max(0, now - self.last_analysis_mono),
                "water_ratio": None if self.last_metrics is None else self.last_metrics.water_ratio,
                "confidence": None if self.last_metrics is None else self.last_metrics.confidence,
                "last_error": self.connection_error or ("Analysis paused because storage is low" if self.storage_paused else None) or self.analysis_error or self.preview_error or self.evidence_error,
                "reconnect_attempts": self.reconnects, "next_retry_sec": max(0, self.next_retry - now),
                "analyzed_frames": self.samples, "ratio_scope": self.configuration["ratio_scope"]}

    def jpeg(self, raw=False):
        now = self.clock()
        with self.lock:
            if self.connection != "connected" or self.latest_frame is None or now - self.latest_frame.received_mono > self.source.stale_after_sec:
                return None
            if raw:
                return self.latest_frame.jpeg
            if self.annotated is not None and self.last_analysis_mono is not None and now - self.last_analysis_mono <= min(self.source.stale_after_sec, self.policy.max_gap_sec):
                return self.annotated
            return self.latest_frame.jpeg


class LiveRegistry:
    def __init__(self):
        self.monitors: dict[str, LiveMonitor] = {}
        self.lock = threading.RLock()

    def start(self, camera_id: str, factory: Callable, maximum: int) -> LiveMonitor:
        with self.lock:
            existing = self.monitors.get(camera_id)
            if existing is not None and existing.running:
                return existing
            if sum(monitor.running for monitor in self.monitors.values()) >= maximum:
                raise ValueError("Live camera limit reached; stop another camera first")
            monitor = factory()
            self.monitors[camera_id] = monitor
            monitor.start()
            return monitor

    def get(self, camera_id: str):
        with self.lock:
            return self.monitors.get(camera_id)

    def stop(self, camera_id: str):
        monitor = self.get(camera_id)
        if monitor is not None:
            monitor.stop()

    def close(self):
        with self.lock:
            monitors = list(self.monitors.values())
        for monitor in monitors:
            monitor.stop()
