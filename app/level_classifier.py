from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import cv2
import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from torchvision import models, transforms

from .config import BASE_DIR

LEVEL_ENABLED = os.getenv("LEVEL_ENABLED", "1") == "1"
default_level_path = BASE_DIR / "models" / "best_flood_level_model.pth"
if not default_level_path.exists():
    default_level_path = BASE_DIR / "models" / "flood_level_resnet18.pth"
LEVEL_MODEL_PATH = Path(os.getenv("LEVEL_MODEL_PATH", default_level_path))
LEVEL_MIN_CONF = float(os.getenv("LEVEL_MIN_CONF", "0.4"))
LEVEL_EMA_ALPHA = float(os.getenv("LEVEL_EMA_ALPHA", "0.4"))
USE_CAR_CROP = os.getenv("USE_CAR_CROP", "0") == "1"
CAR_DETECTOR_PATH = Path(os.getenv("CAR_DETECTOR_PATH", BASE_DIR / "models" / "yolov8n.pt"))
LEVEL_INFO: dict[str, dict[str, Any]] = {
    "Level0": {
        "title": "Level 0 - Không ngập (An toàn)",
        "short_title": "Level 0",
        "desc": "Mặt đường khô ráo, không có nước ngập",
        "depth": "< 5 cm",
        "severity": "SAFE",
        "color": "#55c978",
        "advisory": "Tất cả phương tiện lưu thông an toàn, điều kiện di chuyển bình thường.",
        "rank": 0,
        "ascii_title": "LEVEL 0 - AN TOAN",
        "ascii_depth": "< 5 cm",
        "ascii_desc": "Mat duong kho rao",
        "ascii_advisory": "Tat ca phuong tien luu thong an toan",
    },
    "Level1": {
        "title": "Level 1 - Ngập mép lốp xe",
        "short_title": "Level 1",
        "desc": "Nước ngập chạm mép dưới lốp xe",
        "depth": "5 - 15 cm",
        "severity": "LOW",
        "color": "#e2c44f",
        "advisory": "Xe máy và ô tô vẫn di chuyển an toàn; cần chú ý giảm tốc độ tránh trơn trượt.",
        "rank": 1,
        "ascii_title": "LEVEL 1 - MEP LOP XE",
        "ascii_depth": "5 - 15 cm",
        "ascii_desc": "Ngap cham mep duoi lop xe",
        "ascii_advisory": "Xe may & oto giam toc do, tranh tron truot",
    },
    "Level2": {
        "title": "Level 2 - Ngập nửa bánh xe",
        "short_title": "Level 2",
        "desc": "Nước dâng tới trục bánh hoặc nửa bánh xe",
        "depth": "15 - 30 cm",
        "severity": "MODERATE",
        "color": "#ee963f",
        "advisory": "Cảnh báo ngập vừa! Xe sedan gầm thấp và xe máy có nguy cơ vào nước ống xả, hạn chế đi qua.",
        "rank": 2,
        "ascii_title": "LEVEL 2 - NUA BANH XE (CANH BAO)",
        "ascii_depth": "15 - 30 cm",
        "ascii_desc": "Ngap toi truc / nua banh xe",
        "ascii_advisory": "Xe sedan & xe may nguy co vao nuoc ong xa",
    },
    "Level3": {
        "title": "Level 3 - Ngập qua bánh xe",
        "short_title": "Level 3",
        "desc": "Nước ngập vượt qua đỉnh bánh xe, chạm đáy cửa",
        "depth": "30 - 50 cm",
        "severity": "CRITICAL",
        "color": "#ef5757",
        "advisory": "Nguy hiểm cao! Cấm xe con và xe máy lưu thông, nguy cơ thủy kích và trôi xe nghiêm trọng.",
        "rank": 3,
        "ascii_title": "LEVEL 3 - QUA BANH XE (NGUY HIEM)",
        "ascii_depth": "30 - 50 cm",
        "ascii_desc": "Ngap qua dinh banh xe",
        "ascii_advisory": "CAM XE CON & XE MAY - Nguy co thuy kich!",
    },
    "Level4": {
        "title": "Level 4 - Ngập nắp capo / Khẩn cấp",
        "short_title": "Level 4",
        "desc": "Nước ngập nắp capo hoặc gần nóc xe",
        "depth": "> 50 cm",
        "severity": "CRITICAL",
        "color": "#d92525",
        "advisory": "Tình trạng khẩn cấp! Nguy hiểm trực tiếp đến tính mạng. Cần cứu hộ cứu nạn khẩn cấp!",
        "rank": 4,
        "ascii_title": "LEVEL 4 - NAP CAPO (KHAN CAP)",
        "ascii_depth": "> 50 cm",
        "ascii_desc": "Ngap nap capo / gan noc xe",
        "ascii_advisory": "KHAN CAP! Nguy hiem tinh mang, can cuu ho!",
    },
    "NO_CAR": {
        "title": "Chưa phát hiện xe",
        "short_title": "No Car",
        "desc": "Không thấy xe hoặc xe quá xa để đo mức ngập",
        "depth": "--",
        "severity": "NEUTRAL",
        "color": "#8ea2b6",
        "advisory": "Đo độ ngập dựa trên tỷ lệ diện tích phân đoạn vùng nước.",
        "rank": -1,
        "ascii_title": "NO VEHICLE DETECTED",
        "ascii_depth": "--",
        "ascii_desc": "Chua phat hien xe trong khung hinh",
        "ascii_advisory": "Uoc tinh dua tren dien tich vung nuoc",
    },
    "UNCERTAIN": {
        "title": "Đang theo dõi mức ngập",
        "short_title": "Uncertain",
        "desc": "Đang phân tích độ tin cậy của mô hình mức ngập",
        "depth": "--",
        "severity": "NEUTRAL",
        "color": "#8ea2b6",
        "advisory": "Đang thu thập và phân tích dữ liệu khung hình liên tục.",
        "rank": -1,
        "ascii_title": "ANALYZING LEVEL...",
        "ascii_depth": "--",
        "ascii_desc": "Dang phan tich do tin cay",
        "ascii_advisory": "Dang thu thap du lieu khung hinh",
    },
}


def get_level_info(level: str | None) -> dict[str, Any]:
    if not level:
        return LEVEL_INFO["NO_CAR"]
    return LEVEL_INFO.get(level, {
        "title": level,
        "short_title": level,
        "desc": "Đang theo dõi",
        "depth": "--",
        "severity": "NEUTRAL",
        "color": "#8ea2b6",
        "advisory": "",
        "rank": -1,
    })


class CarCropper:
    """Phát hiện xe (COCO: car=2, bus=5, truck=7) và crop xe lớn nhất kèm phần nền xung quanh."""

    def __init__(self, model_path: Path, conf: float = 0.3, pad: float = 0.25, min_side: int = 96):
        from ultralytics import YOLO

        self.model = YOLO(str(model_path))
        self.conf, self.pad, self.min_side = conf, pad, min_side
        self.device = 0 if torch.cuda.is_available() else "cpu"

    def crop(self, frame_bgr: np.ndarray) -> np.ndarray | None:
        h, w = frame_bgr.shape[:2]
        r = self.model.predict(frame_bgr, conf=self.conf, classes=[2, 5, 7],
                               device=self.device, verbose=False)[0]
        if r.boxes is None or len(r.boxes) == 0:
            return None
        boxes = r.boxes.xyxy.detach().cpu().numpy()
        areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        x1, y1, x2, y2 = boxes[int(areas.argmax())]
        bw, bh = x2 - x1, y2 - y1
        if min(bw, bh) < self.min_side:       # xe quá nhỏ: không đủ chi tiết bánh xe
            return None
        x1 = max(0, int(x1 - bw * self.pad)); x2 = min(w, int(x2 + bw * self.pad))
        y1 = max(0, int(y1 - bh * self.pad)); y2 = min(h, int(y2 + bh * self.pad))
        return frame_bgr[y1:y2, x1:x2]


class LevelClassifier:
    def __init__(self, ckpt_path: Path, min_conf: float = 0.5, cropper: CarCropper | None = None):
        ckpt_path = Path(ckpt_path)
        if not ckpt_path.exists():
            raise FileNotFoundError(f"Level model not found: {ckpt_path}")
        self.path = ckpt_path
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        ckpt = torch.load(ckpt_path, map_location=self.device)
        is_dict = isinstance(ckpt, dict)
        state = ckpt["model_state_dict"] if is_dict and "model_state_dict" in ckpt else ckpt
        classes = (ckpt.get("class_names") or ckpt.get("classes")) if is_dict else None
        self.classes = list(classes) if classes else [f"Level{i}" for i in range(5)]
        self.img_size = int(ckpt.get("image_size", ckpt.get("img_size", 224))) if is_dict else 224
        self.min_conf = min_conf
        self.cropper = cropper

        self.model = models.resnet18()
        self.model.fc = nn.Linear(self.model.fc.in_features, len(self.classes))
        self.model.load_state_dict(state)
        self.model.to(self.device).eval()
        # Phải giống hệt tiền xử lý lúc train/val
        self.tf = transforms.Compose([
            transforms.Resize((self.img_size, self.img_size)),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])

    @torch.no_grad()
    def predict(self, frame_bgr: np.ndarray) -> np.ndarray | None:
        """Trả về xác suất 5 lớp, hoặc None nếu bật crop mà không thấy xe đủ lớn."""
        if self.cropper is not None:
            frame_bgr = self.cropper.crop(frame_bgr)
            if frame_bgr is None:
                return None
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        x = self.tf(Image.fromarray(rgb)).unsqueeze(0).to(self.device)
        return torch.softmax(self.model(x), 1)[0].cpu().numpy()

    def decide(self, probs: np.ndarray) -> tuple[str, float]:
        i = int(np.argmax(probs))
        conf = float(probs[i])
        if conf < self.min_conf:
            return "UNCERTAIN", conf
        return self.classes[i], conf

    def info(self) -> Dict[str, Any]:
        return {
            "backend": "resnet18-level",
            "classes": self.classes,
            "img_size": self.img_size,
            "min_conf": self.min_conf,
            "car_crop": self.cropper is not None,
            "device": str(self.device),
            "model_path": str(self.path),
        }


def create_level_classifier() -> LevelClassifier:
    cropper = CarCropper(CAR_DETECTOR_PATH) if (USE_CAR_CROP and CAR_DETECTOR_PATH.exists()) else None
    return LevelClassifier(LEVEL_MODEL_PATH, LEVEL_MIN_CONF, cropper)