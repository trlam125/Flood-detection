# RIWA FloodEye Monitor

This project has been refactored from a physical-depth/reference-object workflow into a FloodEye-style automatic flood warning pipeline.

## Core pipeline

```text
Upload video
   -> analysis starts automatically (no Analyze button)
   -> trained water segmentation
   -> water mask
   -> flooded-area ratio
   -> temporal smoothing
   -> SAFE / LOW / MODERATE / CRITICAL
   -> automatic alert engine
   -> SQLite history + dashboard
   -> optional Email / Telegram / Twilio SMS
```

There is no click calibration, no ArUco marker, no stop-sign/person reference, no manual cm-per-pixel setup, and no separate Analyze button in the main workflow. Uploading a valid video automatically creates and starts the analysis job.

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

The ratio is the fraction of image pixels classified as water. An exponential moving average is applied before severity classification to reduce frame-to-frame noise.

These thresholds are project/demo defaults, not universal physical flood-depth thresholds. They can be changed in the dashboard or `.env`.

## Automatic alerts

By default every alert is:

1. confirmed across consecutive inference frames,
2. stored in SQLite,
3. displayed in Recent Alerts.

External channels are optional:

- Email via SMTP
- Telegram bot
- Twilio SMS

Copy `.env.example` to `.env` and fill only the channels you want to enable.

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

Or use `run_local.bat` after the virtual environment and dependencies are installed.

For an ngrok tunnel, install ngrok and run `run_ngrok.bat`.

## Main API

```text
GET  /api/health
POST /api/upload        upload + automatically start analysis
POST /api/analyze       backward-compatible manual API (dashboard does not use it)
GET  /api/jobs/{job_id}
GET  /api/alerts
GET  /api/history
```

## Project structure

```text
app/
  alerts.py       alert dispatch for dashboard/email/telegram/sms
  config.py       environment and severity configuration
  database.py     SQLite analyses and alert history
  inference.py    water ratio, smoothing input and frame overlay
  main.py         FastAPI upload/analyze/job/history API
  model.py        RIWA and optional YOLO segmentation backends
  severity.py     single source of truth for severity classification
  static/         dashboard CSS/JS
  templates/      dashboard HTML
models/
  README.md
uploads/
outputs/
data/
.env.example
requirements.txt
run.py
run_local.bat
run_ngrok.bat
```

## Important limitation

This version automatically estimates flood severity from image water coverage. It does not claim an exact physical water depth in centimeters. Exact depth for arbitrary camera views requires a physical scale/reference, calibrated scene geometry, depth sensor, stereo/depth camera, or a dedicated monocular-depth/reference-object method.
