from __future__ import annotations

import math
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.cameras import CameraConfiguration
from app.events import EventPolicy, EventTracker
from app.severity import SeverityThresholds, at_least


class Interval(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start_sec: float = Field(ge=0)
    end_sec: float = Field(gt=0)
    label: Literal["flood", "normal", "unreviewed"]


class Clip(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=100)
    path: str
    source_id: str = Field(min_length=1, max_length=100)
    split: Literal["train", "val", "test"] = "test"
    target_camera: bool = False
    conditions: list[str] = Field(default_factory=list)
    duration_sec: float = Field(gt=0)
    intervals: list[Interval] = Field(min_length=1, max_length=10000)
    camera: CameraConfiguration | None = None

    @model_validator(mode="after")
    def partition(self):
        end = 0.0
        for interval in self.intervals:
            if interval.end_sec <= interval.start_sec or abs(interval.start_sec - end) > 1e-6:
                raise ValueError("Labels must partition the full clip in order; use unreviewed for unknown sections")
            end = interval.end_sec
        if abs(end - self.duration_sec) > 1e-6:
            raise ValueError("Label intervals must cover the declared duration exactly")
        return self


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    training_overlap: Literal["unknown", "no_overlap", "overlap"] = "unknown"
    clips: list[Clip] = Field(min_length=1)

    @model_validator(mode="after")
    def independent_sources(self):
        if len({clip.id for clip in self.clips}) != len(self.clips):
            raise ValueError("Clip IDs must be unique")
        sources = {}
        for clip in self.clips:
            if clip.source_id in sources and sources[clip.source_id] != clip.split:
                raise ValueError("A camera/source may not appear in multiple splits")
            sources[clip.source_id] = clip.split
        return self


class Acceptance(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min_reference_events: int = Field(default=10, ge=1)
    min_normal_hours: float = Field(default=1, gt=0)
    min_event_precision: float = Field(default=.9, ge=0, le=1)
    min_event_recall: float = Field(default=.95, ge=0, le=1)
    max_false_alarms_per_normal_hour: float = Field(default=.1, ge=0)
    max_detection_delay_p95_sec: float = Field(default=10, ge=0)
    max_unknown_fraction: float = Field(default=.05, ge=0, le=1)
    required_conditions: list[str] = Field(default_factory=lambda: ["day", "night", "rain", "dry"])


def overlap(left, right):
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def evaluate_observations(clip: Clip, observations, thresholds=None, policy=None):
    thresholds = thresholds or (SeverityThresholds(**clip.camera.thresholds.model_dump()) if clip.camera else SeverityThresholds())
    policy = policy or (clip.camera.event_policy if clip.camera else None) or EventPolicy()
    tracker = EventTracker(thresholds, policy)
    rows, previous_time = [], -1.0
    for observation in observations:
        at, ratio = observation["time_sec"], observation["water_ratio"]
        if not math.isfinite(at) or at <= previous_time or at < 0 or at >= clip.duration_sec:
            raise ValueError("Observation timestamps must increase strictly within the clip")
        if ratio is not None and (not math.isfinite(ratio) or not 0 <= ratio <= 1):
            raise ValueError("Observation water ratios must be finite in [0,1] or null")
        decision = tracker.update(at, ratio)
        rows.append({"time_sec": at, "water_ratio": ratio, **decision.state()})
        previous_time = at
    if not rows:
        raise ValueError("No observations to evaluate")
    tracker.finish(clip.duration_sec)
    references = []
    for interval in clip.intervals:
        if interval.label == "flood":
            if references and abs(references[-1][1] - interval.start_sec) < 1e-6:
                references[-1] = (references[-1][0], interval.end_sec)
            else:
                references.append((interval.start_sec, interval.end_sec))
    reviewed = [(i.start_sec, i.end_sec) for i in clip.intervals if i.label != "unreviewed"]
    normal = [(i.start_sec, i.end_sec) for i in clip.intervals if i.label == "normal"]
    known_sec = false_alarm_sec = 0.0
    predicted = {}
    for index, row in enumerate(rows):
        end = rows[index + 1]["time_sec"] if index + 1 < len(rows) else clip.duration_sec
        valid_end = min(end, row["time_sec"] + policy.max_gap_sec)
        span = (row["time_sec"], valid_end)
        if row["severity"] == "UNKNOWN":
            continue
        known_sec += sum(overlap(span, interval) for interval in reviewed)
        if at_least(row["severity"], policy.alert_min_severity) and row["event_id"]:
            if any(overlap(span, interval) > 0 for interval in reviewed):
                predicted.setdefault(row["event_id"], []).append(span)
            false_alarm_sec += sum(overlap(span, interval) for interval in normal)
    predicted_events = list(predicted.values())
    edges = [[j for j, ref in enumerate(references) if any(overlap(span, ref) > 0 for span in spans)] for spans in predicted_events]
    matched = {}
    def assign(index, visited):
        for target in edges[index]:
            if target in visited:
                continue
            visited.add(target)
            if target not in matched or assign(matched[target], visited):
                matched[target] = index
                return True
        return False
    for index in range(len(predicted_events)):
        assign(index, set())
    delays = [min(max(span[0], references[target][0]) for span in predicted_events[index] if overlap(span, references[target]) > 0)
              - references[target][0] for target, index in matched.items()]
    tp, fp, fn = len(matched), len(predicted_events) - len(matched), len(references) - len(matched)
    reviewed_sec = sum(end - start for start, end in reviewed)
    normal_hours = sum(end - start for start, end in normal) / 3600
    return {"clip_id": clip.id, "source_id": clip.source_id, "target_camera": clip.target_camera,
        "conditions": clip.conditions, "duration_sec": clip.duration_sec, "reviewed_sec": reviewed_sec,
        "unreviewed_sec": clip.duration_sec - reviewed_sec, "normal_hours": normal_hours,
        "reference_events": len(references), "predicted_events": len(predicted_events), "tp": tp, "fp": fp, "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None,
        "false_alarms_per_normal_hour": fp / normal_hours if normal_hours else None,
        "false_alarm_sec": false_alarm_sec, "unknown_sec": max(0, reviewed_sec - known_sec),
        "detection_delays_sec": delays, "observations": rows, "events": tracker.events}


def summarize_events(reports, training_overlap, acceptance=None):
    acceptance = acceptance or Acceptance()
    tp, fp, fn = (sum(report[key] for report in reports) for key in ("tp", "fp", "fn"))
    normal_hours = sum(report["normal_hours"] for report in reports)
    reviewed = sum(report["reviewed_sec"] for report in reports)
    delays = [delay for report in reports for delay in report["detection_delays_sec"]]
    metrics = {"reference_events": tp + fn, "predicted_events": tp + fp, "tp": tp, "fp": fp, "fn": fn,
        "event_precision": tp / (tp + fp) if tp + fp else None, "event_recall": tp / (tp + fn) if tp + fn else None,
        "normal_hours": normal_hours, "reviewed_sec": reviewed,
        "false_alarms_per_normal_hour": fp / normal_hours if normal_hours else None,
        "false_alarm_sec": sum(report["false_alarm_sec"] for report in reports),
        "unknown_fraction": sum(report["unknown_sec"] for report in reports) / reviewed if reviewed else None,
        "detection_delay_p95_sec": float(np.percentile(delays, 95)) if delays else None}
    conditions = {condition for report in reports if report["target_camera"] for condition in report["conditions"]}
    insufficient = []
    if training_overlap != "no_overlap":
        insufficient.append("Training overlap has not been ruled out")
    if not reports or not all(report["target_camera"] for report in reports):
        insufficient.append("Reports must use reviewed data from the target camera(s)")
    if any(report["unreviewed_sec"] > 1e-6 for report in reports):
        insufficient.append("Some sections are unreviewed")
    if tp + fn < acceptance.min_reference_events:
        insufficient.append("Not enough reference events")
    if normal_hours < acceptance.min_normal_hours:
        insufficient.append("Not enough reviewed non-flood footage")
    if not set(acceptance.required_conditions).issubset(conditions):
        insufficient.append("Missing required target-camera conditions")
    checks = {"event_precision": (metrics["event_precision"], acceptance.min_event_precision, "min"),
              "event_recall": (metrics["event_recall"], acceptance.min_event_recall, "min"),
              "false_alarms_per_normal_hour": (metrics["false_alarms_per_normal_hour"], acceptance.max_false_alarms_per_normal_hour, "max"),
              "detection_delay_p95_sec": (metrics["detection_delay_p95_sec"], acceptance.max_detection_delay_p95_sec, "max"),
              "unknown_fraction": (metrics["unknown_fraction"], acceptance.max_unknown_fraction, "max")}
    failed = [key for key, (value, bound, direction) in checks.items() if value is None or (value < bound if direction == "min" else value > bound)]
    decision = "collect_more_data" if insufficient else "investigate_before_training" if failed else "keep_current_model_candidate"
    return {"metrics": metrics, "acceptance": acceptance.model_dump(), "insufficient_evidence": insufficient,
            "failed_checks": failed, "decision": decision, "needs_retraining": None,
            "scope": "offline reviewed events; does not establish field or notification delivery reliability"}
