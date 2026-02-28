"""
detector.py — Real-Time Water Level Detection Engine

Refactored from the original water_level_detection.py.
Processes individual frames for live streaming use.
Uses Canny edge detection + HoughLinesP to find the water surface.
"""

import cv2
import numpy as np
import time
import logging
from collections import deque

logger = logging.getLogger(__name__)


class WaterLevelDetector:
    """Detects water level from individual video frames."""

    def __init__(self, config):
        self.config = config
        det = config['detection']

        # ROI
        self.roi = det['roi']  # [y_start, y_end, x_start, x_end]

        # Canny
        self.canny_low = det['canny_low']
        self.canny_high = det['canny_high']

        # HoughLines
        self.hough_rho = det['hough_rho']
        self.hough_threshold = det['hough_threshold']
        self.hough_min_line_length = det['hough_min_line_length']
        self.hough_max_line_gap = det['hough_max_line_gap']

        # Calibration
        cal = det['calibration']
        self.top_px = cal['top_px']
        self.top_cm = cal['top_cm']
        self.bottom_px = cal['bottom_px']
        self.bottom_cm = cal['bottom_cm']

        # Offset adjustment
        self.offset = det['water_level_offset']

        # Smoothing
        window = det['smoothing_window']
        self._history = deque(maxlen=window)
        self._last_level_px = None
        self._last_level_cm = None

        # Auto-calibration state
        self._auto_calibrated = False
        self._auto_cal_attempted = False
        self._auto_cal_last_attempt = 0
        self._auto_cal_interval = 60  # retry every 60s if failed

    def detect(self, frame):
        """
        Detect water level from a single frame.

        Returns:
            dict with keys:
                - water_level_px: pixel position of water level
                - water_level_cm: calibrated water level in cm
                - confidence: detection confidence (0.0 - 1.0)
                - timestamp: detection timestamp
                - annotated_frame: frame with detection overlay drawn
                - detected: whether detection succeeded
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

        try:
            h, w = frame.shape[:2]

            # --- Auto-calibration attempt ---
            if not self._auto_calibrated:
                if (timestamp - self._auto_cal_last_attempt) > self._auto_cal_interval:
                    self._auto_cal_last_attempt = timestamp
                    self._try_auto_calibrate(frame)

            # Extract ROI
            y1, y2, x1, x2 = self.roi

            # Handle None/null from config
            y1 = 0 if y1 is None else int(y1)
            y2 = h if y2 is None else int(y2)
            x1 = 0 if x1 is None else int(x1)
            x2 = w if x2 is None else int(x2)

            # Clamp to frame dimensions
            y1 = max(0, min(y1, h))
            y2 = max(0, min(y2, h))
            x1 = max(0, min(x1, w))
            x2 = max(0, min(x2, w))

            if y2 <= y1 or x2 <= x1:
                logger.warning("Invalid ROI dimensions, using full frame")
                slc = frame
            else:
                slc = frame[y1:y2, x1:x2]

            # Draw ROI rectangle on annotated frame
            cv2.rectangle(result['annotated_frame'], (x1, y1), (x2, y2), (0, 0, 255), 2)

            # Convert to grayscale
            gray = cv2.cvtColor(slc, cv2.COLOR_BGR2GRAY)

            # Canny edge detection
            edges = cv2.Canny(gray, self.canny_low, self.canny_high)

            # HoughLinesP
            lines = cv2.HoughLinesP(
                edges,
                self.hough_rho,
                np.pi / 180,
                self.hough_threshold,
                np.array([]),
                self.hough_min_line_length,
                self.hough_max_line_gap
            )

            if lines is not None and len(lines) > 0:
                # Find the lowest line (highest Y value = water surface)
                water_level_px = int((np.max(lines[:, 0, 1]) + np.max(lines[:, 0, 3])) / 2)
                water_level_px = water_level_px - self.offset

                # Convert to absolute frame coordinates
                abs_water_level = water_level_px + y1

                # Add to smoothing buffer
                self._history.append(abs_water_level)
                smoothed_px = int(np.mean(self._history))

                # Calibrate to cm
                water_level_cm = self._px_to_cm(smoothed_px)

                # Confidence based on number of detected lines
                confidence = min(1.0, len(lines) / 20.0)

                # Update result
                result['water_level_px'] = smoothed_px
                result['water_level_cm'] = water_level_cm
                result['confidence'] = confidence
                result['detected'] = True

                self._last_level_px = smoothed_px
                self._last_level_cm = water_level_cm

                # Draw water level line on annotated frame
                cv2.line(
                    result['annotated_frame'],
                    (x1, smoothed_px), (x2, smoothed_px),
                    (0, 255, 255), 3
                )

                # Draw water level text
                text = f"Water Level: {water_level_cm:.0f} cm"
                cv2.putText(
                    result['annotated_frame'], text,
                    (10, h - 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (255, 255, 255), 2
                )

                # Draw confidence
                conf_text = f"Confidence: {confidence:.0%}"
                cv2.putText(
                    result['annotated_frame'], conf_text,
                    (10, h - 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (200, 200, 200), 1
                )

                # Draw detected lines (faint blue)
                for line in lines:
                    lx1, ly1, lx2, ly2 = line[0]
                    cv2.line(
                        result['annotated_frame'],
                        (lx1 + x1, ly1 + y1), (lx2 + x1, ly2 + y1),
                        (255, 100, 0), 1
                    )

            else:
                # No lines detected — use last known value
                if self._last_level_px is not None:
                    result['water_level_px'] = self._last_level_px
                    result['water_level_cm'] = self._last_level_cm
                    result['confidence'] = 0.1  # Low confidence
                    result['detected'] = False

                    cv2.putText(
                        result['annotated_frame'],
                        f"Water Level: {self._last_level_cm:.0f} cm (last known)",
                        (10, h - 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        (100, 100, 255), 2
                    )

        except Exception as e:
            logger.error(f"Detection error: {e}")
            if self._last_level_px is not None:
                result['water_level_px'] = self._last_level_px
                result['water_level_cm'] = self._last_level_cm

        return result

    def _px_to_cm(self, px):
        """Convert pixel position to cm using linear calibration."""
        cm = ((px - self.top_px) / (self.bottom_px - self.top_px)) * \
             (self.bottom_cm - self.top_cm) + self.top_cm
        return round(cm, 1)

    def _try_auto_calibrate(self, frame):
        """
        Attempt to auto-detect ruler tick marks in the ROI for calibration.
        
        Looks for evenly-spaced horizontal lines in a narrow vertical strip
        (where a ruler/gauge would be). If found, uses the spacing to
        automatically set the px-to-cm calibration.
        """
        try:
            h, w = frame.shape[:2]
            y1, y2, x1, x2 = self.roi
            y1 = max(0, min(y1, h))
            y2 = max(0, min(y2, h))
            x1 = max(0, min(x1, w))
            x2 = max(0, min(x2, w))
            
            if y2 <= y1 or x2 <= x1:
                return
            
            roi = frame[y1:y2, x1:x2]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            
            # Apply adaptive thresholding to find ruler marks
            binary = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY_INV, 11, 2
            )
            
            # Find horizontal line segments using HoughLinesP
            # with strict horizontal constraint
            lines = cv2.HoughLinesP(
                binary, 1, np.pi / 180, 10,
                minLineLength=int((x2 - x1) * 0.15),  # At least 15% width
                maxLineGap=5
            )
            
            if lines is None or len(lines) < 3:
                if not self._auto_cal_attempted:
                    logger.info("Auto-calibration: insufficient tick marks detected, using manual calibration")
                    self._auto_cal_attempted = True
                return
            
            # Filter for near-horizontal lines (slope < 5 degrees)
            tick_ys = []
            for line in lines:
                lx1, ly1, lx2, ly2 = line[0]
                if abs(ly2 - ly1) < 5:  # Nearly horizontal
                    tick_ys.append((ly1 + ly2) / 2)
            
            if len(tick_ys) < 3:
                if not self._auto_cal_attempted:
                    logger.info("Auto-calibration: too few horizontal marks, using manual calibration")
                    self._auto_cal_attempted = True
                return
            
            # Sort and remove duplicates (merge ticks within 5px)
            tick_ys = sorted(set(tick_ys))
            merged = [tick_ys[0]]
            for y in tick_ys[1:]:
                if y - merged[-1] > 8:  # Minimum 8px between ticks
                    merged.append(y)
            tick_ys = merged
            
            if len(tick_ys) < 3:
                return
            
            # Calculate spacings between consecutive ticks
            spacings = [tick_ys[i+1] - tick_ys[i] for i in range(len(tick_ys) - 1)]
            median_spacing = np.median(spacings)
            
            # Check if spacings are consistent (within 30% of median)
            consistent = [s for s in spacings if abs(s - median_spacing) < median_spacing * 0.3]
            
            if len(consistent) < 2:
                if not self._auto_cal_attempted:
                    logger.info("Auto-calibration: tick spacing too irregular, using manual calibration")
                    self._auto_cal_attempted = True
                return
            
            # Auto-calibrate: assume each tick = 10 cm (common ruler marking)
            # Top tick = highest cm value, bottom tick = lowest
            cm_per_tick = 10  # Standard ruler increment
            top_tick_px = int(tick_ys[0]) + y1   # Convert to absolute coords
            bottom_tick_px = int(tick_ys[-1]) + y1
            num_ticks = len(tick_ys)
            total_cm = (num_ticks - 1) * cm_per_tick
            
            # Use existing top_cm as reference point for the highest tick
            new_top_cm = self.top_cm
            new_bottom_cm = new_top_cm - total_cm  # Lower ticks = lower water = less cm
            
            old_top_px, old_bottom_px = self.top_px, self.bottom_px
            self.top_px = top_tick_px
            self.bottom_px = bottom_tick_px
            self.top_cm = new_top_cm
            self.bottom_cm = new_bottom_cm
            
            self._auto_calibrated = True
            logger.info(
                f"Auto-calibration SUCCESS: {len(tick_ys)} ruler ticks detected "
                f"({median_spacing:.0f}px spacing). "
                f"Calibration: {top_tick_px}px={new_top_cm}cm → {bottom_tick_px}px={new_bottom_cm}cm "
                f"(was {old_top_px}px → {old_bottom_px}px)"
            )
            
        except Exception as e:
            logger.warning(f"Auto-calibration error: {e}")

    def update_calibration(self, top_px, top_cm, bottom_px, bottom_cm):
        """Update calibration values at runtime."""
        self.top_px = top_px
        self.top_cm = top_cm
        self.bottom_px = bottom_px
        self.bottom_cm = bottom_cm
        self._auto_calibrated = True  # Mark as calibrated to stop auto-attempts
        logger.info(f"Calibration updated: top={top_px}px/{top_cm}cm, bottom={bottom_px}px/{bottom_cm}cm")

    def update_roi(self, roi):
        """Update region of interest at runtime."""
        self.roi = roi
        self._auto_calibrated = False  # Re-attempt auto-calibration with new ROI
        logger.info(f"ROI updated: {roi}")

