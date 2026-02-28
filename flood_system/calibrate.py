"""
calibrate.py — Interactive Calibration Tool

Connects to DroidCam and lets you:
  1. Select the Region of Interest (ROI)
  2. Click reference points for water level calibration
  3. Saves values to config.yaml

Usage: python calibrate.py [--config config.yaml]
"""

import cv2
import yaml
import argparse
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


class Calibrator:
    """Interactive calibration tool for water level detection."""

    def __init__(self, config_path='config.yaml'):
        self.config_path = config_path

        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)

        self.roi_points = []
        self.cal_points = []
        self.mode = 'roi'  # 'roi' or 'calibrate'
        self.frame = None

    def run(self):
        """Run the interactive calibration."""
        # Connect to camera
        url = self.config['camera']['stream_url']
        fallback = self.config['camera']['fallback_webcam']

        logger.info(f"Connecting to camera at {url}...")
        cap = cv2.VideoCapture(url)

        if not cap.isOpened():
            logger.warning(f"DroidCam not available, trying webcam {fallback}...")
            cap = cv2.VideoCapture(fallback)

        if not cap.isOpened():
            logger.error("No camera available!")
            sys.exit(1)

        ret, self.frame = cap.read()
        if not ret:
            logger.error("Cannot read from camera!")
            sys.exit(1)

        h, w = self.frame.shape[:2]
        logger.info(f"Frame size: {w}x{h}")
        logger.info("")
        logger.info("=" * 50)
        logger.info("  CALIBRATION MODE")
        logger.info("=" * 50)
        logger.info("")
        logger.info("STEP 1: Select Region of Interest (ROI)")
        logger.info("  Click TOP-LEFT corner, then BOTTOM-RIGHT corner")
        logger.info("  of the area where the water level gauge is visible.")
        logger.info("")
        logger.info("Press 'r' to reset, 'q' to quit, 'n' for next step")
        logger.info("")

        # Create window
        cv2.namedWindow('Calibration', cv2.WINDOW_NORMAL)
        cv2.setMouseCallback('Calibration', self._mouse_callback)

        while True:
            display = self.frame.copy()
            self._draw_overlay(display)
            cv2.imshow('Calibration', display)

            key = cv2.waitKey(1) & 0xFF

            if key == ord('q'):
                break
            elif key == ord('r'):
                if self.mode == 'roi':
                    self.roi_points = []
                    logger.info("ROI reset")
                else:
                    self.cal_points = []
                    logger.info("Calibration points reset")
            elif key == ord('n'):
                if self.mode == 'roi' and len(self.roi_points) == 2:
                    self.mode = 'calibrate'
                    logger.info("")
                    logger.info("STEP 2: Set Calibration Reference Points")
                    logger.info("  Click the TOP reference point (high water mark)")
                    logger.info("  Then click the BOTTOM reference point (low water mark)")
                    logger.info("  You will be prompted for the cm values.")
                    logger.info("")
            elif key == ord('s'):
                self._save_calibration()

            # Grab new frame periodically
            ret, new_frame = cap.read()
            if ret:
                self.frame = new_frame

        cap.release()
        cv2.destroyAllWindows()

    def _mouse_callback(self, event, x, y, flags, param):
        """Handle mouse clicks for ROI and calibration point selection."""
        if event != cv2.EVENT_LBUTTONDOWN:
            return

        if self.mode == 'roi':
            if len(self.roi_points) < 2:
                self.roi_points.append((x, y))
                label = "TOP-LEFT" if len(self.roi_points) == 1 else "BOTTOM-RIGHT"
                logger.info(f"ROI {label}: ({x}, {y})")
                if len(self.roi_points) == 2:
                    logger.info("ROI selected! Press 'n' to proceed to calibration, or 'r' to redo.")

        elif self.mode == 'calibrate':
            if len(self.cal_points) < 2:
                label = "TOP" if len(self.cal_points) == 0 else "BOTTOM"
                # Ask user for cm value
                cm = input(f"  Enter the water level in cm at the {label} point (y={y}): ")
                try:
                    cm = float(cm)
                    self.cal_points.append((y, cm))
                    logger.info(f"Calibration {label}: y={y}px → {cm} cm")
                    if len(self.cal_points) == 2:
                        logger.info("Calibration complete! Press 's' to save, or 'r' to redo.")
                except ValueError:
                    logger.error("Invalid number, try again")

    def _draw_overlay(self, frame):
        """Draw ROI and calibration points on the frame."""
        h, w = frame.shape[:2]

        # Instructions
        if self.mode == 'roi':
            text = f"STEP 1: Select ROI ({len(self.roi_points)}/2 points)"
            color = (0, 255, 255)
        else:
            text = f"STEP 2: Calibration ({len(self.cal_points)}/2 points)"
            color = (0, 255, 0)

        cv2.putText(frame, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

        # Draw ROI rectangle
        if len(self.roi_points) >= 2:
            cv2.rectangle(frame, self.roi_points[0], self.roi_points[1], (0, 0, 255), 2)
            cv2.putText(frame, "ROI", (self.roi_points[0][0], self.roi_points[0][1] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        elif len(self.roi_points) == 1:
            cv2.circle(frame, self.roi_points[0], 5, (0, 0, 255), -1)

        # Draw calibration points
        for i, (y, cm) in enumerate(self.cal_points):
            label = "TOP" if i == 0 else "BOTTOM"
            cv2.line(frame, (0, y), (w, y), (0, 255, 0), 2)
            cv2.putText(frame, f"{label}: {cm} cm", (10, y - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

        # Controls bar
        cv2.putText(frame, "r=Reset | n=Next | s=Save | q=Quit",
                    (10, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

    def _save_calibration(self):
        """Save calibration values to config.yaml."""
        if len(self.roi_points) < 2:
            logger.warning("ROI not set yet!")
            return

        # Update ROI
        x1 = min(self.roi_points[0][0], self.roi_points[1][0])
        x2 = max(self.roi_points[0][0], self.roi_points[1][0])
        y1 = min(self.roi_points[0][1], self.roi_points[1][1])
        y2 = max(self.roi_points[0][1], self.roi_points[1][1])
        self.config['detection']['roi'] = [y1, y2, x1, x2]
        logger.info(f"ROI saved: [{y1}, {y2}, {x1}, {x2}]")

        # Update calibration if set
        if len(self.cal_points) == 2:
            top_y, top_cm = self.cal_points[0]
            bottom_y, bottom_cm = self.cal_points[1]

            self.config['detection']['calibration']['top_px'] = top_y
            self.config['detection']['calibration']['top_cm'] = top_cm
            self.config['detection']['calibration']['bottom_px'] = bottom_y
            self.config['detection']['calibration']['bottom_cm'] = bottom_cm
            logger.info(f"Calibration saved: top={top_y}px/{top_cm}cm, bottom={bottom_y}px/{bottom_cm}cm")

        # Write to file
        with open(self.config_path, 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Configuration saved to {self.config_path}")


def main():
    parser = argparse.ArgumentParser(description="Water Level Calibration Tool")
    parser.add_argument('--config', type=str, default='config.yaml',
                        help='Path to config file')
    args = parser.parse_args()

    calibrator = Calibrator(args.config)
    calibrator.run()


if __name__ == '__main__':
    main()
