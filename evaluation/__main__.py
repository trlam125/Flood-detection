from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

import cv2
import numpy as np

from .dataset import (
    Sample, archive_pairs, color_image, comparison_preview, decode_archive_image,
    decode_mask, load_manifest, read_image, save_image, select_samples, write_json,
)
from .runner import evaluate_dataset, fingerprint, new_output, write_csv


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("Must be a positive integer")
    return result


def positive_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("Must be a positive finite number")
    return result


def inspect_archive(args: argparse.Namespace) -> None:
    with ZipFile(args.archive) as archive:
        pairs, inventory = archive_pairs(archive)
        if not pairs:
            raise ValueError("No unambiguous image/annotation pairs found")
        new_output(args.output)
        (args.output / "previews").mkdir()
        samples = [
            # Sample selection uses the same source balancing as evaluation.
            {"id": image, "image": image, "mask": mask, "source_id": PurePosixPath(image).parent.name}
            for image, mask in pairs
        ]
        entries = [Sample(s["id"], Path(s["image"]), Path(s["mask"]), s["source_id"], "unassigned", "unknown") for s in samples]
        selected = select_samples(entries, "all", args.palette_samples)
        palette: Counter = Counter()
        previews = []
        for index, sample in enumerate(selected, 1):
            image = decode_archive_image(archive, sample.image.as_posix())
            annotation = decode_archive_image(archive, sample.mask.as_posix())
            display_annotation = annotation
            if annotation.ndim == 3 and annotation.shape[2] in {3, 4}:
                rgb = cv2.cvtColor(annotation[:, :, :3], cv2.COLOR_BGR2RGB)
                packed = rgb.astype(np.uint32)
                packed = (packed[:, :, 0] << 16) | (packed[:, :, 1] << 8) | packed[:, :, 2]
                if annotation.shape[2] == 4:
                    packed = packed[annotation[:, :, 3] == 255]
                    display_annotation = annotation[:, :, :3].copy()
                    display_annotation[annotation[:, :, 3] != 255] = 0
                values, counts = np.unique(packed, return_counts=True)
                palette.update({f"{int(v) >> 16},{(int(v) >> 8) & 255},{int(v) & 255}": int(n) for v, n in zip(values, counts)})
            else:
                values, counts = np.unique(annotation, return_counts=True)
                palette.update({str(v): int(n) for v, n in zip(values, counts)})
            name = f"previews/{index:03d}.jpg"
            save_image(args.output / name, comparison_preview(image, display_annotation))
            previews.append({"image": sample.image.as_posix(), "mask": sample.mask.as_posix(), "preview": name})
    write_json(args.output / "inventory.json", {
        "archive": str(args.archive.resolve()), "archive_sha256": fingerprint(args.archive),
        **inventory, "palette_sample_count": len(selected),
        "palette_sampled_pixel_counts": dict(palette.most_common()), "previews": previews,
        "notes": ["Palette frequencies are from sampled annotations only, not the whole archive.",
                  "No water class is inferred from color names or model predictions.",
                  "Ambiguous filename stems are excluded rather than guessed."],
    })
    print(json.dumps({"paired_samples": len(pairs), "sources": inventory["source_counts"],
                      "palette": dict(palette.most_common(20)), "output": str(args.output)}, ensure_ascii=False, indent=2))


def prepare_archive(args: argparse.Namespace) -> None:
    if any(not 0 <= value <= 255 for color in args.water_rgb + args.ignore_rgb for value in color):
        raise ValueError("RGB channels must be in 0..255")
    with ZipFile(args.archive) as archive:
        pairs, inventory = archive_pairs(archive)
        if not pairs:
            raise ValueError("No unambiguous image/annotation pairs found")
        new_output(args.output)
        for folder in ("images", "masks"):
            (args.output / folder).mkdir()
        samples = []
        for image, mask in pairs:
            identifier = hashlib.sha256(image.encode("utf-8")).hexdigest()[:20]
            image_name = f"images/{identifier}{PurePosixPath(image).suffix.lower()}"
            mask_name = f"masks/{identifier}{PurePosixPath(mask).suffix.lower()}"
            # Never extract archive-controlled paths; write generated safe filenames.
            (args.output / image_name).write_bytes(archive.read(image))
            (args.output / mask_name).write_bytes(archive.read(mask))
            samples.append({"id": PurePosixPath(image).stem, "image": image_name, "mask": mask_name,
                            "source_id": PurePosixPath(image).parent.name, "split": "unassigned", "condition": "unknown",
                            "original_image": image, "original_mask": mask})
    write_json(args.output / "manifest.json", {
        "version": 1, "name": args.archive.stem, "training_overlap": "unknown",
        "archive_sha256": fingerprint(args.archive),
        "mask_encoding": {"kind": "rgb", "water_values": args.water_rgb, "ignore_values": args.ignore_rgb},
        "label_mapping_note": "User-supplied RGB values; document visual/source verification before interpreting scores.",
        "samples": samples,
    })
    write_json(args.output / "pairing.json", inventory)
    print(f"Prepared {len(samples)} pairs: {args.output / 'manifest.json'}")
    if not args.water_rgb:
        print("Water mapping is intentionally unset. Set mask_encoding.water_values after checking the previews.")


def validate_dataset(args: argparse.Namespace) -> None:
    manifest, samples, warnings = load_manifest(args.manifest)
    for sample in samples:
        image = color_image(read_image(sample.image))
        reference, _ = decode_mask(read_image(sample.mask), manifest["mask_encoding"])
        if image.shape[:2] != reference.shape:
            raise ValueError(f"Image/mask dimension mismatch: {sample.id}")
    print(json.dumps({"valid_samples": len(samples), "sources": len({s.source_id for s in samples}),
                      "splits": dict(Counter(s.split for s in samples)), "warnings": warnings}, ensure_ascii=False, indent=2))


def load_segmenter(args: argparse.Namespace):
    # Keep torch and app imports out of inspect/prepare/validate/help.
    import torch
    from app.config import settings
    from app.model import create_segmenter

    torch.set_num_threads(args.cpu_threads)
    backend = args.backend or settings.model_backend
    riwa = args.model if args.model and backend == "riwa" else settings.model_path
    yolo = args.model if args.model and backend == "yolo" else settings.yolo_model_path
    started = time.perf_counter()
    segmenter = create_segmenter(backend, riwa, yolo)
    return segmenter, (time.perf_counter() - started) * 1000


def evaluate(args: argparse.Namespace) -> None:
    # Reject invalid structure/selection before loading model weights.
    _, samples, _ = load_manifest(args.manifest)
    select_samples(samples, args.split, args.limit)
    segmenter, load_ms = load_segmenter(args)
    report = evaluate_dataset(args.manifest, args.output, segmenter, split=args.split, limit=args.limit,
                              worst=args.worst, warmup=args.warmup, load_ms=load_ms,
                              runtime_settings={"cpu_threads": args.cpu_threads})
    print(json.dumps(report["summary"], indent=2))
    print(f"Report: {args.output / 'report.html'}")


def profile_video(args: argparse.Namespace) -> None:
    if not args.video.is_file():
        raise ValueError(f"Video not found: {args.video}")
    cap = cv2.VideoCapture(str(args.video))
    try:
        if not cap.isOpened():
            raise ValueError("Cannot open video")
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("Video has no valid FPS; cannot establish sampling timestamps")
        if args.interval_sec * fps < 1:
            raise ValueError("Sampling interval must be at least one video frame")
        segmenter, load_ms = load_segmenter(args)
        new_output(args.output)
        for folder in ("images", "predictions", "previews", "masks"):
            (args.output / folder).mkdir()
        rows, annotations = [], []
        for index in range(args.max_frames):
            target = round(index * args.interval_sec * fps)
            cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, frame = cap.read()
            if not ok:
                break
            actual = max(0, round(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1)
            if rows and actual <= rows[-1]["frame_index"]:
                raise ValueError("Video backend did not advance to the requested frame")
            identifier = f"frame_{actual:09d}"
            if index == 0:
                for _ in range(args.warmup):
                    segmenter.predict_mask(frame)
            started = time.perf_counter()
            predicted, _ = segmenter.predict_mask(frame)
            elapsed = (time.perf_counter() - started) * 1000
            save_image(args.output / "images" / f"{identifier}.png", frame)
            save_image(args.output / "predictions" / f"{identifier}.png", (predicted > 0).astype(np.uint8) * 255)
            preview = comparison_preview(frame, np.zeros_like(frame), predicted)
            save_image(args.output / "previews" / f"{identifier}.jpg", preview)
            rows.append({"id": identifier, "frame_index": actual, "time_sec": actual / fps,
                         "raw_water_ratio": float(np.mean(predicted > 0)), "inference_ms": elapsed})
            annotations.append({"id": identifier, "image": f"images/{identifier}.png", "mask": f"masks/{identifier}.png",
                                "source_id": args.video.stem, "split": "unassigned", "condition": "unknown"})
            print(f"Profiled {index + 1}/{args.max_frames} requested frames", flush=True)
        if not rows:
            raise ValueError("Video contained no readable frames")
    finally:
        cap.release()
    timings = [row["inference_ms"] for row in rows]
    write_csv(args.output / "samples.csv", rows)
    write_json(args.output / "profile.json", {
        "task": "unlabelled_inference_profile", "video": str(args.video.resolve()), "video_sha256": fingerprint(args.video),
        "model": segmenter.info(), "model_sha256": fingerprint(Path(segmenter.info()["model_path"])),
        "model_load_ms": load_ms, "cpu_threads": args.cpu_threads, "fps": fps, "samples": len(rows),
        "interval_sec": args.interval_sec, "warmup_frames_excluded": args.warmup,
        "latency_ms": {"mean": float(np.mean(timings)), "p95": float(np.percentile(timings, 95))},
        "notes": ["No ground truth: no accuracy, IoU, Dice, event or depth claims can be made.",
                  "Latency covers predict_mask only; excludes seeking, decoding and saving artifacts.",
                  "predictions/ contains model outputs, never reference labels. masks/ is deliberately empty.",
                  "Previews show a blank reference panel because no reference labels exist."],
    })
    write_json(args.output / "annotation_manifest.json", {"version": 1, "training_overlap": "unknown",
               "mask_encoding": {"kind": "binary", "water_values": [255], "ignore_values": []}, "samples": annotations})
    print(f"Profile: {args.output / 'profile.json'}; label images into masks/ before evaluating.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline flood-water segmentation assessment; never sends alerts.")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Inspect paired ZIP images and annotation palette without extracting all data")
    inspect.add_argument("--archive", type=Path, required=True)
    inspect.add_argument("--output", type=Path, required=True)
    inspect.add_argument("--palette-samples", type=positive_int, default=10)
    inspect.set_defaults(func=inspect_archive)
    prepare = commands.add_parser("prepare", help="Prepare paired images and a manifest from a ZIP")
    prepare.add_argument("--archive", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--water-rgb", nargs=3, type=int, action="append", default=[], metavar=("R", "G", "B"))
    prepare.add_argument("--ignore-rgb", nargs=3, type=int, action="append", default=[], metavar=("R", "G", "B"))
    prepare.set_defaults(func=prepare_archive)
    validate = commands.add_parser("validate", help="Check labels, dimensions and train/validation/test leakage")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.set_defaults(func=validate_dataset)
    evaluation = commands.add_parser("evaluate", help="Score the deployed segmentation pipeline against reference masks")
    evaluation.add_argument("--manifest", type=Path, required=True)
    evaluation.add_argument("--split", choices=["train", "validation", "test", "unassigned", "all"], default="test")
    evaluation.add_argument("--limit", type=positive_int)
    evaluation.add_argument("--worst", type=int, default=12)
    evaluation.set_defaults(func=evaluate)
    profile = commands.add_parser("profile", help="Profile unlabelled video and export frames for manual annotation")
    profile.add_argument("--video", type=Path, required=True)
    profile.add_argument("--interval-sec", type=positive_float, default=2.0)
    profile.add_argument("--max-frames", type=positive_int, default=12)
    profile.set_defaults(func=profile_video)
    for subparser in (evaluation, profile):
        subparser.add_argument("--output", type=Path, required=True)
        subparser.add_argument("--backend", choices=["riwa", "yolo"])
        subparser.add_argument("--model", type=Path)
        subparser.add_argument("--cpu-threads", type=positive_int, default=4)
        subparser.add_argument("--warmup", type=int, default=1)
    args = parser.parse_args()
    if getattr(args, "warmup", 0) < 0 or getattr(args, "worst", 0) < 0:
        parser.error("--warmup and --worst cannot be negative")
    try:
        args.func(args)
    except (ValueError, OSError, RuntimeError, BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
