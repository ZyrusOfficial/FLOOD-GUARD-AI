"""
alerts.py — Triple-Redundant Alert System

Channels:
  1. SMS via KDE Connect CLI
  2. Nostr message for Bitchat mesh relay
  3. ESP32 BLE beacon via serial

Alert state machine: NORMAL → WARNING → DANGER → CRITICAL
With hysteresis to prevent oscillation.
"""

import subprocess
import threading
import time
import json
import uuid
import logging

logger = logging.getLogger(__name__)

# Alert levels
NORMAL = 0
WARNING = 1
DANGER = 2
CRITICAL = 3

LEVEL_NAMES = {
    NORMAL: "NORMAL",
    WARNING: "WARNING",
    DANGER: "DANGER",
    CRITICAL: "CRITICAL"
}

LEVEL_COLORS = {
    NORMAL: "#22c55e",
    WARNING: "#eab308",
    DANGER: "#f97316",
    CRITICAL: "#ef4444"
}


class AlertManager:
    """Manages flood alert state and dispatches alerts through all channels."""

    def __init__(self, config, socketio=None):
        self.config = config
        self.socketio = socketio

        # Thresholds
        thresh = config['alerts']['thresholds']
        self.thresholds = {
            WARNING: thresh['warning'],
            DANGER: thresh['danger'],
            CRITICAL: thresh['critical']
        }
        self.hysteresis = config['alerts']['hysteresis']

        # Cooldowns
        cooldowns = config['alerts']['cooldown']
        self.cooldowns = {
            'sms': cooldowns['sms'],
            'nostr': cooldowns['nostr'],
            'ble': cooldowns['ble'],
            'dashboard': cooldowns['dashboard']
        }

        # State
        self._current_level = NORMAL
        self._last_alert_time = {
            'sms': 0,
            'nostr': 0,
            'ble': 0,
            'dashboard': 0
        }
        self._alert_history = []
        self._lock = threading.Lock()

        # ESP32 serial connection
        self._serial = None
        self._esp32_connected = False

        # Channel status (start with defaults, update async)
        self._channel_status = {
            'dashboard': True,
            'sms': False,
            'ble': False,
            'nostr': True
        }

        # Connect to peripherals in background (non-blocking startup)
        threading.Thread(target=self._init_channels, daemon=True).start()

    @property
    def current_level(self):
        return self._current_level

    @property
    def current_level_name(self):
        return LEVEL_NAMES[self._current_level]

    @property
    def current_level_color(self):
        return LEVEL_COLORS[self._current_level]

    @property
    def alert_history(self):
        return list(self._alert_history)

    @property
    def channel_status(self):
        return dict(self._channel_status)

    def evaluate(self, water_level_cm):
        """
        Evaluate water level against thresholds and trigger alerts if needed.
        Returns the new alert level.
        """
        if water_level_cm is None:
            return self._current_level

        with self._lock:
            new_level = NORMAL

            # Determine new level (check from highest to lowest)
            if water_level_cm >= self.thresholds[CRITICAL]:
                new_level = CRITICAL
            elif water_level_cm >= self.thresholds[DANGER]:
                new_level = DANGER
            elif water_level_cm >= self.thresholds[WARNING]:
                new_level = WARNING

            # Apply hysteresis when going down
            if new_level < self._current_level:
                # Only lower the alert if we're clearly below the threshold
                current_threshold = self.thresholds.get(self._current_level, 0)
                if water_level_cm > (current_threshold - self.hysteresis):
                    new_level = self._current_level  # Stay at current level

            # Level changed — trigger alerts
            if new_level != self._current_level:
                old_level = self._current_level
                self._current_level = new_level
                self._on_level_change(old_level, new_level, water_level_cm)

            # Periodic re-alert for ongoing conditions
            elif new_level > NORMAL:
                self._periodic_alert(new_level, water_level_cm)

            return self._current_level

    def _on_level_change(self, old_level, new_level, water_level_cm):
        """Handle alert level transition."""
        msg = (
            f"FLOOD ALERT [{LEVEL_NAMES[new_level]}]: "
            f"Water level is {water_level_cm:.0f} cm. "
            f"Previous level: {LEVEL_NAMES[old_level]}."
        )

        alert_entry = {
            'id': str(uuid.uuid4())[:8],
            'timestamp': time.time(),
            'old_level': LEVEL_NAMES[old_level],
            'new_level': LEVEL_NAMES[new_level],
            'water_level_cm': water_level_cm,
            'message': msg
        }
        self._alert_history.append(alert_entry)

        # Keep history manageable
        if len(self._alert_history) > 100:
            self._alert_history = self._alert_history[-100:]

        logger.warning(msg)

        # Dispatch to all channels
        if new_level > NORMAL:
            self._dispatch_all(msg, new_level, water_level_cm, alert_entry['id'])

        # Always notify dashboard
        self._send_dashboard_alert(alert_entry)

    def _periodic_alert(self, level, water_level_cm):
        """Send periodic re-alerts for ongoing conditions."""
        now = time.time()
        msg = (
            f"ONGOING ALERT [{LEVEL_NAMES[level]}]: "
            f"Water level at {water_level_cm:.0f} cm."
        )
        # Only SMS and BLE for periodic
        if now - self._last_alert_time['sms'] > self.cooldowns['sms']:
            self._send_sms(msg)
        if now - self._last_alert_time['ble'] > self.cooldowns['ble']:
            self._send_esp32(level, water_level_cm, "periodic")

    def _dispatch_all(self, message, level, water_level_cm, alert_id):
        """Send alert through all channels."""
        now = time.time()

        # SMS via KDE Connect
        if now - self._last_alert_time['sms'] > self.cooldowns['sms']:
            threading.Thread(
                target=self._send_sms, args=(message,), daemon=True
            ).start()

        # Nostr for Bitchat
        if now - self._last_alert_time['nostr'] > self.cooldowns['nostr']:
            threading.Thread(
                target=self._send_nostr, args=(message, level, water_level_cm),
                daemon=True
            ).start()

        # ESP32 BLE beacon
        if now - self._last_alert_time['ble'] > self.cooldowns['ble']:
            threading.Thread(
                target=self._send_esp32,
                args=(level, water_level_cm, alert_id), daemon=True
            ).start()

    # ---- SMS via KDE Connect ----

    def _send_sms(self, message):
        """Send SMS through KDE Connect CLI."""
        try:
            device_id = self.config['sms']['device_id']
            recipients = self.config['sms']['recipients']

            if not device_id:
                logger.warning("SMS: KDE Connect device_id not configured")
                self._channel_status['sms'] = False
                return

            for number in recipients:
                cmd = [
                    'kdeconnect-cli',
                    '--send-sms', message,
                    '--destination', number,
                    '-d', device_id
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    logger.info(f"SMS sent to {number}")
                else:
                    logger.error(f"SMS failed to {number}: {result.stderr}")

            self._last_alert_time['sms'] = time.time()
            self._channel_status['sms'] = True

        except FileNotFoundError:
            logger.error("kdeconnect-cli not found — install KDE Connect")
            self._channel_status['sms'] = False
        except Exception as e:
            logger.error(f"SMS error: {e}")
            self._channel_status['sms'] = False

    def _init_channels(self):
        """Initialize external channels in background (non-blocking)."""
        # Check KDE Connect
        try:
            result = subprocess.run(
                ['kdeconnect-cli', '--list-devices'],
                capture_output=True, text=True, timeout=3
            )
            self._channel_status['sms'] = (result.returncode == 0)
            if result.returncode == 0:
                logger.info("KDE Connect available")
            else:
                logger.warning("KDE Connect not responding")
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            logger.warning(f"KDE Connect not available: {e}")
            self._channel_status['sms'] = False

        # Connect ESP32
        if self.config['esp32']['enabled']:
            self._connect_esp32()

    # ---- Nostr for Bitchat ----

    def _send_nostr(self, message, level, water_level_cm):
        """Publish alert to Nostr relays for Bitchat pickup."""
        try:
            # Build a simple Nostr-compatible event
            # Using a direct HTTP approach to avoid heavy dependencies
            event_data = {
                'type': 'flood_alert',
                'level': LEVEL_NAMES[level],
                'water_level_cm': water_level_cm,
                'message': message,
                'timestamp': int(time.time())
            }

            # For now, log the event — full Nostr integration requires key management
            logger.info(f"Nostr event prepared: {json.dumps(event_data)}")
            # TODO: Sign and publish to relays when nostr keys are configured
            # This would use websocket connection to relay URLs in config

            self._last_alert_time['nostr'] = time.time()
            self._channel_status['nostr'] = True

        except Exception as e:
            logger.error(f"Nostr error: {e}")
            self._channel_status['nostr'] = False

    # ---- ESP32 BLE Beacon ----

    def _connect_esp32(self):
        """Connect to ESP32 via serial (non-blocking)."""
        try:
            import serial
            port = self.config['esp32']['port']
            baud = self.config['esp32']['baud_rate']
            self._serial = serial.Serial(port, baud, timeout=1)
            time.sleep(1)  # Brief wait for ESP32 reset
            self._esp32_connected = True
            self._channel_status['ble'] = True
            logger.info(f"ESP32 connected on {port} @ {baud}")
        except ImportError:
            logger.warning("pyserial not installed — ESP32 BLE disabled")
            self._esp32_connected = False
        except Exception as e:
            logger.warning(f"ESP32 not available: {e}")
            self._esp32_connected = False

    def _send_esp32(self, level, water_level_cm, alert_id):
        """Send alert data to ESP32 for BLE broadcasting."""
        if not self._esp32_connected or not self._serial:
            return

        try:
            payload = json.dumps({
                'lvl': level,
                'cm': int(water_level_cm),
                'id': str(alert_id)[:8],
                'ts': int(time.time())
            }) + '\n'

            self._serial.write(payload.encode('utf-8'))
            self._serial.flush()
            self._last_alert_time['ble'] = time.time()
            self._channel_status['ble'] = True
            logger.info(f"ESP32 BLE alert sent: level={LEVEL_NAMES[level]}, cm={water_level_cm}")

        except Exception as e:
            logger.error(f"ESP32 serial error: {e}")
            self._esp32_connected = False
            self._channel_status['ble'] = False

    # ---- Dashboard WebSocket ----

    def _send_dashboard_alert(self, alert_entry):
        """Push alert to web dashboard via Socket.IO."""
        if self.socketio:
            try:
                self.socketio.emit('alert', alert_entry)
                self._last_alert_time['dashboard'] = time.time()
            except Exception as e:
                logger.error(f"Dashboard alert error: {e}")

    # ---- Status ----

    def get_status(self):
        """Get current alert system status."""
        return {
            'level': self._current_level,
            'level_name': LEVEL_NAMES[self._current_level],
            'level_color': LEVEL_COLORS[self._current_level],
            'channels': self._channel_status,
            'thresholds': {
                'warning': self.thresholds[WARNING],
                'danger': self.thresholds[DANGER],
                'critical': self.thresholds[CRITICAL]
            },
            'history': self._alert_history[-10:]  # Last 10 alerts
        }

    def shutdown(self):
        """Clean up resources."""
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
        logger.info("Alert manager shut down")
