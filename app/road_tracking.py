"""Keep a road footprint across frames when rising water hides road pixels."""
from __future__ import annotations

import cv2
import numpy as np


class RoadFootprintTracker:
    """Warp and accumulate road detections for a single video/camera session.

    Optical flow compensates for small camera motion. A poor alignment resets the
    footprint instead of carrying a stale road region into a different view.
    """

    def __init__(self, max_side: int = 480):
        self.max_side = max_side
        self._gray: np.ndarray | None = None
        self._road: np.ndarray | None = None

    def reset(self) -> None:
        self._gray = None
        self._road = None

    def update(self, frame_bgr: np.ndarray, road_mask: np.ndarray) -> np.ndarray:
        road = np.asarray(road_mask, dtype=bool)
        if road.shape != frame_bgr.shape[:2]:
            raise ValueError("Road mask does not match frame")
        h, w = road.shape
        scale = min(1.0, self.max_side / max(h, w))
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        gray = cv2.resize(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY), size)
        current = cv2.resize(road.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST) > 0
        if self._road is None or self._gray is None:
            accumulated = current.copy()
        else:
            points = cv2.goodFeaturesToTrack(self._gray, maxCorners=500, qualityLevel=.01,
                                              minDistance=5, blockSize=5)
            aligned = None
            if points is not None and len(points) >= 8:
                moved, status, _ = cv2.calcOpticalFlowPyrLK(self._gray, gray, points, None)
                good = status.reshape(-1) == 1
                if np.count_nonzero(good) >= 8:
                    transform, inliers = cv2.estimateAffinePartial2D(points[good], moved[good],
                        method=cv2.RANSAC, ransacReprojThreshold=3.0)
                    if transform is not None and inliers is not None and np.mean(inliers) >= .35:
                        warped = cv2.warpAffine(self._road.astype(np.uint8), transform,
                            size, flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT)
                        aligned = warped > 0
            # Static cameras often lack enough texture; keep the exact mask then.
            if aligned is None:
                mean_shift = float(np.mean(cv2.absdiff(self._gray, gray)))
                old_area, new_area = int(self._road.sum()), int(current.sum())
                intersection = int(np.count_nonzero(self._road & current))
                union = old_area + new_area - intersection
                overlap = intersection / max(1, union)
                # An empty current road mask commonly means full inundation.
                # Reset only when a visible, substantially shifted road is found.
                reset = new_area > 0 and mean_shift >= 8.0 and overlap < .35
                aligned = current.copy() if reset else self._road
            accumulated = aligned | current
        self._gray = gray
        self._road = accumulated
        return cv2.resize(accumulated.astype(np.uint8), (w, h),
                          interpolation=cv2.INTER_NEAREST).astype(bool)
