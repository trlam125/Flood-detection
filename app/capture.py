"""Isolated OpenCV capture with one bounded shared JPEG slot, no IPC queue."""
from __future__ import annotations

import ctypes
import multiprocessing as mp
import os
import time
from dataclasses import dataclass

MAX_CAPTURE_BYTES = 3 * 1024 * 1024


@dataclass(frozen=True)
class CameraFrame:
    sequence: int
    received_mono: float
    jpeg: bytes
    source_width: int
    source_height: int


@dataclass(frozen=True)
class CapturePoll:
    frame: CameraFrame | None = None
    failed: bool = False
    alive: bool = True


def _capture_worker(source, kind, open_timeout, read_timeout, buffer, lock, length, sequence,
                    received, width, height, state, stop):
    # Native decoder errors can contain an RTSP URL. Never send them to server logs.
    os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
    os.environ["OPENCV_FFMPEG_DEBUG"] = "0"
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 2)
    import cv2
    import numpy as np
    cap = None
    try:
        if kind == "rtsp":
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, round(open_timeout * 1000),
                 cv2.CAP_PROP_READ_TIMEOUT_MSEC, round(read_timeout * 1000)])
        elif kind == "webcam":
            cap = cv2.VideoCapture(source, cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY)
        else:
            # Internal recorded-source test path; public source schemas forbid file URLs.
            cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            return
        last_encoded = -float("inf")
        while not stop.value:
            if kind == "test_file":
                time.sleep(1 / max(1, cap.get(cv2.CAP_PROP_FPS)))
            ok, frame = cap.read()
            now = time.monotonic()
            if not ok or frame is None:
                break
            if now - last_encoded < .1:
                continue
            source_h, source_w = frame.shape[:2]
            scale = min(1, 1280 / max(source_w, source_h))
            if scale < 1:
                frame = cv2.resize(frame, (max(2, round(source_w * scale)), max(2, round(source_h * scale))))
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
            if not ok or encoded.size > MAX_CAPTURE_BYTES:
                continue
            if lock.acquire(timeout=.02):
                try:
                    np.frombuffer(buffer, np.uint8)[:encoded.size] = encoded.reshape(-1)
                    length.value, received.value = encoded.size, now
                    width.value, height.value = source_w, source_h
                    sequence.value += 1
                    state.value = 1
                finally:
                    lock.release()
            last_encoded = now
    except Exception:
        pass
    finally:
        if cap is not None:
            cap.release()
        state.value = 2


class ProcessCapture:
    def __init__(self, source: str | int, kind: str, open_timeout: float, read_timeout: float):
        context = mp.get_context("spawn")
        self.buffer = context.RawArray(ctypes.c_ubyte, MAX_CAPTURE_BYTES)
        self.lock = context.Lock()
        self.length = context.RawValue(ctypes.c_int, 0)
        self.sequence = context.RawValue(ctypes.c_longlong, 0)
        self.received = context.RawValue(ctypes.c_double, 0)
        self.width, self.height = context.RawValue(ctypes.c_int, 0), context.RawValue(ctypes.c_int, 0)
        self.state, self.stop_flag = context.RawValue(ctypes.c_int, 0), context.RawValue(ctypes.c_byte, 0)
        self.last_sequence = 0
        self.process = context.Process(target=_capture_worker,
            args=(source, kind, open_timeout, read_timeout, self.buffer, self.lock, self.length, self.sequence,
                  self.received, self.width, self.height, self.state, self.stop_flag), daemon=True)
        self.process.start()

    def poll(self) -> CapturePoll:
        alive = self.process.is_alive()
        if not self.lock.acquire(timeout=.01):
            return CapturePoll(alive=alive)
        try:
            if self.state.value == 2 or not alive:
                return CapturePoll(failed=True, alive=alive)
            if self.sequence.value == self.last_sequence or not self.length.value:
                return CapturePoll(alive=alive)
            self.last_sequence = self.sequence.value
            frame = CameraFrame(self.last_sequence, self.received.value, ctypes.string_at(ctypes.addressof(self.buffer), self.length.value),
                                self.width.value, self.height.value)
            return CapturePoll(frame, alive=alive)
        finally:
            self.lock.release()

    def close(self):
        self.stop_flag.value = 1
        self.process.join(timeout=.25)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=1)
        if not self.process.is_alive():
            self.process.close()
        # All IPC objects belong to this generation. Never reuse them after a kill.
