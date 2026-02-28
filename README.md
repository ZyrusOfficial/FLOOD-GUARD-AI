# 🌊 Flood Early Warning System

AI-powered early warning system that uses computer vision to monitor river levels in real-time and broadcasts life-saving flood alerts through a **triple-redundant communication network** — combining a web dashboard, automated SMS, and Bitchat Bluetooth mesh messaging — to ensure the community stays informed even if the town's internet and cellular infrastructure fail during a disaster.

## Architecture

```
📷 Phone (DroidCam)                     🌐 Web Dashboard
   ↓ MJPEG over Wi-Fi                      ↑ Flask + Socket.IO
💻 PC (Main System)  ───────────────────────┘
   ├── camera.py      → DroidCam stream
   ├── detector.py    → water level CV detection
   ├── alerts.py      → triple alert channels:
   │   ├── 📲 SMS via KDE Connect
   │   ├── 📡 Nostr → Bitchat mesh relay
   │   └── 🔵 Serial → ESP32 BLE beacon
   ├── dashboard.py   → Flask web dashboard
   └── app.py         → orchestrator

📟 ESP32 (USB) → BLE beacon → Bitchat phones relay
```

## Prerequisites

| Item | Purpose |
|------|---------|
| Python 3.8+ | Run the main system |
| Android phone + [DroidCam](https://www.dev47apps.com/) | Camera source |
| [KDE Connect](https://kdeconnect.kde.org/) on phone + PC | Send SMS alerts |
| ESP32 board + USB cable | BLE beacon broadcasting |
| [Arduino IDE](https://www.arduino.cc/en/software) | Flash ESP32 firmware |
| [Bitchat](https://bitchat.cv) on community phones | Mesh relay alerts |

## Quick Start

### 1. Install Python Dependencies

```bash
cd flood_system
pip install -r requirements.txt
```

### 2. Configure the System

Edit `flood_system/config.yaml`:

```yaml
camera:
  droidcam_ip: "YOUR_PHONE_IP"     # Shown in DroidCam app
  stream_url: "http://YOUR_PHONE_IP:4747/video"

sms:
  device_id: "YOUR_KDE_CONNECT_DEVICE_ID"  # Find via: kdeconnect-cli --list-devices
  recipients:
    - "+639XXXXXXXXX"

esp32:
  port: "/dev/ttyUSB0"    # Your ESP32's serial port
  enabled: true
```

### 3. Flash the ESP32

1. Open `esp32_beacon/flood_beacon.ino` in Arduino IDE
2. Install the **ESP32 board package** and **ArduinoJson** library
3. Select your ESP32 board and port
4. Upload

### 4. Set Up DroidCam

1. Install DroidCam on your Android phone
2. Connect phone and PC to the same Wi-Fi network
3. Open DroidCam on phone — note the IP address shown
4. Update `config.yaml` with the IP

### 5. Calibrate (First Time)

```bash
cd flood_system
python calibrate.py
```

- Click to set the **ROI** (region of interest) around the water gauge
- Click to set **top** and **bottom** reference points for cm calibration
- Press `s` to save

### 6. Run the System

```bash
cd flood_system
python app.py
```

Open **http://localhost:5000** in your browser to see the dashboard.

## Alert Levels

| Level | Color | Water Level | Action |
|-------|-------|-------------|--------|
| NORMAL | 🟢 Green | < 220 cm | Monitoring |
| WARNING | 🟡 Yellow | ≥ 220 cm | Elevated — watch closely |
| DANGER | 🟠 Orange | ≥ 260 cm | Prepare to evacuate |
| CRITICAL | 🔴 Red | ≥ 290 cm | **EVACUATE IMMEDIATELY** |

## Alert Channels

| Channel | Method | Works When |
|---------|--------|------------|
| 🌐 Web Dashboard | Flask + Socket.IO | Internet up |
| 📲 SMS | KDE Connect → phone | Cell network up |
| 📡 Bitchat/BLE | ESP32 beacon → Bitchat mesh | **Everything else down** |

## Project Structure

```
FLOOD_DETECT/
├── WaterLevelDetection/      # Original repo (untouched)
├── flood_system/
│   ├── app.py                # Main entry point
│   ├── camera.py             # DroidCam connector
│   ├── detector.py           # Water level detection (CV)
│   ├── alerts.py             # SMS + Nostr + ESP32 alerts
│   ├── dashboard.py          # Flask web dashboard
│   ├── calibrate.py          # Interactive calibration
│   ├── config.yaml           # Configuration
│   ├── requirements.txt
│   ├── static/css/style.css
│   ├── static/js/dashboard.js
│   └── templates/index.html
├── esp32_beacon/
│   └── flood_beacon.ino      # ESP32 BLE firmware
└── README.md
```

## How the BLE Mesh Works

1. **PC detects flood** → sends alert to ESP32 via USB serial
2. **ESP32 broadcasts** BLE advertisement: `FLOOD-L3-285cm`
3. **Bitchat phones** in range (~100m) pick up the beacon
4. Each phone **relays** the alert to the next, creating a mesh chain
5. Alert **ripples across the community** — no internet needed

## Credits

- Water level detection algorithm based on [WaterLevelDetection](https://github.com/aldinorizaldy/WaterLevelDetection) by Aldino Rizaldy
- Mesh messaging via [Bitchat](https://bitchat.cv)
