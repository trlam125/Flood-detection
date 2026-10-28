from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class CameraConflict(ValueError):
    pass


class Database:
    def __init__(self, path: Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not read_only:
            self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = (sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30) if self.read_only else
               sqlite3.connect(self.path, timeout=30))
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def _init_schema(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS analyses (
                    id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    finished_at REAL,
                    status TEXT NOT NULL,
                    highest_severity TEXT,
                    max_water_ratio REAL,
                    mean_water_ratio REAL,
                    output_url TEXT,
                    frames INTEGER DEFAULT 0,
                    analyzed_frames INTEGER DEFAULT 0,
                    result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    analysis_id TEXT,
                    severity TEXT NOT NULL,
                    water_ratio REAL NOT NULL,
                    time_sec REAL,
                    message TEXT NOT NULL,
                    channels TEXT NOT NULL,
                    dispatch_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_analyses_created ON analyses(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);
                CREATE TABLE IF NOT EXISTS camera_configs (
                    camera_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (camera_id, revision)
                );
                CREATE TABLE IF NOT EXISTS flood_events (
                    id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL,
                    camera_id TEXT, camera_revision INTEGER,
                    status TEXT NOT NULL, severity TEXT NOT NULL, peak_severity TEXT NOT NULL,
                    event_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_transitions (
                    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, analysis_id TEXT NOT NULL,
                    time_sec REAL NOT NULL, event_type TEXT NOT NULL,
                    transition_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS camera_sources (
                    camera_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    config_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_checkpoints (
                    analysis_id TEXT PRIMARY KEY, camera_id TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL, state TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER NOT NULL, transition_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL, channel TEXT NOT NULL,
                    target_fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    retry_from_attempt INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, next_attempt_at REAL NOT NULL,
                    lease_token TEXT, lease_until REAL, last_error TEXT,
                    completed_at REAL, provider_id TEXT,
                    UNIQUE(transition_id, channel)
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_due ON notification_outbox(status,next_attempt_at,id);
                CREATE INDEX IF NOT EXISTS idx_outbox_scope ON notification_outbox(scope_key,channel,id);
                CREATE TABLE IF NOT EXISTS operational_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operational_samples_time ON operational_samples(created_at);
                CREATE TABLE IF NOT EXISTS media_retention (
                    id TEXT PRIMARY KEY, area TEXT NOT NULL, relative_path TEXT NOT NULL, kind TEXT NOT NULL,
                    analysis_id TEXT, status TEXT NOT NULL, bytes INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, protected_until REAL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_created ON flood_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_transitions_event ON event_transitions(event_id, time_sec);
                """
            )
            # Additive migration: keep existing analyses/alerts and result JSON intact.
            for table in ("analyses", "alerts"):
                columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
                for name, kind in (("camera_id", "TEXT"), ("camera_revision", "INTEGER")):
                    if name not in columns:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                if table == "analyses" and "config_json" not in columns:
                    con.execute("ALTER TABLE analyses ADD COLUMN config_json TEXT")
                if table == "alerts":
                    for name in ("event_id", "event_type", "event_transition_id", "snapshot_url"):
                        if name not in columns:
                            con.execute(f"ALTER TABLE alerts ADD COLUMN {name} TEXT")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_transition ON alerts(event_transition_id)")

    @staticmethod
    def _event_payload(analysis_id: str, event: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
        camera = configuration.get("camera") or {}
        return {**event, "analysis_id": analysis_id, "camera_id": camera.get("id"),
                "camera_revision": camera.get("revision"), "camera_name": camera.get("name"),
                "ratio_scope": configuration["ratio_scope"],
                "analysis_mode": configuration.get("analysis_mode", "video"),
                "time_source": configuration.get("time_source", "video"),
                "started_at_unix": configuration.get("started_at_unix")}

    def save_source(self, camera_id: str, configuration: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT revision FROM camera_sources WHERE camera_id=?", (camera_id,)).fetchone()
            revision = 0 if row is None else row["revision"]
            if revision != expected_revision:
                raise CameraConflict("Camera source has changed. Reload it before saving.")
            con.execute("""INSERT INTO camera_sources(camera_id,revision,config_json,enabled,updated_at) VALUES (?,?,?,0,?)
                ON CONFLICT(camera_id) DO UPDATE SET revision=excluded.revision,config_json=excluded.config_json,updated_at=excluded.updated_at""",
                (camera_id, revision + 1, json.dumps(configuration, allow_nan=False), time.time()))
        return self.get_source(camera_id)

    def get_source(self, camera_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM camera_sources WHERE camera_id=?", (camera_id,)).fetchone()
        if row is None:
            return None
        return {"camera_id": camera_id, "revision": row["revision"], "configuration": json.loads(row["config_json"]),
                "enabled": bool(row["enabled"]), "updated_at": row["updated_at"]}

    def set_source_enabled(self, camera_id: str, enabled: bool) -> None:
        with self._lock, self._connect() as con:
            con.execute("UPDATE camera_sources SET enabled=?,updated_at=? WHERE camera_id=?", (int(enabled), time.time(), camera_id))

    def resumable_sources(self) -> list[str]:
        with self._connect() as con:
            rows = con.execute("SELECT camera_id,config_json FROM camera_sources WHERE enabled=1 ORDER BY camera_id").fetchall()
        return [row["camera_id"] for row in rows if json.loads(row["config_json"]).get("auto_start", False)]

    @staticmethod
    def _upsert_event(con: sqlite3.Connection, payload: dict[str, Any]) -> None:
        now = time.time()
        con.execute("""INSERT INTO flood_events(id,analysis_id,camera_id,camera_revision,status,severity,peak_severity,event_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            status=excluded.status,severity=excluded.severity,peak_severity=excluded.peak_severity,
            event_json=excluded.event_json,updated_at=excluded.updated_at""",
            (payload["id"], payload["analysis_id"], payload["camera_id"], payload["camera_revision"],
             payload["status"], payload["severity"], payload["peak_severity"], json.dumps(payload, ensure_ascii=False, allow_nan=False), now, now))

    def save_event(self, analysis_id: str, event: dict[str, Any], configuration: dict[str, Any]) -> None:
        with self._lock, self._connect() as con:
            self._upsert_event(con, self._event_payload(analysis_id, event, configuration))

    def recent_events(self, limit: int = 20, analysis_id: str | None = None, camera_id: str | None = None) -> list[dict[str, Any]]:
        clauses, values = [], []
        for key, value in (("analysis_id", analysis_id), ("camera_id", camera_id)):
            if value is not None:
                clauses.append(f"{key}=?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as con:
            rows = con.execute("SELECT event_json FROM flood_events" + where + " ORDER BY created_at DESC LIMIT ?",
                               (*values, max(1, min(100, int(limit))))).fetchall()
        return [json.loads(row["event_json"]) for row in rows]

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT event_json FROM flood_events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                return None
            transitions = con.execute("SELECT transition_json FROM event_transitions WHERE event_id=? ORDER BY time_sec,created_at,id", (event_id,)).fetchall()
        return {**json.loads(row["event_json"]), "transitions": [json.loads(item["transition_json"]) for item in transitions]}

    def save_camera(self, configuration: dict[str, Any], camera_id: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()
            if camera_id is None:
                camera_id, revision, created = uuid.uuid4().hex, 1, now
            else:
                row = con.execute("SELECT config_json, revision FROM camera_configs WHERE camera_id=? ORDER BY revision DESC LIMIT 1", (camera_id,)).fetchone()
                if row is None:
                    raise KeyError(camera_id)
                if expected_revision != row["revision"]:
                    raise CameraConflict("Camera configuration has changed. Reload it before saving.")
                revision, created = row["revision"] + 1, json.loads(row["config_json"])["created_at"]
            result = {**configuration, "id": camera_id, "revision": revision, "created_at": created, "updated_at": now}
            con.execute("INSERT INTO camera_configs(camera_id, revision, config_json, created_at) VALUES (?, ?, ?, ?)",
                        (camera_id, revision, json.dumps(result, ensure_ascii=False, allow_nan=False), now))
        return result

    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT config_json FROM camera_configs WHERE camera_id=? ORDER BY revision DESC LIMIT 1", (camera_id,)).fetchone()
        return None if row is None else json.loads(row["config_json"])

    def cameras(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("""SELECT c.config_json FROM camera_configs c
                JOIN (SELECT camera_id, MAX(revision) revision FROM camera_configs GROUP BY camera_id) latest
                ON c.camera_id=latest.camera_id AND c.revision=latest.revision ORDER BY c.created_at DESC""").fetchall()
        return [json.loads(row["config_json"]) for row in rows]

    def camera_revisions(self, camera_id: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT config_json FROM camera_configs WHERE camera_id=? ORDER BY revision DESC", (camera_id,)).fetchall()
        return [json.loads(row["config_json"]) for row in rows]

    def start_analysis(self, analysis_id: str, video_id: str, configuration: dict[str, Any] | None = None,
                       checkpoint: dict[str, Any] | None = None) -> None:
        camera = (configuration or {}).get("camera") or {}
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO analyses(id, video_id, created_at, status, camera_id, camera_revision, config_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (analysis_id, video_id, time.time(), "running", camera.get("id"), camera.get("revision"), json.dumps(configuration) if configuration is not None else None),
            )
            if checkpoint is not None:
                self._save_checkpoint(con, analysis_id, camera["id"], checkpoint, "active")
                con.execute("UPDATE camera_sources SET enabled=1,updated_at=? WHERE camera_id=?", (time.time(), camera["id"]))

    @staticmethod
    def _save_checkpoint(con, analysis_id, camera_id, checkpoint, state):
        con.execute("""INSERT INTO live_checkpoints VALUES (?,?,?,?,?) ON CONFLICT(analysis_id) DO UPDATE SET
            checkpoint_json=excluded.checkpoint_json,state=excluded.state,updated_at=excluded.updated_at""",
            (analysis_id, camera_id, json.dumps(checkpoint, allow_nan=False), state, time.time()))

    def commit_events(self, analysis_id, transitions, configuration, specifications, checkpoint=None,
                      final_result=None, checkpoint_state="active") -> list[dict[str, Any]]:
        """One transaction for event state, alert intent, per-channel outbox and checkpoint."""
        created = []
        camera = configuration.get("camera") or {}
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for transition in transitions:
                cursor = con.execute("""INSERT OR IGNORE INTO event_transitions VALUES (?,?,?,?,?,?,?)""",
                    (transition["id"], transition["event_id"], analysis_id, transition["time_sec"], transition["type"],
                     json.dumps(transition, allow_nan=False), time.time()))
                if cursor.rowcount == 0:
                    continue
                self._upsert_event(con, self._event_payload(analysis_id, transition["event"], configuration))
                spec = specifications.get(transition["id"])
                if spec is None:
                    continue
                payload, targets = spec["payload"], spec["targets"]
                dispatch = {"dashboard": {"ok": True}, "delivery_status": "pending" if targets else "finished"}
                if not targets:
                    dispatch["mode"] = "no_external_channels" if configuration.get("external_alerts_enabled") else "dashboard_only"
                for channel in targets:
                    dispatch[channel] = {"ok": False, "status": "pending", "attempts": 0}
                cursor = con.execute("""INSERT INTO alerts(created_at,analysis_id,severity,water_ratio,time_sec,message,
                    channels,dispatch_json,camera_id,camera_revision,event_id,event_type,event_transition_id,snapshot_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (time.time(), analysis_id, payload["severity"], payload["water_ratio"], payload["time_sec"], spec["message"],
                     json.dumps(["dashboard", *targets]), json.dumps(dispatch), camera.get("id"), camera.get("revision"),
                     payload["event_id"], payload["event_type"], payload["transition_id"], payload.get("snapshot_url")))
                alert_id = cursor.lastrowid
                for channel, fingerprint in targets.items():
                    con.execute("""INSERT INTO notification_outbox(alert_id,transition_id,scope_key,channel,
                        target_fingerprint,payload_json,created_at,next_attempt_at) VALUES (?,?,?,?,?,?,?,?)""",
                        (alert_id, transition["id"], camera.get("id") or analysis_id, channel,
                         fingerprint, json.dumps(payload, allow_nan=False), time.time(), time.time()))
                created.append({"id": alert_id, **payload, "message": spec["message"], "dispatch": dispatch,
                                "camera_id": camera.get("id"), "camera_name": camera.get("name")})
            if checkpoint is not None:
                for event in checkpoint["tracker"]["events"]:
                    if event["id"] == checkpoint["tracker"].get("active_id"):
                        self._upsert_event(con, self._event_payload(analysis_id, event, configuration))
                self._save_checkpoint(con, analysis_id, camera["id"], checkpoint,
                                      "closed" if final_result is not None else checkpoint_state)
                if final_result is None:
                    con.execute("UPDATE analyses SET status=? WHERE id=?",
                                ("recovering" if checkpoint_state == "recoverable" else "running", analysis_id))
            if final_result is not None:
                con.execute("""UPDATE analyses SET status='done',finished_at=?,highest_severity=?,max_water_ratio=?,
                    mean_water_ratio=?,frames=?,analyzed_frames=?,result_json=? WHERE id=?""",
                    (time.time(), final_result["highest_severity"], final_result["max_water_ratio"], final_result["mean_water_ratio"],
                     final_result["frames"], final_result["analyzed_frames"], json.dumps(final_result, allow_nan=False), analysis_id))
        return created

    def unfinished_analyses(self):
        with self._connect() as con:
            rows = con.execute("""SELECT a.id,a.config_json,c.checkpoint_json,c.state FROM analyses a
                LEFT JOIN live_checkpoints c ON c.analysis_id=a.id WHERE a.status IN ('running','recovering')
                ORDER BY a.created_at DESC""").fetchall()
        return [{"id": row["id"], "configuration": json.loads(row["config_json"] or "null"),
                 "checkpoint": json.loads(row["checkpoint_json"] or "null"), "state": row["state"]} for row in rows]

    def recoverable_camera(self, camera_id):
        return next((run for run in self.unfinished_analyses() if run["state"] == "recoverable"
                     and (run["configuration"].get("camera") or {}).get("id") == camera_id), None)

    def interrupt_analysis(self, analysis_id, reason="server_restart"):
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM analyses WHERE id=? AND status IN ('running','recovering')", (analysis_id,)).fetchone()
            if row is None:
                return
            history = dict(row)
            saved = con.execute("SELECT checkpoint_json FROM live_checkpoints WHERE analysis_id=?", (analysis_id,)).fetchone()
            checkpoint = {} if saved is None else json.loads(saved["checkpoint_json"])
            events = con.execute("SELECT event_json FROM flood_events WHERE analysis_id=? AND status IN ('active','unknown')", (analysis_id,)).fetchall()
            for row in events:
                event = json.loads(row["event_json"])
                latest = con.execute("SELECT MAX(time_sec) FROM event_transitions WHERE event_id=?", (event["id"],)).fetchone()[0]
                at = max(event["last_observed_sec"], latest or 0)
                event.update(status="interrupted", severity="UNKNOWN", end_reason=reason, observation_ended_sec=at)
                self._upsert_event(con, event)
                transition = {"id": uuid.uuid4().hex, "event_id": event["id"], "type": "observation_ended", "time_sec": at,
                              "severity": "UNKNOWN", "water_ratio": checkpoint.get("tracker", {}).get("last_ratio"), "notify": False,
                              "reason": reason, "event": event}
                con.execute("INSERT INTO event_transitions VALUES (?,?,?,?,?,?,?)", (transition["id"], event["id"], analysis_id,
                            at, transition["type"], json.dumps(transition), time.time()))
            con.execute("UPDATE live_checkpoints SET state='closed',updated_at=? WHERE analysis_id=?", (time.time(), analysis_id))
            samples = checkpoint.get("samples", history["analyzed_frames"] or 0)
            con.execute("""UPDATE analyses SET status='error',finished_at=?,highest_severity=?,max_water_ratio=?,
                mean_water_ratio=?,frames=?,analyzed_frames=?,result_json=? WHERE id=?""",
                (time.time(), checkpoint.get("tracker", {}).get("highest_confirmed", history["highest_severity"]),
                 checkpoint.get("max_ratio", history["max_water_ratio"]),
                 checkpoint.get("ratio_sum", 0) / max(1, samples) if checkpoint else history["mean_water_ratio"],
                 checkpoint.get("samples", history["frames"] or 0), samples,
                 json.dumps({"error": reason, "recovery": "interrupted"}), analysis_id))

    def finish_analysis(self, analysis_id: str, result: dict[str, Any]) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                """
                UPDATE analyses
                SET finished_at=?, status='done', highest_severity=?, max_water_ratio=?,
                    mean_water_ratio=?, output_url=?, frames=?, analyzed_frames=?, result_json=?
                WHERE id=?
                """,
                (
                    time.time(),
                    result.get("highest_severity"),
                    result.get("max_water_ratio"),
                    result.get("mean_water_ratio"),
                    result.get("output_url"),
                    result.get("frames", 0),
                    result.get("analyzed_frames", 0),
                    json.dumps(result, ensure_ascii=False),
                    analysis_id,
                ),
            )

    def fail_analysis(self, analysis_id: str, error: str, partial_result: dict[str, Any] | None = None) -> None:
        result = {"error": error}
        if partial_result is not None:
            result["partial_result"] = partial_result
        payload = json.dumps(result, ensure_ascii=False)
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE analyses SET finished_at=?, status='error', result_json=? WHERE id=?",
                (time.time(), payload, analysis_id),
            )

    def recent_analyses(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, video_id, created_at, finished_at, status, highest_severity, max_water_ratio, mean_water_ratio, output_url, frames, analyzed_frames, camera_id, camera_revision, config_json FROM analyses ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["configuration"] = json.loads(item.pop("config_json") or "null")
            result.append(item)
        return result

    def recent_alerts(self, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._connect() as con:
            rows = con.execute(
                """SELECT a.id, a.created_at, a.analysis_id, a.severity, a.water_ratio, a.time_sec,
                    a.message, a.channels, a.dispatch_json, a.camera_id, a.camera_revision, a.event_id, a.event_type, a.snapshot_url,
                    c.config_json AS camera_config_json FROM alerts a LEFT JOIN camera_configs c
                    ON a.camera_id=c.camera_id AND a.camera_revision=c.revision
                    ORDER BY a.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            camera = json.loads(item.pop("camera_config_json") or "null")
            item["camera_name"] = None if camera is None else camera["name"]
            item["ratio_scope"] = "roi" if camera is not None and camera["roi"] is not None else "full_frame"
            try:
                item["channels"] = json.loads(item.get("channels") or "[]")
            except Exception:
                item["channels"] = []
            try:
                item["dispatch"] = json.loads(item.pop("dispatch_json") or "{}")
            except Exception:
                item["dispatch"] = {}
                item.pop("dispatch_json", None)
            result.append(item)
        return result
