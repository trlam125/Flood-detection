from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .regions import roi_pixels, validate_polygon
from .events import EventPolicy
from .severity import SeverityThresholds


class CameraThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    low: float = Field(default=0.10, ge=0, le=1)
    moderate: float = Field(default=0.20, ge=0, le=1)
    critical: float = Field(default=0.45, ge=0, le=1)

    @model_validator(mode="after")
    def ordered(self) -> CameraThresholds:
        SeverityThresholds(self.low, self.moderate, self.critical).validate()
        return self


class CameraConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=100)
    location: str = Field(default="", max_length=200)
    thresholds: CameraThresholds = Field(default_factory=CameraThresholds)
    sample_every_n_frames: int = Field(default=2, ge=1, le=30)
    roi: list[tuple[float, float]] | None = Field(default=None, min_length=3, max_length=32)
    reference_width: int | None = Field(default=None, ge=2, le=32768)
    reference_height: int | None = Field(default=None, ge=2, le=32768)
    event_policy: EventPolicy | None = None

    @field_validator("roi")
    @classmethod
    def simple_polygon(cls, value):
        return list(validate_polygon(value)) if value is not None else None

    @model_validator(mode="after")
    def reference_required(self) -> CameraConfiguration:
        if self.event_policy is not None:
            self.event_policy.validate_thresholds(SeverityThresholds(self.thresholds.low, self.thresholds.moderate, self.thresholds.critical))
        if self.roi is not None:
            if self.reference_width is None or self.reference_height is None:
                raise ValueError("ROI requires reference_width and reference_height")
            roi_pixels(self.roi, self.reference_width, self.reference_height, self.reference_width, self.reference_height)
        elif self.reference_width is not None or self.reference_height is not None:
            raise ValueError("Reference dimensions must be null when using the full frame")
        return self


class CameraUpdate(CameraConfiguration):
    expected_revision: int = Field(ge=1)
