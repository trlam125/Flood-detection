from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZipFile

import cv2
import numpy as np

SPLITS = {"train", "validation", "test", "unassigned"}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def read_image(path: Path) -> np.ndarray:
    if not path.is_file():
        raise ValueError(f"File not found: {path}")
    # imdecode/fromfile supports Unicode paths on Windows.
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"Cannot decode image: {path}")
    return image


def save_image(path: Path, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(path.suffix, image)
    if not ok:
        raise ValueError(f"Cannot encode image: {path}")
    encoded.tofile(path)


def color_image(image: np.ndarray) -> np.ndarray:
    if image.dtype != np.uint8:
        raise ValueError("Input photos/previews must be 8-bit images")
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.ndim == 3 and image.shape[2] == 3:
        return image
    if image.ndim == 3 and image.shape[2] == 4 and np.all(image[:, :, 3] == 255):
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    raise ValueError("Input photos must be grayscale or three-channel images")


def decode_mask(raw: np.ndarray, encoding: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    kind = encoding.get("kind")
    water_values = encoding.get("water_values")
    ignore_values = encoding.get("ignore_values", [])
    if not isinstance(water_values, list) or not water_values:
        raise ValueError("Set mask_encoding.water_values explicitly before evaluation")
    if not isinstance(ignore_values, list):
        raise ValueError("ignore_values must be a list")
    values = water_values + ignore_values
    alpha_valid = np.ones(raw.shape[:2], dtype=bool)
    if kind == "rgb":
        if raw.ndim != 3 or raw.shape[2] not in {3, 4} or raw.dtype != np.uint8:
            raise ValueError("RGB masks must be three/four-channel, 8-bit images")
        if raw.shape[2] == 4:
            if not set(np.unique(raw[:, :, 3]).tolist()) <= {0, 255}:
                raise ValueError("Partially transparent annotation colors are ambiguous")
            alpha_valid = raw[:, :, 3] > 0
            raw = raw[:, :, :3]
        for value in values:
            if not isinstance(value, list) or len(value) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in value):
                raise ValueError("RGB values must be [R, G, B] integer lists in 0..255")
        rgb = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)

        def match(selected: list) -> np.ndarray:
            out = np.zeros(raw.shape[:2], dtype=bool)
            for value in selected:
                out |= np.all(rgb == value, axis=2)
            return out
    elif kind in {"binary", "indexed"}:
        if raw.ndim != 2 or not np.issubdtype(raw.dtype, np.integer):
            raise ValueError("Binary/indexed masks must be single-channel integer images")
        if any(type(v) is not int or v < 0 or v > np.iinfo(raw.dtype).max for v in values):
            raise ValueError("Mask values must be integers within the image dtype range")
        if kind == "binary":
            unique = set(np.unique(raw).tolist())
            if not (unique <= {0, 1} or unique <= {0, 255}):
                raise ValueError("Binary mask contains other classes; use indexed or rgb encoding")
            if any(value not in {1, 255} for value in water_values):
                raise ValueError("Binary water_values must contain 1 and/or 255")

        def match(selected: list) -> np.ndarray:
            return np.isin(raw, selected)
    else:
        raise ValueError("mask_encoding.kind must be binary, indexed, or rgb")
    if {json.dumps(v) for v in water_values} & {json.dumps(v) for v in ignore_values}:
        raise ValueError("Water and ignore values cannot overlap")
    water, valid = match(water_values), ~match(ignore_values) & alpha_valid
    if not valid.any():
        raise ValueError("Mask contains no valid pixels")
    return water, valid


@dataclass(frozen=True)
class Sample:
    id: str
    image: Path
    mask: Path
    source_id: str
    split: str
    condition: str


def load_manifest(path: Path) -> tuple[dict[str, Any], list[Sample], list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("samples"), list):
        raise ValueError("Expected manifest version 1 with a samples list")
    if not isinstance(data.get("mask_encoding"), dict):
        raise ValueError("Manifest needs mask_encoding")
    samples, ids, paths = [], set(), set()
    source_splits: dict[str, set[str]] = defaultdict(set)
    for item in data["samples"]:
        if not isinstance(item, dict):
            raise ValueError("Each manifest sample must be an object")
        for field in ("id", "image", "mask", "source_id", "split"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"Sample needs a nonempty {field}")
        if item["split"] not in SPLITS:
            raise ValueError(f"Unknown split: {item['split']}")
        image = (path.parent / item["image"]).resolve()
        mask = (path.parent / item["mask"]).resolve()
        if item["id"] in ids or image in paths:
            raise ValueError(f"Duplicate sample id or image path: {item['id']}")
        ids.add(item["id"])
        paths.add(image)
        if image == mask:
            raise ValueError("An image cannot also be its own reference mask")
        if not image.is_file() or not mask.is_file():
            raise ValueError(f"Image or reference mask is missing: {item['id']}")
        samples.append(Sample(item["id"], image, mask, item["source_id"], item["split"], str(item.get("condition", "unknown"))))
        if item["split"] != "unassigned":
            source_splits[item["source_id"]].add(item["split"])
    if not samples:
        raise ValueError("Manifest has no samples")
    leaked = [source for source, splits in source_splits.items() if len(splits) > 1]
    if leaked:
        raise ValueError(f"Source groups occur in multiple splits: {', '.join(leaked)}")
    # Also catch exact duplicate files across splits even if source_id was changed.
    seen_hashes: dict[str, tuple[str, str]] = {}
    for sample in samples:
        digest = hashlib.sha256(sample.image.read_bytes()).hexdigest()
        previous = seen_hashes.get(digest)
        if previous and previous[0] != sample.split:
            raise ValueError(f"Identical images occur in different splits: {previous[1]}, {sample.id}")
        seen_hashes[digest] = (sample.split, sample.id)
    warnings = [
        "Water segmentation scores do not measure flood depth or event detection quality.",
        "Source-group and exact-file checks cannot establish independence from the original model training set.",
    ]
    if data.get("training_overlap") != "excluded":
        warnings.append("Overlap with original model training data is unknown; treat scores as exploratory.")
    if any(s.split == "unassigned" for s in samples):
        warnings.append("Some samples have no assigned train/validation/test split.")
    return data, samples, warnings


def select_samples(samples: list[Sample], split: str, limit: int | None) -> list[Sample]:
    selected = [s for s in samples if split == "all" or s.split == split]
    if not selected:
        raise ValueError(f"No samples for split '{split}'")
    if limit is None or len(selected) <= limit:
        return selected
    # Spread a limited run across source groups and across each source's duration.
    groups: dict[str, list[Sample]] = defaultdict(list)
    for sample in selected:
        groups[sample.source_id].append(sample)
    quotas = dict.fromkeys(sorted(groups), 0)
    for _ in range(limit):
        eligible = [key for key in quotas if quotas[key] < len(groups[key])]
        key = min(eligible, key=lambda k: (quotas[k], k))
        quotas[key] += 1
    result = []
    for key, count in quotas.items():
        rows = sorted(groups[key], key=lambda s: s.id)
        for index in np.linspace(0, len(rows) - 1, count, dtype=int):
            result.append(rows[index])
    return result


def archive_pairs(archive: ZipFile) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    images: dict[str, list[str]] = defaultdict(list)
    masks: dict[str, list[str]] = defaultdict(list)
    for entry in archive.infolist():
        if entry.is_dir():
            continue
        path = PurePosixPath(entry.filename)
        parts = {p.casefold() for p in path.parts[:-1]}
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            continue
        if "image" in parts or "images" in parts:
            images[path.stem.casefold()].append(entry.filename)
        elif parts & {"anotation", "annotation", "annotations", "mask", "masks"}:
            masks[path.stem.casefold()].append(entry.filename)
    pairs, ambiguous, missing_images, missing_masks = [], [], [], []
    for stem in sorted(images.keys() | masks.keys()):
        photo, label = images.get(stem, []), masks.get(stem, [])
        if len(photo) > 1 or len(label) > 1:
            ambiguous.append({"stem": stem, "images": photo, "masks": label})
        elif photo and label:
            pairs.append((photo[0], label[0]))
        elif photo:
            missing_masks.extend(photo)
        else:
            missing_images.extend(label)
    return pairs, {
        "paired_samples": len(pairs), "images_without_masks": missing_masks,
        "masks_without_images": missing_images, "ambiguous_stems": ambiguous,
        "source_counts": dict(sorted((group, sum(PurePosixPath(i).parent.name == group for i, _ in pairs))
                                     for group in {PurePosixPath(i).parent.name for i, _ in pairs})),
    }


def decode_archive_image(archive: ZipFile, name: str) -> np.ndarray:
    result = cv2.imdecode(np.frombuffer(archive.read(name), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if result is None:
        raise ValueError(f"Cannot decode archive image: {name}")
    return result


def comparison_preview(image: np.ndarray, reference_bgr: np.ndarray, predicted: np.ndarray | None = None) -> np.ndarray:
    panels = [("Image", color_image(image)), ("Reference / annotation", color_image(reference_bgr))]
    if predicted is not None:
        overlay = color_image(image).copy()
        overlay[predicted.astype(bool)] = (0.55 * overlay[predicted.astype(bool)] + 0.45 * np.array([255, 180, 40])).astype(np.uint8)
        panels.append(("Prediction", overlay))
    previews = []
    for title, panel in panels:
        height = max(1, round(panel.shape[0] * 400 / panel.shape[1]))
        panel = cv2.resize(panel, (400, height), interpolation=cv2.INTER_NEAREST)
        panel = cv2.copyMakeBorder(panel, 30, 0, 0, 0, cv2.BORDER_CONSTANT)
        cv2.putText(panel, title, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        previews.append(panel)
    height = max(p.shape[0] for p in previews)
    return np.hstack([cv2.copyMakeBorder(p, 0, height - p.shape[0], 0, 0, cv2.BORDER_CONSTANT) for p in previews])
