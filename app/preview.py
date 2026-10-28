"""Ordered browser-frame analysis and export using the same cached masks."""
from __future__ import annotations

import base64
import copy
import io
import math
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Literal

import cv2
import imageio.v2 as imageio
import numpy as np
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from .events import EventDecision, EventPolicy, EventTracker
from .inference import FrameMetrics, analyze_frame, annotate_frame, segmentation_overlay
from .regions import rasterize_roi
from .severity import SeverityThresholds, max_severity

MAX_FRAME_BYTES = 3 * 1024 * 1024
MAX_FRAME_EDGE = 1280
MAX_SAMPLES = 100000


class PreviewOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    camera_id: str | None = Field(default=None, min_length=1, max_length=64)
    camera_revision: int | None = Field(default=None, ge=1)
    sample_every_n_frames: int | None = Field(default=None, ge=1, le=30)
    low_ratio: float | None = Field(default=None, ge=0, le=1)
    moderate_ratio: float | None = Field(default=None, ge=0, le=1)
    critical_ratio: float | None = Field(default=None, ge=0, le=1)
    event_policy: EventPolicy | None = None


class PreviewStart(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    duration_sec: float = Field(gt=0, le=86400)
    width: int = Field(ge=2, le=32768)
    height: int = Field(ge=2, le=32768)
    interval_sec: float = Field(default=.5, ge=.01, le=5)
    options: PreviewOptions = Field(default_factory=PreviewOptions)
    export_video: bool = True
    source_clock: Literal["browser_media_seek", "native_video_fps"] = "browser_media_seek"


class PreviewConflict(ValueError):
    pass


class PreviewSession:
    def __init__(self, job_id: str, video_id: str, metadata: PreviewStart, configuration: dict, settings,
                 database, provider: Callable, inference_lock, persist: Callable, update: Callable, capacity_check=None):
        self.id, self.video_id, self.metadata = job_id, video_id, metadata
        self.configuration = copy.deepcopy(configuration)
        self.settings, self.db, self.provider = settings, database, provider
        self.inference_lock, self.persist, self.update = inference_lock, persist, update
        self.capacity_check = capacity_check
        self.lock = threading.RLock()
        self.policy = EventPolicy.model_validate(configuration["event_policy"])
        self.thresholds = SeverityThresholds(**configuration["thresholds"])
        if metadata.interval_sec > self.policy.max_gap_sec:
            raise ValueError("Read interval exceeds the maximum observation gap")
        if math.ceil(metadata.duration_sec / metadata.interval_sec) > MAX_SAMPLES:
            raise ValueError("Video needs too many samples; increase the read interval")
        self.cache_dir = settings.output_dir / f"{job_id}_frame_cache"
        self.cache_dir.mkdir()
        self.original_path = settings.upload_dir / f"{video_id}{Path(metadata.filename).suffix.lower()}"
        self.part_path = self.original_path.with_suffix(self.original_path.suffix + ".part")
        self.status = "previewing"
        self.original_ready = self.original_uploading = self.analysis_complete = False
        self.cancelled = self.rendering = False
        self.render_thread: threading.Thread | None = None
        self.original_info: dict[str, Any] = {}
        self.tracker = EventTracker(self.thresholds, self.policy)
        self.decision = EventDecision("UNKNOWN", "UNKNOWN", None, 0, 0, (), None)
        self.records: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        self.previous_ratio = self.previous_time = None
        self.roi = self.shape = None
        self.model_info: dict[str, Any] = {}
        self.last_activity = time.monotonic()
        self.last_metrics: FrameMetrics | None = None
        self.ratio_sum = self.max_ratio = 0.0
        self.highest_observed = "UNKNOWN"
        self.sample_count = 0
        try:
            self.db.start_analysis(job_id, video_id, self.configuration)
        except Exception:
            self.cache_dir.rmdir()
            raise
        self.publish()

    def region(self, width: int, height: int):
        camera = self.configuration.get("camera") or {}
        if camera.get("roi") is None:
            return None
        return rasterize_roi(camera["roi"], width, height, camera["reference_width"], camera["reference_height"])

    def publish(self, **extra):
        live = {**self.decision.state(), "water_ratio": None if self.last_metrics is None else self.last_metrics.water_ratio,
                "confidence": None if self.last_metrics is None else self.last_metrics.confidence,
                "time_sec": self.previous_time,
                "ratio_scope": self.configuration["ratio_scope"]}
        at = self.previous_time or 0
        self.update(status=self.status, progress=min(89, 90 * at / self.metadata.duration_sec),
                    processed_frames=self.sample_count, analyzed_frames=self.sample_count,
                    analysis_mode="browser_frames", analyzed_until_sec=at, duration_sec=self.metadata.duration_sec,
                    original_ready=self.original_ready, analysis_complete=self.analysis_complete, live=live, **extra)

    def require_running(self):
        if self.status in {"error", "done"} or self.cancelled:
            raise PreviewConflict("This analysis session has already ended")

    def frame(self, sequence: int, time_sec: float, jpeg: bytes) -> dict[str, Any]:
        with self.lock:
            self.require_running()
            if self.analysis_complete:
                raise PreviewConflict("Frame analysis is already complete")
            if sequence != self.sample_count:
                raise PreviewConflict(f"Expected frame sequence {self.sample_count}")
            expected = sequence * self.metadata.interval_sec
            if not math.isfinite(time_sec) or time_sec < 0 or time_sec >= self.metadata.duration_sec or abs(time_sec - expected) > .002:
                raise ValueError("Frame timestamp does not match the ordered read interval")
            if not jpeg or len(jpeg) > MAX_FRAME_BYTES:
                raise ValueError("JPEG frame is empty or too large")
            with Image.open(io.BytesIO(jpeg)) as header:
                if header.format != "JPEG" or min(header.size) < 2 or max(header.size) > MAX_FRAME_EDGE:
                    raise ValueError("Frames must be JPEG images with dimensions between 2 and 1280 pixels")
                width, height = header.size
            if abs((width / height) / (self.metadata.width / self.metadata.height) - 1) > .01:
                raise ValueError("Frame aspect ratio differs from the selected video")
            if self.shape is not None and self.shape != (height, width):
                raise ValueError("Frame dimensions changed during analysis")
            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError("Cannot decode JPEG frame")
            self.last_activity = time.monotonic()
            try:
                if self.capacity_check is not None:
                    self.capacity_check()
                if self.shape is None:
                    self.shape = frame.shape[:2]
                    self.roi = self.region(width, height)
                elapsed = None if self.previous_time is None else time_sec - self.previous_time
                if elapsed is not None and elapsed > self.policy.max_gap_sec:
                    self.previous_ratio = None
                with self.inference_lock:
                    segmenter = self.provider()
                    mask, metrics = analyze_frame(segmenter, frame, self.thresholds, self.previous_ratio,
                        self.roi, elapsed, self.policy.ema_time_constant_sec)
                    self.model_info = segmenter.info()
                decision = self.tracker.update(time_sec, metrics.water_ratio)
                self.alerts.extend(self.persist(decision.transitions))
                mask_path = None
                if self.metadata.export_video:
                    mask_path = self.cache_dir / f"{sequence:08d}.png"
                    ok, encoded = cv2.imencode(".png", mask)
                    if not ok:
                        raise RuntimeError("Cannot cache segmentation mask")
                    mask_path.write_bytes(encoded.tobytes())
                else:
                    self.records.clear()
                self.records.append({"time_sec": time_sec, "mask_path": mask_path})
                if not self.timeline or decision.transitions or time_sec - self.timeline[-1]["time_sec"] >= .75:
                    self.timeline.append({"time_sec": time_sec, **asdict(metrics), **decision.state()})
                self.previous_ratio, self.previous_time = metrics.water_ratio, time_sec
                self.decision, self.last_metrics = decision, metrics
                self.ratio_sum += metrics.water_ratio
                self.max_ratio = max(self.max_ratio, metrics.water_ratio)
                self.highest_observed = max_severity(self.highest_observed, metrics.severity)
                self.sample_count += 1
                annotated = annotate_frame(frame, mask, self.roi)
                ok, encoded = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 88])
                if not ok:
                    raise RuntimeError("Cannot encode preview image")
                overlay_ok, overlay = cv2.imencode(".png", segmentation_overlay(mask, self.roi))
                if not overlay_ok:
                    raise RuntimeError("Cannot encode segmentation overlay")
                self.publish()
                return {"job_id": self.id, "sequence": sequence, "time_sec": time_sec,
                        "live": {**decision.state(), "water_ratio": metrics.water_ratio, "ratio_scope": metrics.ratio_scope,
                                 "confidence": metrics.confidence, "time_sec": time_sec},
                        "original_ready": self.original_ready, "analyzed_frames": self.sample_count,
                        "annotated_jpeg": base64.b64encode(encoded).decode("ascii"),
                        "overlay_png": base64.b64encode(overlay).decode("ascii")}
            except Exception as exc:
                self.fail(str(exc))
                raise

    def complete_frames(self) -> dict[str, Any]:
        with self.lock:
            self.require_running()
            if not self.analysis_complete:
                if not self.records or self.metadata.duration_sec - self.records[-1]["time_sec"] > self.metadata.interval_sec + .002:
                    raise ValueError("Frame sequence is incomplete; the video has an unanalysed tail")
                self.alerts.extend(self.persist(self.tracker.finish(self.metadata.duration_sec)))
                for event in self.tracker.events:
                    self.db.save_event(self.id, event, self.configuration)
                self.analysis_complete = True
            self.last_activity = time.monotonic()
            if not self.metadata.export_video:
                result = self.result(frames=self.original_info.get("frames", 0))
                self.db.finish_analysis(self.id, result)
                self.status = "done"
                self.update(status="done", progress=100, result=result)
                self.clear_cache()
                return {"job_id": self.id, "status": self.status, "analysis_result": result}
            self.status = "waiting_upload" if not self.original_ready else self.status
            self.publish()
            self.maybe_render()
            return {"job_id": self.id, "status": self.status, "original_ready": self.original_ready,
                    "analysis_result": self.result()}

    def begin_original(self, filename: str):
        with self.lock:
            self.require_running()
            if not self.metadata.export_video:
                raise PreviewConflict("Video export is disabled for this session")
            if filename != self.metadata.filename:
                raise ValueError("Original filename does not match this analysis session")
            if self.original_ready or self.original_uploading:
                raise PreviewConflict("The original video was already attached or is uploading")
            self.original_uploading = True
            self.last_activity = time.monotonic()

    def attach_original(self, size_bytes: int):
        with self.lock:
            self.require_running()
            if size_bytes != self.metadata.size_bytes:
                raise ValueError("Original file size does not match the selected video")
        cap = cv2.VideoCapture(str(self.part_path))
        try:
            fps = float(cap.get(cv2.CAP_PROP_FPS))
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            ok, first = cap.read()
            if not ok or first is None or not math.isfinite(fps) or fps <= 0 or total <= 0:
                raise ValueError("Cannot read the original video or its frame rate")
            height, width = first.shape[:2]
            if (width, height) != (self.metadata.width, self.metadata.height):
                raise ValueError("Original dimensions differ from the browser video")
            duration = total / fps
            if abs(duration - self.metadata.duration_sec) > max(.25, 2 / fps):
                raise ValueError("Original duration differs from the selected video")
            self.region(width, height)
        finally:
            cap.release()
        with self.lock:
            self.require_running()
            self.part_path.replace(self.original_path)
            self.original_info = {"fps": fps, "frames": total, "width": width, "height": height}
            self.original_ready, self.original_uploading = True, False
            self.last_activity = time.monotonic()
            self.publish()
            self.maybe_render()
            return {"job_id": self.id, "original_ready": True}

    def maybe_render(self):
        if self.analysis_complete and self.original_ready and not self.rendering:
            self.rendering = True
            self.status = "rendering"
            self.publish()
            self.render_thread = threading.Thread(target=self.render, daemon=True)
            self.render_thread.start()

    def result(self, frames: int = 0, output_url: str | None = None) -> dict[str, Any]:
        info = self.original_info
        final = {**self.decision.state(), "event_status": None if self.tracker.active is None else self.tracker.active["status"]}
        if self.status == "error":
            final["severity"] = "UNKNOWN"
        return {"analysis_id": self.id, "video_id": self.video_id, "output_url": output_url,
                "frames": frames, "analyzed_frames": self.sample_count, "fps": info.get("fps"),
                "width": self.metadata.width, "height": self.metadata.height, "duration_sec": self.metadata.duration_sec,
                "max_water_ratio": self.max_ratio, "mean_water_ratio": self.ratio_sum / max(1, self.sample_count),
                "highest_severity": self.tracker.highest_confirmed, "highest_observed_severity": self.highest_observed,
                "final_state": final,
                "final_observation": None if self.last_metrics is None else {
                    **asdict(self.last_metrics), **final, "time_sec": self.previous_time},
                "timeline": copy.deepcopy(self.timeline), "events": copy.deepcopy(self.tracker.events),
                "event_count": len(self.tracker.events), "alerts": copy.deepcopy(self.alerts), "alert_count": len(self.alerts),
                "model": self.model_info, "thresholds": self.configuration["thresholds"], "configuration": self.configuration,
                "pending_export": self.metadata.export_video and self.analysis_complete and output_url is None and self.status != "error",
                "ratio_scope": self.configuration["ratio_scope"], "sample_interval_sec": self.metadata.interval_sec,
                "analysis_resolution": None if self.shape is None else {"width": self.shape[1], "height": self.shape[0]},
                "monitored_pixels": None if self.last_metrics is None else self.last_metrics.monitored_pixels}

    def render(self):
        cap, writer = None, None
        try:
            cap = cv2.VideoCapture(str(self.original_path))
            info = self.original_info
            output = self.settings.output_dir / f"{self.id}_annotated.mp4"
            writer = imageio.get_writer(str(output), fps=info["fps"], codec="libx264", quality=7, macro_block_size=None)
            roi = self.region(info["width"], info["height"])
            index, sample_index, cached_index = 0, 0, -1
            mask = None
            previous_pts = None
            first_pts = 0.0
            pts_supported = None
            while True:
                if self.cancelled:
                    return
                if self.capacity_check is not None and index % max(1, round(info["fps"])) == 0:
                    self.capacity_check()
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                if frame.shape[:2] != (info["height"], info["width"]):
                    raise ValueError("Original frame dimensions changed")
                pts = float(cap.get(cv2.CAP_PROP_POS_MSEC)) / 1000
                if index == 0 and math.isfinite(pts):
                    first_pts = pts
                if index == 1:
                    pts_supported = math.isfinite(pts) and pts > (previous_pts or 0)
                at = pts - first_pts if pts_supported else index / info["fps"]
                if pts_supported and (not math.isfinite(pts) or (previous_pts is not None and pts <= previous_pts)):
                    raise ValueError("Original video timestamps are not monotonic")
                previous_pts = pts
                while sample_index + 1 < len(self.records) and self.records[sample_index + 1]["time_sec"] <= at + 1e-6:
                    sample_index += 1
                if cached_index != sample_index:
                    record = self.records[sample_index]
                    mask = cv2.imdecode(np.frombuffer(record["mask_path"].read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
                    if mask is None:
                        raise RuntimeError("A cached segmentation mask is missing")
                    mask = cv2.resize(mask, (info["width"], info["height"]), interpolation=cv2.INTER_NEAREST)
                    if roi is not None:
                        mask = ((mask > 0) & roi).astype(np.uint8)
                    cached_index = sample_index
                annotated = annotate_frame(frame, mask, roi)
                writer.append_data(cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB))
                index += 1
                if index % max(1, int(info["fps"])) == 0:
                    self.update(status="rendering", progress=min(99, 90 + 10 * index / info["frames"]), rendered_frames=index)
            if index == 0 or index + 1 < info["frames"]:
                raise ValueError("Original video ended before all frames could be exported")
            try:
                writer.close()
            finally:
                writer = None
            with self.lock:
                if self.cancelled:
                    return
                result = self.result(index, f"/media/outputs/{output.name}")
                result["output_alignment_clock"] = "opencv_pts" if pts_supported else "fps_estimate"
                self.db.finish_analysis(self.id, result)
                self.status = "done"
                self.update(status="done", progress=100, processed_frames=index, result=result)
        except Exception as exc:
            self.fail(f"Video export failed: {exc}")
        finally:
            if cap is not None:
                cap.release()
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            self.rendering = False
            self.clear_cache()

    def fail(self, message: str):
        with self.lock:
            if self.status in {"done", "error"}:
                return
            self.cancelled = True
            self.status = "error"
            at = max(self.tracker.last_update or 0, self.previous_time or 0)
            self.persist(self.tracker.finish(at, reason="preview_interrupted"))
            for event in self.tracker.events:
                self.db.save_event(self.id, event, self.configuration)
            partial = self.result()
            partial["partial"] = True
            self.db.fail_analysis(self.id, message, partial_result=partial)
            self.update(status="error", progress=100, error=message, partial_result=partial,
                        live={"severity": "UNKNOWN", "observed_severity": "UNKNOWN"})
            if not self.rendering:
                self.clear_cache()

    def clear_cache(self):
        with self.lock:
            for record in self.records:
                if record["mask_path"] is not None:
                    record["mask_path"].unlink(missing_ok=True)
            self.records.clear()
            if self.cache_dir.exists():
                self.cache_dir.rmdir()


class PreviewRegistry:
    def __init__(self):
        self.sessions: dict[str, PreviewSession] = {}
        self.lock = threading.Lock()

    def add(self, session: PreviewSession):
        with self.lock:
            self.sessions[session.id] = session

    def get(self, job_id: str) -> PreviewSession:
        with self.lock:
            session = self.sessions.get(job_id)
        if session is None:
            raise KeyError(job_id)
        if session.status not in {"done", "error"}:
            session.last_activity = time.monotonic()
        return session

    def expire(self, idle_sec: float):
        now = time.monotonic()
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            if session.rendering:
                continue
            if now - session.last_activity > idle_sec:
                session.fail("Browser analysis expired after inactivity")
                with self.lock:
                    self.sessions.pop(session.id, None)
