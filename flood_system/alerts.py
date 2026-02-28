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
import re
import threading
import time
import json
import uuid
import hashlib
import secrets
import logging

try:
    import websocket as ws_client
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False

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

        # KDE Connect resolved device flag/id
        self._kde_device_flag = None  # e.g. '-n' or '-d'
        self._kde_device_value = None  # e.g. 'Y18' or UUID

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

    def _resolve_kde_device(self):
        """Resolve KDE Connect device — determine if config has name or UUID."""
        device_id = self.config['sms'].get('device_id', '')
        if not device_id:
            return False

        # UUID pattern: hex chars with underscores or hyphens, length > 16
        is_uuid = bool(re.match(r'^[a-f0-9_\-]{16,}$', device_id, re.IGNORECASE))

        if is_uuid:
            self._kde_device_flag = '-d'
            self._kde_device_value = device_id
            logger.info(f"KDE Connect: using device UUID '{device_id}'")
        else:
            # It's a device name — use -n flag
            self._kde_device_flag = '-n'
            self._kde_device_value = device_id
            logger.info(f"KDE Connect: using device name '{device_id}'")

        return True

    def _send_sms(self, message):
        """Send SMS through KDE Connect CLI."""
        try:
            recipients = self.config['sms']['recipients']

            if not self._kde_device_flag or not self._kde_device_value:
                if not self._resolve_kde_device():
                    logger.warning("SMS: KDE Connect device not configured")
                    self._channel_status['sms'] = False
                    return

            for number in recipients:
                cmd = [
                    'kdeconnect-cli',
                    '--send-sms', message,
                    '--destination', number,
                    self._kde_device_flag, self._kde_device_value
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if result.returncode == 0:
                    logger.info(f"SMS sent to {number}")
                else:
                    err = result.stderr.strip()
                    # If -d failed, try -n as fallback
                    if self._kde_device_flag == '-d' and 'find device' in err.lower():
                        logger.warning(f"UUID lookup failed, retrying with device name...")
                        cmd[5] = '-n'
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                        if result.returncode == 0:
                            self._kde_device_flag = '-n'
                            logger.info(f"SMS sent to {number} (via name fallback)")
                            continue
                    logger.error(f"SMS failed to {number}: {err}")

            self._last_alert_time['sms'] = time.time()
            self._channel_status['sms'] = True

        except FileNotFoundError:
            logger.error("kdeconnect-cli not found — install KDE Connect")
            self._channel_status['sms'] = False
        except subprocess.TimeoutExpired:
            logger.error("SMS send timed out — is the phone reachable?")
        except Exception as e:
            logger.error(f"SMS error: {e}")
            self._channel_status['sms'] = False

    def _init_channels(self):
        """Initialize external channels in background (non-blocking)."""
        # Check KDE Connect and resolve device
        try:
            result = subprocess.run(
                ['kdeconnect-cli', '--list-devices'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                logger.info(f"KDE Connect available: {result.stdout.strip()}")
                self._resolve_kde_device()
                self._channel_status['sms'] = True
            else:
                logger.warning("KDE Connect not responding")
                self._channel_status['sms'] = False
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            logger.warning(f"KDE Connect not available: {e}")
            self._channel_status['sms'] = False

        # Connect ESP32
        if self.config['esp32']['enabled']:
            self._connect_esp32()

    # ---- Nostr for Bitchat ----

    def _get_nostr_privkey(self):
        """Get or generate a Nostr private key (32-byte hex)."""
        pk = self.config.get('nostr', {}).get('private_key', '')
        if not pk or len(pk) < 64:
            import secrets
            pk = secrets.token_hex(32)
            if 'nostr' not in self.config:
                self.config['nostr'] = {}
            self.config['nostr']['private_key'] = pk
            logger.info("Generated new Nostr private key (saved to config)")
            # Save key
            try:
                import yaml
                with open('config.yaml', 'w') as f:
                    yaml.dump(self.config, f, default_flow_style=False)
            except Exception:
                pass
        return pk

    def _send_nostr(self, message, level, water_level_cm):
        """Publish alert to Nostr relays for Bitchat pickup."""
        if not HAS_WEBSOCKET:
            logger.warning("Nostr: websocket-client not installed, skipping")
            self._channel_status['nostr'] = False
            return
            
        try:
            from nostr.event import Event
            from nostr.key import PrivateKey
        except ImportError:
            logger.warning("Nostr: python-nostr not installed, please pip install nostr")
            self._channel_status['nostr'] = False
            return

        try:
            privkey_hex = self._get_nostr_privkey()
            pk = PrivateKey(bytes.fromhex(privkey_hex))
            pubkey = pk.public_key.hex()

            created_at = int(time.time())
            # kind 1 text note
            tags = [
                ["t", "flood_alert"],
                ["t", "hydroguard"],
                ["level", LEVEL_NAMES[level]],
                ["water_cm", str(int(water_level_cm))]
            ]
            content = f"🌊 HYDROGUARD FLOOD ALERT [{LEVEL_NAMES[level]}]\n\nWater level: {water_level_cm:.0f} cm\n{message}"

            event = Event(public_key=pubkey, content=content, kind=1, created_at=created_at, tags=tags)
            pk.sign_event(event)

            # Manually extract Enum value if python-nostr uses Enums for EventKind
            kind_val = event.kind.value if hasattr(event.kind, 'value') else event.kind
            
            event_dict = {
                "id": event.id,
                "pubkey": event.public_key,
                "created_at": event.created_at,
                "kind": kind_val,
                "tags": event.tags,
                "content": event.content,
                "sig": event.signature
            }

            relay_msg = json.dumps(["EVENT", event_dict])
            relays = self.config.get('nostr', {}).get('relays', [])

            success_count = 0
            for relay_url in relays:
                try:
                    ws = ws_client.create_connection(relay_url, timeout=5)
                    ws.send(relay_msg)
                    try:
                        resp = ws.recv()
                        logger.info(f"Nostr relay {relay_url}: {resp[:120]}")
                    except Exception:
                        pass
                    ws.close()
                    success_count += 1
                except Exception as e:
                    logger.warning(f"Nostr relay {relay_url} failed: {e}")

            if success_count > 0:
                logger.info(f"Nostr event published to {success_count}/{len(relays)} relays")
            else:
                logger.warning("Nostr: failed to reach any relay")

            self._last_alert_time['nostr'] = time.time()
            self._channel_status['nostr'] = success_count > 0

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
