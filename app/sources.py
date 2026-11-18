from __future__ import annotations

import os
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CameraSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    kind: Literal["rtsp", "webcam"] = "rtsp"
    rtsp_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    device_index: int | None = Field(default=None, ge=0, le=16)
    analysis_interval_sec: float = Field(default=1, ge=.1, le=30)
    stale_after_sec: float = Field(default=5, ge=.5, le=120)
    open_timeout_sec: float = Field(default=10, ge=1, le=60)
    read_timeout_sec: float = Field(default=5, ge=1, le=60)
    auto_start: bool = False
    external_alerts_enabled: bool = False

    @model_validator(mode="after")
    def source_fields(self):
        if self.kind == "rtsp" and (not self.rtsp_env or self.device_index is not None):
            raise ValueError("RTSP requires an environment variable name and no device index")
        if self.kind == "webcam" and (self.device_index is None or self.rtsp_env is not None):
            raise ValueError("Webcam requires a device index and no RTSP environment variable")
        if self.analysis_interval_sec >= self.stale_after_sec:
            raise ValueError("Analysis interval must be shorter than the stale-frame limit")
        return self


class CameraSourceUpdate(CameraSource):
    expected_revision: int = Field(default=0, ge=0)


def resolve_source(configuration: CameraSource) -> str | int:
    if configuration.kind == "webcam":
        return configuration.device_index
    value = os.getenv(configuration.rtsp_env, "").strip()
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"rtsp", "rtsps"} and parsed.hostname and parsed.port != 0
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("The configured environment variable is missing or does not contain a valid RTSP URL")
    return value
