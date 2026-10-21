from __future__ import annotations

import base64
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any
from urllib import parse, request

from .config import Settings
from .level_classifier import LEVEL_INFO, get_level_info


@dataclass(frozen=True)
class AlertPayload:
    analysis_id: str
    severity: str
    water_ratio: float
    time_sec: float
    level: str | None = None
    level_confidence: float = 0.0

    @property
    def message(self) -> str:
        info = get_level_info(self.level)
        level_title = info.get("title", self.level or "N/A")
        depth = info.get("depth", "--")
        advisory = info.get("advisory", "")
        conf_str = f" (Độ tin cậy: {self.level_confidence * 100:.0f}%)" if self.level_confidence > 0 else ""

        if self.level and self.level not in ("NO_CAR", "UNCERTAIN"):
            level_part = f"Mức độ ngập: {level_title} [Độ sâu ước tính: {depth}]{conf_str} (Data: best_flood_level_model)"
        elif self.level == "NO_CAR":
            level_part = "Mức độ ngập: Chưa phát hiện xe cụ thể (Đo theo tỷ lệ diện tích phân đoạn)"
        else:
            level_part = f"Mức độ ngập: {self.level or 'Đang theo dõi'}"

        advisory_part = f"Khuyến cáo: {advisory}" if advisory else ""

        parts = [
            f"🚨 CẢNH BÁO NGẬP LỤT [{self.severity}]",
            level_part,
            f"Diện tích ngập nước: {self.water_ratio * 100:.1f}% khung hình tại {self.time_sec:.1f}s",
        ]
        if advisory_part:
            parts.append(advisory_part)
        parts.append(f"Mã: {self.analysis_id}")
        return " | ".join(parts)


class AlertManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._last_sent: dict[str, float] = {}

    def configured_channels(self) -> list[str]:
        channels = ["dashboard"]
        if self.settings.email_enabled:
            channels.append("email")
        if self.settings.telegram_enabled:
            channels.append("telegram")
        if self.settings.twilio_enabled:
            channels.append("sms")
        return channels

    def can_send(self, key: str) -> bool:
        now = time.time()
        previous = self._last_sent.get(key, 0.0)
        return now - previous >= self.settings.alert_cooldown_sec

    def dispatch(self, payload: AlertPayload) -> dict[str, Any]:
        key = f"{payload.analysis_id}:{payload.severity}"
        results: dict[str, Any] = {"dashboard": {"ok": True}}
        if not self.can_send(key):
            results["cooldown"] = {"ok": False, "detail": "Alert suppressed by cooldown"}
            return results

        if self.settings.email_enabled:
            results["email"] = self._send_email(payload)
        if self.settings.telegram_enabled:
            results["telegram"] = self._send_telegram(payload)
        if self.settings.twilio_enabled:
            results["sms"] = self._send_twilio(payload)

        self._last_sent[key] = time.time()
        return results

    def _send_email(self, payload: AlertPayload) -> dict[str, Any]:
        try:
            if not all([self.settings.smtp_username, self.settings.smtp_password, self.settings.email_to]):
                raise RuntimeError("SMTP credentials or EMAIL_TO are missing")
            msg = EmailMessage()
            level_tag = f" - {payload.level}" if payload.level and payload.level != "NO_CAR" else ""
            msg["Subject"] = f"[{payload.severity}{level_tag}] Cảnh báo ngập lụt tại {payload.time_sec:.1f}s"
            msg["From"] = self.settings.email_from or self.settings.smtp_username
            msg["To"] = self.settings.email_to
            msg.set_content(payload.message)
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=12) as smtp:
                smtp.starttls()
                smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                smtp.send_message(msg)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    def _send_telegram(self, payload: AlertPayload) -> dict[str, Any]:
        try:
            if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
                raise RuntimeError("Telegram token or chat id is missing")
            url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
            body = parse.urlencode({"chat_id": self.settings.telegram_chat_id, "text": payload.message}).encode()
            with request.urlopen(request.Request(url, data=body, method="POST"), timeout=12) as resp:
                ok = 200 <= resp.status < 300
            return {"ok": ok}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    def _send_twilio(self, payload: AlertPayload) -> dict[str, Any]:
        try:
            s = self.settings
            if not all([s.twilio_account_sid, s.twilio_auth_token, s.twilio_from, s.twilio_to]):
                raise RuntimeError("Twilio credentials or phone numbers are missing")
            url = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
            data = parse.urlencode({"From": s.twilio_from, "To": s.twilio_to, "Body": payload.message}).encode()
            auth = base64.b64encode(f"{s.twilio_account_sid}:{s.twilio_auth_token}".encode()).decode()
            req = request.Request(url, data=data, method="POST", headers={"Authorization": f"Basic {auth}"})
            with request.urlopen(req, timeout=12) as resp:
                ok = 200 <= resp.status < 300
            return {"ok": ok}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
