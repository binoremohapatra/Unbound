// ==============================================================================
// OmniBand All-In-One Single File Firmware (Arduino IDE Copy-Paste Ready)
// ==============================================================================
// EXACT PINS AS PER YOUR WIRING:
//   - CH1: GPIO 32  (Left Temple / Left eyebrow)
//   - CH2: GPIO 34  (Centre-Left Forehead)
//   - CH3: GPIO 33  (Centre-Right Forehead)
//   - CH4: GPIO 26  (Right Temple / Jaw Clench Click Trigger)
//   - GND: Sensor Ground
//   - 3.3V: Sensor VCC
// ==============================================================================
// BLUETOOTH BLE SPECS:
//   - Device Name:         "Omniband"
//   - Service UUID:        "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
//   - Characteristic UUID: "beb5483e-36e1-4688-b7f5-ea07361b26a6"
//   - Permissions:         READ, WRITE (PC to ESP32), NOTIFY (ESP32 to PC)
// ==============================================================================

#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// -----------------------------------------------------------------------------
// Hardware Pins (Matching Dishti & your physical wiring)
// -----------------------------------------------------------------------------
const int PIN_CH1 = 32;
const int PIN_CH2 = 34;
const int PIN_CH3 = 33;
const int PIN_CH4 = 26;

// -----------------------------------------------------------------------------
// BLE Constants
// -----------------------------------------------------------------------------
#define BLE_DEVICE_NAME     "Omniband"
#define SERVICE_UUID        "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define CHARACTERISTIC_UUID "beb5483e-36e1-4688-b7f5-ea07361b26a6"

// -----------------------------------------------------------------------------
// Global BLE & State Variables
// -----------------------------------------------------------------------------
BLEServer* pServer = nullptr;
BLECharacteristic* pCharacteristic = nullptr;
bool deviceConnected = false;
bool oldDeviceConnected = false;
uint32_t packetSeq = 0;

// Baseline noise tracking & envelope filter
float baseline[4] = {2048.0f, 2048.0f, 2048.0f, 2048.0f};
float envelope[4] = {0.0f, 0.0f, 0.0f, 0.0f};
const float ALPHA_BASELINE = 0.005f; // Slow drift cancellation
const float ALPHA_ENVELOPE = 0.25f;  // Fast somatic envelope response

// -----------------------------------------------------------------------------
// BLE Server Callbacks (Connection Management)
// -----------------------------------------------------------------------------
class ServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) override {
        deviceConnected = true;
        Serial.println("[BLE] >>> UNBOUND APP CONNECTED VIA BLUETOOTH! <<<");
    }

    void onDisconnect(BLEServer* pServer) override {
        deviceConnected = false;
        Serial.println("[BLE] Client disconnected. Re-advertising...");
    }
};

// -----------------------------------------------------------------------------
// BLE Characteristic Callbacks (PC se ESP32 ke andar Input aane par)
// -----------------------------------------------------------------------------
class CharCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic* pChar) override {
        String rxValue = pChar->getValue();
        if (rxValue.length() > 0) {
            Serial.print("[BLE INPUT FROM PC]: ");
            Serial.println(rxValue.c_str());
            
            // Handle calibration or reset command from PC
            if (rxValue.indexOf("CAL") >= 0 || rxValue.indexOf("RESET") >= 0) {
                for (int i = 0; i < 4; i++) envelope[i] = 0.0f;
                Serial.println("[ESP32] Envelope reset/calibrated by PC command.");
            }
        }
    }
};

// -----------------------------------------------------------------------------
// BLE Setup Function
// -----------------------------------------------------------------------------
void setupBLE() {
    Serial.println("[BLE] Initializing ESP32 BLE Stack...");
    BLEDevice::init(BLE_DEVICE_NAME);

    // 1. Create Server
    pServer = BLEDevice::createServer();
    pServer->setCallbacks(new ServerCallbacks());

    // 2. Create Service
    BLEService* pService = pServer->createService(SERVICE_UUID);

    // 3. Create Characteristic with READ, WRITE, NOTIFY
    pCharacteristic = pService->createCharacteristic(
        CHARACTERISTIC_UUID,
        BLECharacteristic::PROPERTY_READ   |
        BLECharacteristic::PROPERTY_WRITE  |
        BLECharacteristic::PROPERTY_NOTIFY |
        BLECharacteristic::PROPERTY_INDICATE
    );

    pCharacteristic->addDescriptor(new BLE2902());
    pCharacteristic->setCallbacks(new CharCallbacks());
    pCharacteristic->setValue("Omniband Ready");

    // 4. Start Service
    pService->start();

    // 5. Start Advertising so PC can see "Omniband"
    BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
    pAdvertising->addServiceUUID(SERVICE_UUID);
    pAdvertising->setScanResponse(true);
    pAdvertising->setMinPreferred(0x06); // WinRT / Windows 11 compatibility
    pAdvertising->setMinPreferred(0x12);
    BLEDevice::startAdvertising();

    Serial.println("[BLE] Advertising ON. Device visible as 'Omniband'");
}

// -----------------------------------------------------------------------------
// Arduino Setup
// -----------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    delay(500);

    Serial.println("\n==============================================");
    Serial.println("  OmniBand All-In-One Single-File Firmware    ");
    Serial.println("  Pins: CH1=32, CH2=34, CH3=33, CH4=26        ");
    Serial.println("  BLE Device: 'Omniband' (Bidirectional)      ");
    Serial.println("==============================================");

    // Set pin modes
    pinMode(PIN_CH1, INPUT);
    pinMode(PIN_CH2, INPUT);
    pinMode(PIN_CH3, INPUT);
    pinMode(PIN_CH4, INPUT);

    // Initialize BLE Server & Service
    setupBLE();

    // Settle baseline for 500ms
    Serial.println("[ADC] Settling sensor baselines...");
    for (int i = 0; i < 50; i++) {
        baseline[0] = baseline[0] * 0.9f + analogRead(PIN_CH1) * 0.1f;
        baseline[1] = baseline[1] * 0.9f + analogRead(PIN_CH2) * 0.1f;
        baseline[2] = baseline[2] * 0.9f + analogRead(PIN_CH3) * 0.1f;
        baseline[3] = baseline[3] * 0.9f + analogRead(PIN_CH4) * 0.1f;
        delay(10);
    }
    Serial.println("[ADC] Baseline calibration locked. Streaming ready!");
}

// -----------------------------------------------------------------------------
// Arduino Main Loop (Runs at 50 Hz)
// -----------------------------------------------------------------------------
void loop() {
    // 1. Read raw ADC on your exact pins (32, 34, 33, 26)
    int raw[4];
    raw[0] = analogRead(PIN_CH1);
    raw[1] = analogRead(PIN_CH2);
    raw[2] = analogRead(PIN_CH3);
    raw[3] = analogRead(PIN_CH4);

    // 2. Compute rectified envelope above baseline
    for (int i = 0; i < 4; i++) {
        baseline[i] = (1.0f - ALPHA_BASELINE) * baseline[i] + ALPHA_BASELINE * (float)raw[i];
        float rectified = abs((float)raw[i] - baseline[i]);
        envelope[i] = (1.0f - ALPHA_ENVELOPE) * envelope[i] + ALPHA_ENVELOPE * rectified;
    }

    // 3. Format packet: S,ch1,ch2,ch3,ch4,seq,dx,dy\n
    // Exact format that Unbound ble_transport.py parses
    char packet[96];
    packetSeq++;
    snprintf(packet, sizeof(packet), "S,%.1f,%.1f,%.1f,%.1f,%u,0.0,0.0\n",
             envelope[0], envelope[1], envelope[2], envelope[3], packetSeq);

    // 4. USB Serial streaming (agar USB cable se plug kiya ho)
    Serial.print(packet);

    // 5. BLE Notify streaming (agar Bluetooth se connected ho)
    if (deviceConnected && pCharacteristic != nullptr) {
        pCharacteristic->setValue((uint8_t*)packet, strlen(packet));
        pCharacteristic->notify();
    }

    // Auto-restart advertising agar disconnect ho jaye
    if (!deviceConnected && oldDeviceConnected) {
        delay(300);
        pServer->startAdvertising();
        oldDeviceConnected = deviceConnected;
    }
    if (deviceConnected && !oldDeviceConnected) {
        oldDeviceConnected = deviceConnected;
    }

    // Handle USB Serial inputs from PC (agar PC USB se command bheje)
    if (Serial.available()) {
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();
        if (cmd == "HELLO" || cmd == "STATUS") {
            Serial.println("OK:OMNIBAND_V2_READY");
        } else if (cmd == "CAL" || cmd == "RESET") {
            for (int i = 0; i < 4; i++) envelope[i] = 0.0f;
            Serial.println("OK:CALIBRATED");
        }
    }

    // 20ms delay = 50 Hz update rate
    delay(20);
}
