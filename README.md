# RIWA FloodEye Monitor

This project has been refactored from a physical-depth/reference-object workflow into a FloodEye-style automatic flood warning pipeline.

## Core pipeline

```text
Select video
   -> browser reads frames, or server decodes requested byte ranges
   -> analysis starts before full file transfer
   -> trained water segmentation
   -> water mask
   -> flooded-area ratio
   -> time-aware temporal smoothing
   -> observed severity + time-confirmed SAFE / LOW / MODERATE / CRITICAL / UNKNOWN
   -> event lifecycle + reminders + recovery confirmation
   -> SQLite history + dashboard
   -> optional Email / Telegram / Twilio SMS
```

There is no physical-scale calibration or separate Analyze button. Selecting a
video starts frame analysis automatically. If its codec cannot be decoded by the
browser, a bounded WebSocket file reader serves requested byte ranges to an
isolated native decoder, including seeking to metadata near the file end.
The default flow finishes when analysis finishes, without uploading/storing a
full original or exporting an entire video. Saving a full annotated video is
an optional background task. Upload-first compatibility mode remains an explicit
advanced choice; unsupported browser codecs no longer automatically select it.
The main video player runs independently of inference: supported files play
locally at their playback frame rate; other codecs use a progressive H.264/AAC
stream from the byte-range reader, capped at 1280px and 30 FPS. There is no wait
for a complete upload or video export before playback. Playback starts after the
first AI result, with a transparent water/ROI layer drawn directly over the video.
The layer follows the latest analyzed sample at or before the playback position;
seeking or AI delays clear stale masks. Only four decoded overlay images are kept
in memory. The latest AI snapshot also has its own timestamp under **Ảnh AI mới nhất**. Confirmed
level, water coverage and confidence below the player follow analyzed samples
at its playback position; missing/old results are shown as unavailable rather
than reusing another time's confirmed state. Exported videos contain no text panel.
When playing or seeking a completed video, the strip follows the latest recorded
analysis sample at or before the playback time and shows its sample timestamp.
See [immediate video analysis](docs/immediate-video.md) for sampling, codec support
and the new session API.

Live RTSP and server webcam monitoring is also available: save a camera profile
and source, then choose **Start monitoring**. Monitoring runs on the server even
after the browser closes, reconnects automatically, and marks missing/stale
observations UNKNOWN. See [live camera setup](docs/live-cameras.md) for RTSP
environment variables, ROI, evidence snapshots, source limits and recovery scope.

Camera checkpoints can restore unfinished sessions after a crash while requiring
fresh confirmation from UNKNOWN. Alerts and per-channel notification jobs are
committed together in SQLite; a separate worker retries failures and the dashboard
shows delivery status with Retry/Skip controls. See [recovery and delivery](docs/persistence-delivery.md)
for recipient checks, ordering and the remaining possibility of duplicate delivery
when a provider accepted a request before the response was lost.

The **System operations** panel adds persisted trends, model timing, source and
worker health, disk thresholds and recoverable media retention. Low storage blocks
new jobs and pauses live inference as UNKNOWN. See [operations and validation](docs/operations-validation.md)
for retention, camera/channel preflight, a 24-hour soak command and labelled-event
evaluation. Field acceptance and any fine-tuning decision require target-camera data.

## What is preserved

The default model backend is the existing RIWA U-Net + ResNet34 segmentation model. Put the trained deployment checkpoint at:

```text
models/riwa_unet_resnet34_deploy.pth
```

The project also supports an Ultralytics segmentation checkpoint (`models/water_seg.pt`) by setting `MODEL_BACKEND=yolo` in `.env`.

## Severity logic

Default water-coverage thresholds:

- SAFE: below 10%
- LOW: 10% to below 20%
- MODERATE: 20% to below 45%
- CRITICAL: 45% or more

The ratio is the fraction of monitored pixels classified as water: inside the saved camera ROI, or across the full frame when no ROI is configured. An exponential moving average is applied before severity classification to reduce frame-to-frame noise.

These thresholds are project/demo defaults, not universal physical flood-depth thresholds. They can be changed in the dashboard or `.env`.

## Automatic alerts

By default every alert is:

1. confirmed by elapsed observation time, with separate entry/exit thresholds,
2. stored in SQLite,
3. displayed in Recent Alerts.

External channels are optional:

- Email via SMTP
- Telegram bot
- Twilio SMS

Video replay defaults to dashboard-only notifications. To send replay notifications
externally, set `REPLAY_EXTERNAL_ALERTS_ENABLED=true` in `.env` and configure the
desired channel. See [temporal event instructions](docs/temporal-events.md) for
confirmation, recovery, reminders, UNKNOWN and event history.

## Setup on Windows

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python run.py
```

Open:

```text
http://127.0.0.1:8000
```

The default **Phân tích video** workspace only needs a file selection. Camera
monitoring, history, setup and operations have separate workspaces. See the
[quick start](docs/quick-start.md) for the Vietnamese user guide.

Or use `run-local.bat` after the virtual environment and dependencies are installed.

For NVIDIA acceleration, see [GPU setup](docs/gpu-setup.md). `MODEL_DEVICE=auto`
selects CUDA when available; `cuda` requires CUDA and `cpu` forces CPU. The
dashboard shows the GPU name after the model is loaded.

For an ngrok tunnel, install ngrok and run `run-ngrok.bat`.

## Camera profiles and ROI

Create a camera profile, choose a reference image/video, draw the monitored region,
and save before uploading. Water coverage is then calculated inside that region.
Each save creates a configuration revision; running analyses keep their original
snapshot. Uploads without a camera still use the full frame.
See [camera and ROI instructions](docs/camera-roi.md) for the workflow, validation,
version history and API details. Live monitoring uses the same versioned profiles.

## Main API

```text
GET  /api/health
POST /api/upload        upload + automatically start analysis
POST /api/local-readers  allocate an on-demand browser file reader
WS   /api/local-readers/{id}/bridge  requested byte ranges and live results
GET  /api/local-readers/{id}/playback  progressive native-codec video playback
GET  /api/cameras
POST /api/cameras
GET  /api/cameras/{id}
PUT  /api/cameras/{id}
GET  /api/cameras/{id}/revisions
GET  /api/cameras/{id}/source
PUT  /api/cameras/{id}/source
GET  /api/live
GET  /api/cameras/{id}/live
POST /api/cameras/{id}/live/start
POST /api/cameras/{id}/live/stop
POST /api/cameras/{id}/live/restart
GET  /api/cameras/{id}/live/reference.jpg
GET  /api/cameras/{id}/live/stream
GET  /api/jobs/{job_id}
GET  /api/alerts
GET  /api/events
GET  /api/events/{id}
GET  /api/history
GET  /api/deliveries
POST /api/deliveries/{id}/retry
POST /api/deliveries/{id}/skip
GET  /api/operations
GET  /api/operations/history
GET  /api/ready
GET  /api/storage
POST /api/storage/maintain
POST /api/storage/{retention_id}/restore
```

## Project structure

```text
app/
  alerts.py       alert dispatch for dashboard/email/telegram/sms
  cameras.py      camera profile validation
  capture.py      isolated RTSP/webcam reader and bounded shared JPEG slot
  config.py       environment and severity configuration
  database.py     SQLite analyses and alert history
  delivery.py     persistent per-channel notification claims, retry and fencing
  events.py       time confirmation, hysteresis and event lifecycle
  inference.py    water ratio, smoothing input and frame overlay
  live.py         camera watchdog, reconnect and continuous event tracking
  local_reader.py bounded byte-range bridge for browser-unsupported video codecs
  local_video_worker.py isolated native video decoding process
  main.py         FastAPI upload/camera/job/history API
  model.py        RIWA and optional YOLO segmentation backends
  operations.py   operational snapshots, telemetry history and maintenance loop
  preview.py      ordered browser-frame analysis and cached-mask video export
  regions.py      normalized ROI polygons and frame masks
  recovery.py     reconciliation and safe recovery of unfinished camera sessions
  runtime.py      operating-system lock for one supervising server per database
  severity.py     single source of truth for severity classification
  sources.py      source configuration and RTSP environment lookup
  storage.py      capacity protection, owned-media archive and restore journal
  static/         dashboard CSS/JS
  templates/      dashboard HTML
models/
  README.md
uploads/
outputs/
data/
.env.example
requirements.txt
validation/       setup checks, review templates, event replay and soak reports
run.py
run-local.bat
run-ngrok.bat
```

## Offline model assessment

Before deciding whether to fine-tune the model, use the offline evaluation tools:

```powershell
.\.venv\Scripts\python.exe -m evaluation --help
```

They inspect dataset annotations, prepare image/mask manifests, validate source
splits, calculate water IoU/Dice/precision/recall, export error previews, and profile
unlabelled videos without sending alerts. See [the evaluation guide](docs/evaluation.md)
for commands, the locally checked dataset palette, and metric limitations.
The first local assessment is summarized in [baseline results](docs/baseline-results.md).

## Important limitation

This version automatically estimates flood severity from image water coverage. It does not claim an exact physical water depth in centimeters. Exact depth for arbitrary camera views requires a physical scale/reference, calibrated scene geometry, depth sensor, stereo/depth camera, or a dedicated monocular-depth/reference-object method.
