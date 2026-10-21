from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import cv2
import imageio.v2 as imageio
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .alerts import AlertManager, AlertPayload
from .config import BASE_DIR, settings
from .database import Database
from .inference import FrameMetrics, analyze_frame, annotate_frame
from .model import Segmenter, create_segmenter
from .severity import SeverityThresholds, at_least, max_severity

from collections import Counter
from .level_classifier import LEVEL_ENABLED, LEVEL_EMA_ALPHA, LEVEL_INFO, LevelClassifier, create_level_classifier, get_level_info

app = FastAPI(title=settings.app_name, version="6.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/media/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")
app.mount("/media/outputs", StaticFiles(directory=settings.output_dir), name="outputs")

segmenter: Segmenter | None = None
segmenter_error: str | None = None
segmenter_lock = threading.Lock()
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()
db = Database(settings.db_path)
alert_manager = AlertManager(settings)


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
    low_ratio: Optional[float] = None
    moderate_ratio: Optional[float] = None
    critical_ratio: Optional[float] = None


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

level_clf: LevelClassifier | None = None
level_error: str | None = None
level_checked = False
level_lock = threading.Lock()


def get_level_classifier() -> LevelClassifier | None:
    global level_clf, level_error, level_checked
    if level_checked:
        return level_clf
    with level_lock:
        if level_checked:
            return level_clf
        if LEVEL_ENABLED:
            try:
                level_clf = create_level_classifier()
            except Exception as exc:
                level_error = str(exc)
        level_checked = True
        return level_clf

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


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (BASE_DIR / "app" / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    model_path = _active_model_path()
    info = segmenter.info() if segmenter is not None else None
    clf = get_level_classifier()
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
        "thresholds": {
            "low": settings.low_ratio,
            "moderate": settings.moderate_ratio,
            "critical": settings.critical_ratio,
        },
        "alert_min_severity": settings.alert_min_severity,
        "alert_confirm_frames": settings.alert_confirm_frames,
        "alert_cooldown_sec": settings.alert_cooldown_sec,
        "alert_channels": alert_manager.configured_channels(),
        "level_model": None if clf is None else clf.info(),
        "level_error": level_error,
        "level_info": LEVEL_INFO,
    }


@app.post("/api/upload")
async def upload_video(
        file: UploadFile = File(...),
        sample_every_n_frames: int = Form(default=2, ge=1, le=30),
        low_ratio: Optional[float] = Form(default=None, ge=0.0, le=1.0),
        moderate_ratio: Optional[float] = Form(default=None, ge=0.0, le=1.0),
        critical_ratio: Optional[float] = Form(default=None, ge=0.0, le=1.0),
) -> dict[str, Any]:
    """Upload a video and immediately start automatic flood analysis."""
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported video format")

    req = AnalysisRequest(
        video_id="pending",
        sample_every_n_frames=sample_every_n_frames,
        low_ratio=low_ratio,
        moderate_ratio=moderate_ratio,
        critical_ratio=critical_ratio,
    )
    try:
        _thresholds(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Fail early if the configured model is unavailable. Uploading a video should
    # mean that analysis can actually start without another manual action.
    get_segmenter()

    video_id = uuid.uuid4().hex
    video_path = settings.upload_dir / f"{video_id}{suffix}"
    with video_path.open("wb") as dst:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        video_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Cannot open uploaded video")

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
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
    duration = frames / fps if fps > 0 and frames > 0 else 0.0

    req = AnalysisRequest(
        video_id=video_id,
        sample_every_n_frames=sample_every_n_frames,
        low_ratio=low_ratio,
        moderate_ratio=moderate_ratio,
        critical_ratio=critical_ratio,
    )
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
    }


def _record_alert(job_id: str, metrics: FrameMetrics, time_sec: float) -> dict[str, Any]:
    payload = AlertPayload(
        analysis_id=job_id,
        severity=metrics.severity,
        water_ratio=metrics.water_ratio,
        time_sec=time_sec,
        level=metrics.level,
        level_confidence=metrics.level_confidence,
    )
    dispatch = alert_manager.dispatch(payload)
    channels = [name for name, result in dispatch.items() if name != "cooldown" and isinstance(result, dict)]
    alert_id = db.add_alert(
        analysis_id=job_id,
        severity=metrics.severity,
        water_ratio=metrics.water_ratio,
        time_sec=time_sec,
        message=payload.message,
        channels=channels,
        dispatch=dispatch,
        level=metrics.level,
        level_confidence=metrics.level_confidence,
    )
    return {
        "id": alert_id,
        "severity": metrics.severity,
        "water_ratio": metrics.water_ratio,
        "time_sec": time_sec,
        "level": metrics.level,
        "level_confidence": metrics.level_confidence,
        "message": payload.message,
        "dispatch": dispatch,
    }


def process_video(job_id: str, req: AnalysisRequest) -> None:
    writer = None
    cap = None
    try:
        seg = get_segmenter()
        level_clf_local = get_level_classifier()
        thresholds = _thresholds(req)
        video_path = find_video(req.video_id)
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError("Cannot open video")

        fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
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
        highest = "SAFE"
        timeline: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []
        alerted_severities: set[str] = set()
        previous_ratio: float | None = None
        previous_level_probs = None
        level_counts: Counter = Counter()
        last_mask: Any = None
        last_metrics: FrameMetrics | None = None
        current_alert_severity: str | None = None
        alert_streak = 0

        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break

            do_infer = frame_idx % req.sample_every_n_frames == 0 or last_metrics is None
            if do_infer:
                mask, metrics = analyze_frame(
                    seg, frame, thresholds,
                    previous_ratio=previous_ratio,
                    ema_alpha=settings.ema_alpha,
                    level_clf=level_clf_local,
                    previous_level_probs=previous_level_probs,
                    level_alpha=LEVEL_EMA_ALPHA,
                )
                previous_level_probs = metrics.level_probs
                if metrics.level:
                    level_counts[metrics.level] += 1
                previous_ratio = metrics.water_ratio
                last_mask = mask
                last_metrics = metrics
                analyzed_frames += 1
                ratio_sum += metrics.water_ratio
                max_ratio = max(max_ratio, metrics.water_ratio)
                highest = max_severity(highest, metrics.severity)

                time_sec = frame_idx / fps if fps > 0 else 0.0
                if not timeline or time_sec - timeline[-1]["time_sec"] >= 0.75:
                    timeline.append({
                        "time_sec": round(time_sec, 2),
                        "water_ratio": round(metrics.water_ratio, 5),
                        "raw_water_ratio": round(metrics.raw_water_ratio, 5),
                        "severity": metrics.severity,
                        "confidence": round(metrics.confidence, 4),
                        "level": metrics.level,
                        "level_confidence": round(metrics.level_confidence, 4),
                    })

                # Trigger alerts based on area severity OR vehicle flood level (Level1-4 from best_flood_level_model)
                severity_alert = at_least(metrics.severity, settings.alert_min_severity)
                min_lvl = "Level1" if settings.alert_min_severity == "LOW" else "Level2"
                level_alert = (
                        metrics.level in ("Level1", "Level2", "Level3", "Level4")
                        and (
                                metrics.level in ("Level2", "Level3", "Level4")
                                or (metrics.level == "Level1" and min_lvl == "Level1")
                        )
                        and metrics.level_confidence >= 0.35
                )

                if severity_alert or level_alert:
                    alert_state_key = f"{metrics.severity}:{metrics.level or 'NO_CAR'}"
                    if alert_state_key == current_alert_severity:
                        alert_streak += 1
                    else:
                        current_alert_severity = alert_state_key
                        alert_streak = 1
                    if (
                            alert_streak >= settings.alert_confirm_frames
                            and alert_state_key not in alerted_severities
                    ):
                        alert = _record_alert(job_id, metrics, time_sec)
                        alerts.append(alert)
                        alerted_severities.add(alert_state_key)
                else:
                    current_alert_severity = None
                    alert_streak = 0

            if last_mask is None or last_metrics is None:
                writer.append_data(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            else:
                annotated = annotate_frame(frame, last_mask, last_metrics)
                writer.append_data(cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB))

            frame_idx += 1
            if total > 0 and frame_idx % max(1, int(fps)) == 0:
                update_job(
                    job_id,
                    progress=round(min(99.0, frame_idx * 100.0 / total), 1),
                    processed_frames=frame_idx,
                    live={
                        "severity": None if last_metrics is None else last_metrics.severity,
                        "water_ratio": None if last_metrics is None else last_metrics.water_ratio,
                        "level": None if last_metrics is None else last_metrics.level,
                        "level_confidence": None if last_metrics is None else last_metrics.level_confidence,
                    },
                )

        if cap is not None:
            cap.release()
            cap = None
        if writer is not None:
            writer.close()
            writer = None

        # Calculate highest & dominant flood levels from best_flood_level_model
        level_ranks = {"Level0": 0, "Level1": 1, "Level2": 2, "Level3": 3, "Level4": 4}
        highest_level = None
        highest_rank = -1
        dominant_level = None
        max_cnt = -1

        for lvl, cnt in level_counts.items():
            if lvl in level_ranks:
                if level_ranks[lvl] > highest_rank:
                    highest_rank = level_ranks[lvl]
                    highest_level = lvl
            if cnt > max_cnt:
                max_cnt = cnt
                dominant_level = lvl

        if highest_level is None and "NO_CAR" in level_counts:
            highest_level = "NO_CAR"
        if dominant_level is None:
            dominant_level = highest_level or "NO_CAR"

        # Build structured level breakdown for the table
        total_level_frames = sum(level_counts.values()) or 1
        level_breakdown = []
        for lvl in ["Level0", "Level1", "Level2", "Level3", "Level4"]:
            cnt = level_counts.get(lvl, 0)
            info = get_level_info(lvl)
            level_breakdown.append({
                "level": lvl,
                "title": info.get("title", lvl),
                "short_title": info.get("short_title", lvl),
                "desc": info.get("desc", ""),
                "depth": info.get("depth", ""),
                "severity": info.get("severity", "SAFE"),
                "color": info.get("color", "#4da7f3"),
                "advisory": info.get("advisory", ""),
                "count": cnt,
                "percent": round(cnt * 100.0 / total_level_frames, 1),
            })
        if "NO_CAR" in level_counts and level_counts["NO_CAR"] > 0:
            cnt = level_counts["NO_CAR"]
            info = get_level_info("NO_CAR")
            level_breakdown.append({
                "level": "NO_CAR",
                "title": info.get("title", "NO_CAR"),
                "short_title": info.get("short_title", "No Car"),
                "desc": info.get("desc", ""),
                "depth": "--",
                "severity": "NEUTRAL",
                "color": "#8ea2b6",
                "advisory": info.get("advisory", ""),
                "count": cnt,
                "percent": round(cnt * 100.0 / total_level_frames, 1),
            })

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
            "highest_severity": highest,
            "highest_level": highest_level,
            "dominant_level": dominant_level,
            "level_breakdown": level_breakdown,
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
            "level_counts": dict(level_counts),
            "level_model": None if level_clf_local is None else level_clf_local.info(),
            "level_info": LEVEL_INFO,
        }
        db.finish_analysis(job_id, result)
        update_job(job_id, status="done", progress=100.0, processed_frames=frame_idx, result=result)
    except Exception as exc:
        if cap is not None:
            cap.release()
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        db.fail_analysis(job_id, str(exc))
        update_job(job_id, status="error", progress=100.0, error=str(exc))


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
        }
    db.start_analysis(job_id, req.video_id)
    thread = threading.Thread(target=process_video, args=(job_id, req), daemon=True)
    thread.start()
    update_job(job_id, status="running")
    return job_id


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
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
