from __future__ import annotations

from dataclasses import dataclass
import math

import cv2
import numpy as np

from .model import Segmenter
from .severity import SeverityThresholds, classify_ratio


@dataclass(frozen=True)
class FrameMetrics:
    raw_water_ratio: float | None
    water_ratio: float | None
    severity: str
    confidence: float
    full_frame_water_ratio: float = 0.0
    ratio_scope: str = "road"
    monitored_pixels: int = 0


def analyze_frame(
    segmenter: Segmenter,
    frame_bgr: np.ndarray,
    thresholds: SeverityThresholds,
    previous_ratio: float | None,
    roi_mask: np.ndarray | None = None,
    elapsed_sec: float | None = None,
    ema_time_constant_sec: float = 1.0,
    road_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, FrameMetrics]:
    mask, probability = segmenter.predict_mask(frame_bgr)
    if mask.shape != frame_bgr.shape[:2] or probability.shape != mask.shape:
        raise ValueError("Segmentation output does not match the video frame")
    if not np.isfinite(probability).all():
        raise ValueError("Segmentation probabilities contain invalid values")
    full_ratio = float(np.mean(mask > 0))
    if road_mask is None:
        road_predictor = getattr(segmenter, "predict_road_mask", None)
        if road_predictor is None:
            raise RuntimeError("The active model does not provide a road mask")
        road_mask = road_predictor(frame_bgr)
    road_mask = np.asarray(road_mask, dtype=bool)
    if road_mask.shape != mask.shape:
        raise ValueError("Road mask does not match the video frame")
    valid = road_mask.copy()
    if roi_mask is not None:
        if roi_mask.shape != mask.shape:
            raise ValueError("ROI mask must match the frame")
        valid &= roi_mask.astype(bool)
    monitored_pixels = int(np.count_nonzero(valid))
    if monitored_pixels == 0:
        empty = np.zeros_like(mask, dtype=np.uint8)
        metrics = FrameMetrics(None, None, "UNKNOWN", 0.0, full_ratio, "road", 0)
        return empty, metrics
    mask = ((mask > 0) & valid).astype(np.uint8)
    raw_ratio = float(np.count_nonzero(mask) / monitored_pixels)
    if previous_ratio is None:
        smooth_ratio = raw_ratio
    else:
        if elapsed_sec is None or not math.isfinite(elapsed_sec) or elapsed_sec <= 0 or not math.isfinite(ema_time_constant_sec) or ema_time_constant_sec <= 0:
            raise ValueError("Time-aware EMA requires positive finite elapsed time and time constant")
        alpha = -math.expm1(-elapsed_sec / ema_time_constant_sec)
        smooth_ratio = float(alpha * raw_ratio + (1.0 - alpha) * previous_ratio)
    severity = classify_ratio(smooth_ratio, thresholds)
    if mask.any():
        confidence = float(np.mean(probability[mask > 0]))
    else:
        confidence = float(1.0 - np.mean(probability[valid]))
    confidence = max(0.0, min(1.0, confidence))
    return mask, FrameMetrics(raw_ratio, smooth_ratio, severity, confidence, full_ratio,
                              "road", monitored_pixels)


def segmentation_overlay(mask: np.ndarray, roi_mask: np.ndarray | None = None,
                         road_mask: np.ndarray | None = None) -> np.ndarray:
    """Small transparent BGRA layer for independent, time-aligned video playback."""
    height, width = mask.shape
    scale = min(1.0, 640 / max(height, width))
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    water = cv2.resize((mask > 0).astype(np.uint8), size, interpolation=cv2.INTER_NEAREST)
    overlay = np.zeros((*water.shape, 4), dtype=np.uint8)
    if roi_mask is not None:
        roi = cv2.resize(roi_mask.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST)
        overlay[roi == 0] = (0, 0, 0, 153)
        water &= roi
    overlay[water > 0] = (245, 170, 70, 77)
    contours, _ = cv2.findContours(water, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(overlay, contours, -1, (255, 220, 120, 255), 1)
    if road_mask is not None:
        road = cv2.resize(road_mask.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST)
        road_contours, _ = cv2.findContours(road, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(overlay, road_contours, -1, (70, 235, 150, 255), 1)
    if roi_mask is not None:
        contours, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(overlay, contours, -1, (70, 235, 255, 255), 1)
    return overlay


def annotate_frame(frame: np.ndarray, mask: np.ndarray, roi_mask: np.ndarray | None = None,
                   road_mask: np.ndarray | None = None) -> np.ndarray:
    """Draw the segmentation and region; observation details are shown in the UI."""
    out = frame.copy()
    if roi_mask is not None:
        out[~roi_mask] = (out[~roi_mask] * 0.40).astype(np.uint8)
    if mask is not None and mask.any():
        overlay = out.copy()
        overlay[mask > 0] = (245, 170, 70)
        out = cv2.addWeighted(out, 0.70, overlay, 0.30, 0)
        contours, _ = cv2.findContours((mask * 255).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, contours, -1, (255, 220, 120), 2)

    if roi_mask is not None:
        contours, _ = cv2.findContours(roi_mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, contours, -1, (70, 235, 255), 2)
    if road_mask is not None:
        contours, _ = cv2.findContours(road_mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, contours, -1, (70, 235, 150), 2)

    return out
