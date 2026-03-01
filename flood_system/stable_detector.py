import cv2
import numpy as np
import logging
from collections import deque
import time

logger = logging.getLogger(__name__)

class StableWaterDetector:
    """
    Robust water level detector using Horizontal Projection.
    Calculates row brightness averages and uses temporal median filtering
    to completely eliminate noise and hallucinated jumps.
    """
    def __init__(self, config):
        self.config = config
        self.history = deque(maxlen=30)
        
        # Load ROI properly from the 'detection' sub-dict
        roi = config.get('detection', {}).get('roi')
        self.update_roi(roi)
        
        self.detector_name = "stable"
        self._is_loaded = False

    def load(self):
        """No-op for this lightweight math model, but maintains interface parity."""
        if not self._is_loaded:
            logger.info("Initializing Stable Detector Engine...")
            self._is_loaded = True

    def unload(self):
        """No-op."""
        if self._is_loaded:
            logger.info("Unloading Stable Detector Engine...")
            self._is_loaded = False

    def update_roi(self, roi_array):
        """Update detection region of interest: [y1, y2, x1, x2]"""
        if roi_array and len(roi_array) == 4 and all(v is not None for v in roi_array):
            logger.info(f"Stable Detector ROI updated: {roi_array}")
            self.roi = {
                'y1': int(roi_array[0]),
                'y2': int(roi_array[1]),
                'x1': int(roi_array[2]),
                'x2': int(roi_array[3])
            }
            # Clear history when ROI moves because previous spatial data is useless
            self.history.clear()
        else:
            self.roi = None

    def detect(self, main_frame, draw=False):
        """
        Detects the waterline in the given frame using horizontal projection.
        Returns: Tuple of (water_level_cm, debug_frame)
        """
        result = {
            'water_level_cm': None,
            'water_level_px': None,
            'confidence': 0.0,
            'detected': False,
            'timestamp': time.time(),
            'raw_frame': main_frame.copy(),
            'output_frame': main_frame
        }

        # If no ROI is configured yet, just return None
        if not self.roi or not self._is_loaded:
            if draw:
                cv2.putText(result['output_frame'], "Stable Model: Mssing ROI. Check Settings.", (10, 30), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            return result

        y1, y2 = self.roi['y1'], self.roi['y2']
        x1, x2 = self.roi['x1'], self.roi['x2']

        # Clamp ROI to frame bounds to avoid crashes
        fh, fw = main_frame.shape[:2]
        x1, x2 = max(0, min(x1, fw)), max(0, min(x2, fw))
        y1, y2 = max(0, min(y1, fh)), max(0, min(y2, fh))

        # Check for invalid bounding box
        if y2 <= y1 or x2 <= x1:
            return result

        roi_frame = main_frame[y1:y2, x1:x2]

        # 1. Pre-process for Stability - STRONGER BLUR
        gray = cv2.cvtColor(roi_frame, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (15, 15), 0)

        # 2. Horizontal Projection (The math engine)
        row_averages = blurred.mean(axis=1).astype(int)
        
        display_y = None

        if len(row_averages) > 5:
            # 1D Smooth
            smoothed_rows = np.convolve(row_averages, np.ones(5)/5, mode='valid')
            diff = np.diff(smoothed_rows)
            
            # Find max absolute difference line
            waterline_relative = np.argmax(np.abs(diff)) 
            waterline_relative += 2 # Offset for convolve reduction
            
            # 3. Temporal Median Filtering
            self.history.append(waterline_relative)
            stable_waterline = int(np.median(self.history))
            
            # Map back to full frame
            display_y = stable_waterline + y1

        # Calculate a pseudo-CM reading based on percentage of ROI box height
        # Later, we can map this linearly using the physical line coordinates
        water_level_cm = None
        if display_y is not None:
            roi_h = y2 - y1
            # Inverse percentage: Top of ROI is highest CM, Bottom is lowest CM.
            # Simplified map: Top = 300cm, Bottom = 0cm
            percentage = 1.0 - (stable_waterline / float(roi_h))
            water_level_cm = int(percentage * 300) # Assuming 3m ruler

        if draw:
            # Draw ROI Box
            cv2.rectangle(main_frame, (x1, y1), (x2, y2), (255, 0, 0), 2)
            cv2.putText(main_frame, "Detector: Stable PRO", (x1, max(0, y1-10)), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 1)

            if display_y is not None:
                cv2.line(main_frame, (x1-20, display_y), (x2+20, display_y), (0, 255, 0), 3)
                cv2.putText(main_frame, f"SURFACE (y={display_y})", (x2 + 5, display_y), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                if water_level_cm is not None:
                    cv2.putText(main_frame, f"{water_level_cm} CM", (max(0, x1-80), display_y+5), 
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

        result['water_level_cm'] = water_level_cm
        result['water_level_px'] = display_y
        result['confidence'] = 1.0 if water_level_cm is not None else 0.0
        result['detected'] = water_level_cm is not None
        result['output_frame'] = main_frame.copy() if draw else main_frame
        
        return result
