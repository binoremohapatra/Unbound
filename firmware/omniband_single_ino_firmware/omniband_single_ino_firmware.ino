// ==============================================================================
// OmniBand All-In-One Single File Firmware (Arduino IDE Copy-Paste Ready)
// ==============================================================================
// EXACT PINS AS PER YOUR WIRING:
//   - CH1: GPIO 32  (Left Temple / Left eyebrow)
//   - CH2: GPIO 34  (Centre-Left Forehead)
//   - CH3: GPIO 33  (Centre-Right Forehead)
//   - CH4: GPIO 26  (Right Temple / Jaw Clench Click Trigger)
//   - MPU6050 SDA: GPIO 21 (I2C Data)
//   - MPU6050 SCL: GPIO 22 (I2C Clock)
//   - GND: Common Ground
//   - 3.3V: VCC (Sensors + MPU6050)
// ==============================================================================
// BLUETOOTH BLE SPECS:
//   - Device Name:         "Omniband"
//   - Service UUID:        "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
//   - Characteristic UUID: "beb5483e-36e1-4688-b7f5-ea07361b26a6"
//   - Permissions:         READ, WRITE (PC se Device me), NOTIFY (Device se PC me)
// ==============================================================================
// ARCHITECTURE RULES:
//   1. Primary Cursor: MPU6050 Gyro (dx, dy) directly drives mouse pointer!
//   2. Primary Action: CH4 Jaw Clench directly triggers Click!
//   3. Optical Fallback: If MPU is not detected or fails, PC app automatically
//      falls back to Webcam Face Tracking head-mouse.
// ==============================================================================

#include <Arduino.h>
#include <Wire.h>
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

const int PIN_I2C_SDA = 21;
const int PIN_I2C_SCL = 22;
const uint8_t MPU_ADDR = 0x68;

// -----------------------------------------------------------------------------
// BLE Constants
// -----------------------------------------------------------------------------
#define BLE_DEVICE_NAME     "Omniband"
#define SERVICE_UUID        "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define CHARACTERISTIC_UUID "beb5483e-36e1-4688-b7f5-ea07361b26a6"

// -----------------------------------------------------------------------------
// Global Variables
// -----------------------------------------------------------------------------
BLEServer* pServer = nullptr;
BLECharacteristic* pCharacteristic = nullptr;
bool deviceConnected = false;
bool oldDeviceConnected = false;
uint32_t packetSeq = 0;

// Sensor baseline and envelope tracking
float baseline[4] = {2048.0f, 2048.0f, 2048.0f, 2048.0f};
float envelope[4] = {0.0f, 0.0f, 0.0f, 0.0f};
const float ALPHA_BASELINE = 0.005f; 
const float ALPHA_ENVELOPE = 0.25f;  

// MPU6050 Motion tracking state
bool mpuAvailable = false;
float gyroBiasZ = 0.0f;
float gyroBiasX = 0.0f;

// -----------------------------------------------------------------------------
// BLE Connection Management
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
// PC se ESP32 ke andar Input lene ka Callback (PROPERTY_WRITE)
// -----------------------------------------------------------------------------
class CharCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic* pChar) override {
        String rxValue = pChar->getValue();
        if (rxValue.length() > 0) {
            Serial.print("[BLE INPUT FROM PC]: ");
            Serial.println(rxValue.c_str());
            
            // Handle calibration command from PC
            if (rxValue.indexOf("CAL") >= 0 || rxValue.indexOf("RESET") >= 0) {
                for (int i = 0; i < 4; i++) envelope[i] = 0.0f;
                Serial.println("[ESP32] Calibrated by PC input command.");
            }
        }
    }
};

// -----------------------------------------------------------------------------
// MPU6050 Setup & Auto-Detection
// -----------------------------------------------------------------------------
void setupMPU() {
    Serial.println("[MPU] Probing MPU6050 on I2C (SDA=21, SCL=22)...");
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
    Wire.setTimeOut(50); // Prevent hanging if sensor disconnected

    Wire.beginTransmission(MPU_ADDR);
    byte err = Wire.endTransmission();
    if (err == 0) {
        Serial.println("[MPU] MPU6050 detected at address 0x68!");
        // Wake up MPU6050 (Write 0 to PWR_MGMT_1 register 0x6B)
        Wire.beginTransmission(MPU_ADDR);
        Wire.write(0x6B);
        Wire.write(0x00);
        Wire.endTransmission();

        // Configure Gyro Full Scale to +/- 500 deg/s (FS_SEL = 1 -> 0x08)
        Wire.beginTransmission(MPU_ADDR);
        Wire.write(0x1B);
        Wire.write(0x08);
        Wire.endTransmission();

        // Measure stationary bias over 30 samples
        Serial.println("[MPU] Calibrating gyro zero-bias...");
        float sumZ = 0, sumX = 0;
        for (int i = 0; i < 30; i++) {
            Wire.beginTransmission(MPU_ADDR);
            Wire.write(0x43);
            Wire.endTransmission(false);
            Wire.requestFrom((uint8_t)MPU_ADDR, (uint8_t)6);
            if (Wire.available() >= 6) {
                int16_t gx = (Wire.read() << 8) | Wire.read();
                int16_t gy = (Wire.read() << 8) | Wire.read();
                int16_t gz = (Wire.read() << 8) | Wire.read();
                sumX += (float)gx;
                sumZ += (float)gz;
            }
            delay(10);
        }
        gyroBiasX = sumX / 30.0f;
        gyroBiasZ = sumZ / 30.0f;
        mpuAvailable = true;
        Serial.println("[MPU] MPU6050 Ready! Hardware cursor motion enabled.");
    } else {
        Serial.println("[MPU] MPU6050 NOT detected. System will use Webcam Optical Fallback for cursor.");
        mpuAvailable = false;
    }
}

// -----------------------------------------------------------------------------
// Read MPU6050 Relative Mouse Movement (dx, dy)
// -----------------------------------------------------------------------------
void readMPUMotion(float &dx, float &dy) {
    dx = 0.0f;
    dy = 0.0f;
    if (!mpuAvailable) return;

    Wire.beginTransmission(MPU_ADDR);
    Wire.write(0x43); // Gyro start register
    if (Wire.endTransmission(false) != 0) {
        mpuAvailable = false; // Sensor disconnected
        return;
    }
    Wire.requestFrom((uint8_t)MPU_ADDR, (uint8_t)6);
    if (Wire.available() >= 6) {
        int16_t rawX = (Wire.read() << 8) | Wire.read(); // Pitch -> dy
        int16_t rawY = (Wire.read() << 8) | Wire.read(); // Roll
        int16_t rawZ = (Wire.read() << 8) | Wire.read(); // Yaw   -> dx

        float gz = (float)rawZ - gyroBiasZ;
        float gx = (float)rawX - gyroBiasX;

        // Deadzone threshold (remove sensor drift jitter)
        const float DEADZONE = 250.0f;
        // Sensitivity scale factor
        const float SENSITIVITY = 180.0f;

        if (abs(gz) > DEADZONE) {
            float excess = (gz > 0) ? (gz - DEADZONE) : (gz + DEADZONE);
            dx = -(excess / SENSITIVITY); // Turn right -> move right
        }
        if (abs(gx) > DEADZONE) {
            float excess = (gx > 0) ? (gx - DEADZONE) : (gx + DEADZONE);
            dy = (excess / SENSITIVITY);  // Pitch down -> move down
        }
    }
}

// -----------------------------------------------------------------------------
// Bluetooth BLE Initialization
// -----------------------------------------------------------------------------
void setupBLE() {
    Serial.println("[BLE] Starting BLE Device 'Omniband'...");
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
        BLECharacteristic::PROPERTY_WRITE  |   // <--- PC se Input lene ke liye
        BLECharacteristic::PROPERTY_NOTIFY |   // <--- PC ko Sensor data bhejne ke liye
        BLECharacteristic::PROPERTY_INDICATE
    );

    pCharacteristic->addDescriptor(new BLE2902());
    pCharacteristic->setCallbacks(new CharCallbacks());
    pCharacteristic->setValue("Omniband Ready");

    // 4. Start Service
    pService->start();

    // 5. Start Advertising taaki Bluetooth scan me "Omniband" dikhe
    BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
    pAdvertising->addServiceUUID(SERVICE_UUID);
    pAdvertising->setScanResponse(true);
    pAdvertising->setMinPreferred(0x06); // Fast Windows WinRT connection
    pAdvertising->setMinPreferred(0x12);
    BLEDevice::startAdvertising();

    Serial.println("[BLE] Advertising ON. Bluetooth Name: 'Omniband'");
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
    Serial.println("  I2C MPU6050: SDA=21, SCL=22                 ");
    Serial.println("  BLE Device: 'Omniband'                      ");
    Serial.println("==============================================");

    // Set pin modes
    pinMode(PIN_CH1, INPUT);
    pinMode(PIN_CH2, INPUT);
    pinMode(PIN_CH3, INPUT);
    pinMode(PIN_CH4, INPUT);

    // Start MPU6050 (Auto-probes I2C)
    setupMPU();

    // Start BLE
    setupBLE();

    // Calibrate sensor baseline (500 ms)
    Serial.println("[ADC] Calibrating baseline...");
    for (int i = 0; i < 50; i++) {
        baseline[0] = baseline[0] * 0.9f + analogRead(PIN_CH1) * 0.1f;
        baseline[1] = baseline[1] * 0.9f + analogRead(PIN_CH2) * 0.1f;
        baseline[2] = baseline[2] * 0.9f + analogRead(PIN_CH3) * 0.1f;
        baseline[3] = baseline[3] * 0.9f + analogRead(PIN_CH4) * 0.1f;
        delay(10);
    }
    Serial.println("[ADC] Calibration locked! Streaming ready.");
}

// -----------------------------------------------------------------------------
// Arduino Loop (50 Hz)
// -----------------------------------------------------------------------------
void loop() {
    // 1. Read pins (32, 34, 33, 26)
    int raw[4];
    raw[0] = analogRead(PIN_CH1);
    raw[1] = analogRead(PIN_CH2);
    raw[2] = analogRead(PIN_CH3);
    raw[3] = analogRead(PIN_CH4);

    // 2. Calculate rectified envelopes
    for (int i = 0; i < 4; i++) {
        baseline[i] = (1.0f - ALPHA_BASELINE) * baseline[i] + ALPHA_BASELINE * (float)raw[i];
        float rectified = abs((float)raw[i] - baseline[i]);
        envelope[i] = (1.0f - ALPHA_ENVELOPE) * envelope[i] + ALPHA_ENVELOPE * rectified;
    }

    // 3. Read MPU6050 gyro cursor motion (dx, dy)
    float dx = 0.0f, dy = 0.0f;
    readMPUMotion(dx, dy);

    // 4. Packet format: S,ch1,ch2,ch3,ch4,seq,dx,dy\n
    char packet[96];
    packetSeq++;
    snprintf(packet, sizeof(packet), "S,%.1f,%.1f,%.1f,%.1f,%u,%.2f,%.2f\n",
             envelope[0], envelope[1], envelope[2], envelope[3], packetSeq, dx, dy);

    // 5. USB Serial streaming (COM port ke liye)
    Serial.print(packet);

    // 6. BLE Notify streaming (Bluetooth ke liye)
    if (deviceConnected && pCharacteristic != nullptr) {
        pCharacteristic->setValue((uint8_t*)packet, strlen(packet));
        pCharacteristic->notify();
    }

    // Auto-reconnect advertising
    if (!deviceConnected && oldDeviceConnected) {
        delay(300);
        pServer->startAdvertising();
        oldDeviceConnected = deviceConnected;
    }
    if (deviceConnected && !oldDeviceConnected) {
        oldDeviceConnected = deviceConnected;
    }

    // USB Serial Command Receiver
    if (Serial.available()) {
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();
        if (cmd == "HELLO" || cmd == "STATUS") {
            Serial.println(mpuAvailable ? "OK:OMNIBAND_MPU_ACTIVE" : "OK:OMNIBAND_NO_MPU");
        } else if (cmd == "CAL" || cmd == "RESET") {
            for (int i = 0; i < 4; i++) envelope[i] = 0.0f;
            Serial.println("OK:CALIBRATED");
        }
    }

    // 20ms delay = 50 Hz update rate
    delay(20);
}
