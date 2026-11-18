"""SQLite delivery claims, retries and fencing; no network I/O in analysis threads."""
from __future__ import annotations

import json
import math
import threading
import time
import uuid

from .alerts import AlertPayload


class OutboxStore:
    def __init__(self, database):
        self.db = database

    @staticmethod
    def _refresh(con, alert_id):
        rows = con.execute("SELECT channel,status,attempts,last_error,next_attempt_at,provider_id FROM notification_outbox WHERE alert_id=?", (alert_id,)).fetchall()
        dispatch = {"dashboard": {"ok": True}}
        for row in rows:
            dispatch[row["channel"]] = {"ok": row["status"] == "sent", "status": row["status"],
                "attempts": row["attempts"], "detail": row["last_error"], "next_attempt_at": row["next_attempt_at"],
                "provider_id": row["provider_id"]}
        states = {row["status"] for row in rows}
        dispatch["delivery_status"] = ("attention" if states & {"failed", "blocked"} else
                                       "pending" if states & {"pending", "retry", "sending"} else "finished")
        con.execute("UPDATE alerts SET dispatch_json=? WHERE id=?", (json.dumps(dispatch), alert_id))

    def claim(self, now, lease_seconds, max_attempts):
        with self.db._lock, self.db._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            expired = con.execute("SELECT id,alert_id FROM notification_outbox WHERE status='sending' AND lease_until<=?", (now,)).fetchall()
            con.execute("""UPDATE notification_outbox SET status=CASE WHEN attempts-retry_from_attempt>=? THEN 'failed' ELSE 'retry' END,
                lease_token=NULL,lease_until=NULL,next_attempt_at=?,last_error='Previous delivery outcome unknown after worker lease expired'
                WHERE status='sending' AND lease_until<=?""", (max_attempts, now, now))
            for item in expired:
                self._refresh(con, item["alert_id"])
            row = con.execute("""SELECT o.* FROM notification_outbox o WHERE o.status IN ('pending','retry') AND o.next_attempt_at<=?
                AND NOT EXISTS (SELECT 1 FROM notification_outbox earlier WHERE earlier.scope_key=o.scope_key
                  AND earlier.channel=o.channel AND earlier.id<o.id AND earlier.status NOT IN ('sent','cancelled'))
                ORDER BY o.id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            con.execute("UPDATE notification_outbox SET status='sending',attempts=attempts+1,lease_token=?,lease_until=? WHERE id=?",
                        (token, now + lease_seconds, row["id"]))
            self._refresh(con, row["alert_id"])
            return {**dict(row), "status": "sending", "attempts": row["attempts"] + 1, "lease_token": token,
                    "payload": json.loads(row["payload_json"])}

    def renew(self, job, now, lease_seconds):
        with self.db._lock, self.db._connect() as con:
            return con.execute("""UPDATE notification_outbox SET lease_until=? WHERE id=? AND status='sending'
                AND lease_token=? AND lease_until>?""", (now + lease_seconds, job["id"], job["lease_token"], now)).rowcount == 1

    def complete(self, job, outcome, now, max_attempts, retry_base, retry_max):
        if outcome.get("ok"):
            status, error = "sent", None
        elif outcome.get("blocked"):
            status, error = "blocked", outcome.get("detail", "Channel configuration requires attention")
        elif outcome.get("retryable", True) and job["attempts"] - job["retry_from_attempt"] < max_attempts:
            status, error = "retry", outcome.get("detail", "Delivery failed")
        else:
            status, error = "failed", outcome.get("detail", "Delivery failed")
        delay = min(retry_max, retry_base * 2 ** min(20, job["attempts"] - job["retry_from_attempt"] - 1))
        try:
            requested = float(outcome.get("retry_after_sec", 0))
        except (TypeError, ValueError):
            requested = 0
        if math.isfinite(requested):
            delay = max(delay, max(0, requested))
        with self.db._lock, self.db._connect() as con:
            cursor = con.execute("""UPDATE notification_outbox SET status=?,last_error=?,next_attempt_at=?,
                lease_token=NULL,lease_until=NULL,completed_at=?,provider_id=?
                WHERE id=? AND status='sending' AND lease_token=? AND lease_until>?""",
                (status, error, now + delay if status == "retry" else now,
                 now if status in {"sent", "failed", "blocked"} else None, outcome.get("provider_id"),
                 job["id"], job["lease_token"], now))
            if cursor.rowcount:
                self._refresh(con, job["alert_id"])
            return cursor.rowcount == 1

    def action(self, identifier, action, now):
        with self.db._lock, self.db._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM notification_outbox WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            allowed = {"failed", "blocked"} if action == "retry" else {"pending", "retry", "failed", "blocked"}
            if row["status"] not in allowed:
                raise ValueError("This delivery cannot be changed in its current state")
            con.execute("""UPDATE notification_outbox SET status=?,next_attempt_at=?,retry_from_attempt=attempts,
                completed_at=NULL,last_error=?,lease_token=NULL,lease_until=NULL WHERE id=?""",
                ("pending" if action == "retry" else "cancelled", now,
                 "Retry requested by operator" if action == "retry" else "Skipped by operator", identifier))
            self._refresh(con, row["alert_id"])

    def recent(self, limit=30):
        with self.db._connect() as con:
            rows = con.execute("""SELECT o.id,o.alert_id,o.channel,o.status,o.attempts,o.created_at,o.next_attempt_at,
                o.last_error,o.completed_at,o.provider_id,a.event_id,a.event_type,a.severity,a.camera_id,a.message
                FROM notification_outbox o JOIN alerts a ON a.id=o.alert_id ORDER BY o.id DESC LIMIT ?""",
                (max(1, min(200, limit)),)).fetchall()
            counts = {row["status"]: row["n"] for row in con.execute("SELECT status,COUNT(*) n FROM notification_outbox GROUP BY status")}
        return {"items": [dict(row) for row in rows], "counts": counts}


class OutboxWorker:
    def __init__(self, database, manager, settings, clock=time.time):
        self.store, self.manager, self.settings = OutboxStore(database), manager, settings
        self.clock = clock
        self.stop_flag, self.wake = threading.Event(), threading.Event()
        self.thread = None
        self.last_error = None

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self.run, name="notification-outbox", daemon=True)
        self.thread.start()

    def run_once(self):
        s = self.settings
        job = self.store.claim(self.clock(), s.outbox_lease_sec, s.outbox_max_attempts)
        if job is None:
            return False
        heartbeat_stop = threading.Event()
        def heartbeat():
            while not heartbeat_stop.wait(s.outbox_lease_sec / 3):
                try:
                    if not self.store.renew(job, self.clock(), s.outbox_lease_sec):
                        return
                except Exception:
                    self.last_error = "Cannot renew the notification delivery lease"
                    return
        pulse = threading.Thread(target=heartbeat, daemon=True)
        pulse.start()
        try:
            try:
                outcome = self.manager.send_channel(job["channel"], AlertPayload(**job["payload"]), job["target_fingerprint"])
            except Exception:
                outcome = {"ok": False, "retryable": True, "detail": "Unexpected channel delivery failure"}
            acknowledged = self.store.complete(job, outcome, self.clock(), s.outbox_max_attempts,
                                               s.outbox_retry_base_sec, s.outbox_retry_max_sec)
            self.last_error = None if acknowledged else "Delivery result lost its lease; outcome may be ambiguous"
        finally:
            heartbeat_stop.set()
            pulse.join(timeout=1)
        return True

    def run(self):
        while not self.stop_flag.is_set():
            try:
                if self.run_once():
                    continue
            except Exception:
                self.last_error = "Notification worker cannot access persistent delivery state"
            self.wake.wait(.5)
            self.wake.clear()

    def close(self):
        self.stop_flag.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
