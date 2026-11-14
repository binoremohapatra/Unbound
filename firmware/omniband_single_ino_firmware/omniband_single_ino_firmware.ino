/*
 * ============================================================================
 *  OmniBand.ino — ESP32 Wearable Multi-Gesture & MPU Mouse Firmware
 * ============================================================================
 *  Edge Computing Gesture Detection + MPU6050 Mouse Cursor + Bluetooth BLE
 *  Channels: 4x Piezo + MPU6050 Gyro + SSD1306 OLED + BLE GATT
 *
 *  MAPPED GESTURES:
 *    1. JAW CLENCH (CH4)      -> Left Mouse Click
 *    2. JAW TAP / DOUBLE TAP   -> Right Mouse Click
 *    3. JAW HOLD (>350ms)     -> Mouse Drag Lock Toggle
 *    4. BROW RAISE (CH2+CH3)   -> Wheel Scroll Up (or Space)
 *    5. HEAD NOD (MPU pitch)   -> Wheel Scroll Down (or Enter)
 *    6. LEFT EYE WINK (CH1+CH2)-> Escape / Back
 *    7. LEFT CLENCH (CH1)     -> Key 'A' / Steer Left
 *    8. MPU GYRO (Yaw / Pitch) -> Windows Mouse Cursor (dx, dy)
 *
 *  HARDWARE PINS (Matching physical headband wiring):
 *    - CH1: GPIO 32 (Left Temple)
 *    - CH2: GPIO 34 (Centre-Left Forehead)
 *    - CH3: GPIO 33 (Centre-Right Forehead)
 *    - CH4: GPIO 26 (Right Temple / Jaw Clench)
 *    - I2C SDA: GPIO 21
 *    - I2C SCL: GPIO 22
 * ============================================================================
 */

#include <Arduino.h>
#include <Wire.h>
#include <string.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Preferences.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// ============================================================================
// 1. CONFIG & PINOUT
// ============================================================================
static const uint8_t PIN_CH1 = 32;   // Left temple
static const uint8_t PIN_CH2 = 34;   // Centre-left forehead
static const uint8_t PIN_CH3 = 33;   // Centre-right forehead
static const uint8_t PIN_CH4 = 26;   // Right temple / jaw clench
static const uint8_t PIN_PIEZO[4] = {PIN_CH1, PIN_CH2, PIN_CH3, PIN_CH4};

static const uint8_t PIN_SDA = 21;
static const uint8_t PIN_SCL = 22;
static const uint8_t I2C_ADDR_MPU = 0x68;
static const uint8_t OLED_ADDR    = 0x3C;

static const uint8_t PIN_BTN = 23;
static const bool    BTN_ACTIVE_LOW = true;

// OLED
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
static Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// IMU
static Adafruit_MPU6050 mpu;
static bool imuOk = false;

// Storage
static Preferences prefs;

// BLE Specs
#define BLE_DEVICE_NAME     "Omniband"
#define SERVICE_UUID        "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define CHARACTERISTIC_UUID "beb5483e-36e1-4688-b7f5-ea07361b26a6"

static BLEServer* pServer = nullptr;
static BLECharacteristic* pCharacteristic = nullptr;
static bool deviceConnected = false;
static bool oldDeviceConnected = false;
static uint32_t packetSeq = 0;

// Relative Mouse Cursor State
static float cursorDx = 0.0f;
static float cursorDy = 0.0f;
static float gyroBiasX = 0.0f;
static float gyroBiasZ = 0.0f;

// ============================================================================
// 2. TUNABLE CONSTANTS
// ============================================================================
static const uint32_t ADC_PERIOD_US   = 1000;    // ~1 kHz piezo
static const uint32_t IMU_PERIOD_US   = 10000;   // 100 Hz IMU
static const uint8_t  ADC_BITS        = 12;

static const float BASELINE_ALPHA     = 0.002f;
static const float NOISE_ALPHA        = 0.005f;
static const float CAL_BASELINE_ALPHA = 0.01f;

static const float    SPIKE_ONSET_SIGMA = 4.8f;
static const float    SPIKE_ONSET_MIN   = 80.0f;
static const float    SPIKE_RELEASE_K   = 0.40f;
static const float    SPIKE_RELEASE_MIN = 35.0f;
static const uint32_t SPIKE_MAX_US      = 350000;

static const uint32_t SPIKE_GAP_US     = 260000;
static const uint32_t CLENCH_MAX_US    = 900000;
static const uint32_t TAP_WINDOW_US    = 1000000;
static const uint8_t  TAP_MIN_SPIKES   = 3;
static const uint32_t HOLD_MIN_US      = 350000;
static const uint32_t HOLD_MAX_US      = 4000000;
static const uint32_t CHAN_SYNC_US     = 120000;
static const uint32_t GESTURE_WINDOW_US= 4500000;

static const float T1_THRESH_FRAC = 0.55f;
static const float T1_MIN_COUNTS  = 90.0f;
static const float T1_MAX_COUNTS  = 3000.0f;
static const float EYE_HARD_MULT  = 1.35f;

static const uint32_t LOCKOUT_MS = 320;

static const uint8_t  FEATURE_DIM    = 24;
static const uint32_t WIN_QUIET_US   = 180000;
static const float    MIN_TIER2_CONF = 0.12f;
static const uint8_t  T2_GESTURES[]  = {6, 8, 9};
static const uint8_t  N_T2 = 3;

static const float    NOD_THRESH_DEG  = 5.0f;
static const uint32_t NOD_WINDOW_US   = 2000000;
static const uint8_t  NOD_MIN_CROSS   = 4;

static const uint32_t CAL_REST_MS        = 5000;
static const uint8_t  CAL_REPS           = 5;
static const uint32_t CAL_REP_TIMEOUT_MS = 9000;
static const uint32_t BTN_HOLD_MS        = 3000;

static const uint8_t  TEST_REPS       = 3;
static const uint32_t TEST_TIMEOUT_MS = 9000;

static const uint32_t LOG_DECIMATE  = 5;
static const uint32_t PLOT_DECIMATE = 10;
static const uint32_t OLED_PERIOD_MS = 250;

// ============================================================================
// 3. GESTURES & ACTION MAPPINGS
// ============================================================================
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

void sendBleEvent(const char* eventStr) {
    Serial.print("[BLE EVENT] >>> ");
    Serial.println(eventStr);
    if (deviceConnected && pCharacteristic != nullptr) {
        char buf[32];
        snprintf(buf, sizeof(buf), "%s\n", eventStr);
        pCharacteristic->setValue((uint8_t*)buf, strlen(buf));
        pCharacteristic->notify();
    }
}

// FULL GESTURE DISPATCHER
void onGesture(int id, float confidence) {
    if (id == G_RIGHT_CLENCH) {
        sendBleEvent("CLICK");        // Single Clench -> Left Click
    } else if (id == G_JAW_TAP) {
        sendBleEvent("JAW_TAP");      // Double Clench / Tap -> Right Click
    } else if (id == G_JAW_HOLD) {
        sendBleEvent("HOLD");         // Hold Clench -> Drag Lock
    } else if (id == G_BROW_RAISE) {
        sendBleEvent("BROW_RAISE");   // Eyebrow Raise -> Scroll Up
    } else if (id == G_NOD) {
        sendBleEvent("NOD");          // Head Nod -> Scroll Down
    } else if (id == G_LEFT_EYE) {
        sendBleEvent("LEFT_EYE");     // Left Eye Wink -> Escape / Back
    } else if (id == G_LEFT_CLENCH) {
        sendBleEvent("LEFT_CLENCH");  // Left Clench -> Key 'A'
    } else if (id == G_SMILE) {
        sendBleEvent("SMILE");
    } else if (id == G_CHEEK_PUFF) {
        sendBleEvent("CHEEK_PUFF");
    }
}

// ============================================================================
// 4. STATE
// ============================================================================
enum SysMode : uint8_t { MODE_NORMAL, MODE_CAL, MODE_TEST };
static SysMode sysMode = MODE_NORMAL;
static bool logEnable = false;
static bool plotEnable = false;

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

struct WinChan {
    float    sum, sumsq, peakPos, peakNeg, ePos, eNeg;
    uint32_t tPeakPos, tPeakNeg;
    uint16_t zc;
    int8_t   prevSign;
    bool     seen;
    WinChan() { memset(this, 0, sizeof(*this)); }
};
struct WinState {
    bool     active;
    uint32_t startUs, lastActiveUs;
    WinChan  c[4];
    WinState() : active(false), startUs(0), lastActiveUs(0) {}
};
static WinState win;

static float  t1Amp[10];
static int8_t t1Pol[2];
static bool   t1Valid = false;
static uint32_t tier1ClaimUntilMs = 0;

static float t2Mean[FEATURE_DIM];
static float t2Std[FEATURE_DIM];
static float t2Centroid[N_T2][FEATURE_DIM];
static bool  t2Valid = false;

static int      lastGesture    = G_NONE;
static float    lastConfidence = 0.0f;
static uint32_t lastFireMs     = 0;

static uint16_t nodCrossTimes[NOD_MIN_CROSS * 2];
static uint8_t  nodCrossHead = 0, nodCrossCount = 0;
static int8_t   nodSign = 0;
static float    pitchAbsDeg = 0, pitchDevDeg = 0, restPitchDeg = 0;
static bool     restPitchValid = false;
static float    calPitchMax = 0;
static float    bootPitchSum = 0;
static uint16_t bootPitchN = 0;

static float imuAx=0, imuAy=0, imuAz=0, imuGx=0, imuGy=0, imuGz=0;
static uint32_t lastAdcUs = 0, lastImuUs = 0, lastOledMs = 0, lastBtnMs = 0, lastStreamUs = 0;
static bool     btnWasDown = false;
static uint32_t adcDiv = 0;

enum CalPhase : uint8_t { CAL_REST, CAL_PROMPT, CAL_DONE };
static CalPhase calPhase = CAL_DONE;
static uint32_t calPhaseStart = 0, calPromptStart = 0;
static uint8_t  calGesture = 0, calRep = 0;
static bool     calWaiting = false;
static float    calSamples[10][CAL_REPS][FEATURE_DIM];
static uint8_t  calCount[10];
static const uint8_t CAL_ORDER[10] = {0,1,2,3,4,5,6,7,8,9};

static uint8_t  testGesture = 0;
static uint8_t  testDeck[10 * TEST_REPS];
static uint16_t testDeckPos = 0, testDeckLen = 0;
static bool     testArmed = false;
static uint32_t testPromptStart = 0;
static uint8_t  confusion[10][10];
static uint8_t  gestureTotal[10], gestureCorrect[10];

// ============================================================================
// 5. UTILS & FORWARD DECLS
// ============================================================================
static float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }
static float absf_(float v) { return v < 0 ? -v : v; }
static uint32_t nowMs() { return millis(); }

static void emitGesture(int id, float conf);
static void closeWindow();
static void detectTier1(uint32_t nowUs);
static void channelSpikeStep(uint8_t i, float dev, uint32_t nowUs, float onset, float release);
static void processChannel(uint8_t i, int raw, uint32_t nowUs);
static void accumulateWin(uint8_t i, float dev, uint32_t nowUs, float onset);
static void extractFeatures(float feat[FEATURE_DIM], uint32_t durUs);
static void calOnWindow(const float feat[FEATURE_DIM]);
static void testOnWindow(const float feat[FEATURE_DIM]);
static void classifyTier2(const float feat[FEATURE_DIM]);
static void finishCalibration();
static void computeCalibration();
static void saveCalibration();
static bool loadCalibration();
static void printCalSummary();

// ============================================================================
// BLE SERVER CALLBACKS
// ============================================================================
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
            Serial.print("[BLE CMD FROM PC]: ");
            Serial.println(rxValue.c_str());
            if (rxValue.indexOf("CAL") >= 0 || rxValue.indexOf("RESET") >= 0) {
                for (int i = 0; i < 4; i++) {
                    ch[i].baseline = (float)analogRead(PIN_PIEZO[i]);
                    ch[i].env = 0.0f;
                }
            }
        }
    }
};

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

    Serial.println("[BLE] Advertising ON. Device Name: 'Omniband'");
}

// ============================================================================
// 6. SAMPLING & MPU RELATIVE MOUSE CURSOR
// ============================================================================
static void sampleAdc(uint32_t nowUs) {
    for (uint8_t i = 0; i < 4; i++)
        processChannel(i, analogRead(PIN_PIEZO[i]), nowUs);

    // 50 Hz Packet Streaming over BLE & USB (every 20 ms)
    if (nowUs - lastStreamUs >= 20000) {
        lastStreamUs = nowUs;
        packetSeq++;
        char pkt[96];
        snprintf(pkt, sizeof(pkt), "S,%.1f,%.1f,%.1f,%.1f,%u,%.2f,%.2f\n",
                 ch[0].env, ch[1].env, ch[2].env, ch[3].env, packetSeq, cursorDx, cursorDy);

        if (logEnable) Serial.print(pkt);

        if (deviceConnected && pCharacteristic != nullptr) {
            pCharacteristic->setValue((uint8_t*)pkt, strlen(pkt));
            pCharacteristic->notify();
        }
    }
}

static void sampleImu(uint32_t nowUs) {
    if (!imuOk) return;
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);
    imuAx = a.acceleration.x; imuAy = a.acceleration.y; imuAz = a.acceleration.z;
    imuGx = g.gyro.x;         imuGy = g.gyro.y;         imuGz = g.gyro.z;

    // ========================================================================
    // MPU6050 RELATIVE MOUSE CURSOR (Left/Right -> Yaw, Up/Down -> Pitch)
    // ========================================================================
    float gz_deg = (imuGz - gyroBiasZ) * 57.2958f;
    float gx_deg = (imuGx - gyroBiasX) * 57.2958f;
    const float DEADZONE_DEG = 12.0f;
    const float SCALE_DEG = 10.0f;

    if (absf_(gz_deg) > DEADZONE_DEG) {
        float ex = (gz_deg > 0) ? (gz_deg - DEADZONE_DEG) : (gz_deg + DEADZONE_DEG);
        cursorDx = -(ex / SCALE_DEG);
    } else {
        cursorDx = 0.0f;
    }

    if (absf_(gx_deg) > DEADZONE_DEG) {
        float ex = (gx_deg > 0) ? (gx_deg - DEADZONE_DEG) : (gx_deg + DEADZONE_DEG);
        cursorDy = (ex / SCALE_DEG);
    } else {
        cursorDy = 0.0f;
    }

    // Pitch for nodding gesture
    pitchAbsDeg = atan2f(imuAy, imuAz) * 57.2958f;
    pitchDevDeg = pitchAbsDeg - restPitchDeg;

    if (!restPitchValid && sysMode == MODE_NORMAL && bootPitchN < 300) {
        bootPitchSum += pitchAbsDeg;
        bootPitchN++;
        if (bootPitchN >= 300) {
            restPitchDeg = bootPitchSum / bootPitchN;
            restPitchValid = true;
            pitchDevDeg = pitchAbsDeg - restPitchDeg;
        }
    }

    if (sysMode == MODE_CAL && calWaiting)
        calPitchMax = max(calPitchMax, absf_(pitchDevDeg));

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
        if (inWin >= NOD_MIN_CROSS && nowMs() > lastFireMs + LOCKOUT_MS)
            emitGesture(G_NOD, 0.9f);
    }
    if (s != 0) nodSign = s;
}

// ============================================================================
// 7. FILTERING & SPIKE DETECTION
// ============================================================================
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
        float ba = (sysMode == MODE_CAL && calPhase == CAL_REST) ? CAL_BASELINE_ALPHA : BASELINE_ALPHA;
        c.baseline += ba * (c.raw - c.baseline);
        c.noise    += NOISE_ALPHA * (a - c.noise);
        dev = c.raw - c.baseline;
        c.dev = dev;
    }

    channelSpikeStep(i, dev, nowUs, onset, release);

    if (!win.active) {
        if (absf_(dev) > release) {
            win.active = true;
            win.startUs = nowUs;
            for (uint8_t k = 0; k < 4; k++) win.c[k] = WinChan();
            accumulateWin(i, dev, nowUs, onset);
            win.lastActiveUs = nowUs;
        }
    } else {
        accumulateWin(i, dev, nowUs, onset);
        if (absf_(dev) > SPIKE_RELEASE_MIN) win.lastActiveUs = nowUs;
        bool anyActive = false;
        for (uint8_t k = 0; k < 4; k++)
            if (absf_(ch[k].dev) > SPIKE_RELEASE_MIN) { anyActive = true; break; }
        if (!anyActive && nowUs - win.lastActiveUs > WIN_QUIET_US)
            closeWindow();
    }
}

static void accumulateWin(uint8_t i, float dev, uint32_t nowUs, float onset) {
    WinChan &w = win.c[i];
    w.seen = true;
    w.sum += dev;
    w.sumsq += dev * dev;
    if (dev > w.peakPos) { w.peakPos = dev; w.tPeakPos = nowUs; }
    if (dev < w.peakNeg) { w.peakNeg = dev; w.tPeakNeg = nowUs; }
    if (w.zc == 0 && w.prevSign == 0 && absf_(dev) > onset)
        w.prevSign = dev > 0 ? 1 : -1;
    int8_t sg = dev > 0 ? 1 : (dev < 0 ? -1 : 0);
    if (sg != 0 && w.prevSign != 0 && sg != w.prevSign && absf_(dev) > onset * 0.5f)
        w.zc++;
    if (sg != 0) w.prevSign = sg;
    if (dev > 0) w.ePos += dev * dev; else w.eNeg += dev * dev;
}

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

static void closeWindow() {
    win.active = false;
    uint32_t dur = win.lastActiveUs - win.startUs;
    if (dur < 20000) return;

    float feat[FEATURE_DIM];
    extractFeatures(feat, dur);

    if (sysMode == MODE_CAL)        { calOnWindow(feat);  return; }
    if (sysMode == MODE_TEST)       { testOnWindow(feat); return; }
    if (nowMs() < tier1ClaimUntilMs) return;
    classifyTier2(feat);
}

static void extractFeatures(float feat[FEATURE_DIM], uint32_t durUs) {
    float n = max(1.0f, (float)(durUs / 1000));
    float totE = 0;
    for (uint8_t i = 0; i < 4; i++) totE += win.c[i].ePos + win.c[i].eNeg;
    if (totE <= 0) totE = 1;

    uint8_t prim = 0; float bestE = -1;
    for (uint8_t i = 0; i < 4; i++) {
        float e = win.c[i].ePos + win.c[i].eNeg;
        if (e > bestE) { bestE = e; prim = i; }
    }

    for (uint8_t i = 0; i < 4; i++) {
        WinChan &w = win.c[i];
        float mean = w.sum / n;
        float var  = w.sumsq / n - mean * mean;
        float rms  = var > 0 ? sqrtf(var) : 0;
        float domPk = absf_(w.peakPos) >= absf_(w.peakNeg) ? w.peakPos : w.peakNeg;
        uint32_t tDom = (absf_(w.peakPos) >= absf_(w.peakNeg)) ? w.tPeakPos : w.tPeakNeg;
        float riseFrac = durUs > 0 ? clampf((float)(tDom - win.startUs) / (float)durUs, 0, 1) : 0.5f;
        feat[5 * i + 0] = absf_(domPk);
        feat[5 * i + 1] = rms;
        feat[5 * i + 2] = domPk;
        feat[5 * i + 3] = riseFrac;
        feat[5 * i + 4] = (float)w.zc;
    }
    feat[20] = log10f(totE + 1.0f);
    feat[21] = (float)durUs / 1000000.0f;
    feat[22] = 0;
    for (uint8_t i = 0; i < 4; i++)
        if ((win.c[i].ePos + win.c[i].eNeg) > 0.05f * totE) feat[22] += 1;
    feat[22] /= 4.0f;
    feat[23] = (float)prim / 3.0f;
}

// ============================================================================
// 8. TIER-1 RULE DETECTORS
// ============================================================================
static float t1Threshold(int gesture) {
    return clampf(t1Amp[gesture] * T1_THRESH_FRAC, T1_MIN_COUNTS, T1_MAX_COUNTS);
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
    uint32_t onsetToReleaseUs;
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
    b.onsetToReleaseUs = (cnt >= 2) ? buf[n - 1].tStart - buf[start].tEnd : 0;
    return true;
}

static bool detectHold(uint8_t chIdx, int holdId, uint32_t nowUs, uint32_t &tConsume) {
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
    float thr = t1Threshold(holdId);
    if (absf_(on.peak) <= thr || absf_(rel.peak) <= thr * 0.7f) return false;
    tConsume = rel.tEnd;
    return true;
}

static bool detectTap(uint8_t chIdx, uint32_t nowUs, uint32_t &tConsume) {
    BurstInfo b;
    if (!analyzeLastBurst(chIdx, nowUs, b)) return false;
    if (b.tLast <= spikeConsumed[chIdx]) return false;
    if (!(b.n >= TAP_MIN_SPIKES &&
          (b.tLast - b.tFirst) <= TAP_WINDOW_US &&
          b.maxAbs > t1Threshold(G_JAW_TAP))) return false;
    tConsume = b.tLast;
    return true;
}

static bool detectClench(uint8_t chIdx, int clenchId, uint32_t nowUs, uint32_t &tConsume) {
    BurstInfo b;
    if (!analyzeLastBurst(chIdx, nowUs, b)) return false;
    if (b.tLast <= spikeConsumed[chIdx]) return false;
    if ((b.tLast - b.tFirst) > CLENCH_MAX_US) return false;
    if (b.maxAbs <= t1Threshold(clenchId)) return false;
    int8_t want = t1Pol[chIdx == 3 ? 1 : 0];
    if (t1Valid && want != 0 && b.firstPol != want) return false;
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
    const uint8_t JAW[2] = {0, 3};
    uint32_t tC;

    for (uint8_t c = 0; c < 2; c++)
        if (detectHold(JAW[c], G_JAW_HOLD, nowUs, tC)) {
            spikeConsumed[JAW[c]] = tC;
            emitGesture(G_JAW_HOLD, 0.95f); return; }
    for (uint8_t c = 0; c < 2; c++)
        if (detectTap(JAW[c], nowUs, tC)) {
            spikeConsumed[JAW[c]] = tC;
            emitGesture(G_JAW_TAP, 0.9f); return; }
    if (detectClench(0, G_LEFT_CLENCH, nowUs, tC)) {
        spikeConsumed[0] = tC;
        emitGesture(G_LEFT_CLENCH, 0.9f); return; }
    if (detectClench(3, G_RIGHT_CLENCH, nowUs, tC)) {
        spikeConsumed[3] = tC;
        emitGesture(G_RIGHT_CLENCH, 0.9f); return; }

    // Left eye close: CH1 + CH2
    {
        float browThr = t1Threshold(G_BROW_RAISE);
        float thr = max(t1Threshold(G_LEFT_EYE), browThr * EYE_HARD_MULT);
        uint32_t o1, r1, o2, r2;
        if (channelHasPair(0, nowUs, thr, o1, r1) &&
            channelHasPair(1, nowUs, thr, o2, r2) &&
            abs((int32_t)(o2 - o1)) < (int32_t)CHAN_SYNC_US) {
            spikeConsumed[0] = r1; spikeConsumed[1] = r2;
            emitGesture(G_LEFT_EYE, 0.9f); return;
        }
    }
    // Eyebrow raise: CH2 + CH3
    {
        float thr = t1Threshold(G_BROW_RAISE);
        uint32_t o1, r1, o2, r2;
        if (channelHasPair(1, nowUs, thr, o1, r1) &&
            channelHasPair(2, nowUs, thr, o2, r2) &&
            abs((int32_t)(o2 - o1)) < (int32_t)CHAN_SYNC_US) {
            spikeConsumed[1] = r1; spikeConsumed[2] = r2;
            emitGesture(G_BROW_RAISE, 0.85f); return;
        }
    }
}

// ============================================================================
// 9. TIER-2 CLASSIFIER
// ============================================================================
static void normalizeFeat(const float f[FEATURE_DIM], float out[FEATURE_DIM]) {
    for (uint8_t j = 0; j < FEATURE_DIM; j++) {
        float s = t2Std[j] > 1e-6f ? t2Std[j] : 1.0f;
        out[j] = (f[j] - t2Mean[j]) / s;
    }
}
static float featDist(const float a[FEATURE_DIM], const float b[FEATURE_DIM]) {
    float d = 0;
    for (uint8_t j = 0; j < FEATURE_DIM; j++) {
        float v = a[j] - b[j]; d += v * v;
    }
    return sqrtf(d);
}

static int8_t tier2Match(const float feat[FEATURE_DIM], float &confOut) {
    if (!t2Valid) return -1;
    float fn[FEATURE_DIM];
    normalizeFeat(feat, fn);
    float best = 1e9f, second = 1e9f; int8_t bi = -1;
    for (uint8_t g = 0; g < N_T2; g++) {
        float d = featDist(fn, t2Centroid[g]);
        if (d < best)  { second = best; best = d; bi = (int8_t)g; }
        else if (d < second) second = d;
    }
    if (bi < 0 || second >= 1e8f) return -1;
    confOut = clampf((second - best) / (second + best + 1e-3f), 0.0f, 1.0f);
    return confOut >= MIN_TIER2_CONF ? bi : -1;
}

static void classifyTier2(const float feat[FEATURE_DIM]) {
    float conf;
    int8_t bi = tier2Match(feat, conf);
    if (bi >= 0) emitGesture(T2_GESTURES[bi], conf);
}

// ============================================================================
// 10. CALIBRATION & PERSISTENCE
// ============================================================================
static void startCalibration() {
    sysMode = MODE_CAL;
    calPhase = CAL_REST;
    calPhaseStart = nowMs();
    calGesture = 0; calRep = 0; calWaiting = false;
    memset(calCount, 0, sizeof(calCount));
    memset(calSamples, 0, sizeof(calSamples));
    nodSign = 0; nodCrossCount = 0; nodCrossHead = 0;
    Serial.println("[CAL] Entering calibration. Stay still for rest phase.");
}

static void promptNextCal() {
    calWaiting = true;
    calPitchMax = 0;
    calPromptStart = nowMs();
    uint8_t g = CAL_ORDER[calGesture];
    Serial.print("[CAL] Perform: "); Serial.print(GESTURE_NAME[g]);
    Serial.print("  "); Serial.print(calRep + 1); Serial.print("/");
    Serial.println(CAL_REPS);
}

static void advanceCalRep() {
    calRep++;
    if (calRep >= CAL_REPS) {
        calRep = 0; calGesture++;
        if (calGesture >= 10) { finishCalibration(); return; }
    }
    promptNextCal();
}

static void calOnWindow(const float feat[FEATURE_DIM]) {
    if (!calWaiting) return;
    if (win.startUs < (uint32_t)calPromptStart * 1000UL) return;
    uint8_t g = CAL_ORDER[calGesture];
    if (g == G_NOD) return;
    if (calCount[g] >= CAL_REPS) return;
    memcpy(calSamples[g][calCount[g]], feat, sizeof(float) * FEATURE_DIM);
    calCount[g]++;
    calWaiting = false;
    advanceCalRep();
}

static void calOnNod(float pitchAmpDeg) {
    if (!calWaiting || sysMode != MODE_CAL) return;
    uint8_t g = CAL_ORDER[calGesture];
    if (g != G_NOD || calCount[g] >= CAL_REPS) return;
    memset(calSamples[g][calCount[g]], 0, sizeof(float) * FEATURE_DIM);
    calSamples[g][calCount[g]][0] = pitchAmpDeg;
    calCount[g]++;
    calWaiting = false;
    advanceCalRep();
}

static void calOnEmit(int id, float pitchAmp) {
    if (sysMode == MODE_CAL && calWaiting && id == G_NOD)
        calOnNod(pitchAmp);
}

static void finishCalibration() {
    calPhase = CAL_DONE;
    sysMode = MODE_NORMAL;
    computeCalibration();
    saveCalibration();
    printCalSummary();
    Serial.println("[CAL] Done — saved to flash.");
}

static void computeCalibration() {
    for (uint8_t g = 0; g < 10; g++) {
        if (!calCount[g]) { t1Amp[g] = 0; continue; }
        float acc = 0;
        for (uint8_t r = 0; r < calCount[g]; r++) acc += absf_(calSamples[g][r][0]);
        t1Amp[g] = acc / calCount[g];
    }
    if (calCount[G_LEFT_CLENCH]) {
        float s = 0;
        for (uint8_t r = 0; r < calCount[G_LEFT_CLENCH]; r++) s += calSamples[G_LEFT_CLENCH][r][2];
        t1Pol[0] = s >= 0 ? +1 : -1;
    }
    if (calCount[G_RIGHT_CLENCH]) {
        float s = 0;
        for (uint8_t r = 0; r < calCount[G_RIGHT_CLENCH]; r++) s += calSamples[G_RIGHT_CLENCH][r][2];
        t1Pol[1] = s >= 0 ? +1 : -1;
    }
    float sum[FEATURE_DIM] = {0}, sq[FEATURE_DIM] = {0};
    uint32_t nt = 0;
    for (uint8_t g = 0; g < N_T2; g++) {
        uint8_t gid = T2_GESTURES[g];
        for (uint8_t r = 0; r < calCount[gid]; r++)
            for (uint8_t j = 0; j < FEATURE_DIM; j++) {
                sum[j] += calSamples[gid][r][j];
                sq[j]  += calSamples[gid][r][j] * calSamples[gid][r][j];
            }
        nt += calCount[gid];
    }
    if (nt > 0) {
        for (uint8_t j = 0; j < FEATURE_DIM; j++) {
            t2Mean[j] = sum[j] / nt;
            float v = sq[j] / nt - t2Mean[j] * t2Mean[j];
            t2Std[j]  = v > 0 ? sqrtf(v) : 1.0f;
        }
    }
    for (uint8_t g = 0; g < N_T2; g++) {
        uint8_t gid = T2_GESTURES[g];
        for (uint8_t j = 0; j < FEATURE_DIM; j++) t2Centroid[g][j] = 0;
        if (!calCount[gid]) continue;
        for (uint8_t r = 0; r < calCount[gid]; r++)
            for (uint8_t j = 0; j < FEATURE_DIM; j++)
                t2Centroid[g][j] += calSamples[gid][r][j];
        for (uint8_t j = 0; j < FEATURE_DIM; j++)
            t2Centroid[g][j] /= calCount[gid];
        float tmp[FEATURE_DIM];
        normalizeFeat(t2Centroid[g], tmp);
        memcpy(t2Centroid[g], tmp, sizeof(tmp));
    }
    t2Valid = nt >= N_T2;
    t1Valid = true;
}

static void saveCalibration() {
    prefs.begin("omniband", false);
    String s = "";
    for (uint8_t g = 0; g < 10; g++) { s += String(t1Amp[g], 1); s += ','; }
    s += String(t1Pol[0]); s += ',';
    s += String(t1Pol[1]); s += ',';
    s += String(restPitchDeg, 2);
    prefs.putString("t1", s);

    s = "";
    for (uint8_t j = 0; j < FEATURE_DIM; j++) { s += String(t2Mean[j], 2); s += ','; }
    for (uint8_t j = 0; j < FEATURE_DIM; j++) { s += String(t2Std[j], 2);  s += ','; }
    prefs.putString("t2n", s);

    s = "";
    for (uint8_t g = 0; g < N_T2; g++)
        for (uint8_t j = 0; j < FEATURE_DIM; j++) {
            s += String(t2Centroid[g][j], 3); s += ',';
        }
    prefs.putString("t2c", s);
    prefs.putBool("t2v", t2Valid);
    prefs.end();
}

static uint16_t parseFloats(const String &s, float *out, uint16_t maxN) {
    uint16_t n = 0; int idx = 0, len = s.length();
    while (n < maxN && idx <= len) {
        int comma = s.indexOf(',', idx);
        if (comma < 0) comma = len;
        out[n++] = s.substring(idx, comma).toFloat();
        idx = comma + 1;
        if (comma >= len) break;
    }
    return n;
}

static bool loadCalibration() {
    if (!prefs.begin("omniband", true)) return false;
    if (!prefs.isKey("t1")) { prefs.end(); return false; }

    float t1buf[13];
    if (parseFloats(prefs.getString("t1", ""), t1buf, 13) >= 13) {
        for (uint8_t g = 0; g < 10; g++) t1Amp[g] = t1buf[g];
        t1Pol[0] = (int8_t)t1buf[10];
        t1Pol[1] = (int8_t)t1buf[11];
        restPitchDeg = t1buf[12];
        restPitchValid = true;
    }
    float norm[FEATURE_DIM * 2];
    if (parseFloats(prefs.getString("t2n", ""), norm, FEATURE_DIM * 2) >= FEATURE_DIM * 2) {
        for (uint8_t j = 0; j < FEATURE_DIM; j++) {
            t2Mean[j] = norm[j];
            t2Std[j]  = norm[FEATURE_DIM + j];
        }
    }
    float cen[N_T2 * FEATURE_DIM];
    if (parseFloats(prefs.getString("t2c", ""), cen, N_T2 * FEATURE_DIM) >= N_T2 * FEATURE_DIM) {
        for (uint8_t g = 0; g < N_T2; g++)
            for (uint8_t j = 0; j < FEATURE_DIM; j++)
                t2Centroid[g][j] = cen[g * FEATURE_DIM + j];
    }
    t2Valid = prefs.getBool("t2v", false);
    t1Valid = true;
    prefs.end();
    return true;
}

static void printCalSummary() {
    Serial.println("[CAL] ===== Calibration Summary =====");
    for (uint8_t g = 0; g < 10; g++) {
        Serial.print("  "); Serial.print(GESTURE_NAME[g]);
        Serial.print(": samples="); Serial.print(calCount[g]);
        Serial.print("  amp="); Serial.println(t1Amp[g], 0);
    }
}

static void calUpdate() {
    if (sysMode != MODE_CAL || calPhase == CAL_DONE) return;
    uint32_t t = nowMs();
    if (calPhase == CAL_REST) {
        if (t - calPhaseStart >= CAL_REST_MS) {
            calPhase = CAL_PROMPT;
            calPhaseStart = t;
            restPitchDeg = pitchAbsDeg;
            restPitchValid = true;
            promptNextCal();
        }
        return;
    }
    if (calWaiting && t - calPromptStart > CAL_REP_TIMEOUT_MS) {
        calWaiting = false;
        advanceCalRep();
    }
}

// ============================================================================
// 11. OLED UI
// ============================================================================
static void oledDraw() {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);

    if (sysMode == MODE_CAL) {
        display.println("== CALIBRATION ==");
        if (calPhase == CAL_REST) {
            display.print("REST: stay still");
        } else {
            uint8_t g = CAL_ORDER[calGesture];
            display.print("Do: "); display.println(GESTURE_NAME[g]);
        }
    } else {
        display.println("== OMNIBAND ==");
        display.print("Last: ");
        if (lastGesture >= 0) display.println(GESTURE_NAME[lastGesture]);
        else display.println("-");
        display.print("BLE: "); display.println(deviceConnected ? "CONNECTED" : "WAITING");
        display.print("Cursor dx,dy: ");
        display.print(cursorDx, 1); display.print(","); display.println(cursorDy, 1);
    }
    display.display();
}

// ============================================================================
// 12. EMIT & SETUP & LOOP
// ============================================================================
static void emitGesture(int id, float conf) {
    lastGesture = id;
    lastConfidence = conf;
    lastFireMs = nowMs();
    tier1ClaimUntilMs = nowMs() + WIN_QUIET_US / 1000UL + 100;

    Serial.print("[GESTURE FIRED] ");
    Serial.print(id >= 0 && id < 10 ? GESTURE_NAME[id] : "?");
    Serial.print(" conf="); Serial.println(conf, 2);

    if (sysMode == MODE_CAL) { calOnEmit(id, calPitchMax); return; }

    // Dispatch action to BLE & PC!
    onGesture(id, conf);
}

void setup() {
    Serial.begin(115200);
    delay(300);

    Serial.println("\n==============================================");
    Serial.println("  OmniBand Multi-Gesture Wearable Controller  ");
    Serial.println("  Pins: CH1=32, CH2=34, CH3=33, CH4=26        ");
    Serial.println("  Bluetooth Device: 'Omniband'                ");
    Serial.println("==============================================");

    pinMode(PIN_BTN, INPUT_PULLUP);
    analogReadResolution(ADC_BITS);
#if ESP_ARDUINO_VERSION >= 30000
    for (uint8_t i = 0; i < 4; i++) analogSetPinAttenuation(PIN_PIEZO[i], ADC_11db);
#else
    analogSetPinAttenuation(ADC_11db);
#endif

    Wire.begin(PIN_SDA, PIN_SCL);
    Wire.setClock(400000);

    if (display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
        display.clearDisplay();
        display.setTextSize(1);
        display.setTextColor(SSD1306_WHITE);
        display.setCursor(0, 0);
        display.println("OmniBand booting...");
        display.display();
    }

    // Initialize MPU6050
    imuOk = mpu.begin(I2C_ADDR_MPU, &Wire);
    if (imuOk) {
        mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
        mpu.setGyroRange(MPU6050_RANGE_500_DEG);
        Serial.println("[IMU] MPU6050 ok! Calibrating gyro zero-bias...");

        float sumX = 0, sumZ = 0;
        for (int i = 0; i < 40; i++) {
            sensors_event_t a, g, temp;
            mpu.getEvent(&a, &g, &temp);
            sumX += g.gyro.x;
            sumZ += g.gyro.z;
            delay(10);
        }
        gyroBiasX = sumX / 40.0f;
        gyroBiasZ = sumZ / 40.0f;
        Serial.println("[IMU] Gyro bias locked. Hardware mouse cursor active!");
    } else {
        Serial.println("[IMU] MPU6050 NOT detected.");
    }

    // Start BLE
    setupBLE();

    // Baseline calibration
    for (uint8_t i = 0; i < 4; i++) {
        ch[i].baseline = (float)analogRead(PIN_PIEZO[i]);
        ch[i].noise    = 8.0f;
        spikes[i].head = spikes[i].count = 0;
        spikeActive[i] = false;
    }

    randomSeed(esp_random());
    if (!loadCalibration()) {
        for (uint8_t g = 0; g < 10; g++) t1Amp[g] = 220.0f;
        t1Pol[0] = t1Pol[1] = 0;
        t1Valid = true;
    }
    Serial.println("OmniBand ready! Bluetooth Name: 'Omniband'");
}

void loop() {
    uint32_t nowUs = micros();
    uint32_t tMs   = millis();

    // 1. 1 kHz Piezo ADC Sampling
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

    detectTier1(nowUs);
    calUpdate();

    // 3. Hold button for 3s to trigger calibration
    bool down = BTN_ACTIVE_LOW ? (digitalRead(PIN_BTN) == LOW) : (digitalRead(PIN_BTN) == HIGH);
    if (down && !btnWasDown) lastBtnMs = tMs;
    if (down && tMs - lastBtnMs >= BTN_HOLD_MS && sysMode == MODE_NORMAL) {
        startCalibration();
        btnWasDown = true;
    }
    if (!down) btnWasDown = false;

    // 4. OLED UI Update
    if (tMs - lastOledMs >= OLED_PERIOD_MS) {
        lastOledMs = tMs;
        oledDraw();
    }

    // 5. BLE Reconnect Advertising
    if (!deviceConnected && oldDeviceConnected) {
        delay(300);
        pServer->startAdvertising();
        oldDeviceConnected = deviceConnected;
    }
    if (deviceConnected && !oldDeviceConnected) {
        oldDeviceConnected = deviceConnected;
    }
}
