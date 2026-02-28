/*
 * flood_beacon.ino — ESP32 BLE Flood Alert Beacon
 *
 * Connects to PC via USB serial, receives flood alert data,
 * and broadcasts it as BLE advertisements for Bitchat mesh relay.
 *
 * Protocol:
 *   PC sends JSON over serial: {"lvl":3,"cm":285,"id":"abc12345","ts":1709100000}
 *   ESP32 broadcasts BLE advertisement with this data encoded
 *   Bitchat phones in range pick up and relay the alert
 *
 * Board: ESP32 (any variant)
 * Upload: Arduino IDE or PlatformIO
 *
 * Required Board Package: esp32 by Espressif Systems
 */

#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEAdvertising.h>
#include <ArduinoJson.h>

// ---- Configuration ----
#define DEVICE_NAME       "FloodAlert"
#define SERIAL_BAUD       115200
#define LED_PIN           2          // Built-in LED (most ESP32 boards)
#define ADV_INTERVAL_MS   1000       // BLE advertisement interval
#define IDLE_TIMEOUT_MS   30000      // Stop advertising after 30s of no data

// Custom service UUID for flood alerts
// Using a unique UUID so Bitchat/BLE scanners can identify our beacons
#define FLOOD_SERVICE_UUID "f100da1e-0001-4c6f-6f64-416c65727421"

// ---- Globals ----
BLEAdvertising *pAdvertising;
bool isAdvertising = false;
unsigned long lastDataTime = 0;
unsigned long lastBlinkTime = 0;

// Alert data
int alertLevel = 0;        // 0=NORMAL, 1=WARNING, 2=DANGER, 3=CRITICAL
int waterLevelCm = 0;
String alertId = "";
unsigned long alertTimestamp = 0;

// LED blink patterns (ms) per alert level
const int BLINK_PATTERNS[] = {0, 2000, 500, 100};  // NORMAL=off, WARNING=slow, DANGER=fast, CRITICAL=strobe

// ---- Serial Input Buffer ----
String serialBuffer = "";

void setup() {
    Serial.begin(SERIAL_BAUD);
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    // Initialize BLE
    BLEDevice::init(DEVICE_NAME);
    pAdvertising = BLEDevice::getAdvertising();

    Serial.println("==========================================");
    Serial.println("  FLOOD ALERT BLE BEACON - ESP32");
    Serial.println("==========================================");
    Serial.println("Waiting for alert data from PC...");
    Serial.println("Format: {\"lvl\":0-3,\"cm\":123,\"id\":\"xxx\",\"ts\":123456}");
    Serial.println();

    // Flash LED to show we're alive
    for (int i = 0; i < 3; i++) {
        digitalWrite(LED_PIN, HIGH);
        delay(100);
        digitalWrite(LED_PIN, LOW);
        delay(100);
    }
}

void loop() {
    // Read serial data from PC
    readSerial();

    // Handle LED blink pattern
    updateLED();

    // Check idle timeout — stop advertising if no data received
    if (isAdvertising && (millis() - lastDataTime > IDLE_TIMEOUT_MS)) {
        stopAdvertising();
        Serial.println("[INFO] Idle timeout — advertising stopped");
    }

    delay(10);
}

// ---- Serial Communication ----
void readSerial() {
    while (Serial.available()) {
        char c = Serial.read();

        if (c == '\n' || c == '\r') {
            if (serialBuffer.length() > 0) {
                processCommand(serialBuffer);
                serialBuffer = "";
            }
        } else {
            serialBuffer += c;

            // Prevent buffer overflow
            if (serialBuffer.length() > 256) {
                serialBuffer = "";
                Serial.println("[ERROR] Buffer overflow — cleared");
            }
        }
    }
}

void processCommand(String json) {
    // Parse JSON from PC
    StaticJsonDocument<256> doc;
    DeserializationError error = deserializeJson(doc, json);

    if (error) {
        Serial.print("[ERROR] JSON parse failed: ");
        Serial.println(error.c_str());
        return;
    }

    // Extract alert data
    alertLevel = doc["lvl"] | 0;
    waterLevelCm = doc["cm"] | 0;
    alertId = doc["id"] | "unknown";
    alertTimestamp = doc["ts"] | 0;
    lastDataTime = millis();

    Serial.printf("[ALERT] Level: %d, Water: %d cm, ID: %s\n",
                  alertLevel, waterLevelCm, alertId.c_str());

    if (alertLevel > 0) {
        // Start or update BLE advertisement
        startAdvertising();
    } else {
        // NORMAL level — stop advertising
        stopAdvertising();
    }
}

// ---- BLE Advertising ----
void startAdvertising() {
    // Build manufacturer-specific data payload
    // Format: [alert_level(1), water_cm_hi(1), water_cm_lo(1), hop_count(1), msg_id(4)]
    uint8_t payload[8];
    payload[0] = (uint8_t)alertLevel;
    payload[1] = (uint8_t)((waterLevelCm >> 8) & 0xFF);  // high byte
    payload[2] = (uint8_t)(waterLevelCm & 0xFF);           // low byte
    payload[3] = 0;  // hop count (starts at 0, relays increment)

    // Message ID (first 4 bytes of the alert ID hash)
    unsigned long hash = 0;
    for (unsigned int i = 0; i < alertId.length(); i++) {
        hash = hash * 31 + alertId[i];
    }
    payload[4] = (hash >> 24) & 0xFF;
    payload[5] = (hash >> 16) & 0xFF;
    payload[6] = (hash >> 8) & 0xFF;
    payload[7] = hash & 0xFF;

    // Configure advertisement
    BLEAdvertisementData advData;

    // Set the service UUID so Bitchat/scanners can filter for our beacons
    advData.setFlags(ESP_BLE_ADV_FLAG_GEN_DISC | ESP_BLE_ADV_FLAG_BREDR_NOT_SPT);

    // Set manufacturer data (company ID 0xFFFF = testing/prototype)
    String mfrData = "";
    mfrData += (char)0xFF;  // Company ID low byte
    mfrData += (char)0xFF;  // Company ID high byte
    for (int i = 0; i < 8; i++) {
        mfrData += (char)payload[i];
    }
    advData.setManufacturerData(mfrData);

    // Set local name to include alert info
    String name = "FLOOD-L";
    name += String(alertLevel);
    name += "-";
    name += String(waterLevelCm);
    advData.setName(name.c_str());

    // Apply and start
    pAdvertising->stop();
    pAdvertising->setAdvertisementData(advData);
    pAdvertising->setMinInterval(0x20);  // 20ms * 0.625 = 12.5ms
    pAdvertising->setMaxInterval(0x40);  // 40ms * 0.625 = 25ms
    pAdvertising->start();

    isAdvertising = true;

    Serial.printf("[BLE] Broadcasting: FLOOD-L%d-%dcm (hop:0, id:%s)\n",
                  alertLevel, waterLevelCm, alertId.c_str());
}

void stopAdvertising() {
    if (isAdvertising) {
        pAdvertising->stop();
        isAdvertising = false;
        alertLevel = 0;
        Serial.println("[BLE] Advertising stopped");
    }
}

// ---- LED Status ----
void updateLED() {
    if (alertLevel == 0) {
        digitalWrite(LED_PIN, LOW);
        return;
    }

    int blinkInterval = BLINK_PATTERNS[alertLevel];
    if (blinkInterval == 0) {
        digitalWrite(LED_PIN, LOW);
        return;
    }

    if (millis() - lastBlinkTime >= (unsigned long)blinkInterval) {
        digitalWrite(LED_PIN, !digitalRead(LED_PIN));
        lastBlinkTime = millis();
    }
}
