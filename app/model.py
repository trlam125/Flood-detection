from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Protocol

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.models import resnet34


class Segmenter(Protocol):
    backend: str
    threshold: float

    def predict_mask(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]: ...
    def info(self) -> Dict[str, Any]: ...


def resolve_device(preference: str | None = None) -> torch.device:
    preference = (preference if preference is not None else os.getenv("MODEL_DEVICE", "auto")).strip().lower()
    if preference not in {"auto", "cpu", "cuda"}:
        raise ValueError("MODEL_DEVICE must be auto, cpu or cuda")
    if preference == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("MODEL_DEVICE=cuda but CUDA is unavailable. Install a CUDA-enabled PyTorch build or use MODEL_DEVICE=auto/cpu.")
    return torch.device("cuda" if preference != "cpu" and torch.cuda.is_available() else "cpu")


def acceleration_info() -> Dict[str, Any]:
    available = torch.cuda.is_available()
    return {"requested_device": os.getenv("MODEL_DEVICE", "auto").strip().lower(),
            "cuda_available": available, "gpu_name": torch.cuda.get_device_name(0) if available else None,
            "torch_version": str(torch.__version__), "cuda_build": torch.version.cuda}


class ConvBlock(nn.Module):
    def __init__(self, in_channels: int, out_channels: int):
        super().__init__()
        self.block = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.block(x)


class DecoderBlock(nn.Module):
    def __init__(self, in_channels: int, skip_channels: int, out_channels: int):
        super().__init__()
        self.up = nn.ConvTranspose2d(in_channels, out_channels, kernel_size=2, stride=2)
        self.conv = ConvBlock(out_channels + skip_channels, out_channels)

    def forward(self, x: torch.Tensor, skip: torch.Tensor) -> torch.Tensor:
        x = self.up(x)
        if x.shape[-2:] != skip.shape[-2:]:
            x = F.interpolate(x, size=skip.shape[-2:], mode="bilinear", align_corners=False)
        return self.conv(torch.cat([x, skip], dim=1))


class UNetResNet34(nn.Module):
    def __init__(self):
        super().__init__()
        encoder = resnet34(weights=None)
        self.conv1 = encoder.conv1
        self.bn1 = encoder.bn1
        self.relu = encoder.relu
        self.maxpool = encoder.maxpool
        self.layer1 = encoder.layer1
        self.layer2 = encoder.layer2
        self.layer3 = encoder.layer3
        self.layer4 = encoder.layer4
        self.decoder4 = DecoderBlock(512, 256, 256)
        self.decoder3 = DecoderBlock(256, 128, 128)
        self.decoder2 = DecoderBlock(128, 64, 64)
        self.decoder1 = DecoderBlock(64, 64, 64)
        self.final_up = nn.ConvTranspose2d(64, 32, kernel_size=2, stride=2)
        self.final = nn.Sequential(ConvBlock(32, 32), nn.Conv2d(32, 1, kernel_size=1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        e1 = self.relu(self.bn1(self.conv1(x)))
        x = self.maxpool(e1)
        e2 = self.layer1(x)
        e3 = self.layer2(e2)
        e4 = self.layer3(e3)
        b = self.layer4(e4)
        d4 = self.decoder4(b, e4)
        d3 = self.decoder3(d4, e3)
        d2 = self.decoder2(d3, e2)
        d1 = self.decoder1(d2, e1)
        return self.final(self.final_up(d1))


class RIWASegmenter:
    backend = "riwa"

    def __init__(self, model_path: str | os.PathLike[str], device: str | None = None):
        self.model_path = Path(model_path)
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"Model not found: {self.model_path}. Put riwa_unet_resnet34_deploy.pth in models/."
            )
        self.device = resolve_device(device)
        checkpoint = torch.load(self.model_path, map_location=self.device)
        if not isinstance(checkpoint, dict) or "model_state_dict" not in checkpoint:
            raise ValueError("Expected RIWA deploy checkpoint with model_state_dict and preprocessing metadata")
        self.img_size = int(checkpoint.get("img_size", 384))
        self.threshold = float(checkpoint.get("threshold", 0.5))
        self.mean = np.array(checkpoint.get("mean", [0.485, 0.456, 0.406]), dtype=np.float32)
        self.std = np.array(checkpoint.get("std", [0.229, 0.224, 0.225]), dtype=np.float32)
        self.architecture = checkpoint.get("architecture", "UNetResNet34")
        self.metrics = {
            "val_dice": checkpoint.get("val_dice"),
            "val_iou": checkpoint.get("val_iou"),
            "test_dice": checkpoint.get("test_dice"),
            "test_iou": checkpoint.get("test_iou"),
        }
        self.model = UNetResNet34().to(self.device)
        self.model.load_state_dict(checkpoint["model_state_dict"], strict=True)
        self.model.eval()

    @torch.no_grad()
    def predict_probability(self, frame_bgr: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (self.img_size, self.img_size), interpolation=cv2.INTER_LINEAR)
        x = resized.astype(np.float32) / 255.0
        x = (x - self.mean) / self.std
        x = np.transpose(x, (2, 0, 1))
        tensor = torch.from_numpy(x.copy()).float().unsqueeze(0).to(self.device)
        prob = torch.sigmoid(self.model(tensor))[0, 0].cpu().numpy()
        return cv2.resize(prob, (frame_bgr.shape[1], frame_bgr.shape[0]), interpolation=cv2.INTER_LINEAR)

    def predict_mask(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        prob = self.predict_probability(frame_bgr)
        return _clean_binary((prob >= self.threshold).astype(np.uint8)), prob

    def info(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "architecture": self.architecture,
            "device": str(self.device),
            "device_name": torch.cuda.get_device_name(self.device) if self.device.type == "cuda" else "CPU",
            "img_size": self.img_size,
            "threshold": self.threshold,
            "model_path": str(self.model_path),
            **self.metrics,
        }


class YOLOWaterSegmenter:
    backend = "yolo"

    def __init__(self, model_path: str | os.PathLike[str], threshold: float = 0.25, device: str | None = None):
        self.model_path = Path(model_path)
        if not self.model_path.exists():
            raise FileNotFoundError(f"YOLO segmentation model not found: {self.model_path}")
        from ultralytics import YOLO

        self.model = YOLO(str(self.model_path))
        self.threshold = float(threshold)
        self.device = 0 if resolve_device(device).type == "cuda" else "cpu"

    def predict_mask(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h, w = frame_bgr.shape[:2]
        result = self.model.predict(frame_bgr, conf=self.threshold, device=self.device, verbose=False)[0]
        union = np.zeros((h, w), dtype=np.uint8)
        prob = np.zeros((h, w), dtype=np.float32)
        if result.masks is None:
            return union, prob
        masks = result.masks.data.detach().float().cpu().numpy()
        boxes = result.boxes
        confs = boxes.conf.detach().float().cpu().numpy() if boxes is not None else np.ones(len(masks), dtype=np.float32)
        for mask, conf in zip(masks, confs):
            resized = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)
            union[resized >= 0.5] = 1
            prob = np.maximum(prob, resized * float(conf))
        return _clean_binary(union), prob

    def info(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "architecture": "Ultralytics segmentation",
            "device": "cuda:0" if self.device == 0 else "cpu",
            "device_name": torch.cuda.get_device_name(0) if self.device == 0 else "CPU",
            "threshold": self.threshold,
            "model_path": str(self.model_path),
        }


def _clean_binary(mask: np.ndarray) -> np.ndarray:
    binary = (mask > 0).astype(np.uint8)
    h, w = binary.shape[:2]
    kernel = np.ones((3, 3), dtype=np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    minimum = max(64, int(h * w * 0.001))
    cleaned = np.zeros_like(binary)
    for idx in range(1, count):
        if int(stats[idx, cv2.CC_STAT_AREA]) >= minimum:
            cleaned[labels == idx] = 1
    return cleaned


def create_segmenter(backend: str, riwa_path: Path, yolo_path: Path, device: str | None = None) -> Segmenter:
    backend = backend.strip().lower()
    if backend == "riwa":
        return RIWASegmenter(riwa_path, device=device)
    if backend == "yolo":
        return YOLOWaterSegmenter(yolo_path, device=device)
    raise ValueError("MODEL_BACKEND must be 'riwa' or 'yolo'")
