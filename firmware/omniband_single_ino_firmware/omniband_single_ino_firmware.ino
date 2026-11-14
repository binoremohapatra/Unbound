/*
 * ============================================================================
 *  OmniBand.ino — ESP32 Native Bluetooth HID Mouse Wearable Firmware
 * ============================================================================
 *  Edge Computing Gesture Detection + MPU6050 Mouse Cursor + Native BLE HID
 *  
 *  NO PC SOFTWARE REQUIRED!
 *  Windows / Mac / Android directly detects this device as a REAL BLUETOOTH MOUSE!
 *
 *  MAPPED NATIVE HARDWARE ACTIONS:
 *    1. JAW CLENCH (CH4)      -> Native Left Mouse Click
 *    2. JAW TAP / DOUBLE TAP   -> Native Right Mouse Click
 *    3. JAW HOLD (>350ms)     -> Native Mouse Drag Lock Toggle
 *    4. BROW RAISE (CH2+CH3)   -> Native Mouse Wheel Scroll UP
 *    5. HEAD NOD (MPU pitch)   -> Native Mouse Wheel Scroll DOWN
 *    6. LEFT CLENCH (CH1)     -> Native Middle Click
 *    7. MPU GYRO (Yaw / Pitch) -> Real-time Windows Mouse Cursor (dx, dy)
 *
 *  HARDWARE PINS (Matching physical headband wiring):
 *    - CH1: GPIO 32 (Left Temple)
 *    - CH2: GPIO 34 (Centre-Left Forehead)
 *    - CH3: GPIO 33 (Centre-Right Forehead)
 *    - CH4: GPIO 26 (Right Temple / Jaw Clench)
 *    - I2C SDA: GPIO 21 (MPU6050 & OLED)
 *    - I2C SCL: GPIO 22 (MPU6050 & OLED)
 * ============================================================================
 */

#include <Arduino.h>
#include <Wire.h>
#include <string.h>
#include <Preferences.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <BLEHIDDevice.h>
#include <BLECharacteristic.h>

// ============================================================================
// 1. PIN CONFIGURATION
// ============================================================================
static const uint8_t PIN_CH1 = 32;   // Left temple
static const uint8_t PIN_CH2 = 34;   // Centre-left forehead
static const uint8_t PIN_CH3 = 33;   // Centre-right forehead
static const uint8_t PIN_CH4 = 26;   // Right temple / jaw clench
static const uint8_t PIN_PIEZO[4] = {PIN_CH1, PIN_CH2, PIN_CH3, PIN_CH4};

static const uint8_t PIN_SDA = 21;
static const uint8_t PIN_SCL = 22;
static const uint8_t I2C_ADDR_MPU = 0x68;

static const uint8_t PIN_BTN = 23;
static const bool    BTN_ACTIVE_LOW = true;

// Storage
static Preferences prefs;

// ============================================================================
// 2. NATIVE BLUETOOTH HID MOUSE ENGINE (Zero PC software needed!)
// ============================================================================
static BLEHIDDevice* hid = nullptr;
static BLECharacteristic* inputMouse = nullptr;
static bool bleConnected = false;
static bool oldBleConnected = false;
static uint8_t mouseButtonMask = 0;

#define MOUSE_BTN_LEFT   0x01
#define MOUSE_BTN_RIGHT  0x02
#define MOUSE_BTN_MIDDLE 0x04

// Standard USB HID Mouse Report Descriptor (Report ID 1)
static const uint8_t hidReportMap[] = {
    0x05, 0x01,                    // USAGE_PAGE (Generic Desktop)
    0x09, 0x02,                    // USAGE (Mouse)
    0xa1, 0x01,                    // COLLECTION (Application)
    0x85, 0x01,                    //   REPORT_ID (1)
    0x09, 0x01,                    //   USAGE (Pointer)
    0xa1, 0x00,                    //   COLLECTION (Physical)
    0x05, 0x09,                    //     USAGE_PAGE (Button)
    0x19, 0x01,                    //     USAGE_MINIMUM (Button 1)
    0x29, 0x03,                    //     USAGE_MAXIMUM (Button 3)
    0x15, 0x00,                    //     LOGICAL_MINIMUM (0)
    0x25, 0x01,                    //     LOGICAL_MAXIMUM (1)
    0x95, 0x03,                    //     REPORT_COUNT (3)
    0x75, 0x01,                    //     REPORT_SIZE (1)
    0x81, 0x02,                    //     INPUT (Data,Var,Abs)
    0x95, 0x01,                    //     REPORT_COUNT (1)
    0x75, 0x05,                    //     REPORT_SIZE (5)
    0x81, 0x03,                    //     INPUT (Cnst,Var,Abs)
    0x05, 0x01,                    //     USAGE_PAGE (Generic Desktop)
    0x09, 0x30,                    //     USAGE (X)
    0x09, 0x31,                    //     USAGE (Y)
    0x09, 0x38,                    //     USAGE (Wheel)
    0x15, 0x81,                    //     LOGICAL_MINIMUM (-127)
    0x25, 0x7f,                    //     LOGICAL_MAXIMUM (127)
    0x75, 0x08,                    //     REPORT_SIZE (8)
    0x95, 0x03,                    //     REPORT_COUNT (3)
    0x81, 0x06,                    //     INPUT (Data,Var,Rel)
    0xc0,                          //   END_COLLECTION
    0xc0                           // END_COLLECTION
};

void sendNativeMouse(int8_t dx, int8_t dy, int8_t wheel = 0, uint8_t buttons = 0) {
    if (!bleConnected || inputMouse == nullptr) return;
    uint8_t report[4] = {buttons, (uint8_t)dx, (uint8_t)dy, (uint8_t)wheel};
    inputMouse->setValue(report, 4);
    inputMouse->notify();
}

void clickNativeMouse(uint8_t button) {
    if (!bleConnected || inputMouse == nullptr) return;
    sendNativeMouse(0, 0, 0, mouseButtonMask | button);
    delay(25);
    sendNativeMouse(0, 0, 0, mouseButtonMask);
}

class BleServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) override {
        bleConnected = true;
        Serial.println("\n>>> [BLE HID] CONNECTED AS NATIVE BLUETOOTH MOUSE TO WINDOWS! <<<");
    }
    void onDisconnect(BLEServer* pServer) override {
        bleConnected = false;
        mouseButtonMask = 0;
        Serial.println("\n[BLE HID] Disconnected. Re-advertising...");
    }
};

class BleSecurity : public BLESecurityCallbacks {
    uint32_t onPassKeyRequest() { return 0; }
    void onPassKeyNotify(uint32_t pass_key) {}
    bool onConfirmPIN(uint32_t pass_key) { return true; }
    bool onSecurityRequest() { return true; }
    void onAuthenticationComplete(esp_ble_auth_cmpl_t cmpl) {}
};

void setupBLEHID() {
    Serial.println("[BLE HID] Initializing Native Bluetooth Mouse Engine...");
    BLEDevice::init("OmniBand Mouse");
    BLEDevice::setSecurityCallbacks(new BleSecurity());

    BLEServer* pServer = BLEDevice::createServer();
    pServer->setCallbacks(new BleServerCallbacks());

    hid = new BLEHIDDevice(pServer);
    inputMouse = hid->inputReport(1);

    hid->manufacturer()->setValue("OmniBand");
    hid->pnp(0x02, 0x045e, 0x0040, 0x0110);
    hid->hidInfo(0x00, 0x02);
    hid->reportMap((uint8_t*)hidReportMap, sizeof(hidReportMap));
    hid->startServices();

    BLESecurity* pSecurity = new BLESecurity();
    pSecurity->setAuthenticationMode(ESP_LE_AUTH_BOND);

    BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
    pAdvertising->setAppearance(0x03C2); // HID Mouse Icon in Windows!
    pAdvertising->addServiceUUID(hid->hidService()->getUUID());
    pAdvertising->start();

    Serial.println("[BLE HID] Native Bluetooth Mouse Advertising ON!");
    Serial.println("[BLE HID] Device Name in Windows: 'OmniBand Mouse'");
}

// ============================================================================
// 3. HARDWARE MPU6050 ENGINE (Zero external libraries needed!)
// ============================================================================
static bool  mpuOk = false;
static float gyroBiasX = 0.0f;
static float gyroBiasZ = 0.0f;
static float imuAx=0, imuAy=0, imuAz=0, imuGx=0, imuGy=0, imuGz=0;
static float pitchAbsDeg = 0, pitchDevDeg = 0, restPitchDeg = 0;
static bool  restPitchValid = false;
static float bootPitchSum = 0;
static uint16_t bootPitchN = 0;
static int8_t  cursorDx = 0;
static int8_t  cursorDy = 0;

void setupMPU() {
    Serial.println("[MPU] Probing MPU6050 on I2C (SDA=21, SCL=22)...");
    Wire.begin(PIN_SDA, PIN_SCL);
    Wire.setTimeOut(50);

    Wire.beginTransmission(I2C_ADDR_MPU);
    if (Wire.endTransmission() == 0) {
        Serial.println("[MPU] MPU6050 detected at address 0x68!");

        // Wake up MPU6050
        Wire.beginTransmission(I2C_ADDR_MPU);
        Wire.write(0x6B); Wire.write(0x00);
        Wire.endTransmission();

        // Gyro Full Scale +/- 500 deg/s
        Wire.beginTransmission(I2C_ADDR_MPU);
        Wire.write(0x1B); Wire.write(0x08);
        Wire.endTransmission();

        // Calibrate stationary gyro bias
        Serial.println("[MPU] Calibrating gyro zero-bias (stay still)...");
        float sumX = 0, sumZ = 0;
        for (int i = 0; i < 40; i++) {
            Wire.beginTransmission(I2C_ADDR_MPU);
            Wire.write(0x43);
            Wire.endTransmission(false);
            Wire.requestFrom((uint8_t)I2C_ADDR_MPU, (uint8_t)6);
            if (Wire.available() >= 6) {
                int16_t gx = (Wire.read() << 8) | Wire.read();
                int16_t gy = (Wire.read() << 8) | Wire.read();
                int16_t gz = (Wire.read() << 8) | Wire.read();
                sumX += (float)gx;
                sumZ += (float)gz;
            }
            delay(10);
        }
        gyroBiasX = sumX / 40.0f;
        gyroBiasZ = sumZ / 40.0f;
        mpuOk = true;
        Serial.println("[MPU] MPU6050 Ready! Hardware cursor active.");
    } else {
        Serial.println("[MPU] MPU6050 NOT detected.");
        mpuOk = false;
    }
}

// ============================================================================
// 4. EDGE COMPUTING DSP CONSTANTS & STATE
// ============================================================================
static const uint32_t ADC_PERIOD_US   = 1000;    // 1 kHz piezo sampling
static const uint32_t IMU_PERIOD_US   = 10000;   // 100 Hz IMU read
static const uint32_t STREAM_PERIOD_US= 20000;   // 50 Hz cursor update

static const float BASELINE_ALPHA     = 0.002f;
static const float NOISE_ALPHA        = 0.005f;
static const float SPIKE_ONSET_SIGMA  = 4.5f;
static const float SPIKE_ONSET_MIN    = 80.0f;
static const float SPIKE_RELEASE_K    = 0.40f;
static const float SPIKE_RELEASE_MIN  = 35.0f;
static const uint32_t SPIKE_MAX_US    = 350000;

static const uint32_t SPIKE_GAP_US     = 260000;
static const uint32_t CLENCH_MAX_US    = 900000;
static const uint32_t TAP_WINDOW_US    = 1000000;
static const uint8_t  TAP_MIN_SPIKES   = 3;
static const uint32_t HOLD_MIN_US      = 350000;
static const uint32_t HOLD_MAX_US      = 4000000;
static const uint32_t CHAN_SYNC_US     = 120000;
static const uint32_t GESTURE_WINDOW_US= 4500000;
static const uint32_t LOCKOUT_MS       = 320;

static const float NOD_THRESH_DEG      = 5.0f;
static const uint32_t NOD_WINDOW_US    = 2000000;
static const uint8_t  NOD_MIN_CROSS    = 4;

enum GestureId : int8_t {
    G_LEFT_CLENCH = 0,
    G_RIGHT_CLENCH,
    G_JAW_TAP,
    G_JAW_HOLD,
    G_LEFT_EYE,
    G_BROW_RAISE,
    G_SMILE,
    G_NOD,
    G_CHEEK_PUFF,
    G_NOSE_WRINKLE,
    G_NONE = -1
};

static const char* const GESTURE_NAME[10] = {
    "LEFT CLENCH", "RIGHT CLENCH", "JAW TAP", "JAW HOLD",
    "LEFT EYE", "BROW RAISE", "SMILE", "NOD",
    "CHEEK PUFF", "NOSE WRINKLE"
};

// ============================================================================
// 5. ON-DEVICE GESTURE -> NATIVE BLUETOOTH MOUSE ACTIONS
// ============================================================================
void onGesture(int id, float confidence) {
    Serial.print("[NATIVE GESTURE] ");
    Serial.print(id >= 0 && id < 10 ? GESTURE_NAME[id] : "?");
    Serial.print(" conf="); Serial.println(confidence, 2);

    if (id == G_RIGHT_CLENCH) {
        Serial.println("⚡ >>> NATIVE ACTION: LEFT CLICK! <<<");
        clickNativeMouse(MOUSE_BTN_LEFT);
    } 
    else if (id == G_JAW_TAP) {
        Serial.println("⚡ >>> NATIVE ACTION: RIGHT CLICK! (Double Clench) <<<");
        clickNativeMouse(MOUSE_BTN_RIGHT);
    } 
    else if (id == G_JAW_HOLD) {
        // Toggle Drag Lock (Hold Left Button down for moving files / dragging)
        mouseButtonMask ^= MOUSE_BTN_LEFT;
        sendNativeMouse(0, 0, 0, mouseButtonMask);
        Serial.println(mouseButtonMask & MOUSE_BTN_LEFT ? 
            "⚡ >>> NATIVE ACTION: DRAG LOCKED (Left button HELD) <<<" : 
            "⚡ >>> NATIVE ACTION: DRAG RELEASED <<<");
    } 
    else if (id == G_BROW_RAISE) {
        Serial.println("⚡ >>> NATIVE ACTION: WHEEL SCROLL UP! <<<");
        sendNativeMouse(0, 0, 3, mouseButtonMask); // Scroll Up
    } 
    else if (id == G_NOD) {
        Serial.println("⚡ >>> NATIVE ACTION: WHEEL SCROLL DOWN! <<<");
        sendNativeMouse(0, 0, -3, mouseButtonMask); // Scroll Down
    } 
    else if (id == G_LEFT_CLENCH) {
        Serial.println("⚡ >>> NATIVE ACTION: MIDDLE CLICK! <<<");
        clickNativeMouse(MOUSE_BTN_MIDDLE);
    }
}

// ============================================================================
// 6. PER-CHANNEL DSP STATE
// ============================================================================
struct ChanState {
    float raw, baseline, noise, dev, env;
    ChanState() : raw(0), baseline(2048), noise(8), dev(0), env(0) {}
};
static ChanState ch[4];

struct Spike {
    int8_t   pol;
    float    peak;
    uint32_t tStart, tEnd;
};
#define SPIKE_RING 16
struct ChanSpikes {
    Spike   s[SPIKE_RING];
    uint8_t head, count;
    ChanSpikes() : head(0), count(0) {}
};
static ChanSpikes spikes[4];
static uint32_t spikeConsumed[4] = {0, 0, 0, 0};
static bool     spikeActive[4];
static int8_t   spikePol[4];
static float    spikePeak[4];
static uint32_t spikeT0[4];

static float  t1Amp[10] = {200, 200, 200, 200, 200, 200, 200, 200, 200, 200};
static int      lastGesture    = G_NONE;
static float    lastConfidence = 0.0f;
static uint32_t lastFireMs     = 0;

static uint16_t nodCrossTimes[NOD_MIN_CROSS * 2];
static uint8_t  nodCrossHead = 0, nodCrossCount = 0;
static int8_t   nodSign = 0;

static uint32_t lastAdcUs = 0, lastImuUs = 0, lastStreamUs = 0;

static float absf_(float v) { return v < 0 ? -v : v; }
static uint32_t nowMs() { return millis(); }

// ============================================================================
// 7. SAMPLING & MPU GYRO CURSOR READ
// ============================================================================
static void sampleAdc(uint32_t nowUs);
static void sampleImu(uint32_t nowUs);
static void detectTier1(uint32_t nowUs);
static void processChannel(uint8_t i, int raw, uint32_t nowUs);

static void sampleAdc(uint32_t nowUs) {
    for (uint8_t i = 0; i < 4; i++)
        processChannel(i, analogRead(PIN_PIEZO[i]), nowUs);

    // 50 Hz Native Mouse Movement Stream
    if (nowUs - lastStreamUs >= STREAM_PERIOD_US) {
        lastStreamUs = nowUs;
        if (bleConnected && (cursorDx != 0 || cursorDy != 0)) {
            sendNativeMouse(cursorDx, cursorDy, 0, mouseButtonMask);
        }
    }
}

static void sampleImu(uint32_t nowUs) {
    if (!mpuOk) return;

    Wire.beginTransmission(I2C_ADDR_MPU);
    Wire.write(0x3B); // Accel + Gyro burst read
    if (Wire.endTransmission(false) != 0) {
        mpuOk = false;
        return;
    }
    Wire.requestFrom((uint8_t)I2C_ADDR_MPU, (uint8_t)14);
    if (Wire.available() >= 14) {
        int16_t ax = (Wire.read() << 8) | Wire.read();
        int16_t ay = (Wire.read() << 8) | Wire.read();
        int16_t az = (Wire.read() << 8) | Wire.read();
        Wire.read(); Wire.read(); // Skip temp
        int16_t gx = (Wire.read() << 8) | Wire.read();
        int16_t gy = (Wire.read() << 8) | Wire.read();
        int16_t gz = (Wire.read() << 8) | Wire.read();

        imuAy = (float)ay; imuAz = (float)az;
        imuGx = (float)gx; imuGz = (float)gz;

        // Relative Mouse Cursor from Gyro (Yaw -> dx, Pitch -> dy)
        float gz_deg = (imuGz - gyroBiasZ);
        float gx_deg = (imuGx - gyroBiasX);

        const float DEADZONE = 220.0f;
        const float SENSITIVITY = 160.0f;

        if (absf_(gz_deg) > DEADZONE) {
            float excess = (gz_deg > 0) ? (gz_deg - DEADZONE) : (gz_deg + DEADZONE);
            cursorDx = (int8_t)constrain(-(excess / SENSITIVITY), -120, 120);
        } else {
            cursorDx = 0;
        }

        if (absf_(gx_deg) > DEADZONE) {
            float excess = (gx_deg > 0) ? (gx_deg - DEADZONE) : (gx_deg + DEADZONE);
            cursorDy = (int8_t)constrain((excess / SENSITIVITY), -120, 120);
        } else {
            cursorDy = 0;
        }

        // Pitch for Nodding detection
        pitchAbsDeg = atan2f(imuAy, imuAz) * 57.2958f;
        pitchDevDeg = pitchAbsDeg - restPitchDeg;

        if (!restPitchValid && bootPitchN < 300) {
            bootPitchSum += pitchAbsDeg;
            bootPitchN++;
            if (bootPitchN >= 300) {
                restPitchDeg = bootPitchSum / bootPitchN;
                restPitchValid = true;
                pitchDevDeg = pitchAbsDeg - restPitchDeg;
            }
        }

        if (!restPitchValid) return;
        float th = NOD_THRESH_DEG;
        int8_t s = (pitchDevDeg >  th) ? 1 : (pitchDevDeg < -th) ? -1 : 0;
        if (s != 0 && nodSign != 0 && s != nodSign) {
            uint16_t tS = (uint16_t)((nowUs / 1000UL) & 0xFFFF);
            nodCrossTimes[nodCrossHead] = tS;
            nodCrossHead = (nodCrossHead + 1) % (NOD_MIN_CROSS * 2);
            if (nodCrossCount < NOD_MIN_CROSS * 2) nodCrossCount++;

            uint16_t nowS = (uint16_t)((nowUs / 1000UL) & 0xFFFF);
            uint8_t inWin = 0;
            for (uint8_t k = 0; k < nodCrossCount; k++) {
                uint8_t idx = (nodCrossHead + NOD_MIN_CROSS * 2 - 1 - k) % (NOD_MIN_CROSS * 2);
                if ((uint16_t)(nowS - nodCrossTimes[idx]) <= NOD_WINDOW_US / 1000UL) inWin++;
                else break;
            }
            if (inWin >= NOD_MIN_CROSS && nowMs() > lastFireMs + LOCKOUT_MS) {
                lastFireMs = nowMs();
                onGesture(G_NOD, 0.9f);
            }
        }
        if (s != 0) nodSign = s;
    }
}

// ============================================================================
// 8. 1 kHz SPIKE & CLENCH DETECTION ENGINE
// ============================================================================
static void channelSpikeStep(uint8_t i, float dev, uint32_t nowUs, float onset, float release) {
    ChanSpikes &cs = spikes[i];

    if (!spikeActive[i]) {
        if (dev > onset)        { spikeActive[i] = true; spikePol[i] = +1; }
        else if (dev < -onset) { spikeActive[i] = true; spikePol[i] = -1; }
        else return;
        spikePeak[i] = dev;
        spikeT0[i]   = nowUs;
        return;
    }

    if (spikePol[i] > 0) { if (dev > spikePeak[i]) spikePeak[i] = dev; }
    else                 { if (dev < spikePeak[i]) spikePeak[i] = dev; }

    float lvl = spikePol[i] > 0 ? release : -release;
    bool ended = (spikePol[i] > 0) ? (dev < lvl) : (dev > lvl);
    if (!ended && nowUs - spikeT0[i] > SPIKE_MAX_US) ended = true;
    if (!ended) return;

    Spike &s = cs.s[cs.head];
    s.pol = spikePol[i];
    s.peak = spikePeak[i];
    s.tStart = spikeT0[i];
    s.tEnd   = nowUs;
    cs.head = (cs.head + 1) % SPIKE_RING;
    if (cs.count < SPIKE_RING) cs.count++;
    spikeActive[i] = false;
}

static void processChannel(uint8_t i, int raw, uint32_t nowUs) {
    ChanState &c = ch[i];
    c.raw = (float)raw;
    float dev = c.raw - c.baseline;
    c.dev = dev;

    float a = absf_(dev);
    c.env = (a > c.env) ? a : c.env * 0.9985f;

    float onset   = max(SPIKE_ONSET_MIN, c.noise * SPIKE_ONSET_SIGMA);
    float release = max(SPIKE_RELEASE_MIN, onset * SPIKE_RELEASE_K);

    if (absf_(dev) < onset) {
        c.baseline += BASELINE_ALPHA * (c.raw - c.baseline);
        c.noise    += NOISE_ALPHA * (a - c.noise);
        dev = c.raw - c.baseline;
        c.dev = dev;
    }

    channelSpikeStep(i, dev, nowUs, onset, release);
}

static uint8_t collectSpikes(uint8_t i, uint32_t nowUs, Spike *out, uint8_t maxN) {
    ChanSpikes &cs = spikes[i];
    uint8_t n = 0;
    for (uint8_t k = 0; k < cs.count && n < maxN; k++) {
        uint8_t idx = (cs.head + SPIKE_RING - 1 - k) % SPIKE_RING;
        Spike &s = cs.s[idx];
        if (nowUs - s.tEnd > GESTURE_WINDOW_US) break;
        out[n++] = s;
    }
    for (uint8_t a = 0, b = n - 1; a < b; a++, b--) {
        Spike tmp = out[a]; out[a] = out[b]; out[b] = tmp;
    }
    return n;
}

struct BurstInfo {
    uint8_t  n;
    uint32_t tFirst, tLast;
    int8_t   firstPol, lastPol;
    float    firstPeak, lastPeak, maxAbs;
};

static bool analyzeLastBurst(uint8_t chIdx, uint32_t nowUs, BurstInfo &b) {
    Spike buf[SPIKE_RING];
    uint8_t n = collectSpikes(chIdx, nowUs, buf, SPIKE_RING);
    if (n == 0) return false;
    int start = (int)n - 1;
    for (int k = (int)n - 2; k >= 0; k--) {
        if (buf[k + 1].tStart - buf[k].tEnd < SPIKE_GAP_US) start = k;
        else break;
    }
    int cnt = (int)n - start;
    b.n = (uint8_t)cnt;
    b.tFirst = buf[start].tStart;
    b.tLast  = buf[n - 1].tEnd;
    b.firstPol  = buf[start].pol;
    b.lastPol   = buf[n - 1].pol;
    b.firstPeak = buf[start].peak;
    b.lastPeak  = buf[n - 1].peak;
    b.maxAbs = 0;
    for (int k = start; k < (int)n; k++)
        b.maxAbs = max(b.maxAbs, absf_(buf[k].peak));
    return true;
}

static bool detectHold(uint8_t chIdx, uint32_t nowUs, uint32_t &tConsume) {
    Spike buf[SPIKE_RING];
    uint8_t n = collectSpikes(chIdx, nowUs, buf, SPIKE_RING);
    if (n < 2) return false;
    Spike &rel = buf[n - 1];
    Spike &on  = buf[n - 2];
    if (rel.tEnd <= spikeConsumed[chIdx]) return false;
    if (nowUs - rel.tEnd > CHAN_SYNC_US * 2) return false;
    if (on.pol == rel.pol) return false;
    uint32_t gap = rel.tStart - on.tEnd;
    if (gap <= HOLD_MIN_US || gap >= HOLD_MAX_US) return false;
    if (absf_(on.peak) <= 90.0f || absf_(rel.peak) <= 65.0f) return false;
    tConsume = rel.tEnd;
    return true;
}

static bool detectTap(uint8_t chIdx, uint32_t nowUs, uint32_t &tConsume) {
    BurstInfo b;
    if (!analyzeLastBurst(chIdx, nowUs, b)) return false;
    if (b.tLast <= spikeConsumed[chIdx]) return false;
    if (!(b.n >= TAP_MIN_SPIKES && (b.tLast - b.tFirst) <= TAP_WINDOW_US && b.maxAbs > 90.0f)) return false;
    tConsume = b.tLast;
    return true;
}

static bool detectClench(uint8_t chIdx, uint32_t nowUs, uint32_t &tConsume) {
    BurstInfo b;
    if (!analyzeLastBurst(chIdx, nowUs, b)) return false;
    if (b.tLast <= spikeConsumed[chIdx]) return false;
    if ((b.tLast - b.tFirst) > CLENCH_MAX_US) return false;
    if (b.maxAbs <= 90.0f) return false;
    tConsume = b.tLast;
    return true;
}

static bool channelHasPair(uint8_t chIdx, uint32_t nowUs, float thr, uint32_t &tOnset, uint32_t &tRelease) {
    Spike buf[SPIKE_RING];
    uint8_t n = collectSpikes(chIdx, nowUs, buf, SPIKE_RING);
    if (n < 2) return false;
    Spike &a = buf[n - 2], &b = buf[n - 1];
    if (a.pol == b.pol) return false;
    if (nowUs - b.tEnd > CHAN_SYNC_US * 2) return false;
    if (b.tEnd <= spikeConsumed[chIdx]) return false;
    if (absf_(a.peak) < thr || absf_(b.peak) < thr) return false;
    tOnset = a.tStart; tRelease = b.tEnd;
    return true;
}

static void detectTier1(uint32_t nowUs) {
    if (nowMs() < lastFireMs + LOCKOUT_MS) return;
    const uint8_t JAW[2] = {0, 3}; // CH1, CH4
    uint32_t tC;

    // Jaw Hold & Release -> Drag Lock
    for (uint8_t c = 0; c < 2; c++) {
        if (detectHold(JAW[c], nowUs, tC)) {
            spikeConsumed[JAW[c]] = tC;
            lastFireMs = nowMs();
            onGesture(G_JAW_HOLD, 0.95f);
            return;
        }
    }

    // Jaw Tap / Double Tap -> Right Click
    for (uint8_t c = 0; c < 2; c++) {
        if (detectTap(JAW[c], nowUs, tC)) {
            spikeConsumed[JAW[c]] = tC;
            lastFireMs = nowMs();
            onGesture(G_JAW_TAP, 0.9f);
            return;
        }
    }

    // Single Jaw Clench on CH4 (GPIO 26) -> Left Click
    if (detectClench(3, nowUs, tC)) {
        spikeConsumed[3] = tC;
        lastFireMs = nowMs();
        onGesture(G_RIGHT_CLENCH, 0.9f);
        return;
    }

    // Left temple clench on CH1 -> Middle Click
    if (detectClench(0, nowUs, tC)) {
        spikeConsumed[0] = tC;
        lastFireMs = nowMs();
        onGesture(G_LEFT_CLENCH, 0.9f);
        return;
    }

    // Eyebrow Raise on CH2 & CH3 -> Scroll Up
    {
        uint32_t o1, r1, o2, r2;
        if (channelHasPair(1, nowUs, 100.0f, o1, r1) &&
            channelHasPair(2, nowUs, 100.0f, o2, r2) &&
            abs((int32_t)(o2 - o1)) < (int32_t)CHAN_SYNC_US) {
            spikeConsumed[1] = r1; spikeConsumed[2] = r2;
            lastFireMs = nowMs();
            onGesture(G_BROW_RAISE, 0.85f);
            return;
        }
    }
}

// ============================================================================
// 9. SETUP & LOOP
// ============================================================================
void setup() {
    Serial.begin(115200);
    delay(400);

    Serial.println("\n=======================================================");
    Serial.println("  OmniBand Native Bluetooth Mouse Firmware             ");
    Serial.println("  NO PC SOFTWARE NEEDED! Connects directly to Windows  ");
    Serial.println("  Pins: CH1=32, CH2=34, CH3=33, CH4=26                 ");
    Serial.println("  Bluetooth Device Name: 'OmniBand Mouse'              ");
    Serial.println("=======================================================");

    pinMode(PIN_BTN, INPUT_PULLUP);
    pinMode(PIN_CH1, INPUT);
    pinMode(PIN_CH2, INPUT);
    pinMode(PIN_CH3, INPUT);
    pinMode(PIN_CH4, INPUT);

    analogReadResolution(12);

    setupMPU();
    setupBLEHID();

    // Baseline calibration
    Serial.println("[DSP] Calibrating 4-channel baselines...");
    for (int i = 0; i < 50; i++) {
        for (int k = 0; k < 4; k++) {
            ch[k].baseline = ch[k].baseline * 0.9f + (float)analogRead(PIN_PIEZO[k]) * 0.1f;
        }
        delay(10);
    }
    Serial.println("[DSP] Calibrated! Device ready to pair as Windows Bluetooth Mouse.");
}

void loop() {
    uint32_t nowUs = micros();

    // 1. 1 kHz Edge DSP Piezo Sampling
    if (nowUs - lastAdcUs >= ADC_PERIOD_US) {
        lastAdcUs += ADC_PERIOD_US;
        if (lastAdcUs + ADC_PERIOD_US < nowUs) lastAdcUs = nowUs;
        sampleAdc(nowUs);
    }

    // 2. 100 Hz MPU Gyro Cursor Read
    if (nowUs - lastImuUs >= IMU_PERIOD_US) {
        lastImuUs += IMU_PERIOD_US;
        sampleImu(nowUs);
    }

    // 3. Detect Gestures on-device
    detectTier1(nowUs);

    // 4. Auto-reconnect advertising
    if (!bleConnected && oldBleConnected) {
        delay(300);
        BLEDevice::startAdvertising();
        oldBleConnected = bleConnected;
    }
    if (bleConnected && !oldBleConnected) {
        oldBleConnected = bleConnected;
    }
}
