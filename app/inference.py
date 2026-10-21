from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .model import Segmenter
from .severity import SeverityThresholds, classify_ratio
from .level_classifier import get_level_info


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
    h, w = frame.shape[:2]

    # Water segmentation overlay & contours
    if mask is not None and mask.any():
        overlay = out.copy()
        overlay[mask > 0] = (245, 170, 70)  # Cyan/Blue tint for water
        out = cv2.addWeighted(out, 0.70, overlay, 0.30, 0)
        contours, _ = cv2.findContours((mask * 255).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, contours, -1, (255, 220, 120), 2)

    # Color definitions (BGR for OpenCV)
    severity_colors = {
        "SAFE": (88, 190, 110),       # Green
        "LOW": (70, 190, 230),        # Yellow
        "MODERATE": (45, 150, 245),   # Orange
        "CRITICAL": (65, 65, 245),    # Red
    }
    level_palette = {
        "Level0": (85, 201, 120),     # #55c978
        "Level1": (79, 196, 226),     # #e2c44f
        "Level2": (63, 150, 238),     # #ee963f
        "Level3": (87, 87, 239),      # #ef5757
        "Level4": (37, 37, 217),      # #d92525
        "NO_CAR": (182, 162, 142),
        "UNCERTAIN": (182, 162, 142),
    }

    info = get_level_info(metrics.level)
    level_color = level_palette.get(metrics.level or "NO_CAR", (182, 162, 142))
    status_color = severity_colors.get(metrics.severity, (230, 230, 230))
    border_color = level_color if metrics.level and metrics.level != "NO_CAR" else status_color

    # Scaling factor for different video dimensions (e.g. 720p, 1080p)
    scale = max(0.60, min(1.30, w / 1280.0))
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_bold = cv2.LINE_AA

    # HUD Box dimensions
    hud_x = int(16 * scale)
    hud_y = int(16 * scale)
    hud_w = int(540 * scale)
    has_level_data = bool(metrics.level and metrics.level != "NO_CAR")
    hud_h = int((210 if has_level_data else 150) * scale)

    # Semi-transparent dark background for HUD
    hud_bg = out.copy()
    cv2.rectangle(hud_bg, (hud_x, hud_y), (hud_x + hud_w, hud_y + hud_h), (10, 16, 22), -1)
    out = cv2.addWeighted(out, 0.22, hud_bg, 0.78, 0)
    # Border with accent color
    cv2.rectangle(out, (hud_x, hud_y), (hud_x + hud_w, hud_y + hud_h), border_color, max(1, int(2 * scale)))

    # Title line
    y_cursor = hud_y + int(24 * scale)
    cv2.putText(
        out,
        "RIWA FLOOD MONITOR | best_flood_level_model",
        (hud_x + int(14 * scale), y_cursor),
        font,
        0.44 * scale,
        (160, 185, 210),
        1,
        font_bold,
        )

    # Status and Area ratio
    y_cursor += int(28 * scale)
    status_str = f"STATUS: {metrics.severity}"
    area_str = f"Area: {metrics.water_ratio * 100:.1f}%"
    cv2.putText(
        out,
        status_str,
        (hud_x + int(14 * scale), y_cursor),
        font,
        0.72 * scale,
        status_color,
        max(1, int(2 * scale)),
        font_bold,
        )
    cv2.putText(
        out,
        area_str,
        (hud_x + int(240 * scale), y_cursor),
        font,
        0.66 * scale,
        (240, 244, 248),
        max(1, int(2 * scale)),
        font_bold,
        )

    # Level info from best_flood_level_model
    y_cursor += int(30 * scale)
    lvl_ascii = info.get("ascii_title") or info.get("short_title") or (metrics.level or "NO_CAR")
    lvl_depth = info.get("ascii_depth") or info.get("depth", "--")
    conf_str = f" ({metrics.level_confidence * 100:.0f}%)" if metrics.level_confidence > 0 else ""
    level_line = f"MUC DO: {lvl_ascii}{conf_str}"
    cv2.putText(
        out,
        level_line,
        (hud_x + int(14 * scale), y_cursor),
        font,
        0.58 * scale,
        level_color,
        max(1, int(2 * scale)),
        font_bold,
        )

    # 5-Segment Level Indicator Gauge [ L0 | L1 | L2 | L3 | L4 ]
    y_cursor += int(14 * scale)
    gauge_h = int(18 * scale)
    gauge_w = int((hud_w - 28 * scale) / 5)
    levels_list = ["Level0", "Level1", "Level2", "Level3", "Level4"]
    active_idx = levels_list.index(metrics.level) if metrics.level in levels_list else -1

    for i, lvl_name in enumerate(levels_list):
        bx1 = hud_x + int(14 * scale) + i * gauge_w
        by1 = y_cursor
        bx2 = bx1 + gauge_w - int(4 * scale)
        by2 = by1 + gauge_h
        seg_color = level_palette[lvl_name]
        is_active = (i == active_idx)

        if is_active:
            # Active level: solid fill with bright color
            cv2.rectangle(out, (bx1, by1), (bx2, by2), seg_color, -1)
            text_color = (15, 15, 15) if i < 3 else (255, 255, 255)
            cv2.putText(
                out,
                f"L{i}",
                (bx1 + int(gauge_w * 0.30), by1 + int(gauge_h * 0.72)),
                font,
                0.44 * scale,
                text_color,
                2,
                font_bold,
                )
        else:
            # Inactive levels: dark background with muted border
            cv2.rectangle(out, (bx1, by1), (bx2, by2), (18, 26, 34), -1)
            cv2.rectangle(out, (bx1, by1), (bx2, by2), (50, 70, 90), 1)
            cv2.putText(
                out,
                f"L{i}",
                (bx1 + int(gauge_w * 0.30), by1 + int(gauge_h * 0.72)),
                font,
                0.38 * scale,
                (110, 130, 150),
                1,
                font_bold,
                )

    # Depth & Advisory / Warning details
    y_cursor += gauge_h + int(20 * scale)
    cv2.putText(
        out,
        f"Do sau uoc tinh: {lvl_depth}  |  Seg Conf: {metrics.confidence * 100:.0f}%",
        (hud_x + int(14 * scale), y_cursor),
        font,
        0.44 * scale,
        (205, 220, 235),
        1,
        font_bold,
        )

    if has_level_data:
        advisory_ascii = info.get("ascii_advisory") or info.get("advisory", "")
        if advisory_ascii:
            y_cursor += int(18 * scale)
            if len(advisory_ascii) > 48:
                advisory_ascii = advisory_ascii[:46] + "..."
            cv2.putText(
                out,
                f"* {advisory_ascii}",
                (hud_x + int(14 * scale), y_cursor),
                font,
                0.40 * scale,
                border_color,
                1,
                font_bold,
                )

    return out