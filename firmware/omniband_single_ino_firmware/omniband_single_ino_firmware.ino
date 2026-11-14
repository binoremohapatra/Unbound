// ==============================================================================
// OmniBand All-In-One Single-File Firmware (Arduino IDE Copy-Paste Ready)
// ==============================================================================
// ON-DEVICE EDGE DSP + BLUETOOTH BLE + MPU6050 HARDWARE CURSOR
//
// EXACT PINS AS PER YOUR PHYSICAL WIRING:
//   - CH1: GPIO 32  (Left Temple / Left Brow)
//   - CH2: GPIO 34  (Centre-Left Forehead)
//   - CH3: GPIO 33  (Centre-Right Forehead)
//   - CH4: GPIO 26  (Right Temple / Jaw Clench Click Trigger)
//   - MPU6050 SDA: GPIO 21 (I2C Data)
//   - MPU6050 SCL: GPIO 22 (I2C Clock)
//   - GND: Common Ground
//   - 3.3V: VCC (Sensors + MPU6050)
//
// ZERO EXTERNAL LIBRARIES REQUIRED:
//   Uses only standard ESP32 built-in libraries (Wire.h, BLEDevice.h).
//   No Adafruit libraries needed. Compiles cleanly on ESP32 Core 2.0.17!
//
// KEY CAPABILITIES:
//   1. 1 kHz Edge DSP on ESP32: Real-time baseline tracking, noise estimation,
//      and spike hysteresis. Jaw Clench is detected directly ON THE CHIP!
//   2. Direct Click Generation: No PC threshold misinterpretation! The ESP32
//      fires the Left Click event immediately over Bluetooth and Serial.
//   3. Hardware MPU6050 Cursor: Relative mouse movement (dx, dy) streamed
//      directly to Windows.
// ==============================================================================

#include <Arduino.h>
#include <Wire.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// -----------------------------------------------------------------------------
// Hardware Pins (Matching your physical wiring)
// -----------------------------------------------------------------------------
static const uint8_t PIN_CH1 = 32;
static const uint8_t PIN_CH2 = 34;
static const uint8_t PIN_CH3 = 33;
static const uint8_t PIN_CH4 = 26; // Jaw Clench
static const uint8_t PINS[4] = {PIN_CH1, PIN_CH2, PIN_CH3, PIN_CH4};

static const uint8_t PIN_I2C_SDA = 21;
static const uint8_t PIN_I2C_SCL = 22;
static const uint8_t MPU_ADDR    = 0x68;

// -----------------------------------------------------------------------------
// BLE UUIDs & Device Name
// -----------------------------------------------------------------------------
#define BLE_DEVICE_NAME     "Omniband"
#define SERVICE_UUID        "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define CHARACTERISTIC_UUID "beb5483e-36e1-4688-b7f5-ea07361b26a6"

BLEServer* pServer = nullptr;
BLECharacteristic* pCharacteristic = nullptr;
bool deviceConnected = false;
bool oldDeviceConnected = false;
uint32_t packetSeq = 0;

// -----------------------------------------------------------------------------
// 1 kHz Edge DSP Constants
// -----------------------------------------------------------------------------
static const uint32_t ADC_PERIOD_US     = 1000;    // 1 kHz sampling rate
static const uint32_t STREAM_PERIOD_US  = 20000;   // 50 Hz packet streaming (20ms)
static const uint32_t IMU_PERIOD_US     = 10000;   // 100 Hz IMU read (10ms)

static const float    BASELINE_ALPHA    = 0.002f;  // EMA baseline tracking
static const float    NOISE_ALPHA       = 0.005f;  // EMA noise floor tracking
static const float    SPIKE_ONSET_SIGMA = 4.5f;    // Threshold multiplier
static const float    SPIKE_ONSET_MIN   = 75.0f;   // Floor threshold (counts)
static const float    SPIKE_RELEASE_K   = 0.40f;   // Hysteresis release ratio
static const float    SPIKE_RELEASE_MIN = 35.0f;   // Release floor
static const uint32_t SPIKE_MAX_US      = 400000;  // 400ms max single spike
static const uint32_t LOCKOUT_MS        = 320;     // Refractory lockout after click

// -----------------------------------------------------------------------------
// Per-Channel DSP State
// -----------------------------------------------------------------------------
struct ChanDsp {
    float raw;
    float baseline;
    float noise;
    float dev;
    float env;
    bool  spikeActive;
    float spikePeak;
    uint32_t spikeStartUs;
    ChanDsp() : raw(2048), baseline(2048), noise(10), dev(0), env(0),
                spikeActive(false), spikePeak(0), spikeStartUs(0) {}
};
static ChanDsp ch[4];

// Edge gesture state
static uint32_t lastClickMs = 0;
static bool     clenchActive = false;
static uint32_t clenchUntilMs = 0;

// MPU6050 Motion tracking state
static bool  mpuAvailable = false;
static float gyroBiasZ = 0.0f;
static float gyroBiasX = 0.0f;
static float imuDx = 0.0f;
static float imuDy = 0.0f;

// -----------------------------------------------------------------------------
// BLE Callbacks
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

class CharCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic* pChar) override {
        String rxValue = pChar->getValue();
        if (rxValue.length() > 0) {
            Serial.print("[BLE INPUT FROM PC]: ");
            Serial.println(rxValue.c_str());
            if (rxValue.indexOf("CAL") >= 0 || rxValue.indexOf("RESET") >= 0) {
                for (int i = 0; i < 4; i++) {
                    ch[i].baseline = (float)analogRead(PINS[i]);
                    ch[i].env = 0.0f;
                    ch[i].noise = 10.0f;
                }
                Serial.println("[ESP32] On-Device Calibrated via PC command.");
            }
        }
    }
};

// -----------------------------------------------------------------------------
// MPU6050 Setup & Auto-Detection (Direct I2C Registers - No Library Needed!)
// -----------------------------------------------------------------------------
void setupMPU() {
    Serial.println("[MPU] Probing MPU6050 on I2C (SDA=21, SCL=22)...");
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
    Wire.setTimeOut(50);

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

        // Calibrate zero-bias over 30 samples
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
        Serial.println("[MPU] MPU6050 Ready! Hardware cursor active.");
    } else {
        Serial.println("[MPU] MPU6050 NOT detected. Using optical head-mouse fallback.");
        mpuAvailable = false;
    }
}

// -----------------------------------------------------------------------------
// Read MPU6050 Relative Mouse Movement (dx, dy)
// -----------------------------------------------------------------------------
void readMPUMotion() {
    if (!mpuAvailable) {
        imuDx = 0.0f;
        imuDy = 0.0f;
        return;
    }

    Wire.beginTransmission(MPU_ADDR);
    Wire.write(0x43);
    if (Wire.endTransmission(false) != 0) {
        mpuAvailable = false;
        imuDx = 0.0f; imuDy = 0.0f;
        return;
    }
    Wire.requestFrom((uint8_t)MPU_ADDR, (uint8_t)6);
    if (Wire.available() >= 6) {
        int16_t rawX = (Wire.read() << 8) | Wire.read(); // Pitch -> dy
        int16_t rawY = (Wire.read() << 8) | Wire.read(); // Roll
        int16_t rawZ = (Wire.read() << 8) | Wire.read(); // Yaw   -> dx

        float gz = (float)rawZ - gyroBiasZ;
        float gx = (float)rawX - gyroBiasX;

        const float DEADZONE = 220.0f;
        const float SENSITIVITY = 175.0f;

        if (abs(gz) > DEADZONE) {
            float excess = (gz > 0) ? (gz - DEADZONE) : (gz + DEADZONE);
            imuDx = -(excess / SENSITIVITY);
        } else {
            imuDx = 0.0f;
        }

        if (abs(gx) > DEADZONE) {
            float excess = (gx > 0) ? (gx - DEADZONE) : (gx + DEADZONE);
            imuDy = (excess / SENSITIVITY);
        } else {
            imuDy = 0.0f;
        }
    }
}

// -----------------------------------------------------------------------------
// 1 kHz Edge DSP & On-Device Gesture Classifier
// -----------------------------------------------------------------------------
void processChannel1kHz(uint8_t i, uint32_t nowUs) {
    ChanDsp &c = ch[i];
    c.raw = (float)analogRead(PINS[i]);
    c.dev = c.raw - c.baseline;

    float absDev = abs(c.dev);
    // Envelope filter
    c.env = (absDev > c.env) ? absDev : c.env * 0.996f;

    // Adaptive noise threshold
    float onset   = max(SPIKE_ONSET_MIN, c.noise * SPIKE_ONSET_SIGMA);
    float release = max(SPIKE_RELEASE_MIN, onset * SPIKE_RELEASE_K);

    // Update baseline only while calm (prevents clench from pulling baseline)
    if (absDev < onset) {
        c.baseline += BASELINE_ALPHA * (c.raw - c.baseline);
        c.noise    += NOISE_ALPHA * (absDev - c.noise);
    }

    // Spike detector state machine
    if (!c.spikeActive) {
        if (absDev > onset) {
            c.spikeActive = true;
            c.spikeStartUs = nowUs;
            c.spikePeak = absDev;
        }
    } else {
        if (absDev > c.spikePeak) c.spikePeak = absDev;
        bool ended = (absDev < release) || (nowUs - c.spikeStartUs > SPIKE_MAX_US);
        if (ended) {
            uint32_t durUs = nowUs - c.spikeStartUs;
            c.spikeActive = false;

            // ==============================================================
            // ON-DEVICE JAW CLENCH (CLICK) CLASSIFIER ON CH4 (PIN 26)
            // ==============================================================
            if (i == 3) { // CH4 is Jaw Clench
                uint32_t nowM = millis();
                if (durUs >= 25000 && durUs <= SPIKE_MAX_US && c.spikePeak > onset &&
                    (nowM - lastClickMs > LOCKOUT_MS)) {
                    
                    lastClickMs = nowM;
                    clenchActive = true;
                    clenchUntilMs = nowM + 90; // 90ms solid click pulse

                    Serial.println("\n⚡ [EDGE DSP] >>> ON-DEVICE JAW CLENCH DETECTED -> LEFT CLICK! <<<");

                    // Send immediate CLICK trigger over Bluetooth
                    if (deviceConnected && pCharacteristic != nullptr) {
                        pCharacteristic->setValue((uint8_t*)"CLICK\n", 6);
                        pCharacteristic->notify();
                    }
                }
            }
        }
    }
}

// -----------------------------------------------------------------------------
// BLE Setup
// -----------------------------------------------------------------------------
void setupBLE() {
    Serial.println("[BLE] Starting BLE Device 'Omniband'...");
    BLEDevice::init(BLE_DEVICE_NAME);

    pServer = BLEDevice::createServer();
    pServer->setCallbacks(new ServerCallbacks());

    BLEService* pService = pServer->createService(SERVICE_UUID);

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

    pService->start();

    BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
    pAdvertising->addServiceUUID(SERVICE_UUID);
    pAdvertising->setScanResponse(true);
    pAdvertising->setMinPreferred(0x06);
    pAdvertising->setMinPreferred(0x12);
    BLEDevice::startAdvertising();

    Serial.println("[BLE] Advertising ON. Bluetooth Name: 'Omniband'");
}

// -----------------------------------------------------------------------------
// Setup
// -----------------------------------------------------------------------------
void setup() {
    Serial.begin(115200);
    delay(400);

    Serial.println("\n=======================================================");
    Serial.println("  OmniBand On-Device Edge DSP Firmware                ");
    Serial.println("  Pins: CH1=32, CH2=34, CH3=33, CH4=26 (Jaw Clench)   ");
    Serial.println("  MPU6050 I2C: SDA=21, SCL=22                         ");
    Serial.println("  Bluetooth Device: 'Omniband'                        ");
    Serial.println("=======================================================");

    pinMode(PIN_CH1, INPUT);
    pinMode(PIN_CH2, INPUT);
    pinMode(PIN_CH3, INPUT);
    pinMode(PIN_CH4, INPUT);

    setupMPU();
    setupBLE();

    // Baseline calibration (500 ms)
    Serial.println("[DSP] Calibrating 4-channel somatic baseline...");
    for (int i = 0; i < 50; i++) {
        for (int k = 0; k < 4; k++) {
            ch[k].baseline = ch[k].baseline * 0.9f + (float)analogRead(PINS[k]) * 0.1f;
        }
        delay(10);
    }
    Serial.println("[DSP] Baseline calibration locked. 1 kHz Edge DSP Active!");
}

// -----------------------------------------------------------------------------
// Main Loop (Non-Blocking Multi-Rate Engine)
// -----------------------------------------------------------------------------
static uint32_t lastAdcUs = 0;
static uint32_t lastImuUs = 0;
static uint32_t lastStreamUs = 0;

void loop() {
    uint32_t nowUs = micros();
    uint32_t nowMs = millis();

    // 1. 1 kHz Edge DSP Sampling
    if (nowUs - lastAdcUs >= ADC_PERIOD_US) {
        lastAdcUs = nowUs;
        for (uint8_t i = 0; i < 4; i++) {
            processChannel1kHz(i, nowUs);
        }
    }

    // 2. 100 Hz MPU Gyro Read
    if (nowUs - lastImuUs >= IMU_PERIOD_US) {
        lastImuUs = nowUs;
        readMPUMotion();
    }

    // 3. 50 Hz Packet Streaming (USB + BLE)
    if (nowUs - lastStreamUs >= STREAM_PERIOD_US) {
        lastStreamUs = nowUs;

        // Check if clench pulse expired
        if (clenchActive && nowMs > clenchUntilMs) {
            clenchActive = false;
        }

        // When on-device clench triggers, enforce high active amplitude (650.0)
        float outCh4 = clenchActive ? 650.0f : ch[3].env;

        packetSeq++;
        char packet[96];
        snprintf(packet, sizeof(packet), "S,%.1f,%.1f,%.1f,%.1f,%u,%.2f,%.2f\n",
                 ch[0].env, ch[1].env, ch[2].env, outCh4, packetSeq, imuDx, imuDy);

        // Stream over Serial
        Serial.print(packet);

        // Stream over BLE
        if (deviceConnected && pCharacteristic != nullptr) {
            pCharacteristic->setValue((uint8_t*)packet, strlen(packet));
            pCharacteristic->notify();
        }
    }

    // 4. Auto-reconnect advertising
    if (!deviceConnected && oldDeviceConnected) {
        delay(300);
        pServer->startAdvertising();
        oldDeviceConnected = deviceConnected;
    }
    if (deviceConnected && !oldDeviceConnected) {
        oldDeviceConnected = deviceConnected;
    }

    // 5. USB Serial Command Handling
    if (Serial.available()) {
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();
        if (cmd == "HELLO" || cmd == "STATUS") {
            Serial.println(mpuAvailable ? "OK:OMNIBAND_MPU_ACTIVE" : "OK:OMNIBAND_NO_MPU");
        } else if (cmd == "CAL" || cmd == "RESET") {
            for (int k = 0; k < 4; k++) {
                ch[k].baseline = (float)analogRead(PINS[k]);
                ch[k].env = 0.0f;
            }
            Serial.println("OK:CALIBRATED");
        }
    }
}
