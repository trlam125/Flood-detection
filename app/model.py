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
    def predict_road_mask(self, frame_bgr: np.ndarray) -> np.ndarray: ...
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
        # Prefer threshold baked into the checkpoint by the training script;
        # allow env-var override only when explicitly set by the user.
        ckpt_threshold = checkpoint.get("threshold")
        default_high = str(ckpt_threshold) if ckpt_threshold is not None else "0.55"
        self.threshold = float(os.getenv("RIWA_HIGH_THRESHOLD", default_high))
        self.low_threshold = float(os.getenv("RIWA_LOW_THRESHOLD", str(max(0.25, self.threshold - 0.15))))
        self.min_area_ratio = float(os.getenv("RIWA_MIN_AREA_RATIO", "0.001"))
        self.mean = np.array(checkpoint.get("mean", [0.485, 0.456, 0.406]), dtype=np.float32)
        self.std = np.array(checkpoint.get("std", [0.229, 0.224, 0.225]), dtype=np.float32)
        self.architecture = checkpoint.get("architecture", "UNetResNet34")
        self.metrics = {
            "val_dice": checkpoint.get("val_dice"),
            "val_iou": checkpoint.get("val_iou"),
            "iou_river": checkpoint.get("iou_river"),
            "iou_road": checkpoint.get("iou_road"),
            "iou_puddle": checkpoint.get("iou_puddle"),
            "fpr_clear": checkpoint.get("fpr_clear"),
            "fpr_rain": checkpoint.get("fpr_rain"),
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
        cleaned = _clean_binary(
            prob,
            high_thresh=self.threshold,
            low_thresh=self.low_threshold,
            min_area_ratio=self.min_area_ratio,
        )
        return cleaned, prob

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
        if result.masks is None or len(result.masks) == 0:
            return union, prob
        masks = result.masks.data.detach().float().cpu().numpy()
        boxes = result.boxes
        confs = boxes.conf.detach().float().cpu().numpy() if boxes is not None else np.ones(len(masks), dtype=np.float32)
        for mask, conf in zip(masks, confs):
            resized = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)
            union[resized >= 0.5] = 1
            prob = np.maximum(prob, resized * float(conf))
        if not union.any():
            return union, prob
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


class CityscapesRoadSegmenter:
    """Pretrained SegFormer semantic road mask for each frame."""
    backend = "segformer-road"

    def __init__(self, model_id: str, device: torch.device):
        try:
            from transformers import SegformerForSemanticSegmentation, SegformerImageProcessor
        except ImportError as exc:
            raise RuntimeError("Road detection requires transformers; install project requirements.txt") from exc
        self.model_id = model_id
        self.device = device
        try:
            self.processor = SegformerImageProcessor.from_pretrained(model_id, local_files_only=True)
            self.model = SegformerForSemanticSegmentation.from_pretrained(model_id, local_files_only=True).to(device)
        except OSError:
            self.processor = SegformerImageProcessor.from_pretrained(model_id)
            self.model = SegformerForSemanticSegmentation.from_pretrained(model_id).to(device)
        self.model.eval()
        labels = self.model.config.id2label
        self.road_class_id = next(
            (int(key) for key, value in labels.items()
             if any(term in str(value).strip().lower() for term in ["road", "route"]) and "railroad" not in str(value).strip().lower()),
            None
        )
        if self.road_class_id is None:
            raise ValueError(f"Road class is missing from semantic model {model_id}")
        # Road-adjacent classes that should be part of road corridor
        self.sidewalk_class_ids = [
            int(key) for key, value in labels.items()
            if any(term in str(value).strip().lower() for term in ["sidewalk", "pavement", "path"])
        ]

    @torch.inference_mode()
    def predict_road_mask(self, frame_bgr: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        original_height, original_width = rgb.shape[:2]
        size = self.processor.size
        target_height = int(size.get("height", 640)) if isinstance(size, dict) else int(size)
        target_width = int(size.get("width", target_height)) if isinstance(size, dict) else int(size)
        scale = min(target_width / original_width, target_height / original_height)
        resized_width = max(1, round(original_width * scale))
        resized_height = max(1, round(original_height * scale))
        resized = cv2.resize(rgb, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
        left = (target_width - resized_width) // 2
        top = (target_height - resized_height) // 2
        canvas = np.zeros((target_height, target_width, 3), dtype=np.uint8)
        canvas[top:top + resized_height, left:left + resized_width] = resized
        inputs = self.processor(images=canvas, return_tensors="pt")
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        logits = self.model(**inputs).logits
        logits = F.interpolate(logits, size=(target_height, target_width), mode="bilinear", align_corners=False)
        # Use softmax probabilities instead of argmax so that road pixels
        # covered by water (where "water" class wins argmax) are still detected
        # when road probability is above a low corridor threshold.
        probs = F.softmax(logits, dim=1)[0]  # shape: (n_classes, H, W)
        road_prob = probs[self.road_class_id]
        # Include sidewalk/pavement classes in road corridor
        for cid in self.sidewalk_class_ids:
            road_prob = torch.maximum(road_prob, probs[cid])
        road_crop = road_prob[top:top + resized_height, left:left + resized_width].cpu().numpy()
        road_full = cv2.resize(road_crop, (original_width, original_height), interpolation=cv2.INTER_LINEAR)
        # Low threshold (0.15) keeps road pixels even when water overlaps them
        return (road_full >= 0.15).astype(bool)

    def info(self) -> Dict[str, Any]:
        return {"backend": self.backend, "model_id": self.model_id,
                "class": "road", "device": str(self.device)}


class RIWAWithRoadSegmenter:
    """RIWA detects water; SegFormer supplies the road-area denominator.
    Road mask is expanded to include RIWA-detected water pixels on the road,
    so flooded sections are not excluded from the road corridor.
    """
    backend = "riwa+segformer-road"

    def __init__(self, water: RIWASegmenter, road: CityscapesRoadSegmenter):
        self.water = water
        self.road = road
        self.threshold = water.threshold
        self.device = water.device
        # Cache last water mask to expand road corridor without double inference
        self._last_water_mask: np.ndarray | None = None
        self._last_water_prob: np.ndarray | None = None
        self._cached_road_mask: np.ndarray | None = None
        self._cached_road_frame_id: int = -1
        self._frame_counter: int = 0

    def predict_mask(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mask, prob = self.water.predict_mask(frame_bgr)
        self._last_water_mask = mask
        self._last_water_prob = prob
        self._cached_road_mask = None  # invalidate road cache for new frame
        self._frame_counter += 1
        return mask, prob

    def predict_road_mask(self, frame_bgr: np.ndarray) -> np.ndarray:
        # Return cached road mask if predict_mask was already called this frame
        if self._cached_road_mask is not None:
            return self._cached_road_mask
        road = self.road.predict_road_mask(frame_bgr)
        # Expand road mask: include any pixel where RIWA is confident of water
        # (water-covered road would otherwise be invisible to SegFormer road class)
        if self._last_water_mask is not None and self._last_water_mask.shape == road.shape:
            road = road | (self._last_water_mask > 0)
        elif self._last_water_prob is not None and self._last_water_prob.shape == road.shape:
            # Even borderline water pixels above low threshold → part of road corridor
            road = road | (self._last_water_prob >= self.water.low_threshold)
        self._cached_road_mask = road
        return road

    def info(self) -> Dict[str, Any]:
        return {"backend": self.backend, "device": str(self.device),
                "water_model": self.water.info(), "road_model": self.road.info()}


def _clean_binary(
    mask_or_prob: np.ndarray,
    high_thresh: float = 0.65,
    low_thresh: float = 0.45,
    min_area_ratio: float = 0.003,
) -> np.ndarray:
    """Filter water mask using Hysteresis thresholding and morphological cleanup."""
    h, w = mask_or_prob.shape[:2]

    # Support both float probability map (for Hysteresis) and binary mask
    if np.issubdtype(mask_or_prob.dtype, np.floating):
        candidate = (mask_or_prob >= low_thresh).astype(np.uint8)
        sure_water = (mask_or_prob >= high_thresh).astype(np.uint8)
    else:
        candidate = (mask_or_prob > 0).astype(np.uint8)
        sure_water = candidate

    # Elliptical kernel removes thin linear artifacts (road markings, shadow borders)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    candidate = cv2.morphologyEx(candidate, cv2.MORPH_OPEN, kernel, iterations=1)
    candidate = cv2.morphologyEx(candidate, cv2.MORPH_CLOSE, kernel, iterations=1)

    count, labels, stats, _ = cv2.connectedComponentsWithStats(candidate, connectivity=8)
    minimum = max(96, int(h * w * min_area_ratio))
    cleaned = np.zeros_like(candidate)

    for idx in range(1, count):
        if int(stats[idx, cv2.CC_STAT_AREA]) >= minimum:
            # Keep candidate region only if it contains confident core water pixels
            if np.any(sure_water[labels == idx]):
                cleaned[labels == idx] = 1

    return cleaned


class SegFormerRoadWaterSegmenter:
    """Unified SegFormer model from Hugging Face detecting road and water simultaneously."""
    backend = "segformer-ade"

    def __init__(self, model_id: str = "nvidia/segformer-b5-finetuned-ade-640-640",
                 device: torch.device | None = None,
                 threshold: float = 0.35):
        from transformers import SegformerForSemanticSegmentation, SegformerImageProcessor

        self.model_id = model_id
        self.device = resolve_device(None) if device is None else device
        self.threshold = float(os.getenv("WATER_THRESHOLD", "0.22"))

        try:
            self.processor = SegformerImageProcessor.from_pretrained(model_id, local_files_only=True)
            self.model = SegformerForSemanticSegmentation.from_pretrained(model_id, local_files_only=True).to(self.device)
        except OSError:
            self.processor = SegformerImageProcessor.from_pretrained(model_id)
            self.model = SegformerForSemanticSegmentation.from_pretrained(model_id).to(self.device)

        self.model.eval()
        labels = self.model.config.id2label

        self.road_class_ids = [
            int(k) for k, v in labels.items()
            if any(term in str(v).lower() for term in ["road", "route"]) and "railroad" not in str(v).lower()
        ]
        self.water_class_ids = [
            int(k) for k, v in labels.items()
            if any(term in str(v).lower() for term in ["water", "river", "lake", "sea", "puddle", "flood"])
        ]
        self._last_road_mask: np.ndarray | None = None

    @torch.inference_mode()
    def _predict(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        orig_h, orig_w = rgb.shape[:2]

        size = self.processor.size
        target_h = int(size.get("height", 640)) if isinstance(size, dict) else int(size)
        target_w = int(size.get("width", target_h)) if isinstance(size, dict) else int(size)

        scale = min(target_w / orig_w, target_h / orig_h)
        resized_w = max(1, round(orig_w * scale))
        resized_h = max(1, round(orig_h * scale))
        resized = cv2.resize(rgb, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR)

        left = (target_w - resized_w) // 2
        top = (target_h - resized_h) // 2
        canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
        canvas[top:top + resized_h, left:left + resized_w] = resized

        inputs = self.processor(images=canvas, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        logits = self.model(**inputs).logits
        logits = F.interpolate(logits, size=(target_h, target_w), mode="bilinear", align_corners=False)
        probs = F.softmax(logits, dim=1)[0]

        road_prob = torch.zeros((target_h, target_w), device=self.device)
        for cid in self.road_class_ids:
            road_prob = torch.maximum(road_prob, probs[cid])

        water_prob = torch.zeros((target_h, target_w), device=self.device)
        for cid in self.water_class_ids:
            water_prob = torch.maximum(water_prob, probs[cid])

        road_crop = road_prob[top:top + resized_h, left:left + resized_w].cpu().numpy()
        water_crop = water_prob[top:top + resized_h, left:left + resized_w].cpu().numpy()

        road_full = cv2.resize(road_crop, (orig_w, orig_h), interpolation=cv2.INTER_LINEAR)
        water_full = cv2.resize(water_crop, (orig_w, orig_h), interpolation=cv2.INTER_LINEAR)

        # In ADE20K 150 classes, softmax probabilities for water/puddle typically peak around 0.20-0.45.
        # Use lower hysteresis thresholds suitable for multi-class softmax.
        water_mask = _clean_binary(
            water_full,
            high_thresh=self.threshold,
            low_thresh=max(0.10, self.threshold - 0.10),
            min_area_ratio=0.002,
        )

        # Full road corridor encompasses both dry pavement AND water-covered road
        road_corridor = (road_full >= 0.20) | (water_mask > 0)
        self._last_road_mask = road_corridor
        return water_mask, water_full

    def predict_mask(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self._predict(frame_bgr)

    def predict_road_mask(self, frame_bgr: np.ndarray) -> np.ndarray:
        if self._last_road_mask is None or self._last_road_mask.shape != frame_bgr.shape[:2]:
            self._predict(frame_bgr)
        return self._last_road_mask

    def info(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "architecture": "SegFormer Unified (ADE20K)",
            "model_id": self.model_id,
            "device": str(self.device),
            "threshold": self.threshold,
        }


class YOLOWithRoadSegmenter:
    """Hybrid: YOLO puddle model for water (only when water exists) + SegFormer-B5 for road (every frame)."""
    backend = "yolo"

    def __init__(self, water: YOLOWaterSegmenter, road: CityscapesRoadSegmenter):
        self.water = water
        self.road = road
        self.threshold = water.threshold
        self.device = resolve_device(None)

    def predict_mask(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self.water.predict_mask(frame_bgr)

    def predict_road_mask(self, frame_bgr: np.ndarray) -> np.ndarray:
        # Run SegFormer-B5 on every single frame as requested
        return self.road.predict_road_mask(frame_bgr)

    def info(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "architecture": "YOLO11 Puddle + SegFormer Road",
            "device": str(self.device),
            "water_model": self.water.info(),
            "road_model": self.road.info(),
        }


def create_segmenter(backend: str, riwa_path: Path, yolo_path: Path, device: str | None = None,
                     road_model_id: str | None = None,
                     ade_model_id: str | None = None) -> Segmenter:
    backend = backend.strip().lower()
    if backend in {"yolo", "yolo-water", "hybrid"}:
        water = YOLOWaterSegmenter(yolo_path, device=device)
        road = CityscapesRoadSegmenter(road_model_id or "nvidia/segformer-b5-finetuned-ade-640-640", resolve_device(device))
        return YOLOWithRoadSegmenter(water, road)
    if backend in {"segformer", "segformer-ade", "ade"}:
        return SegFormerRoadWaterSegmenter(model_id=ade_model_id or "nvidia/segformer-b5-finetuned-ade-640-640",
                                           device=resolve_device(device))
    if backend == "riwa":
        water = RIWASegmenter(riwa_path, device=device)
        road = CityscapesRoadSegmenter(road_model_id or "nvidia/segformer-b5-finetuned-ade-640-640", water.device)
        return RIWAWithRoadSegmenter(water, road)
    raise ValueError("MODEL_BACKEND must be 'yolo', 'segformer-ade' or 'riwa'")
