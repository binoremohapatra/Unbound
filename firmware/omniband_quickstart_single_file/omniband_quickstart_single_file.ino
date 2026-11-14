// ==============================================================================
// OmniBand Quickstart Standalone Firmware (Single-File Plug & Play)
// Compatible with Unbound Assistive OS (Unbound.exe) via BLE & USB Serial (COM)
// ==============================================================================
// No external libraries required! Only standard Arduino-ESP32 core.
// 
// WIRING PINOUT (ESP32 DevKit 30/38-pin):
//   - CH1 (Left Temple):              GPIO 32 (ADC1_CH4)
//   - CH2 (Centre-Left Forehead):     GPIO 33 (ADC1_CH5)
//   - CH3 (Centre-Right Forehead):    GPIO 34 (ADC1_CH6 - Input only)
//   - CH4 (Right Temple/Jaw Clench):  GPIO 35 (ADC1_CH7 - Input only)
//   - GND:                            GND on sensors
//   - 3.3V:                           VCC on sensors
// ==============================================================================

#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// BLE Identifiers (Must match Unbound host)
#define DEVICE_NAME         "Omniband"
#define SERVICE_UUID        "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define CHARACTERISTIC_UUID "beb5483e-36e1-4688-b7f5-ea07361b26a6"

// Pin definitions
const int PIN_CH1 = 32;
const int PIN_CH2 = 33;
const int PIN_CH3 = 34;
const int PIN_CH4 = 35;

// BLE Server & Characteristic
BLEServer* pServer = nullptr;
BLECharacteristic* pCharacteristic = nullptr;
bool deviceConnected = false;
bool oldDeviceConnected = false;
uint32_t packetSequence = 0;

// Baseline tracking for noise cancellation
float baseline[4] = {2048.0f, 2048.0f, 2048.0f, 2048.0f};
const float ALPHA_BASELINE = 0.005f; // Slow drift tracker
const float ALPHA_ENVELOPE = 0.25f;  // Fast envelope filter
float envelope[4] = {0.0f, 0.0f, 0.0f, 0.0f};

class MyServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) override {
        deviceConnected = true;
        Serial.println("[BLE] Client connected successfully!");
    }

    void onDisconnect(BLEServer* pServer) override {
        deviceConnected = false;
        Serial.println("[BLE] Client disconnected. Restarting advertising...");
    }
};

class MyCharCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic* pChar) override {
        String rxValue = pChar->getValue();
        if (rxValue.length() > 0) {
            Serial.print("[BLE RX] ");
            Serial.println(rxValue.c_str());
        }
    }
};

void initBLE() {
    Serial.println("[BLE] Starting BLE Workstation...");
    BLEDevice::init(DEVICE_NAME);
    
    // Create BLE Server
    pServer = BLEDevice::createServer();
    pServer->setCallbacks(new MyServerCallbacks());

    // Create BLE Service
    BLEService* pService = pServer->createService(SERVICE_UUID);

    // Create BLE Characteristic
    pCharacteristic = pService->createCharacteristic(
        CHARACTERISTIC_UUID,
        BLECharacteristic::PROPERTY_READ   |
        BLECharacteristic::PROPERTY_WRITE  |
        BLECharacteristic::PROPERTY_NOTIFY |
        BLECharacteristic::PROPERTY_INDICATE
    );

    pCharacteristic->addDescriptor(new BLE2902());
    pCharacteristic->setCallbacks(new MyCharCallbacks());

    // Start service
    pService->start();

    // Start advertising
    BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
    pAdvertising->addServiceUUID(SERVICE_UUID);
    pAdvertising->setScanResponse(true);
    pAdvertising->setMinPreferred(0x06); // iPhone/WinRT connection helper
    pAdvertising->setMinPreferred(0x12);
    BLEDevice::startAdvertising();
    Serial.println("[BLE] Advertising started. Device visible as 'Omniband'");
}

void setup() {
    // 115200 Baud for USB Serial
    Serial.begin(115200);
    delay(500);
    Serial.println("\n==========================================");
    Serial.println("   OmniBand Quickstart ESP32 Firmware     ");
    Serial.println("   Dual Link: USB Serial (COM) + BLE      ");
    Serial.println("==========================================");

    // Configure ADC pins
    pinMode(PIN_CH1, INPUT);
    pinMode(PIN_CH2, INPUT);
    pinMode(PIN_CH3, INPUT);
    pinMode(PIN_CH4, INPUT);

    // Setup BLE
    initBLE();

    // Initial baseline calibration (take 50 samples)
    Serial.println("[INIT] Calibrating sensor baseline...");
    for (int i = 0; i < 50; i++) {
        baseline[0] = baseline[0] * 0.9f + analogRead(PIN_CH1) * 0.1f;
        baseline[1] = baseline[1] * 0.9f + analogRead(PIN_CH2) * 0.1f;
        baseline[2] = baseline[2] * 0.9f + analogRead(PIN_CH3) * 0.1f;
        baseline[3] = baseline[3] * 0.9f + analogRead(PIN_CH4) * 0.1f;
        delay(10);
    }
    Serial.println("[INIT] Baseline ready. Ready to stream!");
}

void loop() {
    // 1. Read ADC raw values (12-bit: 0 - 4095)
    int raw1 = analogRead(PIN_CH1);
    int raw2 = analogRead(PIN_CH2);
    int raw3 = analogRead(PIN_CH3);
    int raw4 = analogRead(PIN_CH4);

    // 2. Compute rectified envelope above baseline
    int raw[4] = {raw1, raw2, raw3, raw4};
    for (int i = 0; i < 4; i++) {
        // Update slow baseline
        baseline[i] = (1.0f - ALPHA_BASELINE) * baseline[i] + ALPHA_BASELINE * (float)raw[i];
        float rectified = abs((float)raw[i] - baseline[i]);
        // Update smoothed envelope
        envelope[i] = (1.0f - ALPHA_ENVELOPE) * envelope[i] + ALPHA_ENVELOPE * rectified;
    }

    // 3. Format payload: S,ch1,ch2,ch3,ch4,seq,dx,dy
    // Format compatible with Unbound ble_transport.py parser
    char packet[96];
    packetSequence++;
    snprintf(packet, sizeof(packet), "S,%.1f,%.1f,%.1f,%.1f,%u,0.0,0.0\n",
             envelope[0], envelope[1], envelope[2], envelope[3], packetSequence);

    // 4. Send over USB Serial (for COM port 1-click fallback in Unbound)
    Serial.print(packet);

    // 5. Send over BLE Notify if connected
    if (deviceConnected && pCharacteristic != nullptr) {
        pCharacteristic->setValue((uint8_t*)packet, strlen(packet));
        pCharacteristic->notify();
    }

    // Handle BLE reconnection cycle
    if (!deviceConnected && oldDeviceConnected) {
        delay(300); // Bluetooth stack stabilize
        pServer->startAdvertising();
        oldDeviceConnected = deviceConnected;
    }
    if (deviceConnected && !oldDeviceConnected) {
        oldDeviceConnected = deviceConnected;
    }

    // Check for incoming USB Serial commands
    if (Serial.available()) {
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();
        if (cmd == "HELLO" || cmd == "STATUS") {
            Serial.println("OK:OMNIBAND_QUICKSTART_V1");
        } else if (cmd == "CAL" || cmd == "RESET") {
            for (int i = 0; i < 4; i++) envelope[i] = 0.0f;
            Serial.println("OK:CALIBRATED");
        }
    }

    // 50 Hz loop rate (20ms delay)
    delay(20);
}
