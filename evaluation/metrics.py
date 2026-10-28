"""Water-class metrics with explicit handling of absent/ignored water pixels."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


@dataclass(frozen=True)
class PixelCounts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def __add__(self, other: PixelCounts) -> PixelCounts:
        return PixelCounts(*(a + b for a, b in zip(asdict(self).values(), asdict(other).values())))

    def scores(self) -> dict[str, float | int | None]:
        def ratio(numerator: int, denominator: int) -> float | None:
            return numerator / denominator if denominator else None

        return {
            **asdict(self),
            "iou": ratio(self.tp, self.tp + self.fp + self.fn),
            "dice": ratio(2 * self.tp, 2 * self.tp + self.fp + self.fn),
            "precision": ratio(self.tp, self.tp + self.fp),
            "recall": ratio(self.tp, self.tp + self.fn),
            "false_positive_rate": ratio(self.fp, self.fp + self.tn),
            "water_ratio_predicted": ratio(self.tp + self.fp, self.tp + self.fp + self.fn + self.tn),
            "water_ratio_reference": ratio(self.tp + self.fn, self.tp + self.fp + self.fn + self.tn),
        }


def count_pixels(predicted: np.ndarray, reference: np.ndarray, valid: np.ndarray | None = None) -> PixelCounts:
    if predicted.ndim != 2 or predicted.shape != reference.shape:
        raise ValueError("Prediction and reference must be 2D masks with identical dimensions")
    if valid is None:
        valid = np.ones(reference.shape, dtype=bool)
    if valid.shape != reference.shape:
        raise ValueError("Valid-pixel mask has the wrong dimensions")
    valid = valid.astype(bool)
    if not valid.any():
        raise ValueError("Reference has no valid pixels after ignore values are removed")
    p, r = predicted.astype(bool), reference.astype(bool)
    return PixelCounts(
        tp=int(np.count_nonzero(p & r & valid)),
        fp=int(np.count_nonzero(p & ~r & valid)),
        fn=int(np.count_nonzero(~p & r & valid)),
        tn=int(np.count_nonzero(~p & ~r & valid)),
    )
