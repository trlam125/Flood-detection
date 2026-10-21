from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .model import Segmenter
from .severity import SeverityThresholds, classify_ratio


@dataclass(frozen=True)
class FrameMetrics:
    raw_water_ratio: float
    water_ratio: float
    severity: str
    confidence: float
    level: str | None = None
    level_confidence: float = 0.0
    level_probs: tuple[float, ...] | None = None


def analyze_frame(
    segmenter: Segmenter,
    frame_bgr: np.ndarray,
    thresholds: SeverityThresholds,
    previous_ratio: float | None,
    ema_alpha: float,
    level_clf=None,
    previous_level_probs=None,
    level_alpha: float = 0.4,
) -> tuple[np.ndarray, FrameMetrics]:
    mask, probability = segmenter.predict_mask(frame_bgr)
    raw_ratio = float(mask.mean()) if mask.size else 0.0
    if previous_ratio is None:
        smooth_ratio = raw_ratio
    else:
        smooth_ratio = float(ema_alpha * raw_ratio + (1.0 - ema_alpha) * previous_ratio)
    severity = classify_ratio(smooth_ratio, thresholds)
    if mask.any():
        confidence = float(np.mean(probability[mask > 0]))
    else:
        confidence = float(1.0 - np.mean(probability)) if probability.size else 0.0
    confidence = max(0.0, min(1.0, confidence))

    level, level_conf, level_probs = None, 0.0, None
    if level_clf is not None:
        probs = level_clf.predict(frame_bgr)
        if probs is None:  # không thấy xe đủ lớn
            level, level_probs = "NO_CAR", previous_level_probs
        else:
            if previous_level_probs is not None:
                probs = level_alpha * probs + (1.0 - level_alpha) * np.asarray(previous_level_probs)
            level, level_conf = level_clf.decide(probs)
            level_probs = tuple(float(p) for p in probs)

    return mask, FrameMetrics(
        raw_water_ratio=raw_ratio,
        water_ratio=smooth_ratio,
        severity=severity,
        confidence=confidence,
        level=level,
        level_confidence=level_conf,
        level_probs=level_probs,
    )


def annotate_frame(frame: np.ndarray, mask: np.ndarray, metrics: FrameMetrics) -> np.ndarray:
    out = frame.copy()
    if mask is not None and mask.any():
        overlay = out.copy()
        overlay[mask > 0] = (245, 170, 70)
        out = cv2.addWeighted(out, 0.70, overlay, 0.30, 0)
        contours, _ = cv2.findContours((mask * 255).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, contours, -1, (255, 220, 120), 2)

    severity_colors = {
        "SAFE": (88, 190, 110),
        "LOW": (70, 190, 230),
        "MODERATE": (45, 150, 245),
        "CRITICAL": (65, 65, 245),
    }
    color = severity_colors.get(metrics.severity, (230, 230, 230))
    box_height = 138 if metrics.level else 112
    cv2.rectangle(out, (18, 18), (390, box_height), (12, 18, 24), -1)
    cv2.rectangle(out, (18, 18), (390, box_height), color, 2)
    cv2.putText(out, f"STATUS: {metrics.severity}", (34, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.78, color, 2, cv2.LINE_AA)
    cv2.putText(out, f"Flooded area: {metrics.water_ratio * 100:.1f}%", (34, 82), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (240, 244, 248), 2, cv2.LINE_AA)
    cv2.putText(out, f"Segmentation confidence: {metrics.confidence * 100:.0f}%", (34, 105), cv2.FONT_HERSHEY_SIMPLEX, 0.47, (190, 202, 214), 1, cv2.LINE_AA)
    if metrics.level:
        level_text = f"Level: {metrics.level}"
        if metrics.level_confidence > 0:
            level_text += f" ({metrics.level_confidence * 100:.0f}%)"
        cv2.putText(out, level_text, (34, 128), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 215, 0), 1, cv2.LINE_AA)
    return out