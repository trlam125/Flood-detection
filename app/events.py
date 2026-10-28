"""Temporal alert state driven only by observation timestamps, with no I/O."""
from __future__ import annotations

import copy
import math
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .severity import SEVERITY_ORDER, SeverityThresholds, at_least, classify_ratio, max_severity

LEVELS = tuple(SEVERITY_ORDER)


class EventPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    confirm_sec: float = Field(default=3, ge=0, le=600)
    clear_sec: float = Field(default=10, ge=0, le=3600)
    reminder_sec: float = Field(default=300, ge=0, le=86400)
    hysteresis_ratio: float = Field(default=.03, ge=0, le=1)
    max_gap_sec: float = Field(default=5, gt=0, le=3600)
    ema_time_constant_sec: float = Field(default=1, gt=0, le=60)
    alert_min_severity: Literal["LOW", "MODERATE", "CRITICAL"] = "MODERATE"

    def validate_thresholds(self, thresholds: SeverityThresholds) -> None:
        boundaries = (thresholds.low, thresholds.moderate - thresholds.low, thresholds.critical - thresholds.moderate)
        if self.hysteresis_ratio >= min(gap for gap in boundaries if gap > 0):
            raise ValueError("Exit margin must be smaller than each positive gap between severity thresholds (including zero to LOW)")


@dataclass(frozen=True)
class EventDecision:
    confirmed_severity: str
    observed_severity: str
    pending_severity: str | None
    pending_elapsed_sec: float
    pending_required_sec: float
    transitions: tuple[dict[str, Any], ...]
    event: dict[str, Any] | None

    def state(self) -> dict[str, Any]:
        return {
            "severity": self.confirmed_severity, "observed_severity": self.observed_severity,
            "pending_severity": self.pending_severity,
            "pending_elapsed_sec": self.pending_elapsed_sec, "pending_required_sec": self.pending_required_sec,
            "event_id": None if self.event is None else self.event["id"],
            "event_status": None if self.event is None else self.event["status"],
        }


class EventTracker:
    def __init__(self, thresholds: SeverityThresholds, policy: EventPolicy):
        thresholds.validate()
        policy.validate_thresholds(thresholds)
        self.thresholds, self.policy = thresholds, policy
        self.confirmed = "UNKNOWN"
        self.anchor = "UNKNOWN"
        self.highest_confirmed = "UNKNOWN"
        self.events: list[dict[str, Any]] = []
        self.active: dict[str, Any] | None = None
        self.last_update: float | None = None
        self.last_observation: float | None = None
        self.last_ratio: float | None = None
        self._above: dict[str, float | None] = dict.fromkeys(LEVELS[1:])
        self._below: dict[str, float | None] = dict.fromkeys(LEVELS[1:])
        self._safe_since: float | None = None
        self._hold_since: float | None = None
        self._reminder_elapsed = 0.0
        self._batch: list[dict[str, Any]] = []
        self._finished = False

    def checkpoint(self) -> dict[str, Any]:
        return {"version": 1, "anchor": self.anchor, "highest_confirmed": self.highest_confirmed,
                "last_ratio": self.last_ratio, "last_update": self.last_update,
                "active_id": None if self.active is None else self.active["id"], "events": copy.deepcopy(self.events)}

    @classmethod
    def restore(cls, thresholds: SeverityThresholds, policy: EventPolicy, state: dict, at: float):
        """Restore identity/history only; require new confirmation after downtime."""
        levels = {*SEVERITY_ORDER, "UNKNOWN"}
        if state.get("version") != 1 or state.get("anchor") not in levels or state.get("highest_confirmed") not in levels:
            raise ValueError("Unsupported event checkpoint")
        last = state.get("last_update")
        if not math.isfinite(at) or at < 0 or (last is not None and (not math.isfinite(last) or at < last)):
            raise ValueError("Invalid recovery time")
        ratio = state.get("last_ratio")
        if ratio is not None and (not math.isfinite(ratio) or not 0 <= ratio <= 1):
            raise ValueError("Invalid checkpoint water ratio")
        tracker = cls(thresholds, policy)
        tracker.events = copy.deepcopy(state["events"])
        identifiers = [event["id"] for event in tracker.events]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Duplicate checkpoint event IDs")
        for event in tracker.events:
            if event["peak_severity"] not in SEVERITY_ORDER or not math.isfinite(event["started_sec"]) or event["started_sec"] < 0:
                raise ValueError("Invalid checkpoint event")
        active_id = state.get("active_id")
        tracker.active = next((event for event in tracker.events if event["id"] == active_id), None)
        if active_id is not None and (tracker.active is None or tracker.active["status"] not in {"active", "unknown"}
                                      or state["anchor"] == "UNKNOWN"):
            raise ValueError("Invalid active checkpoint event")
        tracker.anchor, tracker.highest_confirmed = state["anchor"], state["highest_confirmed"]
        tracker.last_ratio, tracker.last_update = ratio, at
        tracker._unknown(at, "server_restart")
        return tracker, tracker._decision("UNKNOWN", None, at)

    def _entry(self, level: str) -> float:
        return {"LOW": self.thresholds.low, "MODERATE": self.thresholds.moderate, "CRITICAL": self.thresholds.critical}[level]

    def _below_exit(self, ratio: float, level: str) -> bool:
        entry = self._entry(level)
        exit_ratio = max(0, entry - self.policy.hysteresis_ratio)
        # At a clipped zero exit, an exactly dry observation can clear a positive entry.
        return ratio < exit_ratio or (entry > 0 and exit_ratio == 0 and ratio == 0)

    @staticmethod
    def _held(since: float | None, now: float, duration: float) -> bool:
        return since is not None and now - since + 1e-9 >= duration

    def _target(self, ratio: float, observed: str) -> str:
        if self.anchor == "UNKNOWN" or SEVERITY_ORDER[observed] >= SEVERITY_ORDER[self.anchor]:
            return observed
        rank = SEVERITY_ORDER[self.anchor]
        while rank > 0 and self._below_exit(ratio, LEVELS[rank]):
            rank -= 1
        return LEVELS[rank]

    def _emit(self, kind: str, now: float, notify: bool = True, reason: str | None = None) -> None:
        assert self.active is not None
        self._batch.append({
            "id": uuid.uuid4().hex, "event_id": self.active["id"], "type": kind,
            "time_sec": now, "severity": self.confirmed, "water_ratio": self.last_ratio,
            "notify": notify, "reason": reason, "event": copy.deepcopy(self.active),
        })
        if notify:
            self._reminder_elapsed = 0

    def _unknown(self, now: float, reason: str) -> None:
        self.confirmed = "UNKNOWN"
        self._above = dict.fromkeys(LEVELS[1:])
        self._below = dict.fromkeys(LEVELS[1:])
        self._safe_since = self._hold_since = None
        self._reminder_elapsed = 0
        if self.active is not None and self.active["status"] != "unknown":
            self.active["status"] = "unknown"
            self.active["severity"] = "UNKNOWN"
            self._emit("data_lost", now, notify=False, reason=reason)

    def _confirm(self, severity: str, now: float) -> None:
        previous = self.anchor
        self.confirmed = self.anchor = severity
        self.highest_confirmed = max_severity(self.highest_confirmed, severity)
        if self.active is None:
            if at_least(severity, self.policy.alert_min_severity):
                self.active = {
                    "id": uuid.uuid4().hex, "status": "active", "severity": severity,
                    "peak_severity": severity, "started_sec": now,
                    "last_known_severity": severity,
                    "first_exceedance_sec": self._above[self.policy.alert_min_severity],
                    "last_observed_sec": now, "resolved_sec": None,
                    "observation_ended_sec": None, "end_reason": None,
                    "max_water_ratio": self.last_ratio,
                }
                self.events.append(self.active)
                self._emit("started", now)
            return
        was_unknown = self.active["status"] == "unknown"
        self.active["severity"] = severity
        self.active["last_known_severity"] = severity
        self.active["peak_severity"] = max_severity(self.active["peak_severity"], severity)
        if not at_least(severity, self.policy.alert_min_severity):
            self.active.update(status="resolved", resolved_sec=now, end_reason="below_alert_exit_confirmed")
            self._emit("resolved", now)
            self.active = None
        else:
            self.active["status"] = "active"
            if previous != severity:
                self._emit("escalated" if SEVERITY_ORDER[severity] > SEVERITY_ORDER[previous] else "deescalated", now)
            elif was_unknown:
                self._emit("data_resumed", now, notify=False)

    def _decision(self, observed: str, target: str | None, now: float) -> EventDecision:
        pending, elapsed, required = None, 0.0, 0.0
        if target is not None and target != self.confirmed:
            pending = target
            if self.anchor != "UNKNOWN" and SEVERITY_ORDER[target] < SEVERITY_ORDER[self.anchor]:
                boundaries = LEVELS[SEVERITY_ORDER[target] + 1:SEVERITY_ORDER[self.anchor] + 1]
                starts = [self._below[level] for level in boundaries]
                since = max(starts) if all(value is not None for value in starts) else now
                required = self.policy.clear_sec
            else:
                since = self._safe_since if target == "SAFE" else self._above[target]
                if target == self.anchor and self.confirmed == "UNKNOWN":
                    since = self._hold_since
                required = self.policy.confirm_sec
            elapsed = max(0, now - since) if since is not None else 0
        return EventDecision(self.confirmed, observed, pending, elapsed, required,
                             tuple(copy.deepcopy(self._batch)), copy.deepcopy(self.active))

    def update(self, time_sec: float, water_ratio: float | None) -> EventDecision:
        if self._finished:
            raise ValueError("Cannot add observations after finish")
        if not math.isfinite(time_sec) or time_sec < 0 or (self.last_update is not None and time_sec < self.last_update):
            raise ValueError("Observation timestamps must be finite, nonnegative and monotonic")
        self._batch = []
        self.last_update = time_sec
        if water_ratio is None or not math.isfinite(water_ratio):
            self._unknown(time_sec, "missing_observation")
            return self._decision("UNKNOWN", None, time_sec)
        if not 0 <= water_ratio <= 1:
            raise ValueError("Water ratio must be between 0 and 1")
        gap = self.last_observation is not None and time_sec - self.last_observation > self.policy.max_gap_sec + 1e-9
        delta = 0 if self.last_observation is None or gap or self.confirmed == "UNKNOWN" else time_sec - self.last_observation
        if gap:
            self._unknown(time_sec, "observation_gap")
        self.last_observation, self.last_ratio = time_sec, water_ratio
        if self.active is not None:
            self.active["last_observed_sec"] = time_sec
            self.active["max_water_ratio"] = max(self.active["max_water_ratio"], water_ratio)
        observed = classify_ratio(water_ratio, self.thresholds)
        for level in LEVELS[1:]:
            self._above[level] = (self._above[level] if self._above[level] is not None else time_sec) if water_ratio >= self._entry(level) else None
            self._below[level] = (self._below[level] if self._below[level] is not None else time_sec) if self._below_exit(water_ratio, level) else None
        self._safe_since = (self._safe_since if self._safe_since is not None else time_sec) if observed == "SAFE" else None
        target = self._target(water_ratio, observed)
        self._hold_since = (self._hold_since if self._hold_since is not None else time_sec) if target == self.anchor else None
        candidate = self.anchor
        if self.anchor == "UNKNOWN":
            ready = [level for level in LEVELS[1:] if self._held(self._above[level], time_sec, self.policy.confirm_sec)]
            if ready:
                candidate = ready[-1]
            elif self._held(self._safe_since, time_sec, self.policy.confirm_sec):
                candidate = "SAFE"
        else:
            ready = [level for level in LEVELS[1:] if SEVERITY_ORDER[level] > SEVERITY_ORDER[self.anchor]
                     and self._held(self._above[level], time_sec, self.policy.confirm_sec)]
            if ready:
                candidate = ready[-1]
            else:
                rank = SEVERITY_ORDER[self.anchor]
                while rank > 0 and self._held(self._below[LEVELS[rank]], time_sec, self.policy.clear_sec):
                    rank -= 1
                candidate = LEVELS[rank]
                if candidate == self.anchor and self.confirmed == "UNKNOWN" and not self._held(self._hold_since, time_sec, self.policy.confirm_sec):
                    candidate = "UNKNOWN"
        if candidate != "UNKNOWN" and candidate != self.confirmed:
            self._confirm(candidate, time_sec)
        if self.active is not None and self.confirmed != "UNKNOWN":
            self._reminder_elapsed += delta
            # A severity transition already notified at this timestamp resets the timer.
            if any(item["notify"] for item in self._batch):
                self._reminder_elapsed = 0
            elif self.policy.reminder_sec > 0 and self._reminder_elapsed + 1e-9 >= self.policy.reminder_sec:
                self._emit("reminder", time_sec)
        return self._decision(observed, self._target(water_ratio, observed), time_sec)

    def finish(self, time_sec: float, reason: str = "video_end") -> tuple[dict[str, Any], ...]:
        if self._finished:
            return ()
        if not math.isfinite(time_sec) or time_sec < (self.last_update or 0):
            raise ValueError("Finish time must not precede the last update")
        self._batch = []
        self._finished = True
        if self.active is not None:
            self.active.update(status="open_at_end" if reason == "video_end" else "interrupted",
                               observation_ended_sec=time_sec, end_reason=reason)
            if reason != "video_end":
                self.confirmed = "UNKNOWN"
                self.active["severity"] = "UNKNOWN"
            self._emit("observation_ended", time_sec, notify=False, reason=reason)
        return tuple(copy.deepcopy(self._batch))
