from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

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
                """
            )

    def start_analysis(self, analysis_id: str, video_id: str) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO analyses(id, video_id, created_at, status) VALUES (?, ?, ?, ?)",
                (analysis_id, video_id, time.time(), "running"),
            )

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

    def fail_analysis(self, analysis_id: str, error: str) -> None:
        payload = json.dumps({"error": error}, ensure_ascii=False)
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE analyses SET finished_at=?, status='error', result_json=? WHERE id=?",
                (time.time(), payload, analysis_id),
            )

    def add_alert(
        self,
        analysis_id: str,
        severity: str,
        water_ratio: float,
        time_sec: float,
        message: str,
        channels: list[str],
        dispatch: dict[str, Any],
    ) -> int:
        with self._lock, self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO alerts(created_at, analysis_id, severity, water_ratio, time_sec, message, channels, dispatch_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(), analysis_id, severity, water_ratio, time_sec, message,
                    json.dumps(channels), json.dumps(dispatch, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def recent_analyses(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, video_id, created_at, finished_at, status, highest_severity, max_water_ratio, mean_water_ratio, output_url, frames, analyzed_frames FROM analyses ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_alerts(self, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, created_at, analysis_id, severity, water_ratio, time_sec, message, channels, dispatch_json FROM alerts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
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
