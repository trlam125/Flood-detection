from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "RIWA FloodEye Monitor")
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))

    upload_dir: Path = BASE_DIR / "uploads"
    output_dir: Path = BASE_DIR / "outputs"
    data_dir: Path = BASE_DIR / "data"
    db_path: Path = BASE_DIR / "data" / "flood_monitor.db"

    model_backend: str = os.getenv("MODEL_BACKEND", "riwa").strip().lower()
    model_path: Path = Path(os.getenv("RIWA_MODEL_PATH", str(BASE_DIR / "models" / "riwa_unet_resnet34_deploy.pth")))
    yolo_model_path: Path = Path(os.getenv("YOLO_WATER_MODEL_PATH", str(BASE_DIR / "models" / "water_seg.pt")))

    low_ratio: float = float(os.getenv("LOW_RATIO", "0.10"))
    moderate_ratio: float = float(os.getenv("MODERATE_RATIO", "0.20"))
    critical_ratio: float = float(os.getenv("CRITICAL_RATIO", "0.45"))
    alert_min_severity: str = os.getenv("ALERT_MIN_SEVERITY", "MODERATE").strip().upper()
    alert_confirm_frames: int = max(1, int(os.getenv("ALERT_CONFIRM_FRAMES", "2")))
    alert_cooldown_sec: int = max(0, int(os.getenv("ALERT_COOLDOWN_SEC", "300")))
    ema_alpha: float = min(1.0, max(0.01, float(os.getenv("EMA_ALPHA", "0.35"))))

    email_enabled: bool = _env_bool("EMAIL_ENABLED", False)
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    email_from: str = os.getenv("EMAIL_FROM", "")
    email_to: str = os.getenv("EMAIL_TO", "")

    telegram_enabled: bool = _env_bool("TELEGRAM_ENABLED", False)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    twilio_enabled: bool = _env_bool("TWILIO_ENABLED", False)
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_from: str = os.getenv("TWILIO_FROM", "")
    twilio_to: str = os.getenv("TWILIO_TO", "")


settings = Settings()
if not settings.model_path.is_absolute():
    object.__setattr__(settings, "model_path", BASE_DIR / settings.model_path)
if not settings.yolo_model_path.is_absolute():
    object.__setattr__(settings, "yolo_model_path", BASE_DIR / settings.yolo_model_path)
for folder in (settings.upload_dir, settings.output_dir, settings.data_dir, settings.model_path.parent, settings.yolo_model_path.parent):
    folder.mkdir(parents=True, exist_ok=True)
