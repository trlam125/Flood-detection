"""Operational snapshots, persistent trends and a bounded maintenance loop."""
from __future__ import annotations

import json
import os
import threading
import time

from .sources import CameraSource, resolve_source


def configuration_readiness(database, manager):
    cameras = []
    for camera in database.cameras():
        source = database.get_source(camera["id"])
        ready, issue = False, "No camera source saved"
        if source is not None:
            try:
                resolve_source(CameraSource.model_validate(source["configuration"]))
                ready, issue = True, None
            except ValueError:
                issue = "Source configuration or RTSP environment variable is unavailable"
        cameras.append({"camera_id": camera["id"], "name": camera["name"], "source_ready": ready,
                        "enabled": bool(source and source["enabled"]), "issue": issue})
    s = manager.settings
    channels = {"email": {"enabled": s.email_enabled, "configured": bool(s.smtp_username and s.smtp_password and s.email_to)},
                "telegram": {"enabled": s.telegram_enabled, "configured": bool(s.telegram_bot_token and s.telegram_chat_id)},
                "sms": {"enabled": s.twilio_enabled, "configured": bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_from and s.twilio_to)}}
    return {"cameras": cameras, "channels": channels, "configured_camera_count": sum(c["source_ready"] for c in cameras),
            "external_channel_count": sum(c["enabled"] and c["configured"] for c in channels.values())}


class OperationsMonitor:
    def __init__(self, database, settings, storage, camera_states, worker_state, manager, model_state):
        self.db, self.settings, self.storage = database, settings, storage
        self.camera_states, self.worker_state, self.manager, self.model_state = camera_states, worker_state, manager, model_state
        self.started = time.monotonic()
        self.stop_flag = threading.Event()
        self.thread = None
        self.last_error = None
        self.last_sample_at = None
        self.next_retention = 0

    def snapshot(self):
        storage = self.storage.capacity()
        worker = self.worker_state()
        states = self.camera_states()
        with self.db._connect() as con:
            con.execute("SELECT 1").fetchone()
            analyses = {row["status"]: row["n"] for row in con.execute("SELECT status,COUNT(*) n FROM analyses GROUP BY status")}
            events = {row["status"]: row["n"] for row in con.execute("SELECT status,COUNT(*) n FROM flood_events GROUP BY status")}
            deliveries = {row["status"]: row["n"] for row in con.execute("SELECT status,COUNT(*) n FROM notification_outbox GROUP BY status")}
        config = configuration_readiness(self.db, self.manager)
        active = [camera for camera in states if camera["lifecycle"] not in {"idle", "stopped", "error"}]
        expected = {camera["camera_id"] for camera in config["cameras"] if camera["enabled"]}
        live_ready = bool(active) and all(camera["connection"] == "connected" and camera["severity"] != "UNKNOWN"
            and camera.get("frame_age_sec") is not None and camera["frame_age_sec"] <= camera["fresh_frame_limit_sec"]
            and camera.get("analysis_age_sec") is not None and camera["analysis_age_sec"] <= camera["fresh_analysis_limit_sec"] for camera in active)
        monitoring_ok = live_ready and expected.issubset({camera["camera_id"] for camera in active})
        model = self.model_state()
        issues = []
        if storage["status"] != "ok":
            issues.append(f"Storage: {storage['status']}")
        if not model["exists"] or model.get("error"):
            issues.append("Segmentation model is unavailable")
        if not worker["running"] or worker.get("error"):
            issues.append("Notification worker requires attention")
        if deliveries.get("failed", 0) + deliveries.get("blocked", 0):
            issues.append("Some notifications need Retry or Skip")
        if expected and not monitoring_ok:
            issues.append("An enabled camera has no fresh confirmed observation")
        if self.last_error:
            issues.append(self.last_error)
        critical = storage["status"] == "critical" or not model["exists"] or bool(model.get("error"))
        status = "critical" if critical else "degraded" if issues else "healthy" if active else "idle"
        return {"created_at_unix": time.time(), "status": status, "uptime_sec": time.monotonic() - self.started,
                "process_id": os.getpid(), "database": {"ok": True, "analyses": analyses, "events": events},
                "storage": storage, "worker": worker, "deliveries": deliveries, "cameras": states,
                "model": model, "issues": issues, "configuration": config,
                "ready_for_live_monitoring": monitoring_ok and not critical and worker["running"] and not worker.get("error"),
                "field_validation": "required", "last_sample_at_unix": self.last_sample_at}

    def sample(self):
        snapshot = self.snapshot()
        with self.db._lock, self.db._connect() as con:
            con.execute("INSERT INTO operational_samples(created_at,payload_json) VALUES (?,?)",
                        (snapshot["created_at_unix"], json.dumps(snapshot, allow_nan=False)))
            con.execute("DELETE FROM operational_samples WHERE created_at<?", (time.time() - self.settings.telemetry_retention_days * 86400,))
        self.last_sample_at = snapshot["created_at_unix"]
        return snapshot

    def history(self, limit=60):
        with self.db._connect() as con:
            rows = con.execute("SELECT payload_json FROM operational_samples ORDER BY id DESC LIMIT ?", (max(1, min(1440, limit)),)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def run_once(self):
        try:
            if time.monotonic() >= self.next_retention:
                self.storage.run()
                self.next_retention = time.monotonic() + self.settings.retention_interval_sec
            self.sample()
            self.last_error = None
        except Exception:
            self.last_error = "Operational sampling or media maintenance failed; check storage"

    def start(self):
        self.stop_flag.clear()
        def loop():
            while not self.stop_flag.is_set():
                self.run_once()
                self.stop_flag.wait(self.settings.operations_interval_sec)
        self.thread = threading.Thread(target=loop, name="operations-monitor", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_flag.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
