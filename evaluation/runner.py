from __future__ import annotations

import csv
import hashlib
import html
import platform
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .dataset import (
    Sample, color_image, comparison_preview, decode_mask, load_manifest,
    read_image, save_image, select_samples, write_json,
)
from .metrics import PixelCounts, count_pixels


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_output(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(f"Output must be a new or empty directory: {path}")
    path.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = PixelCounts()
    for row in rows:
        counts += PixelCounts(*(row[key] for key in ("tp", "fp", "fn", "tn")))
    macro = {}
    for metric in ("iou", "dice", "precision", "recall"):
        values = [r[metric] for r in rows if r[metric] is not None]
        macro[metric] = {"mean": float(np.mean(values)) if values else None, "defined_samples": len(values)}
    timings = [r["inference_ms"] for r in rows]
    return {
        "samples": len(rows), "micro": counts.scores(), "macro": macro,
        "negative_reference_samples": sum(r["tp"] + r["fn"] == 0 for r in rows),
        "negative_samples_with_false_positives": sum(r["tp"] + r["fn"] == 0 and r["fp"] > 0 for r in rows),
        "water_ratio_mae": float(np.mean([r["water_ratio_absolute_error"] for r in rows])),
        "latency_ms": {"mean": float(np.mean(timings)), "p50": float(np.percentile(timings, 50)), "p95": float(np.percentile(timings, 95))},
    }


def evaluate_dataset(
    manifest_path: Path, output: Path, segmenter: Any, *, split: str = "test",
    limit: int | None = None, worst: int = 12, warmup: int = 1,
    model_sha256: str | None = None, load_ms: float | None = None,
    runtime_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest, samples, warnings = load_manifest(manifest_path)
    manifest_sha256 = fingerprint(manifest_path)
    selected = select_samples(samples, split, limit)
    # Validate selected labels and dimensions before inference or writing a report.
    for sample in selected:
        image = color_image(read_image(sample.image))
        reference, _ = decode_mask(read_image(sample.mask), manifest["mask_encoding"])
        if image.shape[:2] != reference.shape:
            raise ValueError(f"Image/mask dimension mismatch: {sample.id}; masks are never resized automatically")
    new_output(output)
    first = color_image(read_image(selected[0].image))
    for _ in range(warmup):
        segmenter.predict_mask(first)
    rows = []
    by_id = {sample.id: sample for sample in selected}
    for index, sample in enumerate(selected, 1):
        image = color_image(read_image(sample.image))
        reference, valid = decode_mask(read_image(sample.mask), manifest["mask_encoding"])
        started = time.perf_counter()
        predicted, _ = segmenter.predict_mask(image)
        elapsed = (time.perf_counter() - started) * 1000
        scores = count_pixels(predicted, reference, valid).scores()
        rows.append({
            "id": sample.id, "source_id": sample.source_id, "split": sample.split,
            "condition": sample.condition, "image": str(sample.image), "mask": str(sample.mask),
            **scores,
            "water_ratio_absolute_error": abs(scores["water_ratio_predicted"] - scores["water_ratio_reference"]),
            "inference_ms": elapsed,
        })
        if index == 1 or index % 10 == 0 or index == len(selected):
            print(f"Evaluated {index}/{len(selected)}", flush=True)
    groups = {}
    for field in ("source_id", "condition"):
        buckets: dict[str, list] = defaultdict(list)
        for row in rows:
            buckets[row[field]].append(row)
        groups[field] = {key: summarize(bucket) for key, bucket in sorted(buckets.items())}
    info = segmenter.info()
    info_path = Path(info.get("model_path", ""))
    report = {
        "report_version": 1, "task": "water_segmentation", "created_at_unix": time.time(),
        "manifest_path": str(manifest_path.resolve()), "manifest_sha256": manifest_sha256,
        "model": info, "model_sha256": model_sha256 or (fingerprint(info_path) if info_path.is_file() else None),
        "mask_encoding": manifest["mask_encoding"], "training_overlap": manifest.get("training_overlap", "unknown"),
        "selection": {"split": split, "limit": limit, "strategy": "source-balanced, evenly spaced when limited"},
        "runtime": {"python": platform.python_version(), "opencv": cv2.__version__, "numpy": np.__version__, **(runtime_settings or {}),
                    "model_load_ms": load_ms, "warmup_frames_excluded": warmup,
                    "latency_scope": "predict_mask including preprocessing, model, transfer to CPU and mask cleanup; excludes image I/O, warmup, reports"},
        "summary": summarize(rows), "by_group": groups, "warnings": warnings,
        "metric_conventions": {
            "undefined": "null, excluded from macro means; counts and defined_samples retained",
            "empty_reference_and_prediction": "IoU/Dice/precision/recall undefined, not counted as perfect water segmentation",
            "background": "All values not declared water or ignore are treated as non-water",
        },
        "samples": rows,
    }
    if split in {"all", "train", "unassigned"}:
        report["warnings"].append("This run is not a held-out test evaluation.")
    if limit is not None and limit < len([s for s in samples if split == "all" or s.split == split]):
        report["warnings"].append("This report evaluates a subset, not the full selected split.")
    worst_rows = sorted(rows, key=lambda r: (r["iou"] if r["iou"] is not None else 1.0, -r["water_ratio_absolute_error"], r["id"]))[:worst]
    previews = []
    if worst_rows:
        (output / "previews").mkdir()
    for index, row in enumerate(worst_rows, 1):
        sample: Sample = by_id[row["id"]]
        image = color_image(read_image(sample.image))
        reference, valid = decode_mask(read_image(sample.mask), manifest["mask_encoding"])
        predicted, _ = segmenter.predict_mask(image)
        reference_bgr = np.zeros_like(image)
        reference_bgr[reference] = (255, 180, 40)
        reference_bgr[~valid] = (110, 110, 110)
        name = f"previews/{index:03d}.jpg"
        save_image(output / name, comparison_preview(image, reference_bgr, predicted))
        previews.append({"sample_id": row["id"], "iou": row["iou"], "path": name})
    report["worst_previews"] = previews
    write_csv(output / "samples.csv", rows)
    write_json(output / "report.json", report)
    write_json(output / "manifest.snapshot.json", manifest)
    write_html_report(output / "report.html", report)
    return report


def write_html_report(path: Path, report: dict[str, Any]) -> None:
    def number(value: Any) -> str:
        return "N/A" if value is None else f"{value:.4f}"

    summary = report["summary"]
    metrics = "".join(f"<tr><th>{name}</th><td>{number(summary['micro'][name])}</td></tr>" for name in ("iou", "dice", "precision", "recall", "false_positive_rate"))
    warnings = "".join(f"<li>{html.escape(w)}</li>" for w in report["warnings"])
    groups = "".join(f"<tr><td>{html.escape(name)}</td><td>{group['samples']}</td><td>{number(group['micro']['iou'])}</td><td>{number(group['micro']['recall'])}</td></tr>"
                     for name, group in report["by_group"]["source_id"].items())
    previews = "".join(f"<figure><figcaption>{html.escape(p['sample_id'])} | IoU {number(p['iou'])}</figcaption><img src='{p['path']}' alt='Image, water reference and prediction'></figure>" for p in report["worst_previews"])
    path.write_text(f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Water segmentation evaluation</title><style>body{{font:16px system-ui;max-width:1250px;margin:32px auto;padding:0 20px;background:#111a25;color:#edf4ff}}table{{border-collapse:collapse}}td,th{{border:1px solid #526070;padding:8px;text-align:left}}img{{max-width:100%}}figure{{margin:24px 0}}li{{margin:8px 0}}a{{color:#8ecaff}}</style>
<h1>Water segmentation evaluation</h1><p>{summary['samples']} samples | backend {html.escape(str(report['model'].get('backend')))} | split {html.escape(report['selection']['split'])}</p>
<ul>{warnings}</ul><h2>Pixel-weighted water metrics</h2><table>{metrics}</table>
<p>Water-ratio MAE: {number(summary['water_ratio_mae'])}. Mean inference: {summary['latency_ms']['mean']:.1f} ms; p95: {summary['latency_ms']['p95']:.1f} ms. Warmup and image I/O excluded.</p>
<p>Undefined metrics are N/A, including IoU/Dice when both masks contain no water. This report does not evaluate flood depth, event recall or alert delay.</p>
<h2>By source</h2><table><tr><th>Source</th><th>Samples</th><th>IoU</th><th>Recall</th></tr>{groups}</table>
<h2>Lowest-IoU samples</h2>{previews}<p><a href="report.json">Full JSON</a> | <a href="samples.csv">Per-sample CSV</a></p></html>""", encoding="utf-8")
