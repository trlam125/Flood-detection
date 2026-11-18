"""Reconcile unfinished sessions before any new capture or inference starts."""
from __future__ import annotations

import copy
import math
import time

from .events import EventPolicy, EventTracker
from .severity import SeverityThresholds


def restore_checkpoint(configuration, checkpoint, now=None):
    now = time.time() if now is None else now
    saved = copy.deepcopy(checkpoint)
    if saved.get("version") != 1:
        raise ValueError("Unsupported live checkpoint")
    for key in ("elapsed_sec", "samples", "ratio_sum", "max_ratio", "recovery_count"):
        if not math.isfinite(saved[key]) or saved[key] < 0:
            raise ValueError("Invalid live checkpoint counters")
    if saved["max_ratio"] > 1 or saved["ratio_sum"] > saved["samples"]:
        raise ValueError("Invalid live checkpoint ratios")
    started = configuration["started_at_unix"]
    if not math.isfinite(started):
        raise ValueError("Invalid session time anchor")
    elapsed = max(saved["elapsed_sec"], now - started, saved["tracker"].get("last_update") or 0)
    tracker, decision = EventTracker.restore(SeverityThresholds(**configuration["thresholds"]),
        EventPolicy.model_validate(configuration["event_policy"]), saved["tracker"], elapsed)
    saved.update(elapsed_sec=elapsed, tracker=tracker.checkpoint())
    return saved, tracker, decision


def matching_source(database, run):
    configuration = run["configuration"] or {}
    camera = configuration.get("camera") or {}
    source = database.get_source(camera.get("id", ""))
    latest = database.get_camera(camera.get("id", ""))
    return bool(source and latest and source["enabled"] and source["revision"] == configuration.get("source_revision")
                and latest["revision"] == camera.get("revision"))


def recover_runs(database, active_ids=(), now=None):
    report = {"recoverable": [], "interrupted": []}
    recovered_cameras = set()
    for run in database.unfinished_analyses():
        if run["id"] in active_ids:
            continue
        cfg = run["configuration"] or {}
        camera_id = (cfg.get("camera") or {}).get("id")
        duplicate_camera = camera_id in recovered_cameras
        if camera_id is not None:
            recovered_cameras.add(camera_id)
        source = database.get_source(camera_id) if camera_id else None
        candidate = (cfg.get("analysis_mode") == "live_camera" and run["checkpoint"] is not None
                     and source and source["configuration"].get("auto_start") and matching_source(database, run)
                     and not duplicate_camera)
        if candidate:
            try:
                checkpoint, _, decision = restore_checkpoint(cfg, run["checkpoint"], now)
                database.commit_events(run["id"], decision.transitions, cfg, {}, checkpoint=checkpoint,
                                       checkpoint_state="recoverable")
                report["recoverable"].append(run["id"])
                continue
            except (ValueError, KeyError, TypeError):
                pass
        database.interrupt_analysis(run["id"], "server_restart_not_resumed")
        report["interrupted"].append(run["id"])
    return report
