"""
dashboard.py — Flask Web Dashboard Backend

Serves the monitoring dashboard with:
  - Live video feed (MJPEG stream)
  - Real-time water level data via Socket.IO
  - REST API for status and configuration
"""

import cv2
import time
import threading
import logging
from flask import Flask, render_template, Response, jsonify, request
from flask_socketio import SocketIO

logger = logging.getLogger(__name__)


class Dashboard:
    """Flask-based web dashboard for flood monitoring."""

    def __init__(self, config, camera, detector, alert_manager):
        self.config = config
        self.camera = camera
        self.detector = detector
        self.alert_manager = alert_manager

        self.app = Flask(__name__,
                         static_folder='static',
                         template_folder='templates')
        self.app.config['SECRET_KEY'] = 'flood-detect-secret'

        self.socketio = SocketIO(self.app, async_mode='threading',
                                  cors_allowed_origins="*")

        # Give alert manager access to socketio
        self.alert_manager.socketio = self.socketio

        # Data for history
        self._history = []
        self._history_lock = threading.Lock()
        self._max_history = config['dashboard']['history_length']
        self._start_time = time.time()

        # Latest detection result
        self._latest_result = None
        self._latest_lock = threading.Lock()

        self._setup_routes()
        self._setup_socketio()

    def _setup_routes(self):
        """Configure Flask routes."""

        @self.app.route('/')
        def index():
            return render_template('index.html')

        @self.app.route('/video_feed')
        def video_feed():
            return Response(
                self._generate_frames(),
                mimetype='multipart/x-mixed-replace; boundary=frame'
            )

        @self.app.route('/api/status')
        def api_status():
            with self._latest_lock:
                result = self._latest_result or {}

            return jsonify({
                'water_level': {
                    'cm': result.get('water_level_cm'),
                    'px': result.get('water_level_px'),
                    'confidence': result.get('confidence', 0),
                    'detected': result.get('detected', False),
                    'timestamp': result.get('timestamp', 0)
                },
                'alert': self.alert_manager.get_status(),
                'camera': {
                    'connected': self.camera.connected,
                    'source': self.camera.source,
                    'fps': round(self.camera.fps, 1)
                },
                'system': {
                    'uptime': time.time() - self._start_time,
                    'history_count': len(self._history)
                }
            })

        @self.app.route('/api/history')
        def api_history():
            with self._history_lock:
                return jsonify(self._history[-200:])  # Last 200 points

        @self.app.route('/api/alerts')
        def api_alerts():
            return jsonify(self.alert_manager.alert_history)

        @self.app.route('/api/config', methods=['GET', 'POST'])
        def api_config():
            if request.method == 'GET':
                return jsonify({
                    'thresholds': self.config['alerts']['thresholds'],
                    'roi': self.config['detection']['roi'],
                    'camera_url': self.config['camera']['stream_url']
                })
            elif request.method == 'POST':
                data = request.get_json()
                if 'thresholds' in data:
                    self.config['alerts']['thresholds'].update(data['thresholds'])
                    self._save_config()
                    logger.info(f"Thresholds updated: {data['thresholds']}")
                return jsonify({'status': 'ok'})

        @self.app.route('/api/settings', methods=['GET'])
        def get_settings():
            return jsonify({
                'sms_device_id': self.config['sms']['device_id'],
                'sms_recipients': ', '.join(self.config['sms']['recipients'])
            })

        @self.app.route('/api/settings', methods=['POST'])
        def update_settings():
            data = request.get_json()
            if 'sms_device_id' in data:
                self.config['sms']['device_id'] = data['sms_device_id']
            if 'sms_recipients' in data:
                # Split by comma, clean whitespace, remove empty
                numbers = [n.strip() for n in data['sms_recipients'].split(',') if n.strip()]
                self.config['sms']['recipients'] = numbers
            
            self._save_config()
            logger.info("Settings updated and saved to config.yaml")
            return jsonify({'status': 'success'})

        @self.app.route('/api/test_alert', methods=['POST'])
        def test_alert():
            """Send a test alert through all channels."""
            self.alert_manager._on_level_change(0, 3, 999) # Send critical test
            return jsonify({'status': 'test alert sent'})

    def _save_config(self):
        """Save current config to YAML file."""
        import yaml
        try:
            with open('config.yaml', 'w') as f:
                yaml.dump(self.config, f, default_flow_style=False)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

    def _setup_socketio(self):
        """Configure Socket.IO event handlers."""

        @self.socketio.on('connect')
        def handle_connect():
            logger.info("Dashboard client connected")
            # Send current status on connect
            self.socketio.emit('status_update', self.alert_manager.get_status())

        @self.socketio.on('disconnect')
        def handle_disconnect():
            logger.info("Dashboard client disconnected")

    def _generate_frames(self):
        """Generator that yields MJPEG frames for the video feed."""
        while True:
            with self._latest_lock:
                result = self._latest_result

            if result and 'annotated_frame' in result:
                frame = result['annotated_frame']
            else:
                frame = self.camera.read()

            if frame is not None:
                # Encode frame as JPEG
                ret, buffer = cv2.imencode('.jpg', frame,
                                            [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ret:
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' +
                           buffer.tobytes() + b'\r\n')

            time.sleep(1 / 15)  # ~15 FPS for the stream

    def update(self, detection_result):
        """Update dashboard with new detection result."""
        with self._latest_lock:
            self._latest_result = detection_result

        # Add to history
        if detection_result and detection_result.get('water_level_cm') is not None:
            entry = {
                'timestamp': detection_result['timestamp'],
                'water_level_cm': detection_result['water_level_cm'],
                'confidence': detection_result['confidence'],
                'alert_level': self.alert_manager.current_level
            }

            with self._history_lock:
                self._history.append(entry)
                if len(self._history) > self._max_history:
                    self._history = self._history[-self._max_history:]

            # Emit to connected clients
            self.socketio.emit('water_level', entry)

    def run(self):
        """Start the Flask dashboard server."""
        host = self.config['dashboard']['host']
        port = self.config['dashboard']['port']
        logger.info(f"Dashboard running at http://{host}:{port}")
        self.socketio.run(self.app, host=host, port=port,
                          debug=False, allow_unsafe_werkzeug=True)
