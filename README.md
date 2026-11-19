# FloodEye – Automated Flood Monitoring System

Detects flooding on road surfaces from **video files or RTSP camera streams** using AI, shows results live in a web interface, and sends alerts via Email / Telegram / SMS.

---

## Overview

```
Video / RTSP stream
    → RIWA UNet-ResNet34   (water segmentation)
    → SegFormer-B5 ADE20K  (road segmentation)
    → Flooded share of road width (computed per horizontal row)
    → Temporal smoothing (EMA)
    → Classification: SAFE / LOW / MODERATE / CRITICAL
    → Temporal confirmation (avoids false alerts)
    → SQLite history + web dashboard
    → Notifications: Email / Telegram / Twilio SMS
```

---

## AI Models

| Component | Model | Role |
|---|---|---|
| 🌊 Water | `riwa_unet_resnet34_deploy_v2.pth` | Flood-area segmentation – trained on river / road surface / puddle data |
| 🛣️ Road | `nvidia/segformer-b5-finetuned-ade-640-640` | Road-surface segmentation – pretrained on ADE20K |

**RIWA v2 model metrics:**

| Metric | Value |
|---|---|
| IoU – River / canal | 0.870 |
| IoU – Flooded road surface | **0.907** |
| IoU – Small puddles | 0.673 |
| FPR on dry roads | 0.003 (0.3%) |
| FPR in rainy conditions | 0.013 (1.3%) |

### Model training

The RIWA v2 model was trained on Kaggle:

🔗 **[Training notebook on Kaggle](https://www.kaggle.com/code/nhhoang14/flood-detection)**

> **Note on model weights (`*.pth`):**  
> The file `riwa_unet_resnet34_deploy_v2.pth` (~96 MB) is **not pushed to Git** (it is listed in `.gitignore`).  
> After cloning the repo, download the weights from Kaggle and place them in the `models/` folder.

---

## Installation

### Requirements

- Python 3.11+
- macOS / Linux / Windows
- A CPU is sufficient; a GPU (CUDA) speeds things up considerably (SegFormer-B5 takes ~4 s/frame on CPU)

### Install dependencies

```bash
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
# Edit .env if needed (the defaults work out of the box)
```

### Place the model

```
models/
└── riwa_unet_resnet34_deploy_v2.pth   ← download from Kaggle
```

---

## Running the application

```bash
python run.py
```

Open your browser at: **http://127.0.0.1:8000**

---

## Web interface

| Tab | Function |
|---|---|
| **Video analysis** | Upload / drag-and-drop a video → view results in real time |
| **Live camera** | Connect an RTSP stream → continuous monitoring with automatic alerts |
| **History & alerts** | Browse all recorded events |
| **Settings** | Adjust thresholds, region of interest (ROI), and alert policy |
| **Operations** | Model status, disk usage, notification outbox |

---

## Flood ratio algorithm

Instead of simply counting pixels, the system computes the **flooded share of road width on each horizontal row**:

$$\text{width\_ratio}(y) = \frac{\text{water width}(y)}{\text{road width}(y)}$$

$$\text{flood\_ratio} = 0.70 \times \overline{\text{width\_ratio}} + 0.30 \times \max(\text{width\_ratio})$$

This reflects the actual **obstruction to traffic** better than area alone.

---

## Alert levels

| Level | Flooded width ratio | Alert |
|---|---|---|
| ✅ **Safe** | < 10% | No |
| 🟡 **Low** | 10–20% | No (display only) |
| 🟠 **Moderate** | 20–45% | ✅ Yes |
| 🔴 **Critical** | ≥ 45% | ✅ Yes (high priority) |

### False-alert prevention

| Parameter | Value | Effect |
|---|---|---|
| `ALERT_CONFIRM_SEC` | 0.5 s | The ratio must stay above the threshold for ≥ 0.5 s before an alert is raised |
| `ALERT_CLEAR_SEC` | 5 s | The ratio must stay low continuously for 5 s before the alert is cleared |
| `ALERT_HYSTERESIS_RATIO` | 3% | The ratio must drop below threshold − 3% to clear the alert |
| `EMA_TIME_CONSTANT_SEC` | 1 s | Temporal smoothing of the ratio |
| `MAX_OBSERVATION_GAP_SEC` | 5 s | If no frame arrives for > 5 s, state resets to UNKNOWN |

---

## Downloading result videos

After analysis, you can download the video with road and water overlays:

- **From the UI**: click **"Download result video"** once analysis is finished
- **Via API**: `GET /api/jobs/{job_id}/download`
- **All outputs**: `GET /api/download_all` → returns a `.zip` file

Result videos are saved at: `outputs/<job_id>_annotated.mp4`

---

## Advanced configuration (`.env`)

```dotenv
# Model
MODEL_BACKEND=riwa
RIWA_MODEL_PATH=models/riwa_unet_resnet34_deploy_v2.pth
ROAD_MODEL_ID=nvidia/segformer-b5-finetuned-ade-640-640
MODEL_DEVICE=auto   # auto | cpu | cuda

# Flood thresholds (width ratio)
LOW_RATIO=0.10
MODERATE_RATIO=0.20
CRITICAL_RATIO=0.45

# Confirmation timing
ALERT_CONFIRM_SEC=0.5
ALERT_CLEAR_SEC=5

# Notifications (optional)
EMAIL_ENABLED=false
TELEGRAM_ENABLED=false
TWILIO_ENABLED=false
```

---

## Project structure

```
FloodEye/
├── app/
│   ├── model.py        # RIWA + SegFormer – water & road detection
│   ├── inference.py    # Width-based flood ratio computation
│   ├── events.py       # Confirmation logic & alert lifecycle
│   ├── main.py         # FastAPI routes & video processing pipeline
│   ├── config.py       # Configuration loaded from .env
│   ├── cameras.py      # RTSP camera profiles
│   ├── live.py         # Real-time camera monitoring
│   ├── alerts.py       # Alert delivery (Email/Telegram/SMS)
│   ├── database.py     # SQLite – history & events
│   ├── severity.py     # Severity classification
│   ├── regions.py      # ROI (region of interest)
│   ├── road_tracking.py # Road mask tracking & accumulation
│   └── templates/
│       └── index.html  # Web interface
├── models/             # Weights (not committed to Git)
├── outputs/            # Annotated result videos
├── uploads/            # Uploaded source videos
├── data/               # SQLite database
├── run.py              # Entry point
├── requirements.txt
└── .env.example
```

---

## License

MIT License