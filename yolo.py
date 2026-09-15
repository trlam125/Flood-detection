import math
import os
import subprocess
import threading

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import LineString, Polygon
from ultralytics import YOLO

_MODEL = None
_MODEL_PATH = None
_MODEL_LOCK = threading.Lock()


def _load_font(size=24):
    candidates = [
        "arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _get_model(model_path):
    global _MODEL, _MODEL_PATH
    model_path = os.path.abspath(model_path)
    with _MODEL_LOCK:
        if _MODEL is None or _MODEL_PATH != model_path:
            _MODEL = YOLO(model_path)
            _MODEL_PATH = model_path
    return _MODEL


def _intersection_candidates(geometry):
    if geometry is None or geometry.is_empty:
        return []

    geom_type = geometry.geom_type
    if geom_type == "Point":
        return [(geometry.x, geometry.y)]
    if geom_type == "MultiPoint":
        return [(point.x, point.y) for point in geometry.geoms]
    if geom_type == "LineString":
        coords = list(geometry.coords)
        return [coords[0], coords[-1]] if coords else []
    if geom_type == "MultiLineString":
        points = []
        for line in geometry.geoms:
            coords = list(line.coords)
            if coords:
                points.extend([coords[0], coords[-1]])
        return points
    if geom_type == "GeometryCollection":
        points = []
        for geom in geometry.geoms:
            points.extend(_intersection_candidates(geom))
        return points
    return []


def _find_nearest_intersection(annotation, line, start_point):
    intersection = annotation.intersection(line)
    candidates = _intersection_candidates(intersection)
    if not candidates:
        return None

    sx, sy = start_point
    return min(candidates, key=lambda p: math.hypot(p[0] - sx, p[1] - sy))


def _browser_compatible_mp4(source_path, target_path):
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                source_path,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-an",
                target_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return target_path
    except Exception:
        if source_path != target_path:
            try:
                os.replace(source_path, target_path)
            except OSError:
                return source_path
        return target_path


def _status_for_level(water_level, low_level, medium_level, high_level):
    if water_level >= high_level:
        return "HIGH", (255, 70, 70)
    if water_level >= medium_level:
        return "MEDIUM", (255, 165, 50)
    if water_level >= low_level:
        return "LOW", (255, 220, 60)
    return "NORMAL", (80, 220, 120)


def _draw_panel(draw, width, height, lines, status_color):
    # Keep the overlay compact on portrait videos and large frames alike.
    font_size = max(16, min(28, int(height * 0.026)))
    font = _load_font(font_size)
    small_font = _load_font(max(14, font_size - 3))

    pad = max(8, int(font_size * 0.45))
    line_gap = max(4, int(font_size * 0.20))
    x = max(10, int(width * 0.025))
    y = max(10, int(height * 0.025))

    boxes = []
    panel_w = 0
    panel_h = pad
    for idx, text in enumerate(lines):
        active_font = font if idx == 0 else small_font
        bbox = draw.textbbox((0, 0), text, font=active_font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        boxes.append((text, active_font, tw, th))
        panel_w = max(panel_w, tw)
        panel_h += th + line_gap
    panel_w += pad * 2
    panel_h += pad - line_gap

    draw.rounded_rectangle(
        [x, y, x + panel_w, y + panel_h],
        radius=max(6, pad),
        fill=(10, 18, 30, 205),
        outline=status_color,
        width=max(2, int(font_size * 0.10)),
    )

    cy = y + pad
    for idx, (text, active_font, _, th) in enumerate(boxes):
        color = status_color if idx == 0 else (245, 248, 252)
        draw.text((x + pad, cy), text, font=active_font, fill=color)
        cy += th + line_gap


def process_image(
    image_path,
    output_path,
    first_x=None,
    first_y=None,
    second_x=None,
    second_y=None,
    pixels_per_meter=200.0,
    tip_height=3.0,
    low_level=0.20,
    medium_level=0.50,
    high_level=0.80,
    auto_line=True,
    model_path="best.pt",
    confidence=0.25,
    iou=0.7,
    imgsz=640,
):
    if pixels_per_meter <= 0:
        raise ValueError("Pixels per meter must be greater than 0.")
    if not (low_level < medium_level < high_level):
        raise ValueError("Alert levels must satisfy LOW < MEDIUM < HIGH.")
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Model not found: {model_path}")

    frame = cv2.imread(image_path)
    if frame is None:
        raise RuntimeError(f"Could not open image: {image_path}")

    height, width = frame.shape[:2]
    if auto_line:
        first_x, first_y = width * 0.50, height * 0.10
        second_x, second_y = width * 0.50, height * 0.90
    else:
        coords = [first_x, first_y, second_x, second_y]
        if any(value is None for value in coords):
            raise ValueError("Manual measurement-line coordinates are missing.")
        if not (0 <= first_x < width and 0 <= second_x < width and 0 <= first_y < height and 0 <= second_y < height):
            raise ValueError(
                f"Measurement line is outside the image. Image size is {width}x{height}; "
                f"received ({first_x:.0f},{first_y:.0f}) -> ({second_x:.0f},{second_y:.0f})."
            )

    model = _get_model(model_path)
    result = model(frame, conf=confidence, iou=iou, imgsz=imgsz, verbose=False)[0]
    try:
        annotated = result.plot(labels=False, boxes=False, conf=False)
    except TypeError:
        annotated = result.plot()

    rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
    pil_image = Image.fromarray(rgb).convert("RGBA")
    overlay = Image.new("RGBA", pil_image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    line_width = max(2, int(min(width, height) * 0.004))
    draw.line([(first_x, first_y), (second_x, second_y)], fill=(50, 220, 255, 230), width=line_width)

    measurement_line = LineString([(first_x, first_y), (second_x, second_y)])
    masks = result.masks
    has_water_mask = masks is not None and len(masks.xyn) > 0
    intersections = []
    best_confidence = None

    if result.boxes is not None and result.boxes.conf is not None and len(result.boxes.conf):
        best_confidence = float(result.boxes.conf.max().item())

    if has_water_mask:
        for segment in masks.xyn:
            if segment is None or len(segment) < 3:
                continue
            vertices = [(float(p[0]) * width, float(p[1]) * height) for p in segment]
            polygon = Polygon(vertices)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if polygon.is_empty:
                continue
            point = _find_nearest_intersection(polygon, measurement_line, (first_x, first_y))
            if point is not None:
                intersections.append(point)

    water_level = None
    status = "NO WATER"
    if intersections:
        ix, iy = min(intersections, key=lambda p: math.hypot(p[0] - first_x, p[1] - first_y))
        water_level = max(0.0, float(tip_height - (math.hypot(ix - first_x, iy - first_y) / pixels_per_meter)))
        water_level = round(water_level, 2)
        status, status_color = _status_for_level(water_level, low_level, medium_level, high_level)
        draw.line([(first_x, first_y), (ix, iy)], fill=(*status_color, 255), width=line_width + 2)
        r = max(4, line_width * 2)
        draw.ellipse([ix-r, iy-r, ix+r, iy+r], fill=(*status_color, 255))
        _draw_panel(draw, width, height, [
            f"STATUS: {status}",
            f"Water level: {water_level:.2f} m",
            f"Confidence: {best_confidence:.2f}" if best_confidence is not None else "Confidence: -",
        ], status_color)
    elif has_water_mask:
        status = "WATER DETECTED"
        _draw_panel(draw, width, height, [
            "STATUS: WATER DETECTED",
            "No level: measurement line does not cross water",
            "Use Auto line or move the manual line",
        ], (255, 210, 60))
    else:
        _draw_panel(draw, width, height, ["STATUS: NO WATER", "No water mask detected"], (80, 220, 120))

    composed = Image.alpha_composite(pil_image, overlay).convert("RGB")
    out_frame = cv2.cvtColor(np.array(composed), cv2.COLOR_RGB2BGR)
    if not cv2.imwrite(output_path, out_frame):
        raise RuntimeError("Could not save output image.")

    return {
        "output_path": output_path,
        "water_detected": bool(has_water_mask),
        "confidence": best_confidence,
        "water_level": water_level,
        "status": status,
        "measurement_line": {
            "start": [round(first_x, 1), round(first_y, 1)],
            "end": [round(second_x, 1), round(second_y, 1)],
        },
        "image_size": [width, height],
    }


def process_video(
    video_path,
    output_path,
    first_x=None,
    first_y=None,
    second_x=None,
    second_y=None,
    pixels_per_meter=200.0,
    tip_height=3.0,
    low_level=0.20,
    medium_level=0.50,
    high_level=0.80,
    auto_line=True,
    model_path="best.pt",
    confidence=0.25,
    iou=0.7,
    imgsz=640,
    smoothing=0.35,
    progress_callback=None,
):
    if pixels_per_meter <= 0:
        raise ValueError("Pixels per meter must be greater than 0.")
    if not (low_level < medium_level < high_level):
        raise ValueError("Alert levels must satisfy LOW < MEDIUM < HIGH.")
    if not (0.0 < confidence <= 1.0):
        raise ValueError("Confidence must be between 0 and 1.")
    if not (0.0 < iou <= 1.0):
        raise ValueError("IoU must be between 0 and 1.")
    if imgsz < 320 or imgsz > 2048:
        raise ValueError("Image size must be between 320 and 2048.")
    if not (0.0 < smoothing <= 1.0):
        raise ValueError("Smoothing must be between 0 and 1.")
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Model not found: {model_path}")

    model = _get_model(model_path)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if width <= 0 or height <= 0:
        cap.release()
        raise RuntimeError("Could not read video dimensions.")

    if auto_line:
        # A resolution-independent line through the center of the scene.
        first_x = width * 0.50
        first_y = height * 0.10
        second_x = width * 0.50
        second_y = height * 0.90
    else:
        coords = [first_x, first_y, second_x, second_y]
        if any(value is None for value in coords):
            cap.release()
            raise ValueError("Manual measurement-line coordinates are missing.")
        if not (0 <= first_x < width and 0 <= second_x < width and 0 <= first_y < height and 0 <= second_y < height):
            cap.release()
            raise ValueError(
                f"Measurement line is outside the video. Video size is {width}x{height}; "
                f"received ({first_x:.0f},{first_y:.0f}) -> ({second_x:.0f},{second_y:.0f})."
            )

    raw_output = os.path.splitext(output_path)[0] + "_raw.mp4"
    writer = cv2.VideoWriter(
        raw_output,
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        cap.release()
        raise RuntimeError("Could not create output video.")

    measurement_line = LineString([(first_x, first_y), (second_x, second_y)])
    frame_index = 0
    water_frames = 0
    measured_frames = 0
    alert_frames = 0
    low_frames = 0
    medium_frames = 0
    high_frames = 0
    max_water_level = None
    smoothed_level = None

    try:
        while cap.isOpened():
            success, frame = cap.read()
            if not success:
                break

            results = model(frame, conf=confidence, iou=iou, imgsz=imgsz, verbose=False)
            result = results[0]

            # Keep the segmentation mask but remove the large YOLO box/label overlay.
            try:
                annotated = result.plot(labels=False, boxes=False, conf=False)
            except TypeError:
                annotated = result.plot()

            rgb = cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB)
            pil_image = Image.fromarray(rgb).convert("RGBA")
            overlay = Image.new("RGBA", pil_image.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            # Measurement line: cyan. This makes it obvious what must intersect the water mask.
            line_width = max(2, int(min(width, height) * 0.004))
            draw.line(
                [(first_x, first_y), (second_x, second_y)],
                fill=(50, 220, 255, 230),
                width=line_width,
            )

            masks = result.masks
            has_water_mask = masks is not None and len(masks.xyn) > 0
            intersections = []

            if has_water_mask:
                water_frames += 1
                for segment in masks.xyn:
                    if segment is None or len(segment) < 3:
                        continue
                    vertices = [
                        (float(point[0]) * frame.shape[1], float(point[1]) * frame.shape[0])
                        for point in segment
                    ]
                    polygon = Polygon(vertices)
                    if not polygon.is_valid:
                        polygon = polygon.buffer(0)
                    if polygon.is_empty:
                        continue

                    point = _find_nearest_intersection(
                        polygon, measurement_line, (first_x, first_y)
                    )
                    if point is not None:
                        intersections.append(point)

            if intersections:
                measured_frames += 1
                ix, iy = min(
                    intersections,
                    key=lambda p: math.hypot(p[0] - first_x, p[1] - first_y),
                )
                water_level = float(
                    tip_height - (math.hypot(ix - first_x, iy - first_y) / pixels_per_meter)
                )
                water_level = max(0.0, water_level)
                smoothed_level = water_level if smoothed_level is None else (
                    smoothing * water_level + (1.0 - smoothing) * smoothed_level
                )
                water_level = round(smoothed_level, 2)
                if max_water_level is None or water_level > max_water_level:
                    max_water_level = water_level

                status, status_color = _status_for_level(
                    water_level, low_level, medium_level, high_level
                )
                if status == "LOW":
                    low_frames += 1
                    alert_frames += 1
                elif status == "MEDIUM":
                    medium_frames += 1
                    alert_frames += 1
                elif status == "HIGH":
                    high_frames += 1
                    alert_frames += 1

                # Highlight only the measured part of the line.
                draw.line(
                    [(first_x, first_y), (ix, iy)],
                    fill=(*status_color, 255),
                    width=line_width + 2,
                )
                r = max(4, line_width * 2)
                draw.ellipse([ix - r, iy - r, ix + r, iy + r], fill=(*status_color, 255))

                _draw_panel(
                    draw,
                    width,
                    height,
                    [
                        f"STATUS: {status}",
                        f"Water level: {water_level:.2f} m",
                        f"Thresholds: {low_level:.2f} / {medium_level:.2f} / {high_level:.2f} m",
                    ],
                    status_color,
                )
            elif has_water_mask:
                # Do not call this SAFE: water was detected, but the configured line did not cross it.
                _draw_panel(
                    draw,
                    width,
                    height,
                    [
                        "STATUS: WATER DETECTED",
                        "No level: measurement line does not cross water",
                        "Use Auto line or move the manual line",
                    ],
                    (255, 210, 60),
                )
            else:
                _draw_panel(
                    draw,
                    width,
                    height,
                    ["STATUS: NO WATER", "No water mask detected"],
                    (80, 220, 120),
                )

            composed = Image.alpha_composite(pil_image, overlay).convert("RGB")
            out_frame = cv2.cvtColor(np.array(composed), cv2.COLOR_RGB2BGR)
            writer.write(out_frame)

            frame_index += 1
            if progress_callback:
                progress = int((frame_index / total_frames) * 100) if total_frames > 0 else 0
                progress_callback(min(progress, 99), frame_index, total_frames)
    finally:
        writer.release()
        cap.release()

    final_path = _browser_compatible_mp4(raw_output, output_path)
    if progress_callback:
        progress_callback(100, frame_index, total_frames)

    return {
        "output_path": final_path,
        "processed_frames": frame_index,
        "water_detected_frames": water_frames,
        "measured_frames": measured_frames,
        "alert_frames": alert_frames,
        "low_frames": low_frames,
        "medium_frames": medium_frames,
        "high_frames": high_frames,
        "max_water_level": max_water_level,
        "measurement_line": {
            "start": [round(first_x, 1), round(first_y, 1)],
            "end": [round(second_x, 1), round(second_y, 1)],
        },
        "video_size": [width, height],
    }
