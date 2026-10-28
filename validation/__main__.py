from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path

from .events import Acceptance, Clip, Manifest, evaluate_observations, summarize_events
from .soak import run_soak


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive finite number")
    return number


def write_report(output, report):
    output.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    (output / "report.json").write_text(text, encoding="utf-8")
    (output / "report.html").write_text('<!doctype html><meta charset="utf-8"><title>FloodEye validation</title>'
        '<style>body{font:15px system-ui;max-width:1100px;margin:32px auto;padding:0 20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f7;padding:20px}</style>'
        '<h1>FloodEye validation</h1><p>Read the scope and evidence limitations before interpreting results.</p><pre>' + html.escape(text) + '</pre>', encoding="utf-8")


def preflight(args):
    from app.alerts import AlertManager, AlertPayload
    from app.capture import ProcessCapture
    from app.config import settings
    from app.database import Database
    from app.operations import configuration_readiness
    from app.sources import CameraSource, resolve_source
    from app.storage import StorageManager
    if args.send_test and not args.channel:
        raise ValueError("--send-test requires --channel email|telegram|sms")
    if args.camera_id and not args.connect:
        raise ValueError("--camera-id requires --connect")
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
    database, manager = Database(settings.db_path, read_only=True), AlertManager(settings)
    report = {"kind": "preflight", "at_unix": time.time(), **configuration_readiness(database, manager),
              "storage": StorageManager(database, settings).capacity(), "connection_checks": [], "test_notification": None}
    if args.camera_id and not any(camera["camera_id"] == args.camera_id for camera in report["cameras"]):
        raise ValueError("The selected camera profile does not exist")
    if args.connect:
        for camera in report["cameras"]:
            if args.camera_id and camera["camera_id"] != args.camera_id:
                continue
            saved = database.get_source(camera["camera_id"])
            if not saved:
                continue
            source = CameraSource.model_validate(saved["configuration"])
            handle, outcome = None, {"camera_id": camera["camera_id"], "ok": False}
            try:
                handle = ProcessCapture(resolve_source(source), source.kind, source.open_timeout_sec, source.read_timeout_sec)
                deadline = time.monotonic() + source.open_timeout_sec + 3
                while time.monotonic() < deadline:
                    result = handle.poll()
                    if result.failed or not result.alive:
                        break
                    if result.frame is not None:
                        outcome.update(ok=True, source_width=result.frame.source_width, source_height=result.frame.source_height,
                                       received_age_sec=max(0, time.monotonic() - result.frame.received_mono))
                        break
                    time.sleep(.05)
            except (ValueError, OSError):
                pass
            finally:
                if handle is not None:
                    handle.close()
            if not outcome["ok"]:
                outcome["detail"] = "No camera image received; check source, credentials, network and server webcam access"
            report["connection_checks"].append(outcome)
    if args.send_test:
        payload = AlertPayload("connectivity-test-" + uuid.uuid4().hex, "TEST", 0, 0, event_type="connectivity_test")
        report["test_notification"] = {"channel": args.channel, **manager.send_channel(args.channel, payload, manager.target_fingerprint(args.channel))}
    report["ready_for_device_test"] = bool(report["configured_camera_count"] and report["external_channel_count"] and report["storage"]["status"] != "critical")
    report["field_acceptance"] = "not_established"
    if args.output:
        write_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.connect or args.send_test:
        success = (not args.connect or bool(report["connection_checks"]) and all(item["ok"] for item in report["connection_checks"]))
        success = success and (not args.send_test or report["test_notification"]["ok"])
        return 0 if success else 2
    return 0 if report["ready_for_device_test"] else 2


def prepare(args):
    import cv2
    if not args.video.is_file():
        raise ValueError("Video must be an existing local file")
    cap = cv2.VideoCapture(str(args.video))
    try:
        fps, count = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if not cap.isOpened() or not math.isfinite(fps) or fps <= 0 or count <= 0:
            raise ValueError("Video has no valid nominal FPS/duration")
        duration = count / fps
        args.output.mkdir(parents=True, exist_ok=False)
        frames = args.output / "review_frames"
        frames.mkdir()
        examples = []
        at = 0.0
        while at < duration and len(examples) < 200:
            cap.set(cv2.CAP_PROP_POS_MSEC, at * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            scale = min(1, 960 / max(frame.shape[:2]))
            if scale < 1:
                frame = cv2.resize(frame, (round(frame.shape[1] * scale), round(frame.shape[0] * scale)))
            name = f"review_frames/{len(examples):04d}.jpg"
            if not cv2.imwrite(str(args.output / name), frame):
                raise OSError("Cannot save review image")
            examples.append({"time_sec": at, "image": name})
            at += args.every_sec
        manifest = Manifest(clips=[Clip(id=args.video.stem, path=str(args.video.resolve()), source_id=args.source_id,
            duration_sec=duration, intervals=[{"start_sec": 0, "end_sec": duration, "label": "unreviewed"}])])
        (args.output / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        (args.output / "review_frames.json").write_text(json.dumps(examples, indent=2), encoding="utf-8")
        print(json.dumps({"manifest": str(args.output / "manifest.json"), "review_images": len(examples),
                          "labels": "unreviewed; replace with human-reviewed flood/normal intervals"}))
    finally:
        cap.release()
    return 0


def replay(args):
    import cv2
    import numpy as np
    import torch
    from app.config import settings
    from app.events import EventPolicy
    from app.inference import analyze_frame
    from app.model import create_segmenter
    from app.regions import rasterize_roi
    from app.severity import SeverityThresholds
    from evaluation.runner import fingerprint
    manifest = Manifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    acceptance = Acceptance.model_validate_json(args.acceptance.read_text(encoding="utf-8")) if args.acceptance else Acceptance()
    selected = [clip for clip in manifest.clips if clip.split == "test"]
    if not selected:
        raise ValueError("No test split clips; train/val footage is not used for held-out event results")
    clip_paths, clip_hashes, seen_paths, seen_hashes = {}, {}, set(), set()
    for clip in manifest.clips:
        path = Path(clip.path)
        path = (path if path.is_absolute() else args.manifest.resolve().parent / path).resolve()
        if not path.is_file():
            raise ValueError(f"Missing local video for clip {clip.id}")
        digest = fingerprint(path)
        if path in seen_paths or digest in seen_hashes:
            raise ValueError("Repeated video path/content in the validation manifest; clips cannot be counted twice or cross splits")
        seen_paths.add(path)
        seen_hashes.add(digest)
        clip_paths[clip.id], clip_hashes[clip.id] = path, digest
    torch.set_num_threads(args.threads)
    model = create_segmenter(settings.model_backend, settings.model_path, settings.yolo_model_path)
    args.output.mkdir(parents=True, exist_ok=False)
    reports = []
    for clip in selected:
        path = clip_paths[clip.id]
        cap = cv2.VideoCapture(str(path))
        observations, latencies = [], []
        try:
            fps, count = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
            if not cap.isOpened() or not math.isfinite(fps) or fps <= 0 or count <= 0 or abs(count / fps - clip.duration_sec) > max(1 / fps, .01):
                raise ValueError(f"Video duration/FPS does not match labels: {clip.id}")
            thresholds = SeverityThresholds(**clip.camera.thresholds.model_dump()) if clip.camera else SeverityThresholds()
            policy = (clip.camera.event_policy if clip.camera else None) or EventPolicy()
            stride = max(1, round(args.interval_sec * fps))
            if stride / fps > policy.max_gap_sec:
                raise ValueError("Replay sampling interval exceeds the observation gap policy")
            previous = previous_time = None
            roi = None
            index = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if index % stride == 0:
                    at = index / fps
                    if clip.camera and clip.camera.roi is not None and roi is None:
                        roi = rasterize_roi(clip.camera.roi, frame.shape[1], frame.shape[0], clip.camera.reference_width, clip.camera.reference_height)
                    began = time.perf_counter()
                    _, metrics = analyze_frame(model, frame, thresholds, previous, roi,
                        elapsed_sec=None if previous_time is None else at - previous_time, ema_time_constant_sec=policy.ema_time_constant_sec)
                    latencies.append((time.perf_counter() - began) * 1000)
                    observations.append({"time_sec": at, "water_ratio": metrics.water_ratio})
                    previous, previous_time = metrics.water_ratio, at
                index += 1
            if index + 1 < count:
                raise ValueError(f"Video decoding ended before the labelled duration: {clip.id}")
        finally:
            cap.release()
        report = evaluate_observations(clip, observations, thresholds, policy)
        report["inference_ms"] = {"p50": float(np.percentile(latencies, 50)), "p95": float(np.percentile(latencies, 95)),
                                  "scope": "analyze_frame wall time including first inference; excludes video decoding"}
        report["video_sha256"] = clip_hashes[clip.id]
        name = hashlib.sha256(clip.id.encode()).hexdigest()[:16]
        (args.output / f"{name}.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        with (args.output / f"{name}.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(report["observations"][0]))
            writer.writeheader()
            writer.writerows(report["observations"])
        reports.append({key: value for key, value in report.items() if key not in {"observations", "events"}})
        print(f"Evaluated clip {len(reports)}/{len(selected)}", flush=True)
    summary = {"kind": "reviewed_event_replay", "created_at_unix": time.time(), "model": model.info(),
               "manifest_sha256": fingerprint(args.manifest), "training_overlap": manifest.training_overlap,
               "threads": args.threads, "sample_interval_requested_sec": args.interval_sec,
               "reports": reports, **summarize_events(reports, manifest.training_overlap, acceptance)}
    summary["model_sha256"] = fingerprint(settings.yolo_model_path if settings.model_backend == "yolo" else settings.model_path)
    summary["limitations"] = ["Ground truth and training-overlap declaration supplied by the operator", "Nominal FPS clock; convert variable-frame-rate recordings to a verified CFR copy", "Offline video does not test RTSP transport or notification delivery"]
    write_report(args.output, summary)
    print(json.dumps({"decision": summary["decision"], "metrics": summary["metrics"], "output": str(args.output)}, indent=2))
    return 0 if summary["decision"] == "keep_current_model_candidate" else 2


def main():
    parser = argparse.ArgumentParser(description="FloodEye operational and reviewed-event validation")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("preflight", help="Check setup; optional bounded camera read or explicit test notification")
    check.add_argument("--connect", action="store_true")
    check.add_argument("--camera-id")
    check.add_argument("--send-test", action="store_true")
    check.add_argument("--channel", choices=["email", "telegram", "sms"])
    check.add_argument("--output", type=Path)
    check.set_defaults(run=preflight)
    collect = sub.add_parser("prepare", help="Create review frames and an explicitly unreviewed label template")
    collect.add_argument("--video", type=Path, required=True)
    collect.add_argument("--source-id", required=True)
    collect.add_argument("--every-sec", type=positive, default=10)
    collect.add_argument("--output", type=Path, required=True)
    collect.set_defaults(run=prepare)
    events = sub.add_parser("replay", help="Run model and temporal-event evaluation against reviewed labels")
    events.add_argument("--manifest", type=Path, required=True)
    events.add_argument("--acceptance", type=Path)
    events.add_argument("--interval-sec", type=positive, default=.5)
    events.add_argument("--threads", type=int, default=min(4, os.cpu_count() or 1), choices=range(1, 65))
    events.add_argument("--output", type=Path, required=True)
    events.set_defaults(run=replay)
    soak = sub.add_parser("soak", help="Poll a running server and save an operational availability report")
    soak.add_argument("--url", default="http://127.0.0.1:8000")
    soak.add_argument("--duration-sec", type=positive, default=86400)
    soak.add_argument("--interval-sec", type=positive, default=5)
    soak.add_argument("--output", type=Path, required=True)
    def soak_command(args):
        report = run_soak(args.url, args.output, args.duration_sec, args.interval_sec)
        write_report(args.output, report)
        print(json.dumps(report, indent=2))
        return 0 if report["completed_requested_duration"] and report["has_fresh_confirmed_observations"] else 2
    soak.set_defaults(run=soak_command)
    try:
        args = parser.parse_args()
        return args.run(args)
    except (ValueError, OSError) as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
