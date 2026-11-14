// ble_transport.cpp - BLE GATT Server implementation for OmniBand V2
#include "ble_transport.h"

OmniBandBleServer g_ble;

void OmniBandBleServer::begin() {
    // 1. Create the BLE Device (exact same implementation as working test)
    BLEDevice::init(OMNIBAND_BLE_DEVICE_NAME);

    // 2. Create the BLE Server
    pServer_ = BLEDevice::createServer();
    pServer_->setCallbacks(this);

    // 3. Create the BLE Service
    BLEService* pService = pServer_->createService(OMNIBAND_SERVICE_UUID);

    // 4. Create Characteristic
    pChar_ = pService->createCharacteristic(
        OMNIBAND_CHAR_UUID,
        BLECharacteristic::PROPERTY_READ   |
        BLECharacteristic::PROPERTY_WRITE  |
        BLECharacteristic::PROPERTY_NOTIFY
    );

    pChar_->setCallbacks(this);
    pChar_->addDescriptor(new BLE2902());
    pChar_->setValue("Hello from OmniBand ESP32!");

    // 5. Start the service
    pService->start();

    // 6. Start advertising (exact same sequence as working test)
    BLEAdvertising* pAdvertising = BLEDevice::getAdvertising();
    pAdvertising->addServiceUUID(OMNIBAND_SERVICE_UUID);
    pAdvertising->setScanResponse(true);
    pAdvertising->setMinPreferred(0x06); // functions that help with iPhone connections issue
    pAdvertising->setMinPreferred(0x12);
    BLEDevice::startAdvertising();
    Serial.println("[BLE] BLE advertised successfully. Looking for devices...");
}

void OmniBandBleServer::onConnect(BLEServer* pServer) {
    (void)pServer;
    connected_ = true;
}

void OmniBandBleServer::onDisconnect(BLEServer* pServer) {
    (void)pServer;
    connected_ = false;
    // Restart advertising so client can reconnect seamlessly
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
