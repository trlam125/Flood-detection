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
    alert_confirm_sec: float = float(os.getenv("ALERT_CONFIRM_SEC", "3"))
    alert_clear_sec: float = float(os.getenv("ALERT_CLEAR_SEC", "10"))
    alert_reminder_sec: float = float(os.getenv("ALERT_REMINDER_SEC", "300"))
    alert_hysteresis_ratio: float = float(os.getenv("ALERT_HYSTERESIS_RATIO", "0.03"))
    max_observation_gap_sec: float = float(os.getenv("MAX_OBSERVATION_GAP_SEC", "5"))
    ema_time_constant_sec: float = float(os.getenv("EMA_TIME_CONSTANT_SEC", "1"))
    replay_external_alerts_enabled: bool = _env_bool("REPLAY_EXTERNAL_ALERTS_ENABLED", False)
    preview_idle_timeout_sec: float = max(60, float(os.getenv("PREVIEW_IDLE_TIMEOUT_SEC", "1800")))
    live_max_cameras: int = max(1, int(os.getenv("LIVE_MAX_CAMERAS", "1")))
    outbox_max_attempts: int = max(1, int(os.getenv("OUTBOX_MAX_ATTEMPTS", "5")))
    outbox_retry_base_sec: float = max(1, float(os.getenv("OUTBOX_RETRY_BASE_SEC", "5")))
    outbox_retry_max_sec: float = max(1, float(os.getenv("OUTBOX_RETRY_MAX_SEC", "300")))
    outbox_lease_sec: float = max(30, float(os.getenv("OUTBOX_LEASE_SEC", "120")))
    operations_interval_sec: float = max(1, float(os.getenv("OPERATIONS_INTERVAL_SEC", "10")))
    telemetry_retention_days: int = max(1, int(os.getenv("TELEMETRY_RETENTION_DAYS", "7")))
    storage_warn_free_gb: float = max(0, float(os.getenv("STORAGE_WARN_FREE_GB", "5")))
    storage_critical_free_gb: float = max(0, float(os.getenv("STORAGE_CRITICAL_FREE_GB", "1")))
    storage_warn_free_percent: float = max(0, float(os.getenv("STORAGE_WARN_FREE_PERCENT", "10")))
    storage_critical_free_percent: float = max(0, float(os.getenv("STORAGE_CRITICAL_FREE_PERCENT", "2")))
    retention_enabled: bool = _env_bool("RETENTION_ENABLED", True)
    retention_purge_enabled: bool = _env_bool("RETENTION_PURGE_ENABLED", False)
    retention_upload_days: int = max(1, int(os.getenv("RETENTION_UPLOAD_DAYS", "30")))
    retention_output_days: int = max(1, int(os.getenv("RETENTION_OUTPUT_DAYS", "30")))
    retention_evidence_days: int = max(1, int(os.getenv("RETENTION_EVIDENCE_DAYS", "90")))
    retention_cache_days: int = max(1, int(os.getenv("RETENTION_CACHE_DAYS", "1")))
    retention_trash_days: int = max(1, int(os.getenv("RETENTION_TRASH_DAYS", "7")))
    retention_interval_sec: float = max(60, float(os.getenv("RETENTION_INTERVAL_SEC", "900")))
    retention_batch_size: int = max(1, min(1000, int(os.getenv("RETENTION_BATCH_SIZE", "100"))))

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
