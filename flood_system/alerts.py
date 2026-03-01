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
import secrets
import logging
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
import asyncio
import os
import sys
from datetime import datetime

# Local imports
from briar_client import BriarClient

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

    def __init__(self, config, config_path=None, socketio=None):
        self.config = config
        self.config_path = config_path
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
            'sms': cooldowns.get('sms', 300),
            'nostr': cooldowns.get('nostr', 120),
            'ble': cooldowns.get('ble', 30),
            'dashboard': cooldowns.get('dashboard', 10),
            'telegram': cooldowns.get('telegram', 300),
            'bitchat': config.get('bitchat', {}).get('cooldown', 60)
        }

        # State
        self._current_level = NORMAL
        self._last_alert_time = {
            'sms': 0,
            'nostr': 0,
            'ble': 0,
            'dashboard': 0,
            'telegram': 0,
            'bitchat': 0
        }
        self._last_alert_level = {
            'sms': 0,
            'nostr': 0,
            'ble': 0,
            'dashboard': 0,
            'telegram': 0,
            'bitchat': 0
        }
        self._alert_history = []
        self._lock = threading.Lock()
        self._last_dispatched_cm = 0
        self._burst_lock = threading.Lock()
        self._is_bursting = False

        # ESP32 serial connection
        self._serial = None
        self._esp32_connected = False

        # KDE Connect resolved device flag/id
        self._kde_device_flag = None  # e.g. '-n' or '-d'
        self._kde_device_value = None  # e.g. 'Y18' or UUID

        # Briar Client
        briar_cfg = self.config.get('briar', {})
        self.briar = BriarClient(
            api_url=briar_cfg.get('api_url', 'http://localhost:8080'),
            api_token=briar_cfg.get('api_token')
        )

        # Channel status (start with defaults, update async)
        self._channel_status = {
            'dashboard': True,
            'sms': False,
            'ble': False,
            'nostr': True,
            'briar': False,
            'telegram': False
        }

        self._telegram_registered_chats = set()
        tg_chats = self.config.get('telegram', {}).get('chat_id', '')
        if tg_chats:
            # Handle both single string and comma-separated string
            for cid in str(tg_chats).split(','):
                if cid.strip():
                    self._telegram_registered_chats.add(cid.strip())

        # BitChat Initializations (Bridge manages its own threads)
        self._bitchat_loop = None
        self._bitchat_client = None

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

            # Level changed OR significant (5%) level change in alert state
            level_changed = (new_level != self._current_level)
            
            # 5% change threshold (relative to calibration max)
            top_cm = self.config.get('detection', {}).get('calibration', {}).get('top_cm', 200)
            threshold = top_cm * 0.05
            significant_change = abs(water_level_cm - self._last_dispatched_cm) >= threshold

            if level_changed or (new_level > NORMAL and significant_change):
                old_level = self._current_level
                self._current_level = new_level
                self._last_dispatched_cm = water_level_cm
                
                # Reset tracking at NORMAL
                if new_level == NORMAL:
                    self._last_dispatched_cm = 0
                
                self._on_level_change(old_level, new_level, water_level_cm)
            
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
            force = new_level > old_level
            self._dispatch_all(msg, new_level, water_level_cm, alert_entry['id'], force=force)

        # Always notify dashboard
        self._send_dashboard_alert(alert_entry)

        # Reset escalation tracking when returning to NORMAL
        if new_level == NORMAL:
            for ch in self._last_alert_level:
                self._last_alert_level[ch] = 0
            logger.info("Alert level returned to NORMAL. Escalation tracking reset.")


    def _dispatch_all(self, message, level, water_level_cm, alert_id, force=False):
        """Send alert burst through all channels."""
        # Burst logic: 3 messages, 5 seconds apart
        def execute_burst():
            with self._burst_lock:
                if self._is_bursting:
                    return # Avoid overlapping bursts if changes are too rapid
                self._is_bursting = True

            try:
                for i in range(3):
                    # Dispatch to all enabled and configured channels
                    # We bypass individual cooldowns here as the 5% rule is our new throttle
                    
                    # SMS
                    threading.Thread(target=self._send_sms, args=(message,), daemon=True).start()
                    
                    # Telegram
                    self._send_telegram(message)
                    
                    # Nostr
                    threading.Thread(target=self._send_nostr, args=(message, level, water_level_cm), daemon=True).start()
                    
                    # Briar
                    if self.config.get('briar', {}).get('enabled', True):
                        threading.Thread(target=self.briar.send_alert, args=(message,), daemon=True).start()
            finally:
                with self._burst_lock:
                    self._is_bursting = False

        threading.Thread(target=execute_burst, daemon=True).start()

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
            logger.debug(f"SMS channel updated: last_time={self._last_alert_time['sms']}")

        except FileNotFoundError:
            logger.error("kdeconnect-cli not found — install KDE Connect")
            self._channel_status['sms'] = False
        except subprocess.TimeoutExpired:
            logger.error("SMS send timed out — is the phone reachable?")
        except Exception as e:
            logger.error(f"SMS error: {e}")
            self._channel_status['sms'] = False

    def _send_telegram(self, message):
        """Send alert via Telegram Bot API to all registered chats."""
        if not self.config.get('telegram', {}).get('enabled', False):
            return

        token = self.config['telegram']['token']
        if not token or not self._telegram_registered_chats:
            return

        def post_telegram(target_id):
            try:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {
                    "chat_id": target_id,
                    "text": message,
                    "parse_mode": "HTML"
                }
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    logger.info(f"Telegram: Alert sent to {target_id} successfully")
                    self._last_alert_time['telegram'] = time.time()
                    self._channel_status['telegram'] = True
                else:
                    logger.error(f"Telegram error for {target_id}: {response.text}")
            except Exception as e:
                logger.error(f"Telegram dispatch failed for {target_id}: {e}")

        # Send to all registered chats
        for cid in self._telegram_registered_chats:
            threading.Thread(target=post_telegram, args=(cid,), daemon=True).start()

    def _briar_status_loop(self):
        """Periodically check Briar connection and sync forum."""
        while True:
            try:
                if self.briar.check_connection():
                    if not self._channel_status['briar']:
                        logger.info("Briar Headless REST API connected")
                    self._channel_status['briar'] = True
                    # Periodically sync forum ID if missing
                    if not self.briar._forum_id:
                        self.briar.sync_forum()
                else:
                    if self._channel_status['briar']:
                        logger.warning("Briar Headless REST API disconnected")
                    self._channel_status['briar'] = False
            except Exception as e:
                logger.debug(f"Briar status check failed: {e}")
                self._channel_status['briar'] = False
            
            time.sleep(30) # Check every 30 seconds

    def _telegram_poll_loop(self):
        """Poll for /start or /register commands to capture chat IDs."""
        token = self.config.get('telegram', {}).get('token')
        if not token: return

        last_update_id = 0
        logger.info("Telegram: Polling for registration commands...")

        while True:
            try:
                url = f"https://api.telegram.org/bot{token}/getUpdates"
                params = {"offset": last_update_id + 1, "timeout": 30}
                response = requests.get(url, params=params, timeout=35)
                
                if response.status_code == 200:
                    data = response.json()
                    for update in data.get("result", []):
                        last_update_id = update["update_id"]
                        message = update.get("message", {})
                        text = message.get("text", "")
                        chat_id = str(message.get("chat", {}).get("id", ""))
                        
                        if text in ["/start", "/register"] and chat_id:
                            if chat_id not in self._telegram_registered_chats:
                                self._telegram_registered_chats.add(chat_id)
                                logger.info(f"Telegram: New registration from chat_id {chat_id}")
                                # Send confirmation
                                self._send_telegram_direct(chat_id, "✅ <b>Registration Successful!</b>\nYou will now receive flood alerts from HydroGuard.")
                                # Save to config (persists as comma-separated if multiple, for now just update)
                                self._update_config_telegram_chat(chat_id)
                elif response.status_code == 401:
                    logger.error("Telegram: Unauthorized (invalid token). Stopping poll.")
                    break
                
                time.sleep(2)
            except requests.exceptions.RequestException as e:
                logger.warning(f"Telegram polling network error: {e}")
                time.sleep(10)
            except Exception as e:
                logger.error(f"Telegram polling error: {e}")
                time.sleep(10)

    def _send_telegram_direct(self, chat_id, text):
        """Internal helper to send a direct message."""
        token = self.config['telegram']['token']
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            requests.post(url, json=payload, timeout=10)
        except: pass

    def _update_config_telegram_chat(self, chat_id):
        """Update config.yaml with the new chat_id."""
        if not self.config_path:
            logger.warning("Telegram: Cannot save chat_id, config_path not set")
            return
            
        try:
            # If multiple chats, store as comma-separated string for simplicity
            current = self.config['telegram'].get('chat_id', '')
            if current:
                chats = set(str(current).split(','))
                chats.add(str(chat_id))
                self.config['telegram']['chat_id'] = ','.join(chats)
            else:
                self.config['telegram']['chat_id'] = str(chat_id)
                
            with open(self.config_path, 'w') as f:
                import yaml
                yaml.dump(self.config, f, default_flow_style=False)
            logger.info(f"Telegram: config.yaml updated with chat_id(s): {self.config['telegram']['chat_id']}")
        except Exception as e:
            logger.error(f"Failed to update config with Telegram chat_id: {e}")

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

        # Start Briar Status Loop
        if self.config.get('briar', {}).get('enabled', True):
            threading.Thread(target=self._briar_status_loop, daemon=True).start()

        # Initial Telegram check
        if self.config.get('telegram', {}).get('enabled', False):
            token = self.config['telegram']['token']
            if token:
                try:
                    # Start polling in background
                    threading.Thread(target=self._telegram_poll_loop, daemon=True).start()
                    self._channel_status['telegram'] = True
                except:
                    self._channel_status['telegram'] = False

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
            nickname = self.config.get('bitchat', {}).get('nickname', 'HYDROGUARD_NODE')
            
            # Base tags for BitChat compatibility
            tags = [
                ["t", "flood_alert"],
                ["t", "hydroguard"],
                ["level", LEVEL_NAMES[level]],
                ["water_cm", str(int(water_level_cm))],
                ["n", nickname]  # Critical for BitChat display name
            ]
            
            # Add geohash tag if available (crucial for BitChat "Location" view)
            geohash = self.config.get('nostr', {}).get('geohash')
            if geohash:
                tags.append(["g", geohash])
                logger.debug(f"Nostr: Added geohash tag: {geohash}")

            content = f"🌊 HYDROGUARD [ {LEVEL_NAMES[level]} ]\n\nWater level: {water_level_cm:.0f} cm\n{message}"

            # --- SEND PUBLIC EVENTS ---
            # 1. Kind 1 (Text Note / Location Note)
            event1 = Event(public_key=pubkey, content=content, kind=1, created_at=created_at, tags=tags)
            pk.sign_event(event1)
            self._publish_event(event1)

            # 2. Kind 20000 (Ephemeral / Geochat) - This is what shows up in the BitChat chat tab
            event20000 = Event(public_key=pubkey, content=content, kind=20000, created_at=created_at, tags=tags)
            pk.sign_event(event20000)
            self._publish_event(event20000)

            self._last_alert_time['nostr'] = time.time()
            self._channel_status['nostr'] = True

            # --- SEND PRIVATE ALERT TO ADMIN ---
            admin_npub = self.config.get('nostr', {}).get('admin_npub')
            if admin_npub:
                # Note: BitChat app expects NIP-17 (Kind 1059) for private chats.
                # Kind 4 is sent as a fallback.
                self._send_nostr_private(pk, admin_npub, f"⚠️ PRIVATE ALERT: {content}")

        except Exception as e:
            logger.error(f"Nostr error: {e}")
            self._channel_status['nostr'] = False

    def _send_nostr_private(self, sender_pk, recipient_npub, message):
        """Send an encrypted private message (Kind 4) to the admin's npub."""
        try:
            from nostr.event import Event
            from nostr.key import PublicKey
            import bech32
            
            # Convert npub to hex
            decoded = bech32.bech32_decode(recipient_npub)
            if len(decoded) == 3:
                hrp, data, spec = decoded
            else:
                hrp, data = decoded

            if hrp != 'npub':
                logger.error(f"Nostr: Invalid NPUB HRP: {hrp}")
                return
            
            pubkey_bytes = bytes(bech32.convertbits(data, 5, 8, False))
            recipient_pubkey_hex = pubkey_bytes.hex()
            
            # Encrypt message (NIP-04)
            # Use the sender's private key to encrypt for recipient
            encrypted_content = sender_pk.encrypt_message(message, recipient_pubkey_hex)
            
            # Create Event (Kind 4)
            dm = Event(
                public_key=sender_pk.public_key.hex(),
                content=encrypted_content,
                kind=4,
                tags=[["p", recipient_pubkey_hex]]
            )
            sender_pk.sign_event(dm)
            
            self._publish_event(dm)
            logger.info(f"Nostr: Private alert sent (Kind 4) to {recipient_npub}")
            
        except Exception as e:
            logger.error(f"Nostr Private error: {e}")

    def _nip44_encrypt(self, sender_pk, recipient_pubkey_hex, plaintext):
        """Perform NIP-44 v2 encryption placeholder."""
        # TODO: Implement full XChaCha20-Poly1305 if needed
        return ""

    def _publish_event(self, event):
        """Internal helper to push an event to configured relays."""
        import json
        
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

        def push_to_relay(url):
            try:
                import websocket as ws_client
                ws = ws_client.create_connection(url, timeout=5)
                ws.send(relay_msg)
                
                # Wait for response (OK or NOTICE)
                response = ws.recv()
                ws.close()
                
                if response:
                    logger.info(f"Nostr: Relay {url} response: {response}")
                else:
                    logger.info(f"Nostr: Event sent to {url} (no immediate response)")
                return True
            except Exception as e:
                logger.debug(f"Nostr relay {url} failed: {e}")
                return False

        threads = []
        for relay_url in relays:
            t = threading.Thread(target=push_to_relay, args=(relay_url,), daemon=True)
            t.start()
            threads.append(t)

        # Wait briefly for some to finish, but don't block the system
        # We consider it "sent" if we started the threads
        self._last_alert_time['nostr'] = time.time()
        self._channel_status['nostr'] = True
        logger.info(f"Nostr: Publishing event {event.id[:8]} to {len(relays)} relays...")
        return True

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
        
        if self._bitchat_loop:
            try:
                self._bitchat_loop.call_soon_threadsafe(self._bitchat_loop.stop)
            except Exception:
                pass

        logger.info("Alert manager shut down")

    def _send_bitchat(self, message):
        """Send message through the BitChat bridge."""
        logger.info(f"BitChat bridge: Broadcasting alert: {message[:50]}...")
        self.bitchat_bridge.send_message(message)
