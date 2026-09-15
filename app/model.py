from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.models import resnet34


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
        x = torch.cat([x, skip], dim=1)
        return self.conv(x)


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
        self.final = nn.Sequential(
            ConvBlock(32, 32),
            nn.Conv2d(32, 1, kernel_size=1),
        )

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
    def __init__(self, model_path: str | os.PathLike[str]):
        self.model_path = Path(model_path)
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"Model not found: {self.model_path}. Put riwa_unet_resnet34_deploy.pth in models/."
            )

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        checkpoint = torch.load(self.model_path, map_location=self.device)

        if not isinstance(checkpoint, dict) or "model_state_dict" not in checkpoint:
            raise ValueError(
                "Expected deploy checkpoint containing model_state_dict, img_size, threshold, mean and std."
            )

        self.img_size = int(checkpoint.get("img_size", 384))
        self.threshold = float(checkpoint.get("threshold", 0.5))
        self.mean = np.array(checkpoint.get("mean", [0.485, 0.456, 0.406]), dtype=np.float32)
        self.std = np.array(checkpoint.get("std", [0.229, 0.224, 0.225]), dtype=np.float32)
        self.val_dice = checkpoint.get("val_dice")
        self.val_iou = checkpoint.get("val_iou")
        self.test_dice = checkpoint.get("test_dice")
        self.test_iou = checkpoint.get("test_iou")
        self.architecture = checkpoint.get("architecture", "UNetResNet34")

        self.model = UNetResNet34().to(self.device)
        self.model.load_state_dict(checkpoint["model_state_dict"], strict=True)
        self.model.eval()

    @torch.no_grad()
    def predict_probability(self, frame_bgr: np.ndarray) -> np.ndarray:
        import cv2

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (self.img_size, self.img_size), interpolation=cv2.INTER_LINEAR)
        x = resized.astype(np.float32) / 255.0
        x = (x - self.mean) / self.std
        x = np.transpose(x, (2, 0, 1))
        x = torch.from_numpy(x.copy()).float().unsqueeze(0).to(self.device)
        logits = self.model(x)
        prob = torch.sigmoid(logits)[0, 0].cpu().numpy()
        prob = cv2.resize(prob, (frame_bgr.shape[1], frame_bgr.shape[0]), interpolation=cv2.INTER_LINEAR)
        return prob

    def predict_mask(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        import cv2

        prob = self.predict_probability(frame_bgr)
        mask = (prob >= self.threshold).astype(np.uint8)

        # Remove tiny isolated blobs while preserving main water regions.
        kernel = np.ones((3, 3), dtype=np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        min_area = max(64, int(mask.size * 0.001))
        cleaned = np.zeros_like(mask)
        for i in range(1, count):
            if stats[i, cv2.CC_STAT_AREA] >= min_area:
                cleaned[labels == i] = 1

        return cleaned, prob

    def info(self) -> Dict[str, Any]:
        return {
            "architecture": self.architecture,
            "device": str(self.device),
            "img_size": self.img_size,
            "threshold": self.threshold,
            "mean": self.mean.tolist(),
            "std": self.std.tolist(),
            "val_dice": self.val_dice,
            "val_iou": self.val_iou,
            "test_dice": self.test_dice,
            "test_iou": self.test_iou,
            "model_path": str(self.model_path),
        }
