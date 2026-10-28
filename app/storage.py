"""Capacity checks and recoverable retention of database-owned media only."""
from __future__ import annotations

import os
import re
import shutil
import stat
import threading
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit


class LowStorageError(OSError):
    pass


class StorageManager:
    def __init__(self, database, settings, active_ids=lambda: set(), disk_usage=shutil.disk_usage):
        self.db, self.settings, self.active_ids, self.disk_usage = database, settings, active_ids, disk_usage
        self.roots = {"uploads": settings.upload_dir, "outputs": settings.output_dir}
        self.trash = database.path.parent / "retention_trash"
        self.lock = threading.RLock()
        self.capacity_lock = threading.RLock()
        self.last_run = None
        self.last_error = None
        self._capacity = None
        self._capacity_at = -float("inf")

    @staticmethod
    def _safe(root: Path, relative: str) -> Path:
        if not relative or "\\" in relative or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise ValueError("Unsafe media path")
        if Path(relative).is_absolute() or ":" in relative:
            raise ValueError("Unsafe media path")
        root_abs = root.absolute()
        path = root_abs / relative
        if not path.resolve().is_relative_to(root_abs.resolve()):
            raise ValueError("Media path escapes its storage root")
        # Reject Windows junctions/reparse points and POSIX links at every level,
        # including the configured root. Never follow them during maintenance.
        for item in [root_abs, *path.relative_to(root_abs).parents]:
            candidate = item if item == root_abs else root_abs / item
            if candidate.exists() or candidate.is_symlink():
                info = candidate.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Linked storage paths cannot be maintained")
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or not stat.S_ISREG(info.st_mode):
                raise ValueError("Only regular media files may be maintained")
        return path

    def capacity(self, force=False):
        now = time.monotonic()
        with self.capacity_lock:
            if self._capacity is not None and not force and now - self._capacity_at < 2:
                return self._capacity
            disks = []
            for area, root in {**self.roots, "database": self.db.path.parent, "upload_spool": Path(tempfile.gettempdir())}.items():
                try:
                    usage = self.disk_usage(root)
                    percent = 100 * usage.free / max(1, usage.total)
                    gb = usage.free / 1024 ** 3
                    critical = gb < self.settings.storage_critical_free_gb or percent < self.settings.storage_critical_free_percent
                    warning = gb < max(self.settings.storage_warn_free_gb, self.settings.storage_critical_free_gb) or percent < max(self.settings.storage_warn_free_percent, self.settings.storage_critical_free_percent)
                    disks.append({"area": area, "total_bytes": usage.total, "free_bytes": usage.free,
                                  "free_percent": round(percent, 2), "status": "critical" if critical else "warning" if warning else "ok"})
                except OSError:
                    disks.append({"area": area, "status": "critical", "error": "Cannot read storage capacity"})
            self._capacity = {"status": "critical" if any(d["status"] == "critical" for d in disks) else
                              "warning" if any(d["status"] == "warning" for d in disks) else "ok", "disks": disks}
            self._capacity_at = now
            return self._capacity

    def require_capacity(self):
        if self.capacity()["status"] == "critical":
            raise LowStorageError("Storage is below its safe free-space limit. Free space before continuing analysis.")

    @staticmethod
    def _protected(con, active):
        protected = set(active)
        protected.update(row[0] for row in con.execute("SELECT id FROM analyses WHERE status IN ('running','recovering')"))
        protected.update(row[0] for row in con.execute("SELECT analysis_id FROM flood_events WHERE status IN ('active','unknown')"))
        protected.update(row[0] for row in con.execute("""SELECT DISTINCT a.analysis_id FROM notification_outbox o JOIN alerts a ON a.id=o.alert_id
            WHERE o.status NOT IN ('sent','cancelled')"""))
        return protected

    def _candidates(self, con, now, active):
        protected = self._protected(con, active)
        protected_paths = set()
        for row in con.execute("SELECT id,video_id FROM analyses"):
            if row["id"] in protected and re.fullmatch(r"[0-9a-f]{32}", row["video_id"] or ""):
                protected_paths.update(("uploads", row["video_id"] + suffix) for suffix in (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".part", ".mp4.part", ".mov.part", ".avi.part", ".mkv.part", ".m4v.part"))
        for row in con.execute("SELECT analysis_id,snapshot_url FROM alerts WHERE snapshot_url IS NOT NULL"):
            if row["analysis_id"] in protected:
                protected_paths.add(("outputs", unquote(urlsplit(row["snapshot_url"]).path.removeprefix("/media/outputs/"))))
        held = {(r["area"], r["relative_path"]) for r in con.execute("""SELECT area,relative_path FROM media_retention
            WHERE status IN ('moving','archived','restoring') OR (status='restored' AND protected_until>?)""", (now,))}
        result, seen = [], set()
        def add(area, relative, kind, analysis_id, days):
            key = (area, relative)
            if key in seen or key in held or key in protected_paths or analysis_id in protected:
                return
            seen.add(key)
            try:
                path = self._safe(self.roots[area], relative)
                info = path.stat()
            except (OSError, ValueError):
                return
            if now - info.st_mtime < days * 86400:
                return
            result.append({"area": area, "relative_path": relative, "kind": kind, "analysis_id": analysis_id,
                           "bytes": info.st_size, "modified_ns": info.st_mtime_ns})
        for row in con.execute("SELECT id,video_id,output_url,status FROM analyses WHERE status NOT IN ('running','recovering')").fetchall():
            identifier, video_id = row["id"], row["video_id"]
            if re.fullmatch(r"[0-9a-f]{32}", video_id or ""):
                for suffix in (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".part", ".mp4.part", ".mov.part", ".avi.part", ".mkv.part", ".m4v.part"):
                    add("uploads", video_id + suffix, "upload", identifier, self.settings.retention_upload_days)
            if re.fullmatch(r"[0-9a-f]{32}", identifier):
                add("outputs", identifier + "_annotated.mp4", "output", identifier, self.settings.retention_output_days)
                cache = self.roots["outputs"] / (identifier + "_frame_cache")
                if cache.is_dir() and not cache.is_symlink():
                    for path in cache.glob("????????.png"):
                        if re.fullmatch(r"\d{8}\.png", path.name):
                            add("outputs", f"{cache.name}/{path.name}", "cache", identifier, self.settings.retention_cache_days)
        for row in con.execute("SELECT analysis_id,snapshot_url FROM alerts WHERE snapshot_url IS NOT NULL").fetchall():
            url = urlsplit(row["snapshot_url"])
            relative = unquote(url.path.removeprefix("/media/outputs/"))
            if not url.netloc and not url.scheme and re.fullmatch(r"live_evidence/[0-9a-f]{32}_[0-9a-f]{32}\.jpg", relative):
                add("outputs", relative, "evidence", row["analysis_id"], self.settings.retention_evidence_days)
        return sorted(result, key=lambda item: (item["modified_ns"], item["relative_path"]))

    def plan(self, now=None):
        now = time.time() if now is None else now
        active = self.active_ids()
        with self.lock, self.db._connect() as con:
            candidates = self._candidates(con, now, active)
            archived = con.execute("SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM media_retention WHERE status='archived'").fetchone()
        return {"enabled": self.settings.retention_enabled, "purge_enabled": self.settings.retention_purge_enabled,
                "candidate_count": len(candidates), "candidate_bytes": sum(item["bytes"] for item in candidates),
                "candidates": candidates[:self.settings.retention_batch_size], "archived_count": archived[0], "archived_bytes": archived[1],
                "last_run": self.last_run, "last_error": self.last_error,
                "days": {"uploads": self.settings.retention_upload_days, "outputs": self.settings.retention_output_days,
                         "evidence": self.settings.retention_evidence_days, "cache": self.settings.retention_cache_days,
                         "trash": self.settings.retention_trash_days}}

    def _trash_path(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Invalid retention ID")
        return self._safe(self.trash, identifier)

    def reconcile(self):
        with self.lock, self.db._lock, self.db._connect() as con:
            for row in con.execute("SELECT * FROM media_retention WHERE status IN ('moving','restoring')").fetchall():
                source, target = self._safe(self.roots[row["area"]], row["relative_path"]), self._trash_path(row["id"])
                if source.exists() == target.exists():
                    state, error = "error", "Ambiguous interrupted move; files were preserved"
                else:
                    state = "archived" if target.exists() else "restored"
                    error = None
                con.execute("UPDATE media_retention SET status=?,error=?,updated_at=?,protected_until=? WHERE id=?",
                            (state, error, time.time(), time.time() + 7 * 86400 if state == "restored" else None, row["id"]))

    def run(self, now=None):
        now = time.time() if now is None else now
        archived, purged, errors = 0, 0, 0
        self.reconcile()
        if self.settings.retention_enabled:
            active = self.active_ids()
            with self.lock:
                self.trash.mkdir(parents=True, exist_ok=True)
                for candidate in self.plan(now)["candidates"]:
                    with self.db._lock:
                        with self.db._connect() as con:
                            # Recheck protection immediately before each file operation.
                            if candidate["analysis_id"] in self._protected(con, active):
                                continue
                            source = self._safe(self.roots[candidate["area"]], candidate["relative_path"])
                            info = source.stat()
                            if (info.st_size, info.st_mtime_ns) != (candidate["bytes"], candidate["modified_ns"]):
                                continue
                            identifier = uuid.uuid4().hex
                            target = self._trash_path(identifier)
                            con.execute("INSERT INTO media_retention VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (identifier, candidate["area"], candidate["relative_path"], candidate["kind"], candidate["analysis_id"],
                                 "moving", info.st_size, info.st_mtime_ns, now, now, None, None))
                        try:
                            os.replace(source, target)
                            state, error = "archived", None
                            archived += 1
                        except OSError:
                            state, error = "error", "Cannot archive this media file; original was preserved"
                            errors += 1
                        with self.db._connect() as con:
                            con.execute("UPDATE media_retention SET status=?,error=?,updated_at=? WHERE id=?", (state, error, now, identifier))
                if self.settings.retention_purge_enabled:
                    with self.db._connect() as con:
                        rows = con.execute("SELECT * FROM media_retention WHERE status='archived' AND updated_at<? LIMIT ?",
                            (now - self.settings.retention_trash_days * 86400, self.settings.retention_batch_size)).fetchall()
                    for row in rows:
                        with self.db._lock, self.db._connect() as con:
                            if row["analysis_id"] in self._protected(con, active):
                                continue
                            try:
                                self._trash_path(row["id"]).unlink(missing_ok=True)
                                con.execute("UPDATE media_retention SET status='purged',updated_at=? WHERE id=?", (now, row["id"]))
                                purged += 1
                            except OSError:
                                errors += 1
        self.last_run = {"at_unix": now, "archived": archived, "purged": purged, "errors": errors}
        self.last_error = "Some media could not be maintained; check retention history" if errors else None
        return self.last_run

    def history(self, limit=20):
        with self.db._connect() as con:
            return [dict(row) for row in con.execute("SELECT * FROM media_retention ORDER BY updated_at DESC LIMIT ?", (max(1, min(200, limit)),))]

    def restore(self, identifier):
        with self.lock, self.db._lock:
            with self.db._connect() as con:
                row = con.execute("SELECT * FROM media_retention WHERE id=?", (identifier,)).fetchone()
                if row is None:
                    raise KeyError(identifier)
                if row["status"] != "archived":
                    raise ValueError("Only archived media can be restored")
                target = self._safe(self.roots[row["area"]], row["relative_path"])
                source = self._trash_path(identifier)
                if target.exists() or not source.exists():
                    raise ValueError("Restore would overwrite a file or the archive is unavailable")
                target.parent.mkdir(parents=True, exist_ok=True)
                con.execute("UPDATE media_retention SET status='restoring',updated_at=? WHERE id=?", (time.time(), identifier))
            os.replace(source, target)
            with self.db._connect() as con:
                con.execute("UPDATE media_retention SET status='restored',updated_at=?,protected_until=?,error=NULL WHERE id=?",
                            (time.time(), time.time() + 7 * 86400, identifier))
            return {"ok": True, "area": row["area"], "relative_path": row["relative_path"]}
