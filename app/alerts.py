from __future__ import annotations

import base64
import hashlib
import json
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any
from urllib import parse, request
from urllib.error import HTTPError

from .config import Settings


@dataclass(frozen=True)
class AlertPayload:
    analysis_id: str
    severity: str
    water_ratio: float
    time_sec: float
    camera_name: str | None = None
    camera_revision: int | None = None
    ratio_scope: str = "full_frame"
    event_id: str | None = None
    event_type: str | None = None
    transition_id: str | None = None
    time_source: str = "video"
    occurred_at_unix: float | None = None
    snapshot_url: str | None = None

    @property
    def message(self) -> str:
        if self.event_type == "connectivity_test":
            return f"FloodEye connectivity TEST notification. Test ID: {self.analysis_id}."
        camera = "" if self.camera_name is None else f"Camera: {self.camera_name} (config v{self.camera_revision}). "
        scope = "monitored ROI" if self.ratio_scope == "roi" else "frame"
        action = {"started": "Flood alert started", "escalated": "Flood alert increased",
                  "deescalated": "Flood alert decreased", "reminder": "Flood alert ongoing",
                  "resolved": "Alert condition cleared"}.get(self.event_type, "Flood alert")
        timestamp = f"Video time: {self.time_sec:.1f}s."
        if self.time_source == "monotonic":
            wall = "" if self.occurred_at_unix is None else datetime.fromtimestamp(self.occurred_at_unix, timezone.utc).isoformat()
            timestamp = f"Camera session time: {self.time_sec:.1f}s. Observed at: {wall}."
        return (
            f"{action}: {self.severity}. "
            f"Water coverage: {self.water_ratio * 100:.1f}% of {scope}. "
            f"{camera}"
            f"{timestamp} Analysis: {self.analysis_id}."
        )


class AlertManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def configured_channels(self) -> list[str]:
        channels = ["dashboard"]
        if self.settings.email_enabled:
            channels.append("email")
        if self.settings.telegram_enabled:
            channels.append("telegram")
        if self.settings.twilio_enabled:
            channels.append("sms")
        return channels

    def target_fingerprint(self, channel: str) -> str:
        s = self.settings
        targets = {"email": [s.smtp_host, s.smtp_port, s.smtp_username, s.email_from, s.email_to],
                   "telegram": [s.telegram_chat_id], "sms": [s.twilio_account_sid, s.twilio_from, s.twilio_to]}
        return hashlib.sha256(json.dumps(targets[channel], ensure_ascii=False).encode()).hexdigest()

    def outbox_targets(self) -> dict[str, str]:
        return {channel: self.target_fingerprint(channel) for channel in self.configured_channels() if channel != "dashboard"}

    def send_channel(self, channel: str, payload: AlertPayload, target_fingerprint: str) -> dict[str, Any]:
        if channel not in self.configured_channels():
            return {"ok": False, "blocked": True, "detail": "Channel is disabled in server settings"}
        if self.target_fingerprint(channel) != target_fingerprint:
            return {"ok": False, "blocked": True, "detail": "Recipient configuration changed; queued alert was not redirected"}
        return {"email": self._send_email, "telegram": self._send_telegram, "sms": self._send_twilio}[channel](payload)

    @staticmethod
    def _failure(channel, exc):
        # Provider exception strings may contain tokens, URLs or recipients.
        if isinstance(exc, HTTPError):
            result = {"ok": False, "retryable": exc.code == 429 or exc.code >= 500,
                      "detail": f"{channel} provider returned HTTP {exc.code}"}
            try:
                result["retry_after_sec"] = float(exc.headers.get("Retry-After", 0))
            except (ValueError, TypeError, AttributeError):
                try:
                    result["retry_after_sec"] = max(0, parsedate_to_datetime(exc.headers.get("Retry-After")).timestamp() - time.time())
                except (ValueError, TypeError, AttributeError):
                    pass
            if channel == "Telegram":
                try:
                    body = json.loads(exc.read(1024 * 1024))
                    result["retry_after_sec"] = body.get("parameters", {}).get("retry_after", result.get("retry_after_sec", 0))
                except (ValueError, TypeError, AttributeError, OSError):
                    pass
            return result
        if isinstance(exc, smtplib.SMTPResponseException):
            return {"ok": False, "retryable": 400 <= exc.smtp_code < 500,
                    "detail": f"SMTP returned code {exc.smtp_code}"}
        if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPNotSupportedError)):
            return {"ok": False, "retryable": False, "detail": "SMTP rejected recipient or required security settings"}
        return {"ok": False, "retryable": True, "detail": f"{channel} connection failed or response was incomplete"}

    def _send_email(self, payload: AlertPayload) -> dict[str, Any]:
        try:
            if not all([self.settings.smtp_username, self.settings.smtp_password, self.settings.email_to]):
                return {"ok": False, "blocked": True, "detail": "SMTP credentials or EMAIL_TO are missing"}
            msg = EmailMessage()
            msg["Subject"] = f"[{payload.severity}] Flood warning"
            msg["From"] = self.settings.email_from or self.settings.smtp_username
            msg["To"] = self.settings.email_to
            key = payload.transition_id or payload.analysis_id
            msg["Message-ID"] = f"<{hashlib.sha256(key.encode()).hexdigest()}@floodeye.local>"
            msg.set_content(payload.message)
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=12) as smtp:
                smtp.starttls()
                smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                refused = smtp.send_message(msg)
                if refused:
                    return {"ok": False, "retryable": False, "detail": "Some email recipients were refused; others may have accepted the message"}
            return {"ok": True}
        except Exception as exc:
            return self._failure("Email", exc)

    def _send_telegram(self, payload: AlertPayload) -> dict[str, Any]:
        try:
            if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
                return {"ok": False, "blocked": True, "detail": "Telegram token or chat id is missing"}
            url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
            body = parse.urlencode({"chat_id": self.settings.telegram_chat_id, "text": payload.message}).encode()
            with request.urlopen(request.Request(url, data=body, method="POST"), timeout=12) as resp:
                result = json.loads(resp.read(1024 * 1024))
            if result.get("ok") is not True:
                code = int(result.get("error_code", 500))
                return {"ok": False, "retryable": code == 429 or code >= 500,
                        "detail": f"Telegram returned error {code}",
                        "retry_after_sec": result.get("parameters", {}).get("retry_after", 0)}
            return {"ok": True, "provider_id": str(result["result"]["message_id"])}
        except Exception as exc:
            return self._failure("Telegram", exc)

    def _send_twilio(self, payload: AlertPayload) -> dict[str, Any]:
        try:
            s = self.settings
            if not all([s.twilio_account_sid, s.twilio_auth_token, s.twilio_from, s.twilio_to]):
                return {"ok": False, "blocked": True, "detail": "Twilio credentials or phone numbers are missing"}
            url = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
            data = parse.urlencode({"From": s.twilio_from, "To": s.twilio_to, "Body": payload.message}).encode()
            auth = base64.b64encode(f"{s.twilio_account_sid}:{s.twilio_auth_token}".encode()).decode()
            req = request.Request(url, data=data, method="POST", headers={"Authorization": f"Basic {auth}"})
            with request.urlopen(req, timeout=12) as resp:
                result = json.loads(resp.read(1024 * 1024))
            if result.get("status") in {"failed", "undelivered", "canceled"}:
                return {"ok": False, "retryable": False, "provider_id": result["sid"],
                        "detail": "SMS provider created a message with an unsuccessful status"}
            return {"ok": True, "provider_id": result["sid"]}
        except Exception as exc:
            return self._failure("SMS", exc)
