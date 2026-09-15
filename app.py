import json
import os
import sqlite3
import threading
import traceback
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

import yolo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
MODEL_PATH = os.path.join(BASE_DIR, "best.pt")
DB_PATH = os.path.join(BASE_DIR, "history.db")
CONFIG_PATH = os.path.join(BASE_DIR, "calibration.json")

VIDEO_EXTENSIONS = {"mp4", "avi", "mov", "mkv", "m4v"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "bmp", "webp"}
ALLOWED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

jobs = {}
jobs_lock = threading.Lock()
config_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=1)

INFERENCE_CONFIDENCE = float(os.getenv("FLOOD_CONFIDENCE", "0.25"))
INFERENCE_IOU = float(os.getenv("FLOOD_IOU", "0.70"))
INFERENCE_IMGSZ = int(os.getenv("FLOOD_IMGSZ", "640"))
LEVEL_SMOOTHING = float(os.getenv("FLOOD_LEVEL_SMOOTHING", "0.35"))
UPLOAD_RETENTION_HOURS = float(os.getenv("FLOOD_UPLOAD_RETENTION_HOURS", "24"))

DEFAULT_CALIBRATION = {
    "auto_line": True,
    "first_x": None,
    "first_y": None,
    "second_x": None,
    "second_y": None,
    "pixels_per_meter": 200.0,
    "tip_height": 3.0,
    "low_level": 0.20,
    "medium_level": 0.50,
    "high_level": 0.80,
    "profile_name": "Default",
}


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                status TEXT NOT NULL,
                water_detected INTEGER DEFAULT 0,
                max_water_level REAL,
                alert_level TEXT,
                output_name TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def save_history(job_id, filename, media_type, stats, output_name):
    if media_type == "video":
        water_detected = int((stats.get("water_detected_frames") or 0) > 0)
        max_level = stats.get("max_water_level")
        counts = {
            "HIGH": stats.get("high_frames", 0),
            "MEDIUM": stats.get("medium_frames", 0),
            "LOW": stats.get("low_frames", 0),
        }
    else:
        water_detected = int(bool(stats.get("water_detected")))
        max_level = stats.get("water_level")
        status = stats.get("status")
        counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        if status in counts:
            counts[status] = 1

    if counts["HIGH"]:
        alert_level = "HIGH"
    elif counts["MEDIUM"]:
        alert_level = "MEDIUM"
    elif counts["LOW"]:
        alert_level = "LOW"
    elif water_detected:
        alert_level = "NORMAL" if max_level is not None else "WATER DETECTED"
    else:
        alert_level = "NO WATER"

    with db_connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO analyses
            (job_id, filename, media_type, status, water_detected, max_water_level,
             alert_level, output_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                filename,
                media_type,
                "done",
                water_detected,
                max_level,
                alert_level,
                output_name,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()


def cleanup_old_files(directory, retention_hours):
    cutoff = time.time() - retention_hours * 3600
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def extension_of(filename):
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower()


def media_type_for(filename):
    ext = extension_of(filename)
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return None


def parse_coordinate(value, field_name):
    parts = (value or "").strip().replace(",", " ").split()
    if len(parts) != 2:
        raise ValueError(f"{field_name} phải gồm đúng hai số: x y")
    return float(parts[0]), float(parts[1])


def validate_calibration(params):
    if params["pixels_per_meter"] <= 0:
        raise ValueError("Pixels per meter phải lớn hơn 0.")
    if params["tip_height"] < 0:
        raise ValueError("Độ cao điểm đầu không được âm.")
    if not (params["low_level"] < params["medium_level"] < params["high_level"]):
        raise ValueError("Ngưỡng phải thỏa LOW < MEDIUM < HIGH.")
    if not params["auto_line"]:
        coords = (params["first_x"], params["first_y"], params["second_x"], params["second_y"])
        if any(v is None for v in coords):
            raise ValueError("Manual line cần đủ tọa độ điểm bắt đầu và kết thúc.")
        if params["first_x"] == params["second_x"] and params["first_y"] == params["second_y"]:
            raise ValueError("Hai điểm của đường đo không được trùng nhau.")
    return params


def normalize_config(raw):
    cfg = dict(DEFAULT_CALIBRATION)
    if raw:
        for key in cfg:
            if key in raw:
                cfg[key] = raw[key]

    cfg["auto_line"] = bool(cfg["auto_line"])
    for key in ["pixels_per_meter", "tip_height", "low_level", "medium_level", "high_level"]:
        cfg[key] = float(cfg[key])
    for key in ["first_x", "first_y", "second_x", "second_y"]:
        cfg[key] = None if cfg[key] in (None, "") else float(cfg[key])
    cfg["profile_name"] = str(cfg.get("profile_name") or "Default")[:60]
    return validate_calibration(cfg)


def load_calibration():
    with config_lock:
        if not os.path.exists(CONFIG_PATH):
            cfg = normalize_config(DEFAULT_CALIBRATION)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            return cfg

        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return normalize_config(json.load(f))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            cfg = normalize_config(DEFAULT_CALIBRATION)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            return cfg


def save_calibration(config):
    cfg = normalize_config(config)
    with config_lock:
        temp = CONFIG_PATH + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(temp, CONFIG_PATH)
    return cfg


def parse_manual_params(form):
    auto_line = form.get("auto_line") == "on"
    first_x = first_y = second_x = second_y = None
    if not auto_line:
        first_x, first_y = parse_coordinate(form.get("start", ""), "Tọa độ bắt đầu")
        second_x, second_y = parse_coordinate(form.get("end", ""), "Tọa độ kết thúc")

    params = {
        "first_x": first_x,
        "first_y": first_y,
        "second_x": second_x,
        "second_y": second_y,
        "pixels_per_meter": float(form.get("pixels_per_meter", "")),
        "tip_height": float(form.get("tip_height", "")),
        "low_level": float(form.get("low_level", "")),
        "medium_level": float(form.get("medium_level", "")),
        "high_level": float(form.get("high_level", "")),
        "auto_line": auto_line,
        "profile_name": (form.get("profile_name") or "Default").strip() or "Default",
    }
    return validate_calibration(params)


def parse_params(form):
    mode = (form.get("mode") or "auto").lower()
    if mode == "auto":
        cfg = load_calibration()
        params = {k: cfg[k] for k in [
            "first_x", "first_y", "second_x", "second_y", "pixels_per_meter",
            "tip_height", "low_level", "medium_level", "high_level", "auto_line"
        ]}
        return params, "auto", cfg
    if mode != "manual":
        raise ValueError("Mode phải là auto hoặc manual.")

    cfg = parse_manual_params(form)
    params = {k: cfg[k] for k in [
        "first_x", "first_y", "second_x", "second_y", "pixels_per_meter",
        "tip_height", "low_level", "medium_level", "high_level", "auto_line"
    ]}
    return params, "manual", cfg


def update_job(job_id, **kwargs):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(kwargs)


def process_job(job_id, media_path, media_type, original_filename, params, mode):
    output_ext = "mp4" if media_type == "video" else "jpg"
    output_name = f"{job_id}.{output_ext}"
    output_path = os.path.join(OUTPUT_DIR, output_name)

    def progress_callback(progress, frame, total):
        update_job(job_id, progress=progress, frame=frame, total_frames=total)

    try:
        update_job(job_id, status="processing", progress=0)
        common = dict(
            model_path=MODEL_PATH,
            confidence=INFERENCE_CONFIDENCE,
            iou=INFERENCE_IOU,
            imgsz=INFERENCE_IMGSZ,
            **params,
        )

        if media_type == "video":
            stats = yolo.process_video(
                video_path=media_path,
                output_path=output_path,
                progress_callback=progress_callback,
                smoothing=LEVEL_SMOOTHING,
                **common,
            )
        else:
            stats = yolo.process_image(
                image_path=media_path,
                output_path=output_path,
                **common,
            )
            update_job(job_id, progress=100)

        stats["mode"] = mode
        output_name = os.path.basename(stats["output_path"])
        save_history(job_id, original_filename, media_type, stats, output_name)
        update_job(
            job_id,
            status="done",
            progress=100,
            output_name=output_name,
            media_type=media_type,
            stats=stats,
            mode=mode,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            error=str(exc),
            traceback=traceback.format_exc(),
        )
    finally:
        try:
            os.remove(media_path)
        except OSError:
            pass


@app.get("/")
def index():
    with db_connect() as conn:
        recent = conn.execute(
            "SELECT * FROM analyses ORDER BY id DESC LIMIT 5"
        ).fetchall()
        summary = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN water_detected = 1 THEN 1 ELSE 0 END) AS water_count,
                   MAX(max_water_level) AS highest
            FROM analyses
            """
        ).fetchone()
    return render_template(
        "index.html",
        model_exists=os.path.exists(MODEL_PATH),
        recent=recent,
        summary=summary,
        calibration=load_calibration(),
    )


@app.get("/config")
def get_config():
    return jsonify(load_calibration())


@app.post("/config")
def update_config():
    try:
        data = request.get_json(silent=True) or {}
        cfg = normalize_config(data)
        saved = save_calibration(cfg)
        return jsonify({"ok": True, "config": saved})
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/process")
def process():
    if not os.path.exists(MODEL_PATH):
        return jsonify({"error": "Không tìm thấy best.pt trong thư mục project."}), 400

    cleanup_old_files(UPLOAD_DIR, UPLOAD_RETENTION_HOURS)
    cleanup_old_files(OUTPUT_DIR, UPLOAD_RETENTION_HOURS * 7)

    media = request.files.get("media")
    if not media or not media.filename:
        return jsonify({"error": "Hãy chọn ảnh hoặc video."}), 400

    media_type = media_type_for(media.filename)
    if media_type is None:
        return jsonify({"error": "Định dạng file không được hỗ trợ."}), 400

    try:
        params, mode, parsed_cfg = parse_params(request.form)
        if mode == "manual" and request.form.get("save_as_auto") == "on":
            save_calibration(parsed_cfg)
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = uuid.uuid4().hex
    ext = extension_of(secure_filename(media.filename))
    upload_path = os.path.join(UPLOAD_DIR, f"{job_id}.{ext}")
    media.save(upload_path)

    update_job(
        job_id,
        status="queued",
        progress=0,
        filename=media.filename,
        media_type=media_type,
        mode=mode,
    )
    executor.submit(process_job, job_id, upload_path, media_type, media.filename, params, mode)
    return jsonify({"job_id": job_id, "media_type": media_type, "mode": mode})


@app.get("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = dict(jobs.get(job_id, {}))
    if not job:
        return jsonify({"error": "Không tìm thấy tác vụ."}), 404
    if job.get("status") == "done":
        job["result_url"] = f"/outputs/{job['output_name']}"
    return jsonify(job)


@app.get("/history")
def history():
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM analyses ORDER BY id DESC LIMIT 200"
        ).fetchall()
    return render_template("history.html", rows=rows)


@app.delete("/history")
def clear_history():
    with db_connect() as conn:
        conn.execute("DELETE FROM analyses")
        conn.commit()
    return jsonify({"ok": True})


@app.get("/outputs/<path:filename>")
def output_file(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)


init_db()
load_calibration()

if __name__ == "__main__":
    print("Mở http://127.0.0.1:5000 trên trình duyệt")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
