from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Calibration:
    profile: str
    zero_y: float
    known_y: float
    known_cm: float
    zero_x: float | None = None
    known_x: float | None = None
    frame_width: float | None = None

    @property
    def cm_per_pixel(self) -> float:
        delta_px = abs(self.zero_y - self.known_y)
        if delta_px <= 1e-6:
            raise ValueError("Calibration points must be at different vertical positions.")
        return float(self.known_cm) / delta_px

    @property
    def measurement_x(self) -> float | None:
        xs = [x for x in (self.zero_x, self.known_x) if x is not None]
        if not xs:
            return None
        return float(sum(xs) / len(xs))

    def roi_bounds(self, image_width: int, width_ratio: float = 0.16) -> tuple[int, int]:
        """Return the fixed-camera measurement strip around the calibration marker."""
        if image_width <= 0:
            return 0, 0
        center = self.measurement_x
        if center is None:
            center = image_width / 2.0
        # Scale saved x coordinate if the uploaded video resolution differs but aspect is preserved.
        if self.frame_width and self.frame_width > 1 and abs(self.frame_width - image_width) > 1:
            center = center * image_width / self.frame_width
        half = max(18, int(round(image_width * width_ratio / 2.0)))
        x1 = max(0, int(round(center)) - half)
        x2 = min(image_width, int(round(center)) + half + 1)
        if x2 - x1 < 8:
            return 0, image_width
        return x1, x2

    def water_level_cm(self, waterline_y: float | None) -> float | None:
        if waterline_y is None:
            return None
        # Positive level means waterline is above the 0-cm baseline in the image.
        level = (self.zero_y - float(waterline_y)) * self.cm_per_pixel
        return max(0.0, level)


class CalibrationStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, profile: str) -> Path:
        safe = "".join(ch for ch in profile if ch.isalnum() or ch in ("-", "_")) or "default"
        return self.root / f"{safe}.json"

    def save(self, calibration: Calibration) -> None:
        payload = asdict(calibration)
        payload["cm_per_pixel"] = calibration.cm_per_pixel
        payload["measurement_x"] = calibration.measurement_x
        self._path(calibration.profile).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load(self, profile: str = "default") -> Calibration | None:
        path = self._path(profile)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Calibration(
            profile=data.get("profile", profile),
            zero_y=float(data["zero_y"]),
            known_y=float(data["known_y"]),
            known_cm=float(data["known_cm"]),
            zero_x=None if data.get("zero_x") is None else float(data["zero_x"]),
            known_x=None if data.get("known_x") is None else float(data["known_x"]),
            frame_width=None if data.get("frame_width") is None else float(data["frame_width"]),
        )
