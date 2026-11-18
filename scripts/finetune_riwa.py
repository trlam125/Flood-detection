"""Conservatively adapt RIWA to labelled no-flood footage.

The original deployment checkpoint is always preserved. This script samples
frames from the three supplied clips, labels monitored pixels as dry, updates
only the decoder at a low learning rate, and saves a separate checkpoint only
when held-out dry-frame false-positive area improves.
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.model import UNetResNet34

SOURCE_VIDEOS = [
    Path(r"D:\Zalo Data\media1\media\15157-262146024_tiny.mp4"),
    Path(r"D:\Zalo Data\media1\media\15362801_2160_3840_30fps.mp4"),
    Path(r"D:\Zalo Data\media1\media\17464691_3840_2160_60fps.mp4"),
]
SOURCE_CHECKPOINT = ROOT / "models" / "riwa_unet_resnet34_deploy.pth"
OUTPUT_CHECKPOINT = ROOT / "models" / "riwa_no_flood_finetuned.pth"
FRAME_SIZE = 384
FRAMES_PER_VIDEO = 72
BATCH_SIZE = 2
EPOCHS = 4
LEARNING_RATE = 1e-5
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
SEED = 20261118


def extract_frames(path: Path, limit: int) -> list[np.ndarray]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open source video: {path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total < 1:
        cap.release()
        raise RuntimeError(f"Video has no readable frames: {path}")
    start, stop = int(total * .05), max(int(total * .95), 1)
    indices = np.unique(np.linspace(start, stop - 1, min(limit, stop - start), dtype=int))
    frames = []
    for index in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(index))
        ok, frame = cap.read()
        if ok and frame is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(cv2.resize(rgb, (FRAME_SIZE, FRAME_SIZE), interpolation=cv2.INTER_AREA))
    cap.release()
    if len(frames) < 8:
        raise RuntimeError(f"Only {len(frames)} frames decoded from {path}")
    return frames


class DryFrames(Dataset):
    def __init__(self, frames: list[np.ndarray], mean, std, augment=False):
        self.frames = frames
        self.mean = np.asarray(mean, dtype=np.float32)
        self.std = np.asarray(std, dtype=np.float32)
        self.augment = augment

    def __len__(self):
        return len(self.frames)

    def __getitem__(self, index):
        image = self.frames[index].copy()
        if self.augment and random.random() < .5:
            image = image[:, ::-1].copy()
        if self.augment and random.random() < .5:
            image = np.clip(image.astype(np.float32) * random.uniform(.85, 1.15), 0, 255).astype(np.uint8)
        tensor = (image.astype(np.float32) / 255.0 - self.mean) / self.std
        return torch.from_numpy(tensor.transpose(2, 0, 1).copy())


@torch.no_grad()
def false_positive_area(model, loader, threshold):
    model.eval()
    predicted = pixels = 0
    for images in loader:
        probabilities = torch.sigmoid(model(images.to(DEVICE)))
        predicted += int((probabilities >= threshold).sum().item())
        pixels += int(probabilities.numel())
    return predicted / max(pixels, 1)


def train():
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    if not SOURCE_CHECKPOINT.exists():
        raise FileNotFoundError(f"RIWA deployment checkpoint not found: {SOURCE_CHECKPOINT}")
    for path in SOURCE_VIDEOS:
        if not path.exists():
            raise FileNotFoundError(f"No-flood training video not found: {path}")

    print(f"Device: {DEVICE}")
    checkpoint = torch.load(SOURCE_CHECKPOINT, map_location=DEVICE)
    mean = checkpoint.get("mean", [.485, .456, .406])
    std = checkpoint.get("std", [.229, .224, .225])
    threshold = float(checkpoint.get("threshold", .55))
    groups = []
    for path in SOURCE_VIDEOS:
        frames = extract_frames(path, FRAMES_PER_VIDEO)
        groups.append(frames)
        print(f"{path.name}: decoded {len(frames)} dry frames")

    # Hold out a chronological tail from each video to reduce near-duplicate leakage.
    train_frames, validation_frames = [], []
    for frames in groups:
        split = max(1, int(len(frames) * .8))
        train_frames.extend(frames[:split])
        validation_frames.extend(frames[split:])
    train_set = DryFrames(train_frames, mean, std, augment=True)
    val_set = DryFrames(validation_frames, mean, std)
    train_loader = DataLoader(train_set, batch_size=BATCH_SIZE, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_set, batch_size=BATCH_SIZE, shuffle=False, num_workers=0)

    model = UNetResNet34().to(DEVICE)
    model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    for name, parameter in model.named_parameters():
        parameter.requires_grad = not name.startswith(("conv1", "bn1", "layer1", "layer2", "layer3", "layer4"))
    optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=LEARNING_RATE, weight_decay=1e-4)
    loss_fn = nn.BCEWithLogitsLoss()
    baseline_area = false_positive_area(model, val_loader, threshold)
    print(f"Held-out dry false-positive area before fine-tuning: {baseline_area:.6%}")

    best_area = baseline_area
    best_state = None
    for epoch in range(1, EPOCHS + 1):
        model.train()
        started = time.time()
        losses = []
        for images in train_loader:
            images = images.to(DEVICE)
            optimizer.zero_grad(set_to_none=True)
            logits = model(images)
            loss = loss_fn(logits, torch.zeros_like(logits))
            loss.backward()
            optimizer.step()
            losses.append(float(loss.item()))
        area = false_positive_area(model, val_loader, threshold)
        print(f"Epoch {epoch}/{EPOCHS}: loss={np.mean(losses):.5f}, held-out dry FP={area:.6%}, {time.time()-started:.1f}s")
        if area < best_area:
            best_area = area
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}

    if best_state is None:
        print("No improvement on held-out dry frames; RIWA deployment checkpoint remains unchanged.")
        return
    output = dict(checkpoint)
    output.update({
        "model_state_dict": best_state,
        "finetuned": True,
        "finetune_objective": "reduce dry-frame false-positive area",
        "finetune_source_videos": [str(path) for path in SOURCE_VIDEOS],
        "finetune_train_frames": len(train_set),
        "finetune_validation_frames": len(val_set),
        "dry_validation_fp_before": baseline_area,
        "dry_validation_fp_after": best_area,
        "finetune_epochs": EPOCHS,
    })
    torch.save(output, OUTPUT_CHECKPOINT)
    print(f"Saved improved RIWA checkpoint: {OUTPUT_CHECKPOINT}")
    print("The deployment checkpoint was preserved; review the new checkpoint before selecting it in RIWA_MODEL_PATH.")


if __name__ == "__main__":
    train()
