"""Isolated native decoder for an on-demand, loopback-only browser file reader."""
from __future__ import annotations

import base64
import json
import math
import os
import sys


def main():
    # Keep native decoder diagnostics out of the JSON protocol and server logs.
    os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "format_whitelist;mov,matroska,webm,avi"
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 2)
    import cv2

    def emit(message):
        sys.stdout.write(json.dumps(message, allow_nan=False) + "\n")
        sys.stdout.flush()

    cap = None
    try:
        source, interval = sys.argv[1], float(sys.argv[2])
        if not source.startswith("http://127.0.0.1:") or not .01 <= interval <= 5:
            raise ValueError("Invalid local reader")
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 20000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 15000])
        if not cap.isOpened():
            raise ValueError("Cannot decode this video. Try another file or the upload compatibility option.")
        fps, frames = float(cap.get(cv2.CAP_PROP_FPS)), int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not math.isfinite(fps) or fps <= 0 or frames <= 0:
            raise ValueError("This video has no usable duration or frame rate")
        sequence = index = 0
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if index == 0:
                height, width = frame.shape[:2]
                emit({"type": "metadata", "width": width, "height": height, "duration_sec": frames / fps, "fps": fps, "frames": frames})
            while sequence * interval < min((index + 1) / fps, frames / fps) - 1e-8:
                at = sequence * interval
                height, width = frame.shape[:2]
                scale = min(1, 1280 / max(width, height))
                image = cv2.resize(frame, (max(2, round(width * scale)), max(2, round(height * scale)))) if scale < 1 else frame
                ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
                if not ok or len(jpeg) > 3 * 1024 * 1024:
                    raise ValueError("Cannot encode a video frame")
                emit({"type": "frame", "sequence": sequence, "time_sec": at,
                      "jpeg": base64.b64encode(jpeg).decode("ascii")})
                sequence += 1
            index += 1
        if index == 0 or index + 1 < frames:
            raise ValueError("The video ended before all expected frames could be read")
        emit({"type": "end"})
    except Exception as exc:
        emit({"type": "error", "message": str(exc) if isinstance(exc, ValueError) else "Video decoder failed"})
        return 1
    finally:
        if cap is not None:
            cap.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
