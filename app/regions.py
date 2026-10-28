"""Normalized, simple polygon regions for a fixed camera view."""
from __future__ import annotations

import math
from typing import Sequence

import cv2
import numpy as np

Point = tuple[float, float]


def validate_polygon(points: Sequence[Sequence[float]]) -> tuple[Point, ...]:
    if not 3 <= len(points) <= 32:
        raise ValueError("ROI needs 3 to 32 vertices")
    result = []
    for point in points:
        if len(point) != 2:
            raise ValueError("Each ROI vertex needs x and y")
        x, y = float(point[0]), float(point[1])
        if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("ROI coordinates must be finite values between 0 and 1")
        result.append((x, y))
    if len(set(result)) != len(result):
        raise ValueError("ROI vertices must be distinct; do not repeat the first vertex")

    def cross(a: Point, b: Point, c: Point) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a: Point, b: Point, p: Point) -> bool:
        return min(a[0], b[0]) - 1e-10 <= p[0] <= max(a[0], b[0]) + 1e-10 and min(a[1], b[1]) - 1e-10 <= p[1] <= max(a[1], b[1]) + 1e-10

    def intersects(a: Point, b: Point, c: Point, d: Point) -> bool:
        ab_c, ab_d, cd_a, cd_b = cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)
        if ab_c * ab_d < 0 and cd_a * cd_b < 0:
            return True
        return any(abs(value) <= 1e-10 and on_segment(start, end, p) for value, start, end, p in (
            (ab_c, a, b, c), (ab_d, a, b, d), (cd_a, c, d, a), (cd_b, c, d, b)))

    edges = list(zip(result, result[1:] + result[:1]))
    for i, (a, b) in enumerate(edges):
        for j in range(i + 1, len(edges)):
            if j == i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            if intersects(a, b, *edges[j]):
                raise ValueError("ROI edges must not cross or touch other non-adjacent edges")
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)) / 2
    if area < 1e-8:
        raise ValueError("ROI polygon has no usable area")
    return tuple(result)


def roi_pixels(
    points: Sequence[Sequence[float]], width: int, height: int,
    reference_width: int, reference_height: int,
) -> np.ndarray:
    polygon = validate_polygon(points)
    if min(width, height, reference_width, reference_height) < 2:
        raise ValueError("ROI image dimensions must be at least 2 pixels")
    if abs((width / height) / (reference_width / reference_height) - 1) > 0.01:
        raise ValueError("Video aspect ratio differs from the ROI reference; choose a new reference and redraw the ROI")
    pixels = np.rint(np.asarray(polygon) * [width - 1, height - 1]).astype(np.int32)
    if cv2.contourArea(pixels) < 1:
        raise ValueError("ROI is too small for this video resolution")
    return pixels


def rasterize_roi(
    points: Sequence[Sequence[float]], width: int, height: int,
    reference_width: int, reference_height: int,
) -> np.ndarray:
    pixels = roi_pixels(points, width, height, reference_width, reference_height)
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.fillPoly(mask, [pixels], 1)
    return mask.astype(bool)
