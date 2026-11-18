"""Compare original and no-flood RIWA outputs on known dry/flood clips."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import cv2
import numpy as np
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import torch

from app.model import CityscapesRoadSegmenter, RIWASegmenter

VIDEOS = [
    ("dry", Path(r"D:\Zalo Data\media1\media\15157-262146024_tiny.mp4")),
    ("dry", Path(r"D:\Zalo Data\media1\media\15362801_2160_3840_30fps.mp4")),
    ("dry", Path(r"D:\Zalo Data\media1\media\17464691_3840_2160_60fps.mp4")),
    ("flood", ROOT / "uploads" / "42c36caf870c4bb598b93337c1506040.mp4"),
    ("flood", ROOT / "uploads" / "890bd73bee9f421eb26ac234a310fa54.mp4"),
]
OUT = ROOT / "outputs" / "road_water_diagnosis"
SAMPLES_PER_VIDEO = 5
MAX_EDGE = 1280


def resize(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    scale = min(1.0, MAX_EDGE / max(h, w))
    if scale == 1:
        return frame
    return cv2.resize(frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)


def overlay(frame: np.ndarray, mask: np.ndarray, color: tuple[int, int, int]) -> np.ndarray:
    out = frame.copy()
    out[mask] = (0.55 * out[mask] + 0.45 * np.asarray(color)).astype(np.uint8)
    return out


def add_title(frame: np.ndarray, title: str) -> np.ndarray:
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 32), (20, 20, 20), -1)
    cv2.putText(frame, title, (8, 23), cv2.FONT_HERSHEY_SIMPLEX, .65, (255, 255, 255), 1, cv2.LINE_AA)
    return frame


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    device = torch.device("cpu")
    old = RIWASegmenter(ROOT / "models" / "riwa_unet_resnet34_deploy.pth", device="cpu")
    dry = RIWASegmenter(ROOT / "models" / "riwa_no_flood_finetuned.pth", device="cpu")
    road = CityscapesRoadSegmenter("nvidia/segformer-b0-finetuned-cityscapes-640-1280", device)
    rows = []
    for condition, video_path in VIDEOS:
        if not video_path.exists():
            print(f"SKIP missing {video_path}", flush=True)
            continue
        cap = cv2.VideoCapture(str(video_path))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not cap.isOpened() or total < 1:
            cap.release()
            print(f"SKIP unreadable {video_path}", flush=True)
            continue
        indices = np.unique(np.linspace(int(total * .1), max(int(total * .9) - 1, 0), SAMPLES_PER_VIDEO, dtype=int))
        panels = []
        for frame_index in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            frame = resize(frame)
            road_mask = road.predict_road_mask(frame)
            old_mask, old_prob = old.predict_mask(frame)
            dry_mask, dry_prob = dry.predict_mask(frame)
            road_pixels = max(1, int(road_mask.sum()))
            old_on_road = (old_mask > 0) & road_mask
            dry_on_road = (dry_mask > 0) & road_mask
            overlap = old_on_road & dry_on_road
            rows.append({
                "condition": condition, "video": video_path.name, "frame": int(frame_index),
                "road_percent_frame": road_pixels / road_mask.size,
                "original_water_percent_road": int(old_on_road.sum()) / road_pixels,
                "dry_tuned_water_percent_road": int(dry_on_road.sum()) / road_pixels,
                "old_new_overlap_percent_old_water": int(overlap.sum()) / max(1, int(old_on_road.sum())),
                "old_mean_probability_road": float(old_prob[road_mask].mean()) if road_mask.any() else 0,
                "dry_mean_probability_road": float(dry_prob[road_mask].mean()) if road_mask.any() else 0,
            })
            h, w = frame.shape[:2]
            target_w = 360
            target_h = max(1, round(h * target_w / w))
            road_small = cv2.resize(road_mask.astype(np.uint8), (target_w, target_h), interpolation=cv2.INTER_NEAREST).astype(bool)
            old_small = cv2.resize(old_on_road.astype(np.uint8), (target_w, target_h), interpolation=cv2.INTER_NEAREST).astype(bool)
            dry_small = cv2.resize(dry_on_road.astype(np.uint8), (target_w, target_h), interpolation=cv2.INTER_NEAREST).astype(bool)
            src = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
            columns = [
                add_title(src.copy(), f"{condition} {video_path.name} frame {frame_index}"),
                add_title(overlay(src.copy(), road_small, (70, 235, 150)), "SegFormer road"),
                add_title(overlay(src.copy(), old_small, (30, 160, 255)), "RIWA original water / road"),
                add_title(overlay(src.copy(), dry_small, (255, 150, 40)), "RIWA no-flood water / road"),
            ]
            panels.append(cv2.hconcat(columns))
            print(f"{condition} {video_path.name}: frame {frame_index} done", flush=True)
        cap.release()
        if panels:
            cv2.imwrite(str(OUT / f"{video_path.stem}.jpg"), cv2.vconcat(panels))
    if rows:
        with (OUT / "scores.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        for condition in ("dry", "flood"):
            subset = [row for row in rows if row["condition"] == condition]
            if subset:
                print(condition, "samples", len(subset),
                      "old mean", round(float(np.mean([r["original_water_percent_road"] for r in subset])), 4),
                      "dry-tuned mean", round(float(np.mean([r["dry_tuned_water_percent_road"] for r in subset])), 4),
                      "overlap", round(float(np.mean([r["old_new_overlap_percent_old_water"] for r in subset])), 4),
                      flush=True)
        print(f"Output: {OUT}", flush=True)


if __name__ == "__main__":
    main()
