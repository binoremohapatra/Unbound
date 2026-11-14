// ble_transport.cpp - Native BLE HID Mouse + GATT Telemetry Server for OmniBand V2
#include "ble_transport.h"

OmniBandBleServer g_ble;

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

class BleSecurity : public BLESecurityCallbacks {
    uint32_t onPassKeyRequest() { return 0; }
    void onPassKeyNotify(uint32_t pass_key) {}
    bool onConfirmPIN(uint32_t pass_key) { return true; }
    bool onSecurityRequest() { return true; }
    void onAuthenticationComplete(esp_ble_auth_cmpl_t cmpl) {}
};

void OmniBandBleServer::begin() {
    Serial.println("[BLE] Starting Native Bluetooth HID Mouse 'OmniBand Mouse'...");
    BLEDevice::init("OmniBand Mouse");
    BLEDevice::setSecurityCallbacks(new BleSecurity());

    // 1. Create Server
    pServer_ = BLEDevice::createServer();
    pServer_->setCallbacks(this);

    // 2. Create HID Device
    pHid_ = new BLEHIDDevice(pServer_);
    pInputMouse_ = pHid_->inputReport(1); // Report ID 1

    pHid_->manufacturer()->setValue("OmniBand");
    pHid_->pnp(0x02, 0x045e, 0x0040, 0x0110);
    pHid_->hidInfo(0x00, 0x02);
    pHid_->reportMap((uint8_t*)hidReportMap, sizeof(hidReportMap));
    pHid_->startServices();

    // 3. Create Custom GATT Characteristic for Telemetry/Scope
    BLEService* pService = pServer_->createService("4fafc201-1fb5-459e-8fcc-c5c9c331914b");
    pChar_ = pService->createCharacteristic(
        "beb5483e-36e1-4688-b7f5-ea07361b26a6",
        BLECharacteristic::PROPERTY_READ   |
        BLECharacteristic::PROPERTY_WRITE  |
        BLECharacteristic::PROPERTY_NOTIFY
    );
    pChar_->setCallbacks(this);
    pChar_->addDescriptor(new BLE2902());
    pChar_->setValue("OmniBand HID Mouse Ready");
    pService->start();

    // 4. Advertising Configuration
    BLESecurity* pSecurity = new BLESecurity();
    pSecurity->setAuthenticationMode(ESP_LE_AUTH_BOND);

    BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
    pAdvertising->setAppearance(0x03C2); // HID Mouse icon in Windows Bluetooth!
    pAdvertising->addServiceUUID(pHid_->hidService()->getUUID());
    pAdvertising->addServiceUUID("4fafc201-1fb5-459e-8fcc-c5c9c331914b");
    pAdvertising->setScanResponse(true);
    pAdvertising->setMinPreferred(0x06);
    pAdvertising->setMinPreferred(0x12);
    pAdvertising->start();

    Serial.println("[BLE] Native Bluetooth Mouse Advertising ON! (Name: 'OmniBand Mouse')");
}

void OmniBandBleServer::onConnect(BLEServer* pServer) {
    (void)pServer;
    connected_ = true;
    button_mask_ = 0;
    Serial.println("\n>>> [BLE] PAIRED & CONNECTED DIRECTLY AS NATIVE WINDOWS MOUSE! <<<");
}

void OmniBandBleServer::onDisconnect(BLEServer* pServer) {
    (void)pServer;
    connected_ = false;
    button_mask_ = 0;
    Serial.println("\n[BLE] Disconnected from Windows. Re-advertising...");
    BLEDevice::startAdvertising();
}

void OmniBandBleServer::onWrite(BLECharacteristic* pCharacteristic) {
    String val = pCharacteristic->getValue().c_str();
    if (val.length() > 0) {
        portENTER_CRITICAL(&mux_);
        rx_buf_ += val;
        has_rx_ = true;
        portEXIT_CRITICAL(&mux_);
    }
}

String OmniBandBleServer::pop_rx() {
    String res = "";
    portENTER_CRITICAL(&mux_);
    if (has_rx_) {
        res = rx_buf_;
        rx_buf_ = "";
        has_rx_ = false;
    }
    portEXIT_CRITICAL(&mux_);
    return res;
}

// -----------------------------------------------------------------------------
// NATIVE MOUSE REPORTS (Direct to OS Kernel)
// -----------------------------------------------------------------------------
void OmniBandBleServer::mouse_move(int8_t dx, int8_t dy, int8_t wheel) {
    if (!connected_ || pInputMouse_ == nullptr) return;
    uint8_t report[4] = {button_mask_, (uint8_t)dx, (uint8_t)dy, (uint8_t)wheel};
    pInputMouse_->setValue(report, 4);
    pInputMouse_->notify();
}

void OmniBandBleServer::mouse_click(uint8_t button) {
    if (!connected_ || pInputMouse_ == nullptr) return;
    uint8_t reportDown[4] = {(uint8_t)(button_mask_ | button), 0, 0, 0};
    pInputMouse_->setValue(reportDown, 4);
    pInputMouse_->notify();
    delay(25);
    uint8_t reportUp[4] = {button_mask_, 0, 0, 0};
    pInputMouse_->setValue(reportUp, 4);
    pInputMouse_->notify();
}

void OmniBandBleServer::mouse_press(uint8_t button) {
    if (!connected_ || pInputMouse_ == nullptr) return;
    button_mask_ |= button;
    uint8_t report[4] = {button_mask_, 0, 0, 0};
    pInputMouse_->setValue(report, 4);
    pInputMouse_->notify();
}

void OmniBandBleServer::mouse_release(uint8_t button) {
    if (!connected_ || pInputMouse_ == nullptr) return;
    button_mask_ &= ~button;
    uint8_t report[4] = {button_mask_, 0, 0, 0};
    pInputMouse_->setValue(report, 4);
    pInputMouse_->notify();
}

void OmniBandBleServer::mouse_toggle_drag() {
    if (!connected_ || pInputMouse_ == nullptr) return;
    button_mask_ ^= MOUSE_BTN_LEFT;
    uint8_t report[4] = {button_mask_, 0, 0, 0};
    pInputMouse_->setValue(report, 4);
    pInputMouse_->notify();
    Serial.println(button_mask_ & MOUSE_BTN_LEFT ? 
        "⚡ [BLE HID] DRAG LOCKED (Left Button HELD)" : 
        "⚡ [BLE HID] DRAG RELEASED");
}

void OmniBandBleServer::mouse_scroll(int8_t wheel) {
    if (!connected_ || pInputMouse_ == nullptr) return;
    uint8_t report[4] = {button_mask_, 0, 0, (uint8_t)wheel};
    pInputMouse_->setValue(report, 4);
    pInputMouse_->notify();
}

// -----------------------------------------------------------------------------
// TELEMETRY & COMMANDS
// -----------------------------------------------------------------------------
void OmniBandBleServer::notify_frame(int32_t ch1, int32_t ch2, int32_t ch3, int32_t ch4, uint32_t seq, float dx, float dy) {
    if (!connected_ || pChar_ == nullptr) return;
    char buf[96];
    snprintf(buf, sizeof(buf), "S,%ld,%ld,%ld,%ld,%lu,%.2f,%.2f\n",
             (long)ch1, (long)ch2, (long)ch3, (long)ch4, (unsigned long)seq, dx, dy);
    pChar_->setValue((uint8_t*)buf, strlen(buf));
    pChar_->notify();
}

void OmniBandBleServer::notify_click() {
    mouse_click(MOUSE_BTN_LEFT);
    if (connected_ && pChar_ != nullptr) {
        pChar_->setValue((uint8_t*)"CLICK\n", 6);
        pChar_->notify();
    }
}

void OmniBandBleServer::notify_line(const String& line) {
    if (!connected_ || pChar_ == nullptr) return;
    pChar_->setValue((uint8_t*)line.c_str(), line.length());
    pChar_->notify();
}
