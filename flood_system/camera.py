"""
camera.py — DroidCam / IP Camera Connector

Connects to the phone's camera via DroidCam MJPEG stream.
Auto-reconnects on failure, falls back to local webcam.
Thread-safe frame buffer for concurrent access.
"""

import cv2
import time
import threading
import logging

logger = logging.getLogger(__name__)


class CameraStream:
    """Thread-safe camera stream from DroidCam or fallback webcam."""

    def __init__(self, config):
        self.config = config
        self.stream_url = config['camera']['stream_url']
        self.fallback = config['camera']['fallback_webcam']
        self.max_fps = config['camera']['max_fps']
        self.reconnect_delay = config['camera']['reconnect_delay']

        self._frame = None
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._connected = False
        self._source = None  # 'droidcam' or 'webcam'
        self._cap = None
        self._fps = 0
        self._frame_count = 0
        self._fps_timer = time.time()

    @property
    def connected(self):
        return self._connected

    @property
    def source(self):
        return self._source or "disconnected"

    @property
    def fps(self):
        return self._fps

    def start(self):
        """Start the camera capture thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        logger.info("Camera stream thread started")

    def stop(self):
        """Stop the camera capture thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._cap:
            self._cap.release()
        self._connected = False
        logger.info("Camera stream stopped")

    def read(self):
        """Get the latest frame (thread-safe). Returns None if no frame."""
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def _connect(self):
        """Try connecting to DroidCam, then fallback to webcam."""
        # Try DroidCam first
        logger.info(f"Connecting to DroidCam at {self.stream_url}...")
        cap = cv2.VideoCapture(self.stream_url)
        if cap.isOpened():
            ret, frame = cap.read()
            if ret and frame is not None:
                self._cap = cap
                self._connected = True
                self._source = "droidcam"
                logger.info("Connected to DroidCam successfully")
                return True
            cap.release()

        # Fallback to webcam
        logger.warning("DroidCam unavailable, trying local webcam...")
        cap = cv2.VideoCapture(self.fallback)
        if cap.isOpened():
            ret, frame = cap.read()
            if ret and frame is not None:
                self._cap = cap
                self._connected = True
                self._source = "webcam"
                logger.info(f"Connected to local webcam ({self.fallback})")
                return True
            cap.release()

        self._connected = False
        self._source = None
        logger.error("No camera source available")
        return False

    def _capture_loop(self):
        """Main capture loop — runs in a separate thread."""
        frame_interval = 1.0 / self.max_fps if self.max_fps > 0 else 0

        while self._running:
            # Connect if not connected
            if not self._connected or self._cap is None:
                if not self._connect():
                    time.sleep(self.reconnect_delay)
                    continue

            # Read frame
            try:
                ret, frame = self._cap.read()
                if not ret or frame is None:
                    logger.warning("Frame read failed, reconnecting...")
                    self._connected = False
                    if self._cap:
                        self._cap.release()
                        self._cap = None
                    time.sleep(self.reconnect_delay)
                    continue

                # Update frame buffer (thread-safe)
                with self._lock:
                    self._frame = frame

                # FPS calculation
                self._frame_count += 1
                elapsed = time.time() - self._fps_timer
                if elapsed >= 1.0:
                    self._fps = self._frame_count / elapsed
                    self._frame_count = 0
                    self._fps_timer = time.time()

                # Rate limiting
                if frame_interval > 0:
                    time.sleep(frame_interval)

            except Exception as e:
                logger.error(f"Capture error: {e}")
                self._connected = False
                if self._cap:
                    self._cap.release()
                    self._cap = None
                time.sleep(self.reconnect_delay)
