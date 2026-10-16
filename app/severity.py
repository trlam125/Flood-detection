from __future__ import annotations

from dataclasses import dataclass

SEVERITY_ORDER = {"SAFE": 0, "LOW": 1, "MODERATE": 2, "CRITICAL": 3}


@dataclass(frozen=True)
class SeverityThresholds:
    low: float = 0.10
    moderate: float = 0.20
    critical: float = 0.45

    def validate(self) -> None:
        if not (0.0 <= self.low < self.moderate < self.critical <= 1.0):
            raise ValueError("Severity thresholds must satisfy 0 <= low < moderate < critical <= 1")


def classify_ratio(water_ratio: float, thresholds: SeverityThresholds) -> str:
    thresholds.validate()
    ratio = max(0.0, min(1.0, float(water_ratio)))
    if ratio >= thresholds.critical:
        return "CRITICAL"
    if ratio >= thresholds.moderate:
        return "MODERATE"
    if ratio >= thresholds.low:
        return "LOW"
    return "SAFE"


def max_severity(a: str, b: str) -> str:
    return a if SEVERITY_ORDER.get(a, -1) >= SEVERITY_ORDER.get(b, -1) else b


def at_least(value: str, minimum: str) -> bool:
    return SEVERITY_ORDER.get(value, -1) >= SEVERITY_ORDER.get(minimum, 99)
