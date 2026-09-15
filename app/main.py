from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import cv2
import imageio.v2 as imageio
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .calibration import Calibration, CalibrationStore
from .inference import analyze_frame, annotate_frame, smooth_metrics
from .model import RIWASegmenter


BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
CALIB_DIR = BASE_DIR / "calibrations"
MODEL_PATH = Path(os.getenv("RIWA_MODEL_PATH", BASE_DIR / "models" / "riwa_unet_resnet34_deploy.pth"))

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
CALIB_DIR.mkdir(exist_ok=True)

app = FastAPI(title="RIWA Flood Detection Web", version="3.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/media/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/media/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")

segmenter: RIWASegmenter | None = None
segmenter_error: str | None = None
calibrations = CalibrationStore(CALIB_DIR)
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


@app.on_event("startup")
async def suppress_benign_windows_connection_resets() -> None:
    """Hide only WinError 10054 raised when a browser aborts an old media Range request."""
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


class CalibrationRequest(BaseModel):
    profile: str = "default"
    zero_y: float
    known_y: float
    known_cm: float = Field(gt=0)
    zero_x: float | None = None
    known_x: float | None = None
    frame_width: float | None = Field(default=None, gt=0)


class AnalyzeRequest(BaseModel):
    video_id: str
    calibration_mode: str = "auto"  # auto | manual | none
    profile: str = "default"
    warning_cm: float = 20.0
    danger_cm: float = 50.0
    warning_ratio: float = 0.20
    danger_ratio: float = 0.45
    sample_every_n_frames: int = Field(default=1, ge=1, le=30)


def get_segmenter() -> RIWASegmenter:
    global segmenter, segmenter_error
    if segmenter is not None:
        return segmenter
    try:
        segmenter = RIWASegmenter(MODEL_PATH)
        segmenter_error = None
        return segmenter
    except Exception as exc:
        segmenter_error = str(exc)
        raise HTTPException(status_code=503, detail=segmenter_error) from exc


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (BASE_DIR / "app" / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    model_exists = MODEL_PATH.exists()
    info = None
    if model_exists:
        try:
            info = get_segmenter().info()
        except HTTPException:
            pass
    return {
        "ok": model_exists and info is not None,
        "model_exists": model_exists,
        "model_path": str(MODEL_PATH),
        "model_error": segmenter_error,
        "model": info,
    }


@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in {".mp4", ".mov", ".avi", ".mkv", ".m4v"}:
        raise HTTPException(status_code=400, detail="Unsupported video format")

    video_id = uuid.uuid4().hex
    video_path = UPLOAD_DIR / f"{video_id}{suffix}"

    with video_path.open("wb") as dst:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise HTTPException(status_code=400, detail="Cannot open uploaded video")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    ok, first = cap.read()
    cap.release()

    if not ok or first is None:
        raise HTTPException(status_code=400, detail="Cannot read first video frame")

    preview_name = f"{video_id}_preview.jpg"
    preview_path = UPLOAD_DIR / preview_name
    cv2.imwrite(str(preview_path), first)

    return {
        "video_id": video_id,
        "filename": video_path.name,
        "preview_url": f"/media/uploads/{preview_name}",
        "width": width,
        "height": height,
        "fps": fps,
        "frames": frames,
        "duration_sec": frames / fps if fps > 0 else None,
    }


@app.post("/api/calibration")
def save_calibration(req: CalibrationRequest) -> dict[str, Any]:
    cal = Calibration(
        req.profile,
        req.zero_y,
        req.known_y,
        req.known_cm,
        zero_x=req.zero_x,
        known_x=req.known_x,
        frame_width=req.frame_width,
    )
    try:
        cpp = cal.cm_per_pixel
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    calibrations.save(cal)
    return {
        "saved": True,
        "profile": req.profile,
        "cm_per_pixel": cpp,
        "zero_y": req.zero_y,
        "known_y": req.known_y,
        "known_cm": req.known_cm,
        "zero_x": req.zero_x,
        "known_x": req.known_x,
        "measurement_x": cal.measurement_x,
        "frame_width": cal.frame_width,
    }


@app.get("/api/calibration/{profile}")
def get_calibration(profile: str) -> dict[str, Any]:
    cal = calibrations.load(profile)
    if cal is None:
        return {"exists": False, "profile": profile}
    return {
        "exists": True,
        "profile": profile,
        "zero_y": cal.zero_y,
        "known_y": cal.known_y,
        "known_cm": cal.known_cm,
        "cm_per_pixel": cal.cm_per_pixel,
        "zero_x": cal.zero_x,
        "known_x": cal.known_x,
        "measurement_x": cal.measurement_x,
        "frame_width": cal.frame_width,
    }


def find_video(video_id: str) -> Path:
    matches = list(UPLOAD_DIR.glob(f"{video_id}.*"))
    matches = [p for p in matches if "_preview" not in p.stem]
    if not matches:
        raise FileNotFoundError(video_id)
    return matches[0]


def update_job(job_id: str, **kwargs: Any) -> None:
    with jobs_lock:
        jobs[job_id].update(kwargs)


def process_video(job_id: str, req: AnalyzeRequest) -> None:
    try:
        seg = get_segmenter()
        video_path = find_video(req.video_id)

        calibration = None
        if req.calibration_mode != "none":
            calibration = calibrations.load(req.profile)

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError("Cannot open video")

        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        output_name = f"{job_id}_annotated.mp4"
        output_path = OUTPUT_DIR / output_name
        writer = imageio.get_writer(
            str(output_path),
            fps=fps,
            codec="libx264",
            quality=7,
            macro_block_size=None,
        )

        frame_idx = 0
        analyzed_frames = 0
        max_ratio = 0.0
        sum_ratio = 0.0
        max_level: float | None = None
        sum_level = 0.0
        level_count = 0
        highest_status = "SAFE"
        status_rank = {"SAFE": 0, "WARNING": 1, "DANGER": 2}
        timeline: list[dict[str, Any]] = []
        last_mask = None
        last_metrics = None
        smoothed_metrics = None
        confidence_sum = 0.0
        confidence_count = 0

        while True:
            ok, frame = cap.read()
            if not ok:
                break

            do_infer = frame_idx % req.sample_every_n_frames == 0 or last_mask is None
            if do_infer:
                mask, metrics = analyze_frame(
                    seg,
                    frame,
                    calibration,
                    req.warning_cm,
                    req.danger_cm,
                    req.warning_ratio,
                    req.danger_ratio,
                )
                metrics = smooth_metrics(
                    smoothed_metrics,
                    metrics,
                    calibration,
                    req.warning_cm,
                    req.danger_cm,
                    req.warning_ratio,
                    req.danger_ratio,
                )
                smoothed_metrics = metrics
                last_mask, last_metrics = mask, metrics
                analyzed_frames += 1
                max_ratio = max(max_ratio, metrics.water_ratio)
                sum_ratio += metrics.water_ratio
                if metrics.water_level_cm is not None:
                    max_level = metrics.water_level_cm if max_level is None else max(max_level, metrics.water_level_cm)
                    sum_level += metrics.water_level_cm
                    level_count += 1
                    confidence_sum += metrics.waterline_confidence
                    confidence_count += 1
                if status_rank[metrics.status] > status_rank[highest_status]:
                    highest_status = metrics.status

                # One timeline point per ~1 second at most.
                if not timeline or frame_idx / fps - timeline[-1]["time_sec"] >= 0.9:
                    timeline.append(
                        {
                            "time_sec": round(frame_idx / fps, 2),
                            "water_ratio": round(metrics.water_ratio, 5),
                            "water_level_cm": None if metrics.water_level_cm is None else round(metrics.water_level_cm, 2),
                            "waterline_confidence": round(metrics.waterline_confidence, 4),
                            "status": metrics.status,
                        }
                    )

            annotated = annotate_frame(frame, last_mask, last_metrics, calibration)
            writer.append_data(cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB))

            frame_idx += 1
            if total > 0 and frame_idx % max(1, int(fps)) == 0:
                progress = min(99.0, frame_idx * 100.0 / total)
                update_job(job_id, progress=round(progress, 1), processed_frames=frame_idx)

        cap.release()
        writer.close()

        result = {
            "output_url": f"/media/outputs/{output_name}",
            "frames": frame_idx,
            "analyzed_frames": analyzed_frames,
            "fps": fps,
            "width": width,
            "height": height,
            "max_water_ratio": max_ratio,
            "mean_water_ratio": sum_ratio / max(1, analyzed_frames),
            "max_water_level_cm": max_level,
            "mean_water_level_cm": None if level_count == 0 else sum_level / level_count,
            "mean_waterline_confidence": None if confidence_count == 0 else confidence_sum / confidence_count,
            "highest_status": highest_status,
            "timeline": timeline,
            "calibration_used": calibration is not None,
            "calibration_profile": req.profile if calibration is not None else None,
            "model_threshold": seg.threshold,
            "measurement_mode": "automatic_calibrated_level" if calibration is not None else "area_ratio",
            "automatic_waterline": True,
        }
        update_job(job_id, status="done", progress=100.0, result=result)

    except Exception as exc:
        update_job(job_id, status="error", error=str(exc), progress=100.0)


@app.post("/api/analyze")
def start_analysis(req: AnalyzeRequest) -> dict[str, str]:
    try:
        find_video(req.video_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Uploaded video not found") from exc

    if req.danger_cm <= req.warning_cm:
        raise HTTPException(status_code=400, detail="danger_cm must be greater than warning_cm")
    if req.danger_ratio <= req.warning_ratio:
        raise HTTPException(status_code=400, detail="danger_ratio must be greater than warning_ratio")

    # Validate model before starting background work.
    get_segmenter()

    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        "status": "queued",
        "progress": 0.0,
        "processed_frames": 0,
        "created_at": time.time(),
    }
    thread = threading.Thread(target=process_video, args=(job_id, req), daemon=True)
    thread.start()
    update_job(job_id, status="running")
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return dict(job)
