"""
yolo_detector.py — YOLOv8 Water Segmentation Detector

Uses a trained YOLOv8 model (best.pt) to detect water areas in frames,
then calculates water level using polygon-line intersection.

Follows the same detect(frame) → dict interface as detector.py.
"""

import cv2
import numpy as np
import time
import math
import logging
import os
from collections import deque

logger = logging.getLogger(__name__)

# Lazy imports for heavy deps
_YOLO = None
_LineString = None
_Polygon = None


def _load_deps():
    """Lazy-load heavy dependencies."""
    global _YOLO, _LineString, _Polygon
    if _YOLO is None:
        from ultralytics import YOLO as Y
        from shapely.geometry import LineString as LS, Polygon as PG
        _YOLO = Y
        _LineString = LS
        _Polygon = PG


class YoloWaterDetector:
    """Detects water level using YOLOv8 segmentation + line intersection."""

    def __init__(self, config):
        self.config = config
        yolo_cfg = config['detection'].get('yolo', {})

        # Model path
        self.model_path = yolo_cfg.get('model_path', '../Flood-detection/best.pt')
        # Resolve relative path
        if not os.path.isabs(self.model_path):
            base = os.path.dirname(os.path.abspath(__file__))
            self.model_path = os.path.normpath(os.path.join(base, self.model_path))

        # Reference line (perpendicular to water surface)
        line_start = yolo_cfg.get('line_start', [1094, 231])
        line_end = yolo_cfg.get('line_end', [1083, 403])
        self.line_start = tuple(line_start)
        self.line_end = tuple(line_end)

        # Scale parameters
        self.pixels_per_meter = yolo_cfg.get('pixels_per_meter', 15.0)
        self.tip_height = yolo_cfg.get('tip_height', 15.0)
        self.conf_threshold = yolo_cfg.get('confidence', 0.5)

        # Thresholds (use same alert thresholds as main config, in meters)
        # Convert cm thresholds to meters for YOLO model
        thresh = config['alerts']['thresholds']
        self.warning_level = thresh.get('warning', 220) / 100.0  # cm → m

        # Smoothing
        window = config['detection'].get('smoothing_window', 5)
        self._history = deque(maxlen=window)
        self._last_level_cm = None
        self._last_level_px = None

        # Model (loaded on first use)
        self._model = None
        self._loaded = False

    def load(self):
        """Load the YOLO model into memory."""
        if self._loaded:
            return
        try:
            _load_deps()
            logger.info(f"Loading YOLO model from {self.model_path}...")
            self._model = _YOLO(self.model_path)
            self._loaded = True
            logger.info("YOLO model loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load YOLO model: {e}")
            self._model = None
            self._loaded = False

    def unload(self):
        """Unload the YOLO model from memory."""
        if self._model is not None:
            del self._model
            self._model = None
            self._loaded = False
            logger.info("YOLO model unloaded from memory")

    @property
    def is_loaded(self):
        return self._loaded and self._model is not None

    def detect(self, frame):
        """
        Detect water level from a single frame using YOLO segmentation.

        Returns dict matching the same interface as detector.py.
        """
        timestamp = time.time()
        result = {
            'water_level_px': None,
            'water_level_cm': None,
            'confidence': 0.0,
            'timestamp': timestamp,
            'annotated_frame': frame.copy(),
            'detected': False
        }

        if not self.is_loaded:
            self.load()
            if not self.is_loaded:
                return result

        try:
            h, w = frame.shape[:2]

            # Run YOLO inference
            results = self._model(frame, conf=self.conf_threshold, verbose=False)

            if not results or len(results) == 0:
                return self._fallback(result)

            r = results[0]

            # Check if masks were detected
            if r.masks is None or len(r.masks.xy) == 0:
                return self._fallback(result)

            # Get the first (largest) water segmentation mask
            segments = r.masks.xy[0]
            segment_count = int(segments.size / 2)

            if segment_count < 3:
                return self._fallback(result)

            # Coordinates are already in pixel space
            polygon_vertices = []
            for i in range(segment_count):
                px = int(float(segments[i][0]))
                py = int(float(segments[i][1]))
                polygon_vertices.append((px, py))

            # Create shapely geometry
            line = _LineString([self.line_start, self.line_end])
            polygon = _Polygon(polygon_vertices)

            # Find intersection
            intersection = polygon.intersection(line)
            if intersection.is_empty:
                return self._fallback(result)

            # Get intersection point
            try:
                ix = intersection.xy[0][0]
                iy = intersection.xy[1][0]
            except (IndexError, AttributeError):
                return self._fallback(result)

            # Calculate water level in meters
            dist_px = math.sqrt(
                (ix - self.line_start[0]) ** 2 +
                (iy - self.line_start[1]) ** 2
            )
            water_level_m = self.tip_height - (dist_px / self.pixels_per_meter)
            water_level_cm = water_level_m * 100  # Convert to cm for consistency

            # Get confidence from YOLO
            conf = float(r.boxes.conf[0]) if len(r.boxes.conf) > 0 else 0.5

            # Smooth
            self._history.append(water_level_cm)
            smoothed_cm = float(np.mean(self._history))

            # Water level pixel position (intersection Y)
            water_px = int(iy)

            result['water_level_px'] = water_px
            result['water_level_cm'] = round(smoothed_cm, 1)
            result['confidence'] = conf
            result['detected'] = True
            self._last_level_cm = smoothed_cm
            self._last_level_px = water_px

            # --- Draw annotated frame ---
            # Draw YOLO detection overlay
            annotated = r.plot()
            result['annotated_frame'] = annotated

            # Draw reference line
            cv2.line(
                result['annotated_frame'],
                (int(self.line_start[0]), int(self.line_start[1])),
                (int(ix), int(iy)),
                (0, 255, 0), 3
            )

            # Draw water level text
            text = f"Water Level: {smoothed_cm:.0f} cm ({water_level_m:.2f} m)"
            cv2.putText(
                result['annotated_frame'], text,
                (10, h - 60),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (255, 255, 255), 2
            )

            # Warning indicator
            if water_level_m >= self.warning_level:
                cv2.putText(
                    result['annotated_frame'], "WARNING!!!",
                    (10, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2,
                    (0, 0, 255), 3
                )

            # Confidence text
            cv2.putText(
                result['annotated_frame'],
                f"YOLO Conf: {conf:.0%}",
                (10, h - 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (200, 200, 200), 1
            )

        except Exception as e:
            logger.error(f"YOLO detection error: {e}")
            return self._fallback(result)

        return result

    def _fallback(self, result):
        """Use last known value when detection fails."""
        if self._last_level_cm is not None:
            result['water_level_px'] = self._last_level_px
            result['water_level_cm'] = self._last_level_cm
            result['confidence'] = 0.1
        return result

    def update_line(self, start, end):
        """Update reference line coordinates at runtime."""
        self.line_start = tuple(start)
        self.line_end = tuple(end)
        logger.info(f"YOLO reference line updated: {start} → {end}")

    def update_scale(self, pixels_per_meter, tip_height, conf_threshold=0.5):
        """Update scale and confidence parameters at runtime."""
        self.pixels_per_meter = pixels_per_meter
        self.tip_height = tip_height
        self.conf_threshold = conf_threshold
        logger.info(f"YOLO scale updated: {pixels_per_meter} px/m, tip={tip_height}m, conf={conf_threshold}")
