"""Read browser-selected files by bounded byte ranges, before full transfer."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .preview import PreviewOptions

CHUNK_BYTES = 128 * 1024
MAX_READERS = 2


class LocalReaderStart(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=1024 ** 4)
    interval_sec: float = Field(default=.5, ge=.01, le=5)
    export_video: bool = False
    smooth_playback: bool = False
    options: PreviewOptions = Field(default_factory=PreviewOptions)


class LocalReader:
    def __init__(self, metadata: LocalReaderStart, create_preview):
        self.id = uuid.uuid4().hex
        self.metadata, self.create_preview = metadata, create_preview
        self.created = time.monotonic()
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.pending = {}
        self.next_request = 0
        self.session = self.process = self.server = None
        self.playback_process = None
        self.info = None
        self.proxy_url = None
        self.thread = None
        self.bound = False
        self.bytes_read = 0
        self.frame_ack = threading.Event()
        self.ack_sequence = None

    def send(self, message):
        if self.stop_flag.is_set():
            raise RuntimeError("Local video reader was closed")
        async def deliver():
            async with self.send_lock:
                await self.websocket.send_json(message)
        coroutine = deliver()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except RuntimeError as exc:
            coroutine.close()
            raise RuntimeError("The browser file connection closed") from exc
        try:
            future.result(timeout=15)
        except Exception as exc:
            future.cancel()
            raise RuntimeError("The browser file connection closed") from exc

    def read_bytes(self, offset, length):
        with self.lock:
            if len(self.pending) >= 4 or self.stop_flag.is_set():
                raise RuntimeError("Local reader is unavailable")
            self.next_request += 1
            identifier = self.next_request
            request = {"event": threading.Event(), "length": length, "data": None}
            self.pending[identifier] = request
        try:
            self.send({"type": "read", "id": identifier, "offset": offset, "length": length})
            if not request["event"].wait(20) or request["data"] is None:
                raise RuntimeError("Timed out reading the selected file")
            return request["data"]
        finally:
            with self.lock:
                self.pending.pop(identifier, None)

    def accept_bytes(self, message):
        if not 4 <= len(message) <= CHUNK_BYTES + 4:
            raise ValueError("Invalid file chunk")
        identifier = struct.unpack("!I", message[:4])[0]
        with self.lock:
            request = self.pending.get(identifier)
            if request is None or request["data"] is not None or len(message) - 4 != request["length"]:
                raise ValueError("Unexpected file chunk")
            request["data"] = message[4:]
            self.bytes_read += len(message) - 4
            request["event"].set()

    def acknowledge(self, sequence):
        if sequence != self.ack_sequence:
            raise ValueError("Unexpected frame acknowledgement")
        self.frame_ack.set()

    def bind(self, websocket, loop):
        with self.lock:
            if self.bound or self.stop_flag.is_set():
                raise ValueError("This file reader is already connected or closed")
            self.bound = True
        self.websocket, self.loop, self.send_lock = websocket, loop, asyncio.Lock()
        self.thread = threading.Thread(target=self.run, name="browser-file-reader", daemon=True)
        self.thread.start()

    def _proxy(self):
        reader = self
        token = "/" + secrets.token_hex(24)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_HEAD(self):
                self.serve(False)

            def do_GET(self):
                self.serve(True)

            def serve(self, body):
                if self.path != token or reader.stop_flag.is_set():
                    self.send_error(404)
                    return
                total = reader.metadata.size_bytes
                start, end, partial = 0, total - 1, False
                value = self.headers.get("Range")
                if value:
                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", value)
                    if match is None:
                        self.send_error(416)
                        return
                    start, end = int(match[1]), min(total - 1, int(match[2]) if match[2] else total - 1)
                    partial = True
                if not 0 <= start <= end < total:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206 if partial else 200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(end - start + 1))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.end_headers()
                if not body:
                    return
                try:
                    while start <= end and not reader.stop_flag.is_set():
                        data = reader.read_bytes(start, min(CHUNK_BYTES, end - start + 1))
                        self.wfile.write(data)
                        self.wfile.flush()
                        start += len(data)
                except (OSError, RuntimeError, TimeoutError):
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .1}, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}{token}"

    def run(self):
        ended = False
        try:
            url = self._proxy()
            if self.stop_flag.is_set():
                return
            self.process = subprocess.Popen([sys.executable, "-m", "app.local_video_worker", url, str(self.metadata.interval_sec)],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            while not self.stop_flag.is_set():
                line = self.process.stdout.readline(5 * 1024 * 1024)
                if not line:
                    raise ValueError("The native video reader stopped unexpectedly")
                if not line.endswith(b"\n"):
                    raise ValueError("Video reader response exceeded its size limit")
                message = json.loads(line)
                if message["type"] == "metadata":
                    if self.session is not None:
                        raise ValueError("Duplicate video metadata")
                    self.session = self.create_preview(message)
                    self.info = message
                    self.proxy_url = url
                    self.send({"type": "ready", "job_id": self.session.id,
                               "playback_url": f"/api/local-readers/{self.id}/playback" if self.metadata.smooth_playback else None,
                               **{key: message[key] for key in ("width", "height", "duration_sec")}})
                elif message["type"] == "frame" and self.session is not None:
                    result = self.session.frame(message["sequence"], message["time_sec"], base64.b64decode(message["jpeg"], validate=True))
                    self.frame_ack.clear()
                    self.ack_sequence = message["sequence"]
                    self.send({"type": "observation", **result, "bytes_read": self.bytes_read})
                    if not self.frame_ack.wait(30) or self.stop_flag.is_set():
                        raise ValueError("The browser stopped receiving video results")
                elif message["type"] == "end" and self.session is not None:
                    result = self.session.complete_frames()
                    self.send({"type": "complete", **result})
                    ended = True
                    break
                elif message["type"] == "error":
                    raise ValueError(message["message"])
                else:
                    raise ValueError("Invalid native video reader response")
        except Exception as exc:
            try:
                self.send({"type": "error", "message": str(exc) if isinstance(exc, ValueError) else "Cannot read the selected file. Keep this tab open and try again."})
            except Exception:
                pass
        finally:
            # Smooth playback may still need file ranges after AI has finished.
            # Its browser connection owns the remaining proxy lifetime.
            if ended and self.metadata.smooth_playback and not self.stop_flag.is_set():
                process = self.process
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                if process.stdout is not None:
                    process.stdout.close()
                return
            self.close()
            if not ended and self.session is not None and self.session.status not in {"done", "error"}:
                self.session.fail("Local video reading was interrupted")
            async def disconnect():
                try:
                    await self.websocket.close()
                except Exception:
                    pass
            coroutine = disconnect()
            try:
                asyncio.run_coroutine_threadsafe(coroutine, self.loop)
            except RuntimeError:
                coroutine.close()

    def open_playback(self):
        """Transcode as a progressive fragmented MP4, independently of inference."""
        import imageio_ffmpeg
        with self.close_lock:
            if self.stop_flag.is_set() or self.info is None:
                raise ValueError("The selected video is no longer connected")
            if not self.metadata.smooth_playback:
                raise ValueError("Smooth playback was not requested")
            if self.playback_process is not None and self.playback_process.poll() is None:
                raise ValueError("This video is already playing")
            width, height, fps = self.info["width"], self.info["height"], min(30, max(1, self.info["fps"]))
            scale = min(1, 1280 / max(width, height))
            width, height = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
            args = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-threads", "2", "-rw_timeout", "15000000", "-format_whitelist", "mov,matroska,webm,avi",
                    "-i", self.proxy_url, "-map", "0:v:0", "-map", "0:a:0?",
                    "-vf", f"scale={width}:{height}", "-r", str(fps), "-c:v", "libx264", "-threads", "2",
                    "-preset", "veryfast", "-tune", "zerolatency", "-crf", "24", "-pix_fmt", "yuv420p",
                    "-g", str(max(1, round(fps))), "-keyint_min", str(max(1, round(fps))), "-sc_threshold", "0",
                    "-c:a", "aac", "-b:a", "96k", "-ac", "2",
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            self.playback_process = process
        return process

    def playback_chunks(self, process):
        ended = False
        try:
            while not self.stop_flag.is_set():
                chunk = process.stdout.read1(64 * 1024)
                if not chunk:
                    ended = True
                    break
                yield chunk
        except (OSError, ValueError):
            # Browser disconnect/reader cancellation may close the pipe mid-read.
            pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            process.stdout.close()
            if ended and not self.stop_flag.is_set():
                try:
                    self.send({"type": "playback_complete"})
                except RuntimeError:
                    pass

    def close(self):
        with self.close_lock:
            self._close()

    def _close(self):
        self.stop_flag.set()
        self.frame_ack.set()
        with self.lock:
            for request in self.pending.values():
                request["event"].set()
        for process in (self.process, self.playback_process):
            if process is None:
                continue
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
        server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()


class ReaderRegistry:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def add(self, reader):
        with self.lock:
            self.items = {key: item for key, item in self.items.items() if not item.stop_flag.is_set()}
            if len(self.items) >= MAX_READERS:
                raise ValueError("Too many videos are being read. Finish or cancel the current video first.")
            self.items[reader.id] = reader

    def get(self, identifier):
        with self.lock:
            return self.items.get(identifier)

    def close(self, idle_only=False):
        with self.lock:
            items = list(self.items.values())
        for reader in items:
            if not idle_only or (not reader.bound and time.monotonic() - reader.created > 60):
                reader.close()
                if not idle_only and reader.thread is not None and reader.thread is not threading.current_thread():
                    reader.thread.join(timeout=2)
