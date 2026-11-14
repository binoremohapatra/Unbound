// ble_transport.cpp - Ultra-Fast GATT Telemetry Server for OmniBand V2 & Unbound Desktop
#include "ble_transport.h"
#include "hw_config.h"
#include <esp_system.h>
#if __has_include(<esp_mac.h>)
#include <esp_mac.h>
#endif

OmniBandBleServer g_ble;

static const char* SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b";
static const char* CHAR_UUID    = "beb5483e-36e1-4688-b7f5-ea07361b26a6";

void OmniBandBleServer::begin() {
    Serial.printf("[BLE] Initializing OmniBand BLE Server '%s'...\n", omniband::hw::BT_DEVICE_NAME);

    // Increment MAC address by +1 to guarantee Windows sees a fresh unbonded device,
    // bypassing any stale bonding keys cached in Windows Registry.
    uint8_t baseMac[6];
    if (esp_read_mac(baseMac, ESP_MAC_BT) == ESP_OK) {
        baseMac[5] = (baseMac[5] == 0xFF) ? 0x01 : (baseMac[5] + 1);
        esp_base_mac_addr_set(baseMac);
    }

    BLEDevice::init(omniband::hw::BT_DEVICE_NAME);

    // 1. Create GATT Server
    pServer_ = BLEDevice::createServer();
    pServer_->setCallbacks(this);

    // 2. Create Custom GATT Service & Characteristic for Unbound Desktop App
    BLEService* pService = pServer_->createService(SERVICE_UUID);
    pChar_ = pService->createCharacteristic(
        CHAR_UUID,
        BLECharacteristic::PROPERTY_READ   |
        BLECharacteristic::PROPERTY_WRITE  |
        BLECharacteristic::PROPERTY_NOTIFY
    );
    pChar_->setCallbacks(this);
    pChar_->addDescriptor(new BLE2902());
    pChar_->setValue("OmniBand Ready");
    pService->start();

    // 3. Clean Standard BLE Advertising (Strictly splits adv and scan response data to never exceed 31 bytes)
    BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();

    BLEAdvertisementData advData;
    advData.setFlags(0x06); // BR_EDR_NOT_SUPPORTED | LE_GENERAL_DISC_MODE
    advData.setName(omniband::hw::BT_DEVICE_NAME);
    pAdvertising->setAdvertisementData(advData);

    BLEAdvertisementData scanResponseData;
    scanResponseData.setCompleteServices(BLEUUID(SERVICE_UUID));
    pAdvertising->setScanResponseData(scanResponseData);

    pAdvertising->setScanResponse(true);
    pAdvertising->setMinPreferred(0x06); // 7.5ms fast connection interval
    pAdvertising->setMaxPreferred(0x12); // 22.5ms max interval
    BLEDevice::startAdvertising();

    Serial.printf("[BLE] Advertising started successfully for '%s'!\n", omniband::hw::BT_DEVICE_NAME);
}

void OmniBandBleServer::onConnect(BLEServer* pServer) {
    (void)pServer;
    connected_ = true;
    button_mask_ = 0;
    Serial.println("\n>>> [BLE] OmniBand CONNECTED to Unbound Desktop App! <<<");
}

void OmniBandBleServer::onDisconnect(BLEServer* pServer) {
    (void)pServer;
    connected_ = false;
    button_mask_ = 0;
    Serial.println("\n[BLE] OmniBand Disconnected. Restarting advertising...");
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
// MOUSE & GESTURE ACTIONS (Dispatched to Unbound Application)
// -----------------------------------------------------------------------------
void OmniBandBleServer::mouse_move(int8_t dx, int8_t dy, int8_t wheel) {
    (void)dx; (void)dy; (void)wheel;
    // Handled in streaming telemetry notify_frame with sub-pixel float precision
}

void OmniBandBleServer::mouse_click(uint8_t button) {
    if (!connected_ || pChar_ == nullptr) return;
    if (button == MOUSE_BTN_LEFT) {
        notify_line("CLICK\n");
    } else if (button == MOUSE_BTN_RIGHT) {
        notify_line("RIGHT\n");
    } else if (button == MOUSE_BTN_MIDDLE) {
        notify_line("MIDDLE\n");
    }
}

void OmniBandBleServer::mouse_press(uint8_t button) {
    button_mask_ |= button;
    if (button & MOUSE_BTN_LEFT) notify_line("DRAG_ON\n");
}

void OmniBandBleServer::mouse_release(uint8_t button) {
    button_mask_ &= ~button;
    if (!(button_mask_ & MOUSE_BTN_LEFT)) notify_line("DRAG_OFF\n");
}

void OmniBandBleServer::mouse_toggle_drag() {
    button_mask_ ^= MOUSE_BTN_LEFT;
    if (button_mask_ & MOUSE_BTN_LEFT) {
        notify_line("DRAG_ON\n");
        Serial.println("⚡ [BLE] DRAG LOCKED");
    } else {
        notify_line("DRAG_OFF\n");
        Serial.println("⚡ [BLE] DRAG RELEASED");
    }
}

void OmniBandBleServer::mouse_scroll(int8_t wheel) {
    if (!connected_ || pChar_ == nullptr) return;
    char buf[24];
    snprintf(buf, sizeof(buf), "SCROLL,%d\n", wheel);
    notify_line(buf);
}

// -----------------------------------------------------------------------------
// TELEMETRY & NOTIFICATIONS
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
    if (!connected_ || pChar_ == nullptr) return;
    pChar_->setValue((uint8_t*)"CLICK\n", 6);
    pChar_->notify();
}

void OmniBandBleServer::notify_line(const String& line) {
    if (!connected_ || pChar_ == nullptr) return;
    pChar_->setValue((uint8_t*)line.c_str(), line.length());
    pChar_->notify();
}
