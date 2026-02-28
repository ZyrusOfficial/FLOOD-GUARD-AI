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

    def __init__(self, config, camera, detector, alert_manager, system=None):
        self.config = config
        self.camera = camera
        self.detector = detector
        self.alert_manager = alert_manager
        self.system = system  # Reference to FloodWarningSystem for model switching

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
                    # Propagate to live alert manager thresholds
                    thresh = self.config['alerts']['thresholds']
                    self.alert_manager.thresholds[1] = thresh.get('warning', 220)
                    self.alert_manager.thresholds[2] = thresh.get('danger', 260)
                    self.alert_manager.thresholds[3] = thresh.get('critical', 290)
                    logger.info(f"Thresholds updated: {data['thresholds']}")
                if 'camera_url' in data:
                    self.config['camera']['stream_url'] = data['camera_url']
                    # Parse IP and port from URL
                    try:
                        from urllib.parse import urlparse
                        parsed = urlparse(data['camera_url'])
                        if parsed.hostname:
                            self.config['camera']['droidcam_ip'] = parsed.hostname
                        if parsed.port:
                            self.config['camera']['droidcam_port'] = parsed.port
                    except Exception:
                        pass
                    logger.info(f"Camera URL updated: {data['camera_url']}")
                if 'roi' in data:
                    new_roi = data['roi']
                    # Ensure it is a list of 4 ints: y1, y2, x1, x2
                    if isinstance(new_roi, list) and len(new_roi) == 4:
                        self.config['detection']['roi'] = new_roi
                        # Propagate immediately to detector
                        if hasattr(self.detector, 'update_roi'):
                            self.detector.update_roi(new_roi)
                        logger.info(f"ROI updated from dashboard: {new_roi}")
                self._save_config()
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

        @self.app.route('/api/trigger_opencv_roi', methods=['POST'])
        def trigger_opencv_roi():
            import cv2
            """Launch native OS window for precise GUI-based ROI drawing."""
            frame = self.camera.read()
            if frame is None:
                return jsonify({'error': 'No camera feed available to calibrate'}), 400

            logger.info("Triggering Native OpenCV ROI Selector...")
            
            roi_points = []
            selecting = [True]
            
            def select_roi(event, x, y, flags, param):
                if event == cv2.EVENT_LBUTTONDOWN:
                    roi_points.append((x, y))
                    if len(roi_points) == 2:
                        param[0] = False # Stop selecting

            window_name = 'Alerto Capas - Draw Ruler Zone'
            cv2.namedWindow(window_name)
            cv2.setMouseCallback(window_name, select_roi, selecting)

            while selecting[0] and len(roi_points) < 2:
                display = frame.copy()
                if len(roi_points) == 1:
                    cv2.circle(display, roi_points[0], 5, (0, 0, 255), -1)
                    cv2.putText(display, "Click Bottom-Right", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                else:
                    cv2.putText(display, "Click TOP-LEFT of Ruler", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                    
                cv2.imshow(window_name, display)
                
                # Press 'c' to cancel
                key = cv2.waitKey(30) & 0xFF
                if key == ord('c') or key == 27: # c or ESC
                    break

            cv2.destroyWindow(window_name)
            # Necessary for Wayland to flush the window destruction
            for _ in range(4): cv2.waitKey(1) 

            if len(roi_points) == 2:
                x1 = min(roi_points[0][0], roi_points[1][0])
                y1 = min(roi_points[0][1], roi_points[1][1])
                x2 = max(roi_points[0][0], roi_points[1][0])
                y2 = max(roi_points[0][1], roi_points[1][1])
                
                new_roi = [y1, y2, x1, x2]
                logger.info(f"Native OpenCV ROI Selected: {new_roi}")
                
                # Save and Apply
                self.config['detection']['roi'] = new_roi
                if self.system and self.system.detector:
                    self.system.detector.update_roi(new_roi)
                self._save_config()
                
                return jsonify({
                    'status': 'success',
                    'roi': new_roi
                })
            else:
                logger.warning("Native OpenCV ROI Selection cancelled by user.")
                return jsonify({'error': 'Selection cancelled'}), 400

        @self.app.route('/api/test_alert', methods=['POST'])
        def test_alert():
            """Send a test alert through all channels."""
            self.alert_manager._on_level_change(0, 3, 999) # Send critical test
            return jsonify({'status': 'test alert sent'})

        @self.app.route('/api/model', methods=['GET', 'POST'])
        def api_model():
            if request.method == 'GET':
                return jsonify({
                    'active_model': self.config['detection'].get('active_model', 'canny'),
                    'available': ['canny', 'stable']
                })
            elif request.method == 'POST':
                data = request.get_json()
                model_name = data.get('model', 'canny')
                if model_name not in ('canny', 'stable'):
                    return jsonify({'error': 'Invalid model'}), 400
                if self.system:
                    result = self.system.switch_model(model_name)
                    return jsonify({'active_model': result, 'status': 'ok'})
                return jsonify({'error': 'System not available'}), 500



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
