import base64
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

from app.events import EventPolicy
from app.inference import segmentation_overlay
from app.preview import PreviewSession, PreviewStart


class OverlayTests(unittest.TestCase):
    def test_water_background_and_roi_have_separate_alpha(self):
        mask = np.zeros((60, 100), np.uint8)
        mask[15:45, 25:75] = 1
        layer = segmentation_overlay(mask)
        self.assertEqual(layer.shape, (60, 100, 4))
        self.assertEqual(int(layer[5, 5, 3]), 0)
        self.assertEqual(tuple(layer[30, 50]), (245, 170, 70, 77))
        roi = np.zeros_like(mask, dtype=bool)
        roi[:, :60] = True
        layer = segmentation_overlay(mask, roi)
        self.assertEqual(tuple(layer[30, 90]), (0, 0, 0, 153))
        self.assertEqual(int(layer[5, 5, 3]), 0)
        self.assertEqual(tuple(layer[30, 50]), (245, 170, 70, 77))

    def test_large_layer_keeps_aspect_ratio_and_is_bounded(self):
        for height, width in ((720, 1280), (1280, 720)):
            layer = segmentation_overlay(np.zeros((height, width), np.uint8))
            self.assertEqual(max(layer.shape[:2]), 640)
            self.assertAlmostEqual(layer.shape[1] / layer.shape[0], width / height)
            self.assertFalse(layer.any())

    def test_preview_returns_transparent_png_without_original_upload(self):
        class Segmenter:
            def predict_mask(self, frame):
                mask = np.zeros(frame.shape[:2], np.uint8)
                mask[15:45, 25:75] = 1
                return mask, mask.astype(np.float32)

            def info(self):
                return {"backend": "test", "device": "cpu"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(output_dir=root, upload_dir=root)
            database = SimpleNamespace(start_analysis=lambda *args: None)
            metadata = PreviewStart(filename="sample.mp4", size_bytes=100,
                                    duration_sec=1, width=100, height=60, export_video=False)
            configuration = {"event_policy": EventPolicy().model_dump(), "ratio_scope": "full_frame",
                             "thresholds": {"low": .1, "moderate": .2, "critical": .45}}
            session = PreviewSession("overlay", "sample", metadata, configuration, settings, database,
                                     Segmenter, threading.Lock(), lambda _: [], lambda **_: None)
            ok, jpeg = cv2.imencode(".jpg", np.zeros((60, 100, 3), np.uint8))
            self.assertTrue(ok)
            result = session.frame(0, 0, jpeg.tobytes())
            layer = cv2.imdecode(np.frombuffer(base64.b64decode(result["overlay_png"]), np.uint8),
                                 cv2.IMREAD_UNCHANGED)
            self.assertEqual(layer.shape, (60, 100, 4))
            self.assertEqual(int(layer[5, 5, 3]), 0)
            self.assertGreater(int(layer[30, 50, 3]), 0)
            self.assertFalse(result["original_ready"])
            self.assertEqual(result["time_sec"], 0)
            self.assertTrue(result["annotated_jpeg"])
            self.assertEqual(list(session.cache_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
