from __future__ import annotations

from dataclasses import dataclass, replace

import cv2
import numpy as np

from .calibration import Calibration
from .model import RIWASegmenter


@dataclass
class FrameMetrics:
    water_ratio: float
    waterline_y: float | None
    water_level_cm: float | None
    waterline_confidence: float
    status: str


def _clean_mask(mask: np.ndarray) -> np.ndarray:
    binary = (mask > 0).astype(np.uint8)
    h, w = binary.shape[:2]
    k = max(3, int(round(min(h, w) * 0.008)))
    if k % 2 == 0:
        k += 1
    k = min(k, 9)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    cleaned = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, kernel, iterations=1)
    return cleaned


def _select_water_component(mask: np.ndarray, x1: int, x2: int) -> np.ndarray:
    """Prefer a large component visible in the fixed measurement strip and near the lower image."""
    h, w = mask.shape[:2]
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return mask

    best_label = None
    best_score = -1.0
    bottom_start = int(h * 0.72)
    roi_area = max(1, h * max(1, x2 - x1))

    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < max(30, int(0.0008 * h * w)):
            continue
        component = labels == label
        roi_pixels = int(component[:, x1:x2].sum())
        if roi_pixels == 0:
            continue
        bottom_pixels = int(component[bottom_start:, x1:x2].sum())
        roi_fraction = roi_pixels / roi_area
        # A flood/river surface usually occupies a coherent region and often reaches low in frame.
        score = roi_pixels + 2.5 * bottom_pixels + 0.25 * area + 5000.0 * roi_fraction
        if score > best_score:
            best_score = score
            best_label = label

    if best_label is None:
        return mask
    return (labels == best_label).astype(np.uint8)


def estimate_waterline(
    mask: np.ndarray,
    calibration: Calibration | None = None,
) -> tuple[float | None, float, tuple[int, int]]:
    """Automatically estimate water surface position and confidence.

    For a calibrated fixed camera the detector focuses on a vertical measurement strip
    around the calibration marker. This is more stable than using the entire frame.
    """
    h, w = mask.shape[:2]
    cleaned = _clean_mask(mask)
    if calibration is not None:
        x1, x2 = calibration.roi_bounds(w)
    else:
        x1, x2 = 0, w

    component = _select_water_component(cleaned, x1, x2)
    roi = component[:, x1:x2]
    roi_w = max(1, roi.shape[1])

    top_points: list[float] = []
    for x in range(roi_w):
        ys = np.flatnonzero(roi[:, x] > 0)
        if ys.size:
            top_points.append(float(ys[0]))

    min_columns = max(6, int(roi_w * 0.12))
    if len(top_points) < min_columns:
        return None, 0.0, (x1, x2)

    points = np.asarray(top_points, dtype=np.float32)
    low, high = np.percentile(points, [12, 88])
    core = points[(points >= low) & (points <= high)]
    if core.size < min_columns:
        core = points

    line_y = float(np.median(core))
    coverage = min(1.0, len(top_points) / roi_w)
    mad = float(np.median(np.abs(core - np.median(core)))) if core.size else float(h)
    stability = max(0.0, 1.0 - mad / max(8.0, h * 0.08))
    confidence = float(np.clip(0.65 * coverage + 0.35 * stability, 0.0, 1.0))
    return line_y, confidence, (x1, x2)


def classify_status(
    water_ratio: float,
    water_level_cm: float | None,
    warning_cm: float,
    danger_cm: float,
    warning_ratio: float,
    danger_ratio: float,
) -> str:
    if water_level_cm is not None:
        if water_level_cm >= danger_cm:
            return "DANGER"
        if water_level_cm >= warning_cm:
            return "WARNING"
        return "SAFE"

    if water_ratio >= danger_ratio:
        return "DANGER"
    if water_ratio >= warning_ratio:
        return "WARNING"
    return "SAFE"


def analyze_frame(
    segmenter: RIWASegmenter,
    frame_bgr: np.ndarray,
    calibration: Calibration | None,
    warning_cm: float,
    danger_cm: float,
    warning_ratio: float,
    danger_ratio: float,
) -> tuple[np.ndarray, FrameMetrics]:
    mask, _ = segmenter.predict_mask(frame_bgr)
    water_ratio = float(mask.mean())
    waterline_y, confidence, _ = estimate_waterline(mask, calibration)
    water_level_cm = calibration.water_level_cm(waterline_y) if calibration else None
    status = classify_status(
        water_ratio,
        water_level_cm,
        warning_cm,
        danger_cm,
        warning_ratio,
        danger_ratio,
    )
    return mask, FrameMetrics(water_ratio, waterline_y, water_level_cm, confidence, status)


def smooth_metrics(
    previous: FrameMetrics | None,
    current: FrameMetrics,
    calibration: Calibration | None,
    warning_cm: float,
    danger_cm: float,
    warning_ratio: float,
    danger_ratio: float,
    alpha: float = 0.35,
) -> FrameMetrics:
    """Temporal EMA smoothing for fixed-camera video to reduce frame-to-frame jitter."""
    if previous is None:
        return current

    ratio = alpha * current.water_ratio + (1.0 - alpha) * previous.water_ratio

    if current.waterline_y is None:
        waterline_y = previous.waterline_y
        confidence = previous.waterline_confidence * 0.9
    elif previous.waterline_y is None:
        waterline_y = current.waterline_y
        confidence = current.waterline_confidence
    else:
        # Trust high-confidence frames slightly more.
        a = min(0.65, max(0.18, alpha * (0.65 + current.waterline_confidence)))
        waterline_y = a * current.waterline_y + (1.0 - a) * previous.waterline_y
        confidence = alpha * current.waterline_confidence + (1.0 - alpha) * previous.waterline_confidence

    level = calibration.water_level_cm(waterline_y) if calibration else None
    status = classify_status(ratio, level, warning_cm, danger_cm, warning_ratio, danger_ratio)
    return replace(
        current,
        water_ratio=float(ratio),
        waterline_y=None if waterline_y is None else float(waterline_y),
        water_level_cm=level,
        waterline_confidence=float(np.clip(confidence, 0.0, 1.0)),
        status=status,
    )


def format_water_level(water_level_cm: float | None) -> str:
    if water_level_cm is None:
        return ""
    if water_level_cm >= 100.0:
        return f"{water_level_cm / 100.0:.2f} m"
    return f"{water_level_cm:.1f} cm"


def annotate_frame(
    frame: np.ndarray,
    mask: np.ndarray,
    metrics: FrameMetrics,
    calibration: Calibration | None = None,
) -> np.ndarray:
    out = frame.copy()

    overlay = out.copy()
    overlay[mask > 0] = (255, 120, 0)
    out = cv2.addWeighted(out, 0.74, overlay, 0.26, 0)

    if calibration is not None:
        x1, x2 = calibration.roi_bounds(out.shape[1])
        roi_overlay = out.copy()
        cv2.rectangle(roi_overlay, (x1, 0), (max(x1, x2 - 1), out.shape[0] - 1), (80, 80, 80), -1)
        out = cv2.addWeighted(out, 0.92, roi_overlay, 0.08, 0)
        center_x = (x1 + x2) // 2
        cv2.line(out, (center_x, 0), (center_x, out.shape[0] - 1), (190, 190, 190), 1, cv2.LINE_AA)

    if metrics.waterline_y is not None:
        y = int(round(metrics.waterline_y))
        cv2.line(out, (0, y), (out.shape[1] - 1, y), (0, 230, 255), 2)
        cv2.putText(
            out,
            "AUTO WATERLINE",
            (max(8, out.shape[1] - 185), max(20, y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (0, 230, 255),
            1,
            cv2.LINE_AA,
        )

    badge_w = min(355, max(255, int(out.shape[1] * 0.26)))
    badge_h = 108 if calibration is not None else 88
    x1, y1 = 14, 14
    x2 = min(out.shape[1] - 1, x1 + badge_w)
    y2 = min(out.shape[0] - 1, y1 + badge_h)

    panel = out.copy()
    cv2.rectangle(panel, (x1, y1), (x2, y2), (16, 20, 25), -1)
    out = cv2.addWeighted(out, 0.52, panel, 0.48, 0)

    status_color = {
        "SAFE": (80, 220, 80),
        "WARNING": (0, 190, 255),
        "DANGER": (40, 40, 240),
    }[metrics.status]

    cv2.putText(out, metrics.status, (x1 + 10, y1 + 23), cv2.FONT_HERSHEY_SIMPLEX, 0.60, status_color, 2, cv2.LINE_AA)

    if metrics.water_level_cm is not None:
        lines = [
            f"Water level: {format_water_level(metrics.water_level_cm)}",
            f"Water area: {metrics.water_ratio * 100:.1f}%",
            f"Level confidence: {metrics.waterline_confidence * 100:.0f}%",
        ]
    else:
        lines = [
            f"Water area: {metrics.water_ratio * 100:.1f}%",
            "Area-based alert",
        ]

    for i, text in enumerate(lines):
        cv2.putText(
            out,
            text,
            (x1 + 10, y1 + 50 + i * 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.43 if i else 0.49,
            (245, 245, 245) if i == 0 else (205, 215, 225),
            1,
            cv2.LINE_AA,
        )

    return out
