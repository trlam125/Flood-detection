import os
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

import yolo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
MODEL_PATH = os.path.join(BASE_DIR, "best.pt")
ALLOWED_EXTENSIONS = {"mp4", "avi", "mov", "mkv", "m4v"}

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

jobs = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=1)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_coordinate(value, field_name):
    parts = value.strip().replace(",", " ").split()
    if len(parts) != 2:
        raise ValueError(f"{field_name} must contain exactly two numbers: x y")
    return float(parts[0]), float(parts[1])


def update_job(job_id, **kwargs):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(kwargs)


def process_job(job_id, video_path, params):
    output_name = f"{job_id}.mp4"
    output_path = os.path.join(OUTPUT_DIR, output_name)

    def progress_callback(progress, frame, total):
        update_job(job_id, progress=progress, frame=frame, total_frames=total)

    try:
        update_job(job_id, status="processing", progress=0)
        stats = yolo.process_video(
            video_path=video_path,
            output_path=output_path,
            model_path=MODEL_PATH,
            progress_callback=progress_callback,
            **params,
        )
        update_job(
            job_id,
            status="done",
            progress=100,
            output_name=os.path.basename(stats["output_path"]),
            stats=stats,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            error=str(exc),
            traceback=traceback.format_exc(),
        )


@app.get("/")
def index():
    return render_template("index.html", model_exists=os.path.exists(MODEL_PATH))


@app.post("/process")
def process():
    if not os.path.exists(MODEL_PATH):
        return jsonify({"error": "best.pt was not found in the project folder."}), 400

    video = request.files.get("video")
    if not video or not video.filename:
        return jsonify({"error": "Please select a video."}), 400
    if not allowed_file(video.filename):
        return jsonify({"error": "Unsupported video format."}), 400

    try:
        auto_line = request.form.get("auto_line") == "on"
        first_x = first_y = second_x = second_y = None
        if not auto_line:
            first_x, first_y = parse_coordinate(request.form.get("start", ""), "Starting coordinates")
            second_x, second_y = parse_coordinate(request.form.get("end", ""), "Ending coordinates")

        pixels_per_meter = float(request.form.get("pixels_per_meter", ""))
        tip_height = float(request.form.get("tip_height", ""))
        low_level = float(request.form.get("low_level", ""))
        medium_level = float(request.form.get("medium_level", ""))
        high_level = float(request.form.get("high_level", ""))

        if pixels_per_meter <= 0:
            raise ValueError("Pixels per meter must be greater than 0.")
        if not (low_level < medium_level < high_level):
            raise ValueError("Alert levels must satisfy LOW < MEDIUM < HIGH.")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = uuid.uuid4().hex
    ext = secure_filename(video.filename).rsplit(".", 1)[1].lower()
    upload_path = os.path.join(UPLOAD_DIR, f"{job_id}.{ext}")
    video.save(upload_path)

    params = {
        "first_x": first_x,
        "first_y": first_y,
        "second_x": second_x,
        "second_y": second_y,
        "pixels_per_meter": pixels_per_meter,
        "tip_height": tip_height,
        "low_level": low_level,
        "medium_level": medium_level,
        "high_level": high_level,
        "auto_line": auto_line,
    }

    update_job(job_id, status="queued", progress=0, filename=video.filename)
    executor.submit(process_job, job_id, upload_path, params)
    return jsonify({"job_id": job_id})


@app.get("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = dict(jobs.get(job_id, {}))
    if not job:
        return jsonify({"error": "Job not found."}), 404
    if job.get("status") == "done":
        job["result_url"] = f"/outputs/{job['output_name']}"
    return jsonify(job)


@app.get("/outputs/<path:filename>")
def output_file(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)


if __name__ == "__main__":
    print("Open http://127.0.0.1:5000 in your browser")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
