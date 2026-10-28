from __future__ import annotations

import asyncio
import hashlib
import math
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import cv2
import imageio.v2 as imageio
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .alerts import AlertManager, AlertPayload
from .cameras import CameraConfiguration, CameraUpdate
from .capture import ProcessCapture
from .config import BASE_DIR, settings
from .database import CameraConflict, Database
from .delivery import OutboxStore, OutboxWorker
from .events import EventDecision, EventPolicy, EventTracker
from .inference import FrameMetrics, analyze_frame, annotate_frame
from .live import LiveMonitor, LiveRegistry, status_image
from .local_reader import CHUNK_BYTES, LocalReader, LocalReaderStart, ReaderRegistry
from .model import Segmenter, acceleration_info, create_segmenter
from .operations import OperationsMonitor
from .preview import MAX_FRAME_BYTES, PreviewConflict, PreviewRegistry, PreviewSession, PreviewStart
from .regions import rasterize_roi, roi_pixels
from .recovery import matching_source, recover_runs
from .runtime import RuntimeLock
from .severity import SeverityThresholds, max_severity
from .sources import CameraSource, CameraSourceUpdate, resolve_source
from .storage import LowStorageError, StorageManager

app = FastAPI(title=settings.app_name, version="6.6")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/media/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")
app.mount("/media/outputs", StaticFiles(directory=settings.output_dir), name="outputs")

segmenter: Segmenter | None = None
segmenter_error: str | None = None
segmenter_lock = threading.Lock()
inference_lock = threading.Lock()
preview_registry = PreviewRegistry()
reader_registry = ReaderRegistry()
live_registry = LiveRegistry()
delivery_worker: OutboxWorker | None = None
operations_monitor: OperationsMonitor | None = None
storage_manager: StorageManager | None = None
storage_init_lock = threading.Lock()
live_controls_lock = threading.RLock()
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()
db = Database(settings.db_path)
alert_manager = AlertManager(settings)


def active_analysis_ids():
    with jobs_lock:
        active = {identifier for identifier, job in jobs.items() if job.get("status") not in {"done", "error"}}
    with preview_registry.lock:
        active.update(session.id for session in preview_registry.sessions.values()
                      if session.status not in {"done", "error"} or (session.render_thread is not None and session.render_thread.is_alive()))
    with live_registry.lock:
        active.update(monitor.id for monitor in live_registry.monitors.values() if monitor.running)
    return active


def get_storage():
    global storage_manager
    with storage_init_lock:
        if storage_manager is None or storage_manager.db is not db or storage_manager.settings is not settings:
            storage_manager = StorageManager(db, settings, active_analysis_ids)
        return storage_manager


def require_storage():
    try:
        get_storage().require_capacity()
    except LowStorageError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


@app.on_event("startup")
async def suppress_benign_windows_connection_resets() -> None:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def handler(current_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054:
            return
        if previous_handler is not None:
            previous_handler(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


@dataclass(frozen=True)
class AnalysisRequest:
    video_id: str
    sample_every_n_frames: int = 2
    low_ratio: float | None = None
    moderate_ratio: float | None = None
    critical_ratio: float | None = None
    camera_snapshot: dict[str, Any] | None = None
    event_policy: EventPolicy | None = None
    external_alerts_enabled: bool = False


def default_event_policy() -> EventPolicy:
    return EventPolicy(confirm_sec=settings.alert_confirm_sec, clear_sec=settings.alert_clear_sec,
                       reminder_sec=settings.alert_reminder_sec, hysteresis_ratio=settings.alert_hysteresis_ratio,
                       max_gap_sec=settings.max_observation_gap_sec, ema_time_constant_sec=settings.ema_time_constant_sec,
                       alert_min_severity=settings.alert_min_severity)


def request_event_policy(req: AnalysisRequest) -> EventPolicy:
    if req.event_policy is not None:
        return req.event_policy
    camera_policy = (req.camera_snapshot or {}).get("event_policy")
    return EventPolicy.model_validate(camera_policy) if camera_policy is not None else default_event_policy()


def configuration_snapshot(req: AnalysisRequest) -> dict[str, Any]:
    thresholds = _thresholds(req)
    camera = req.camera_snapshot
    return {
        "camera": camera,
        "roi": None if camera is None else camera["roi"],
        "ratio_scope": "roi" if camera and camera["roi"] is not None else "full_frame",
        "thresholds": {"low": thresholds.low, "moderate": thresholds.moderate, "critical": thresholds.critical},
        "sample_every_n_frames": req.sample_every_n_frames,
        "event_policy": request_event_policy(req).model_dump(mode="json"),
        "time_source": "video",
        "external_alerts_enabled": req.external_alerts_enabled,
    }


def region_for_frame(req: AnalysisRequest, width: int, height: int):
    camera = req.camera_snapshot
    if camera is None or camera["roi"] is None:
        return None
    return rasterize_roi(camera["roi"], width, height, camera["reference_width"], camera["reference_height"])


def get_segmenter() -> Segmenter:
    global segmenter, segmenter_error
    if segmenter is not None:
        return segmenter
    with segmenter_lock:
        if segmenter is not None:
            return segmenter
        try:
            segmenter = create_segmenter(settings.model_backend, settings.model_path, settings.yolo_model_path)
            segmenter_error = None
            return segmenter
        except Exception as exc:
            segmenter_error = str(exc)
            raise HTTPException(status_code=503, detail=segmenter_error) from exc


def _active_model_path() -> Path:
    return settings.yolo_model_path if settings.model_backend == "yolo" else settings.model_path


def find_video(video_id: str) -> Path:
    for suffix in VIDEO_EXTENSIONS:
        path = settings.upload_dir / f"{video_id}{suffix}"
        if path.exists():
            return path
    raise FileNotFoundError(video_id)


def update_job(job_id: str, **kwargs: Any) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)


def _thresholds(req: AnalysisRequest) -> SeverityThresholds:
    t = SeverityThresholds(
        low=settings.low_ratio if req.low_ratio is None else req.low_ratio,
        moderate=settings.moderate_ratio if req.moderate_ratio is None else req.moderate_ratio,
        critical=settings.critical_ratio if req.critical_ratio is None else req.critical_ratio,
    )
    t.validate()
    return t


def build_analysis_request(video_id: str, sample_every_n_frames=None, low_ratio=None, moderate_ratio=None,
                           critical_ratio=None, camera_id=None, camera_revision=None, event_policy_json=None) -> AnalysisRequest:
    camera = None
    if camera_id is not None:
        camera = db.get_camera(camera_id)
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        if camera_revision is not None and camera_revision != camera["revision"]:
            raise HTTPException(status_code=409, detail="Camera configuration has changed. Reload it before uploading.")
        if any(value is not None for value in (low_ratio, moderate_ratio, critical_ratio, sample_every_n_frames, event_policy_json)):
            raise HTTPException(status_code=400, detail="Camera uploads use saved settings; omit threshold and sampling overrides")
        sample_every_n_frames = camera["sample_every_n_frames"]
        low_ratio, moderate_ratio, critical_ratio = (camera["thresholds"][key] for key in ("low", "moderate", "critical"))
    elif camera_revision is not None:
        raise HTTPException(status_code=400, detail="camera_revision requires camera_id")
    req = AnalysisRequest(video_id, sample_every_n_frames or 2, low_ratio, moderate_ratio, critical_ratio, camera)
    try:
        _thresholds(req)
        policy = EventPolicy.model_validate_json(event_policy_json) if event_policy_json is not None else request_event_policy(req)
        policy.validate_thresholds(_thresholds(req))
        return replace(req, event_policy=policy, external_alerts_enabled=settings.replay_external_alerts_enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (BASE_DIR / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    # A single version for the template and all assets prevents mixing cached
    # scripts from different UI revisions after a local update.
    digest = hashlib.sha256(html.encode("utf-8"))
    static = BASE_DIR / "app" / "static"
    for asset in sorted([*static.glob("*.css"), *static.glob("*.js")]):
        digest.update(asset.name.encode("utf-8"))
        digest.update(asset.read_bytes())
    html = html.replace("__ASSET_VERSION__", digest.hexdigest()[:16])
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict[str, Any]:
    model_path = _active_model_path()
    info = segmenter.info() if segmenter is not None else None
    return {
        "ok": model_path.exists(),
        "app": settings.app_name,
        "model_backend": settings.model_backend,
        "model_exists": model_path.exists(),
        "model_path": str(model_path),
        "model_loaded": segmenter is not None,
        "model_error": segmenter_error,
        "model": info,
        "device": None if info is None else info.get("device"),
        "acceleration": acceleration_info(),
        "thresholds": {
            "low": settings.low_ratio,
            "moderate": settings.moderate_ratio,
            "critical": settings.critical_ratio,
        },
        "alert_min_severity": settings.alert_min_severity,
        "event_policy": default_event_policy().model_dump(mode="json"),
        "replay_external_alerts_enabled": settings.replay_external_alerts_enabled,
        "alert_channels": alert_manager.configured_channels(),
    }


@app.post("/api/upload")
async def upload_video(
    file: UploadFile = File(...),
    sample_every_n_frames: int | None = Form(default=None, ge=1, le=30),
    low_ratio: float | None = Form(default=None, ge=0.0, le=1.0),
    moderate_ratio: float | None = Form(default=None, ge=0.0, le=1.0),
    critical_ratio: float | None = Form(default=None, ge=0.0, le=1.0),
    camera_id: str | None = Form(default=None),
    camera_revision: int | None = Form(default=None, ge=1),
    event_policy_json: str | None = Form(default=None),
) -> dict[str, Any]:
    """Upload a video and immediately start automatic flood analysis."""
    require_storage()
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported video format")

    req = build_analysis_request("pending", sample_every_n_frames, low_ratio, moderate_ratio, critical_ratio,
                                 camera_id, camera_revision, event_policy_json)
    policy = request_event_policy(req)

    # Fail early if the configured model is unavailable. Uploading a video should
    # mean that analysis can actually start without another manual action.
    get_segmenter()

    video_id = uuid.uuid4().hex
    video_path = settings.upload_dir / f"{video_id}{suffix}"
    try:
        with video_path.open("wb") as dst:
            while True:
                require_storage()
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
    except Exception:
        video_path.unlink(missing_ok=True)
        raise

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        video_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Cannot open uploaded video")

    fps = float(cap.get(cv2.CAP_PROP_FPS))
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    ok, first = cap.read()
    cap.release()
    if not ok or first is None:
        video_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Cannot read uploaded video")

    if width <= 0 or height <= 0:
        height, width = first.shape[:2]
    try:
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("Video has no valid FPS; cannot establish observation timestamps")
        if req.sample_every_n_frames / fps > policy.max_gap_sec:
            raise ValueError("Sampling interval exceeds max_gap_sec; reduce sample_every_n_frames or increase the observation gap limit")
        region_for_frame(req, first.shape[1], first.shape[0])
    except ValueError as exc:
        video_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    duration = frames / fps if fps > 0 and frames > 0 else 0.0

    req = replace(req, video_id=video_id)
    job_id = _start_analysis_job(req)

    return {
        "video_id": video_id,
        "job_id": job_id,
        "filename": file.filename,
        "video_url": f"/media/uploads/{video_path.name}",
        "fps": fps,
        "frames": frames,
        "width": width,
        "height": height,
        "duration_sec": duration,
        "configuration": configuration_snapshot(req),
    }


def persist_transitions(job_id: str, req: AnalysisRequest, transitions, *, database=None, dispatcher=None,
                         configuration=None, checkpoint=None, final_result=None) -> list[dict[str, Any]]:
    camera = req.camera_snapshot or {}
    configuration = configuration if configuration is not None else configuration_snapshot(req)
    database = db if database is None else database
    dispatcher = alert_manager if dispatcher is None else dispatcher
    specifications = {}
    for transition in transitions:
        if not transition["notify"]:
            continue
        payload = AlertPayload(job_id, transition["severity"], transition["water_ratio"], transition["time_sec"],
                               camera.get("name"), camera.get("revision"), configuration["ratio_scope"],
                               transition["event_id"], transition["type"], transition["id"],
                               configuration.get("time_source", "video"),
                               None if configuration.get("started_at_unix") is None else configuration["started_at_unix"] + transition["time_sec"],
                               transition.get("snapshot_url"))
        specifications[transition["id"]] = {"payload": asdict(payload), "message": payload.message,
            "targets": dispatcher.outbox_targets() if req.external_alerts_enabled else {}}
    alerts = database.commit_events(job_id, transitions, configuration, specifications, checkpoint, final_result)
    if delivery_worker is not None and delivery_worker.store.db is database:
        delivery_worker.wake.set()
    return alerts


def process_video(job_id: str, req: AnalysisRequest) -> None:
    writer = None
    cap = None
    tracker = None
    frame_idx, fps = 0, 0.0
    try:
        seg = get_segmenter()
        thresholds = _thresholds(req)
        policy = request_event_policy(req)
        tracker = EventTracker(thresholds, policy)
        configuration = configuration_snapshot(req)
        video_path = find_video(req.video_id)
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError("Cannot open video")

        fps = float(cap.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or fps <= 0:
            raise RuntimeError("Video has no valid observation clock")
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        output_name = f"{job_id}_annotated.mp4"
        output_path = settings.output_dir / output_name
        writer = imageio.get_writer(str(output_path), fps=fps, codec="libx264", quality=7, macro_block_size=None)

        frame_idx = 0
        analyzed_frames = 0
        ratio_sum = 0.0
        max_ratio = 0.0
        highest_observed = "SAFE"
        timeline: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []
        previous_ratio: float | None = None
        last_mask: Any = None
        last_metrics: FrameMetrics | None = None
        previous_time: float | None = None
        decision = EventDecision("UNKNOWN", "UNKNOWN", None, 0, 0, (), None)
        roi_mask = None

        while True:
            if frame_idx % max(1, round(fps)) == 0:
                get_storage().require_capacity()
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if frame_idx == 0:
                height, width = frame.shape[:2]
                roi_mask = region_for_frame(req, width, height)
            elif frame.shape[:2] != (height, width):
                raise RuntimeError("Video frame dimensions changed during analysis")

            do_infer = frame_idx % req.sample_every_n_frames == 0 or last_metrics is None
            if do_infer:
                time_sec = frame_idx / fps
                elapsed = None if previous_time is None else time_sec - previous_time
                if elapsed is not None and elapsed > policy.max_gap_sec:
                    previous_ratio = None
                with inference_lock:
                    mask, metrics = analyze_frame(
                        seg, frame, thresholds, previous_ratio=previous_ratio,
                        roi_mask=roi_mask, elapsed_sec=elapsed, ema_time_constant_sec=policy.ema_time_constant_sec)
                previous_time = time_sec
                decision = tracker.update(time_sec, metrics.water_ratio)
                alerts.extend(persist_transitions(job_id, req, decision.transitions))
                previous_ratio = metrics.water_ratio
                last_mask = mask
                last_metrics = metrics
                analyzed_frames += 1
                ratio_sum += metrics.water_ratio
                max_ratio = max(max_ratio, metrics.water_ratio)
                highest_observed = max_severity(highest_observed, metrics.severity)

                if not timeline or decision.transitions or time_sec - timeline[-1]["time_sec"] >= 0.75:
                    timeline.append({
                        "time_sec": round(time_sec, 2),
                        "water_ratio": round(metrics.water_ratio, 5),
                        "raw_water_ratio": round(metrics.raw_water_ratio, 5),
                        **decision.state(),
                        "confidence": round(metrics.confidence, 4),
                        "full_frame_water_ratio": round(metrics.full_frame_water_ratio, 5),
                    })

            if last_mask is None or last_metrics is None:
                writer.append_data(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            else:
                annotated = annotate_frame(frame, last_mask, roi_mask=roi_mask)
                writer.append_data(cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB))

            frame_idx += 1
            if frame_idx == 1 or frame_idx % max(1, int(fps)) == 0:
                if tracker.active is not None:
                    db.save_event(job_id, tracker.active, configuration)
                update_job(
                    job_id,
                    progress=round(min(99.0, frame_idx * 100.0 / total), 1) if total > 0 else 0,
                    processed_frames=frame_idx,
                    live={
                        **decision.state(),
                        "water_ratio": None if last_metrics is None else last_metrics.water_ratio,
                        "confidence": None if last_metrics is None else last_metrics.confidence,
                        "time_sec": previous_time,
                        "ratio_scope": "roi" if roi_mask is not None else "full_frame",
                    },
                )

        if frame_idx == 0 or (total > 0 and frame_idx + 1 < total):
            raise RuntimeError("Video ended before all expected frames could be read")
        alerts.extend(persist_transitions(job_id, req, tracker.finish(frame_idx / fps)))
        for event in tracker.events:
            db.save_event(job_id, event, configuration)

        if cap is not None:
            cap.release()
            cap = None
        if writer is not None:
            writer.close()
            writer = None

        result = {
            "analysis_id": job_id,
            "video_id": req.video_id,
            "output_url": f"/media/outputs/{output_name}",
            "frames": frame_idx,
            "analyzed_frames": analyzed_frames,
            "fps": fps,
            "width": width,
            "height": height,
            "duration_sec": frame_idx / fps if fps > 0 else 0.0,
            "max_water_ratio": max_ratio,
            "mean_water_ratio": ratio_sum / max(1, analyzed_frames),
            "highest_severity": tracker.highest_confirmed,
            "highest_observed_severity": highest_observed,
            "final_state": {**decision.state(), "event_status": None if tracker.active is None else tracker.active["status"]},
            "final_observation": {**decision.state(), "time_sec": previous_time,
                                  "water_ratio": last_metrics.water_ratio, "confidence": last_metrics.confidence,
                                  "ratio_scope": last_metrics.ratio_scope},
            "events": tracker.events,
            "event_count": len(tracker.events),
            "timeline": timeline,
            "alerts": alerts,
            "alert_count": len(alerts),
            "model": seg.info(),
            "thresholds": {
                "low": thresholds.low,
                "moderate": thresholds.moderate,
                "critical": thresholds.critical,
            },
            "sample_every_n_frames": req.sample_every_n_frames,
            "configuration": configuration,
            "ratio_scope": "roi" if roi_mask is not None else "full_frame",
            "monitored_pixels": None if last_metrics is None else last_metrics.monitored_pixels,
        }
        db.finish_analysis(job_id, result)
        update_job(job_id, status="done", progress=100.0, processed_frames=frame_idx, result=result,
                   live=result["final_observation"])
    except Exception as exc:
        if cap is not None:
            cap.release()
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        if tracker is not None:
            end_time = max(tracker.last_update or 0, frame_idx / fps if fps > 0 else 0)
            persist_transitions(job_id, req, tracker.finish(end_time, reason="analysis_error"))
            for event in tracker.events:
                db.save_event(job_id, event, configuration_snapshot(req))
        db.fail_analysis(job_id, str(exc))
        update_job(job_id, status="error", progress=100.0, error=str(exc), live={"severity": "UNKNOWN", "observed_severity": "UNKNOWN"})


def _start_analysis_job(req: AnalysisRequest) -> str:
    try:
        find_video(req.video_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Uploaded video not found") from exc
    try:
        _thresholds(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    get_segmenter()
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0.0,
            "processed_frames": 0,
            "created_at": time.time(),
            "configuration": configuration_snapshot(req),
        }
    db.start_analysis(job_id, req.video_id, configuration_snapshot(req))
    thread = threading.Thread(target=process_video, args=(job_id, req), daemon=True)
    update_job(job_id, status="running")
    thread.start()
    return job_id


def get_preview(job_id: str) -> PreviewSession:
    try:
        return preview_registry.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Browser analysis session not found or expired") from exc


@app.post("/api/previews", status_code=201)
def start_preview(metadata: PreviewStart) -> dict[str, Any]:
    require_storage()
    if Path(metadata.filename).suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported video format")
    options = metadata.options.model_dump(exclude={"event_policy"})
    options["event_policy_json"] = None if metadata.options.event_policy is None else metadata.options.event_policy.model_dump_json()
    video_id, job_id = uuid.uuid4().hex, uuid.uuid4().hex
    req = build_analysis_request(video_id, **options)
    camera = req.camera_snapshot or {}
    configuration = {**configuration_snapshot(req), "analysis_mode": "browser_frames",
                     "time_source": metadata.source_clock, "sample_interval_sec": metadata.interval_sec,
                     "source": metadata.model_dump(exclude={"options"})}
    database, dispatcher = db, alert_manager
    try:
        if camera.get("roi") is not None:
            roi_pixels(camera["roi"], metadata.width, metadata.height, camera["reference_width"], camera["reference_height"])
        with jobs_lock:
            jobs[job_id] = {"status": "previewing", "created_at": time.time(), "progress": 0,
                            "configuration": configuration, "analysis_mode": "browser_frames"}
        session = PreviewSession(job_id, video_id, metadata, configuration, settings, database,
            provider=lambda: get_segmenter(), inference_lock=inference_lock,
            persist=lambda transitions: persist_transitions(job_id, req, transitions, database=database,
                dispatcher=dispatcher, configuration=configuration),
            update=lambda **values: update_job(job_id, **values), capacity_check=get_storage().require_capacity)
        preview_registry.add(session)
    except ValueError as exc:
        with jobs_lock:
            jobs.pop(job_id, None)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job_id": job_id, "video_id": video_id, "configuration": configuration,
            "max_frame_edge": 1280, "max_frame_bytes": MAX_FRAME_BYTES}


@app.post("/api/local-readers", status_code=201)
def start_local_reader(metadata: LocalReaderStart):
    require_storage()
    if Path(metadata.filename).suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported video format")
    # Validate profile revisions/options before allocating any native resources.
    values = metadata.options.model_dump(exclude={"event_policy"})
    values["event_policy_json"] = None if metadata.options.event_policy is None else metadata.options.event_policy.model_dump_json()
    req = build_analysis_request("local-reader", **values)
    if metadata.interval_sec > request_event_policy(req).max_gap_sec:
        raise HTTPException(status_code=400, detail="Read interval exceeds the observation gap")

    def create_preview(info):
        start = PreviewStart(**metadata.model_dump(exclude={"smooth_playback"}), width=info["width"], height=info["height"],
                             duration_sec=info["duration_sec"], source_clock="native_video_fps")
        created = start_preview(start)
        session = get_preview(created["job_id"])
        session.original_info = {key: info[key] for key in ("fps", "frames", "width", "height")}
        return session

    reader = LocalReader(metadata, create_preview)
    try:
        reader_registry.add(reader)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"reader_id": reader.id, "chunk_bytes": CHUNK_BYTES}


@app.get("/api/local-readers/{identifier}/playback")
def local_video_playback(identifier: str):
    reader = reader_registry.get(identifier)
    if reader is None:
        raise HTTPException(status_code=404, detail="Selected video reader not found")
    try:
        process = reader.open_playback()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StreamingResponse(reader.playback_chunks(process), media_type="video/mp4",
                             headers={"Cache-Control": "no-store", "Accept-Ranges": "none"})


@app.websocket("/api/local-readers/{identifier}/bridge")
async def local_reader_bridge(websocket: WebSocket, identifier: str):
    origin = websocket.headers.get("origin")
    if origin and urlsplit(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=1008)
        return
    reader = reader_registry.get(identifier)
    if reader is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        reader.bind(websocket, asyncio.get_running_loop())
    except ValueError:
        await websocket.close(code=1008)
        return
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                reader.accept_bytes(message["bytes"])
            else:
                control = json.loads(message.get("text", ""))
                if not isinstance(control, dict) or control.get("type") != "ack":
                    raise ValueError("Invalid local reader message")
                reader.acknowledge(control.get("sequence"))
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        await asyncio.to_thread(reader.close)


@app.post("/api/previews/{job_id}/frames")
async def preview_frame(job_id: str, request: Request, sequence: int = Query(ge=0), time_sec: float = Query(ge=0)) -> dict[str, Any]:
    require_storage()
    session = get_preview(job_id)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FRAME_BYTES:
            raise HTTPException(status_code=413, detail="Preview JPEG frame is too large")
    try:
        return await asyncio.to_thread(session.frame, sequence, time_sec, bytes(body))
    except PreviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Frame analysis failed: {exc}") from exc


@app.post("/api/previews/{job_id}/finish")
async def finish_preview(job_id: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(get_preview(job_id).complete_frames)
    except PreviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/previews/{job_id}/original")
async def attach_preview_original(job_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    require_storage()
    session = get_preview(job_id)
    try:
        await asyncio.to_thread(session.begin_original, file.filename or "")
    except PreviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        size = 0
        with session.part_path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                require_storage()
                session.require_running()
                size += len(chunk)
                if size > session.metadata.size_bytes:
                    raise ValueError("Original file is larger than the selected video")
                target.write(chunk)
                session.last_activity = time.monotonic()
        return await asyncio.to_thread(session.attach_original, size)
    except Exception as exc:
        await asyncio.to_thread(session.fail, f"Original upload failed: {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        session.original_uploading = False
        session.part_path.unlink(missing_ok=True)
        await file.close()


@app.post("/api/previews/{job_id}/cancel")
async def cancel_preview(job_id: str) -> dict[str, Any]:
    session = get_preview(job_id)
    await asyncio.to_thread(session.fail, "Browser analysis was cancelled")
    return {"job_id": job_id, "status": session.status}


@app.on_event("startup")
async def start_preview_expiry() -> None:
    async def cleanup():
        while True:
            await asyncio.sleep(30)
            await asyncio.to_thread(preview_registry.expire, settings.preview_idle_timeout_sec)
            await asyncio.to_thread(reader_registry.close, True)
    app.state.preview_cleanup = asyncio.create_task(cleanup())


@app.on_event("shutdown")
async def stop_previews() -> None:
    await asyncio.to_thread(reader_registry.close)
    task = getattr(app.state, "preview_cleanup", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    with preview_registry.lock:
        sessions = list(preview_registry.sessions.values())
    for session in sessions:
        await asyncio.to_thread(session.fail, "Server stopped during browser analysis")


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    try:
        preview_registry.get(job_id)
    except KeyError:
        pass
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return dict(job)


@app.get("/api/history")
def history(limit: int = 20) -> dict[str, Any]:
    return {"items": db.recent_analyses(limit)}


@app.get("/api/alerts")
def alerts(limit: int = 30) -> dict[str, Any]:
    return {"items": db.recent_alerts(limit)}


@app.get("/api/events")
def events(limit: int = 20, analysis_id: str | None = None, camera_id: str | None = None) -> dict[str, Any]:
    return {"items": db.recent_events(limit, analysis_id, camera_id)}


@app.get("/api/events/{event_id}")
def event_detail(event_id: str) -> dict[str, Any]:
    event = db.event_detail(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


def camera_configuration_payload(configuration: CameraConfiguration, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = configuration.model_dump(mode="json", exclude={"expected_revision"})
    if existing is not None and "event_policy" not in configuration.model_fields_set:
        payload["event_policy"] = existing.get("event_policy")
    if payload["event_policy"] is None:
        payload["event_policy"] = default_event_policy().model_dump(mode="json")
    try:
        EventPolicy.model_validate(payload["event_policy"]).validate_thresholds(
            SeverityThresholds(configuration.thresholds.low, configuration.thresholds.moderate, configuration.thresholds.critical))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return payload


@app.get("/api/cameras")
def cameras() -> dict[str, Any]:
    return {"items": db.cameras()}


@app.post("/api/cameras", status_code=201)
def create_camera(configuration: CameraConfiguration) -> dict[str, Any]:
    return db.save_camera(camera_configuration_payload(configuration))


@app.get("/api/cameras/{camera_id}")
def camera_detail(camera_id: str) -> dict[str, Any]:
    camera = db.get_camera(camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


@app.put("/api/cameras/{camera_id}")
def update_camera(camera_id: str, configuration: CameraUpdate) -> dict[str, Any]:
    try:
        return db.save_camera(camera_configuration_payload(configuration, db.get_camera(camera_id)), camera_id, configuration.expected_revision)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Camera not found") from exc
    except CameraConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/cameras/{camera_id}/revisions")
def camera_revisions(camera_id: str) -> dict[str, Any]:
    items = db.camera_revisions(camera_id)
    if not items:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {"items": items}


def capture_factory(value, source: CameraSource):
    return ProcessCapture(value, source.kind, source.open_timeout_sec, source.read_timeout_sec)


@app.get("/api/cameras/{camera_id}/source")
def camera_source(camera_id: str) -> dict[str, Any]:
    camera_detail(camera_id)
    return db.get_source(camera_id) or {"camera_id": camera_id, "revision": 0, "configuration": None, "enabled": False}


@app.put("/api/cameras/{camera_id}/source")
def save_camera_source(camera_id: str, source: CameraSourceUpdate) -> dict[str, Any]:
    with live_controls_lock:
        camera_detail(camera_id)
        monitor = live_registry.get(camera_id)
        if monitor is not None and monitor.running:
            raise HTTPException(status_code=409, detail="Stop monitoring before changing the camera source")
        try:
            return db.save_source(camera_id, source.model_dump(mode="json", exclude={"expected_revision"}), source.expected_revision)
        except CameraConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


def start_live_camera(camera_id: str) -> LiveMonitor:
    with live_controls_lock:
        require_storage()
        camera = camera_detail(camera_id)
        existing = live_registry.get(camera_id)
        if existing is not None and existing._persistence_failed:
            raise HTTPException(status_code=503, detail="The previous session could not be saved. Repair database storage and restart the server to recover its checkpoint.")
        if existing is not None and existing.running:
            return existing
        saved = db.get_source(camera_id)
        if saved is None:
            raise HTTPException(status_code=400, detail="Save the camera source first")
        source = CameraSource.model_validate(saved["configuration"])
        try:
            value = resolve_source(source)
            req = replace(build_analysis_request(f"live:{camera_id}", camera_id=camera_id, camera_revision=camera["revision"]),
                          external_alerts_enabled=source.external_alerts_enabled)
            configuration = {**configuration_snapshot(req), "analysis_mode": "live_camera", "time_source": "monotonic",
                             "started_at_unix": time.time(), "source_revision": saved["revision"],
                             "source": source.model_dump(mode="json")}
            database, dispatcher = db, alert_manager
            factory = capture_factory
            def create():
                recovery = database.recoverable_camera(camera_id)
                if recovery is not None and not matching_source(database, recovery):
                    database.interrupt_analysis(recovery["id"], "configuration_changed_before_resume")
                    recovery = None
                run_id = recovery["id"] if recovery else uuid.uuid4().hex
                active_configuration = recovery["configuration"] if recovery else configuration
                def persist(transitions, **kwargs):
                    return persist_transitions(run_id, req, transitions, database=database, dispatcher=dispatcher,
                                               configuration=active_configuration, **kwargs)
                return LiveMonitor(camera_id, run_id, active_configuration, source, database, settings.output_dir,
                                   get_segmenter, inference_lock, persist, lambda: factory(value, source),
                                   recovery=None if recovery is None else recovery["checkpoint"],
                                   capacity_check=get_storage().require_capacity)
            monitor = live_registry.start(camera_id, create, settings.live_max_cameras)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.set_source_enabled(camera_id, True)
        return monitor


def live_state(camera_id: str) -> dict[str, Any]:
    camera = camera_detail(camera_id)
    monitor = live_registry.get(camera_id)
    saved = db.get_source(camera_id)
    state = {"camera_id": camera_id, "camera_name": camera["name"], "lifecycle": "idle", "connection": "stopped",
             "severity": "UNKNOWN", "observed_severity": "UNKNOWN", "run_id": None, "camera_revision": None}
    if monitor is not None:
        state.update(monitor.state())
    return {**state, "saved_camera_revision": camera["revision"], "source": saved,
            "configuration_changed": monitor is not None and monitor.configuration["camera"]["revision"] != camera["revision"]}


@app.get("/api/live")
def live_cameras() -> dict[str, Any]:
    return {"items": [live_state(camera["id"]) for camera in db.cameras()], "maximum": settings.live_max_cameras,
            "resume_errors": getattr(app.state, "live_resume_errors", [])}


@app.get("/api/cameras/{camera_id}/live")
def live_camera_status(camera_id: str) -> dict[str, Any]:
    return live_state(camera_id)


@app.post("/api/cameras/{camera_id}/live/start")
def start_live(camera_id: str) -> dict[str, Any]:
    start_live_camera(camera_id)
    return live_state(camera_id)


@app.post("/api/cameras/{camera_id}/live/stop")
def stop_live(camera_id: str) -> dict[str, Any]:
    with live_controls_lock:
        camera_detail(camera_id)
        db.set_source_enabled(camera_id, False)
        live_registry.stop(camera_id)
        if (recovery := db.recoverable_camera(camera_id)) is not None:
            db.interrupt_analysis(recovery["id"], "monitor_stopped_before_resume")
        return live_state(camera_id)


@app.post("/api/cameras/{camera_id}/live/restart")
def restart_live(camera_id: str) -> dict[str, Any]:
    with live_controls_lock:
        stop_live(camera_id)
        if (monitor := live_registry.get(camera_id)) is not None and monitor.running:
            raise HTTPException(status_code=409, detail="Camera is still stopping; retry shortly")
        return start_live(camera_id)


@app.get("/api/cameras/{camera_id}/live/reference.jpg")
def live_reference(camera_id: str) -> Response:
    camera_detail(camera_id)
    monitor = live_registry.get(camera_id)
    jpeg = None if monitor is None else monitor.jpeg(raw=True)
    if jpeg is None:
        raise HTTPException(status_code=503, detail="No fresh camera image is available")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


_LIVE_PLACEHOLDER = status_image("Waiting for a fresh camera image")


@app.get("/api/cameras/{camera_id}/live/stream")
async def live_stream(camera_id: str, request: Request):
    camera_detail(camera_id)
    async def images():
        while not await request.is_disconnected():
            monitor = live_registry.get(camera_id)
            jpeg = (None if monitor is None else monitor.jpeg()) or _LIVE_PLACEHOLDER
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
            await asyncio.sleep(.2)
    return StreamingResponse(images(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store"})


@app.on_event("startup")
async def start_persistence() -> None:
    global delivery_worker
    ownership = RuntimeLock(db.path)
    ownership.acquire()
    try:
        active = {monitor.id for monitor in live_registry.monitors.values() if monitor.running}
        active.update(identifier for identifier, job in jobs.items() if job.get("status") in {"running", "queued"})
        app.state.recovery_report = await asyncio.to_thread(recover_runs, db, active)
        delivery_worker = OutboxWorker(db, alert_manager, settings)
        delivery_worker.start()
        app.state.runtime_ownership = ownership
    except Exception:
        ownership.release()
        raise


@app.get("/api/deliveries")
def deliveries(limit: int = 30):
    return {**OutboxStore(db).recent(limit), "worker_running": bool(delivery_worker and delivery_worker.thread and delivery_worker.thread.is_alive()),
            "worker_error": None if delivery_worker is None else delivery_worker.last_error}


@app.post("/api/deliveries/{identifier}/retry")
def retry_delivery(identifier: int):
    return delivery_action(identifier, "retry")


@app.post("/api/deliveries/{identifier}/skip")
def skip_delivery(identifier: int):
    return delivery_action(identifier, "skip")


def delivery_action(identifier, action):
    try:
        OutboxStore(db).action(identifier, action, time.time())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Delivery not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if delivery_worker is not None:
        delivery_worker.wake.set()
    return {"ok": True}


@app.on_event("startup")
async def resume_live_cameras() -> None:
    app.state.live_resume_errors = []
    for camera_id in db.resumable_sources():
        try:
            await asyncio.to_thread(start_live_camera, camera_id)
        except Exception:
            app.state.live_resume_errors.append({"camera_id": camera_id, "detail": "Cannot resume this camera; check the saved source and camera limit"})


@app.on_event("shutdown")
async def stop_live_cameras() -> None:
    # Preserve enabled intent for explicitly opted-in auto-resume sources.
    try:
        if operations_monitor is not None:
            await asyncio.to_thread(operations_monitor.close)
        await asyncio.to_thread(live_registry.close)
    finally:
        if delivery_worker is not None:
            await asyncio.to_thread(delivery_worker.close)
        ownership = getattr(app.state, "runtime_ownership", None)
        if ownership is not None:
            ownership.release()


def get_operations():
    global operations_monitor
    if operations_monitor is None or operations_monitor.db is not db or operations_monitor.settings is not settings:
        def camera_states():
            with live_registry.lock:
                return [monitor.state() for monitor in live_registry.monitors.values()]
        def worker_state():
            worker = delivery_worker
            return {"running": bool(worker and worker.store.db is db and worker.thread and worker.thread.is_alive()),
                    "error": worker.last_error if worker and worker.store.db is db else None}
        def model_state():
            return {"exists": _active_model_path().is_file(), "loaded": segmenter is not None,
                    "backend": settings.model_backend, "error": segmenter_error}
        operations_monitor = OperationsMonitor(db, settings, get_storage(), camera_states, worker_state, alert_manager, model_state)
    return operations_monitor


@app.on_event("startup")
async def start_operations():
    get_operations().start()


@app.get("/api/operations")
def operations_status():
    try:
        return get_operations().snapshot()
    except Exception:
        raise HTTPException(status_code=503, detail="Operational state is unavailable; check database and storage")


@app.get("/api/operations/history")
def operations_history(limit: int = 60):
    return {"items": get_operations().history(limit)}


@app.get("/api/ready")
def live_readiness():
    snapshot = operations_status()
    return Response(json.dumps(snapshot), status_code=200 if snapshot["ready_for_live_monitoring"] else 503, media_type="application/json")


@app.get("/api/storage")
def storage_status():
    storage = get_storage()
    return {"capacity": storage.capacity(), "retention": storage.plan(), "history": storage.history()}


@app.post("/api/storage/maintain")
def maintain_storage():
    return get_storage().run()


@app.post("/api/storage/{identifier}/restore")
def restore_media(identifier: str):
    try:
        return get_storage().restore(identifier)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Archived media not found") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
