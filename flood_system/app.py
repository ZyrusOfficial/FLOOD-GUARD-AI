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
        # Load configuration
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)

        logger.info("=" * 60)
        logger.info("  FLOOD EARLY WARNING SYSTEM")
        logger.info("  AI-Powered Water Level Detection")
        logger.info("=" * 60)

        # Initialize components
        logger.info("Initializing camera stream...")
        self.camera = CameraStream(self.config)

        logger.info("Initializing water level detector...")
        self.detector = WaterLevelDetector(self.config)

        logger.info("Initializing alert manager...")
        self.alert_manager = AlertManager(self.config)

        logger.info("Initializing web dashboard...")
        self.dashboard = Dashboard(
            self.config, self.camera, self.detector, self.alert_manager
        )

        self._running = False
        self._detection_thread = None

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
            frame = self.camera.read()
            if frame is None:
                time.sleep(0.5)
                continue

            # Detect water level
            result = self.detector.detect(frame)

            # Update dashboard
            self.dashboard.update(result)

            # Evaluate alerts
            if result['water_level_cm'] is not None:
                self.alert_manager.evaluate(result['water_level_cm'])

            time.sleep(interval)

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
