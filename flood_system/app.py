"""
app.py — Main Orchestrator

Starts all system components:
  - Camera stream (DroidCam / webcam)
  - Water level detection loop
  - Alert monitoring
  - Web dashboard

Usage: python app.py [--config path/to/config.yaml]
"""

import sys
import time
import signal
import logging
import argparse
import threading
import yaml

from camera import CameraStream
from detector import WaterLevelDetector
from stable_detector import StableWaterDetector
from alerts import AlertManager
from dashboard import Dashboard

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class FloodWarningSystem:
    """Main flood early warning system orchestrator."""

    def __init__(self, config_path='config.yaml'):
        import os
        # Load configuration
        abs_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), config_path)
        self.config_path = abs_config_path
        with open(abs_config_path, 'r') as f:
            self.config = yaml.safe_load(f)

        logger.info("=" * 60)
        logger.info("  FLOOD EARLY WARNING SYSTEM")
        logger.info("  AI-Powered Water Level Detection")
        logger.info("=" * 60)

        # Initialize components
        logger.info("Initializing camera stream...")
        self.camera = CameraStream(self.config)

        # Initialize both detectors but only load the active one
        self.active_model = self.config['detection'].get('active_model', 'canny')
        logger.info(f"Active detection model: {self.active_model}")

        logger.info("Initializing Canny/Hough detector...")
        self.canny_detector = WaterLevelDetector(self.config)

        logger.info("Initializing Stable PRO detector...")
        self.stable_detector = StableWaterDetector(self.config)

        # Set the active detector
        if self.active_model == 'stable':
            self.stable_detector.load()
            self.detector = self.stable_detector
        else:
            self.detector = self.canny_detector

        logger.info("Initializing alert manager...")
        self.alert_manager = AlertManager(self.config, config_path=self.config_path)

        logger.info("Initializing web dashboard...")
        self.dashboard = Dashboard(
            self.config, self.camera, self.detector, self.alert_manager,
            system=self
        )

        self._running = False
        self._detection_thread = None

    def switch_model(self, model_name):
        """Hot-swap the active detection model."""
        if model_name == self.active_model:
            return self.active_model

        logger.info(f"Switching detection model: {self.active_model} → {model_name}")

        if model_name == 'stable':
            self.stable_detector.load()
            self.detector = self.stable_detector
        else:
            self.stable_detector.unload()
            self.detector = self.canny_detector

        self.active_model = model_name
        self.config['detection']['active_model'] = model_name
        self.dashboard.detector = self.detector

        # Persist to config
        try:
            import yaml
            with open(self.config_path, 'w') as f:
                yaml.dump(self.config, f, default_flow_style=False)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

        logger.info(f"Now using: {model_name}")
        return model_name

    def start(self):
        """Start all system components."""
        self._running = True

        # Start camera
        self.camera.start()
        logger.info("Camera stream started")

        # Wait for camera to connect
        logger.info("Waiting for camera connection...")
        for i in range(10):
            if self.camera.connected:
                break
            time.sleep(1)

        if self.camera.connected:
            logger.info(f"Camera connected via {self.camera.source}")
        else:
            logger.warning("Camera not connected — dashboard will show when available")

        # Start detection loop
        self._detection_thread = threading.Thread(
            target=self._detection_loop, daemon=True
        )
        self._detection_thread.start()
        logger.info("Detection loop started")

        # Print status
        alert_status = self.alert_manager.channel_status
        logger.info("-" * 40)
        logger.info("Alert Channels:")
        logger.info(f"  Web Dashboard: ACTIVE")
        logger.info(f"  SMS (KDE Connect): {'READY' if alert_status.get('sms') else 'NOT CONFIGURED'}")
        logger.info(f"  ESP32 BLE: {'CONNECTED' if alert_status.get('ble') else 'NOT CONNECTED'}")
        logger.info(f"  Nostr/Bitchat: {'READY' if alert_status.get('nostr') else 'NOT CONFIGURED'}")
        logger.info("-" * 40)

        port = self.config['dashboard']['port']
        logger.info(f"Dashboard: http://localhost:{port}")
        logger.info("Press Ctrl+C to stop")
        logger.info("")

        # Start dashboard (this blocks)
        self.dashboard.run()

    def _detection_loop(self):
        """Main detection loop — runs in a separate thread."""
        interval = 1.0 / self.config['camera']['max_fps']

        while self._running:
            try:
                frame = self.camera.read()
                if frame is None:
                    time.sleep(0.5)
                    continue

                # Detect water level, requesting visual annotations
                try:
                    result = self.detector.detect(frame, draw=True)
                except TypeError:
                    # Fallback for models that don't accept 'draw' kwarg
                    result = self.detector.detect(frame)

                # Update dashboard
                self.dashboard.update(result)

                # Evaluate alerts
                if result.get('water_level_cm') is not None:
                    self.alert_manager.evaluate(result['water_level_cm'])

                time.sleep(interval)
            except Exception as e:
                import traceback
                logger.error(f"Detection thread crashed: {e}")
                logger.error(traceback.format_exc())
                time.sleep(1)

    def stop(self):
        """Graceful shutdown."""
        logger.info("\nShutting down...")
        self._running = False
        self.camera.stop()
        self.alert_manager.shutdown()
        logger.info("System stopped.")


def main():
    parser = argparse.ArgumentParser(description="Flood Early Warning System")
    parser.add_argument(
        '--config', type=str, default='config.yaml',
        help='Path to configuration file (default: config.yaml)'
    )
    args = parser.parse_args()

    system = FloodWarningSystem(args.config)

    # Handle Ctrl+C
    def signal_handler(sig, frame):
        system.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        system.start()
    except KeyboardInterrupt:
        system.stop()


if __name__ == '__main__':
    main()
