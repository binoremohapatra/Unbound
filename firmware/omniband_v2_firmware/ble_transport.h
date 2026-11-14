// ble_transport.h - BLE GATT Server for OmniBand V2 Headband
#pragma once
#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

#define OMNIBAND_BLE_DEVICE_NAME "Omniband"
#define OMNIBAND_SERVICE_UUID    "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define OMNIBAND_CHAR_UUID       "beb5483e-36e1-4688-b7f5-ea07361b26a6"

class OmniBandBleServer : public BLEServerCallbacks, public BLECharacteristicCallbacks {
public:
    void begin();
    bool is_connected() const { return connected_; }
    void notify_frame(int32_t ch1, int32_t ch2, int32_t ch3, int32_t ch4, uint32_t seq, float dx = 0.0f, float dy = 0.0f);
    void notify_click();
    void notify_line(const String& line);

    // Callbacks
    void onConnect(BLEServer* pServer) override;
    void onDisconnect(BLEServer* pServer) override;
    void onWrite(BLECharacteristic* pCharacteristic) override;

    bool has_rx() const { return has_rx_; }
    String pop_rx();

private:
    BLEServer* pServer_ = nullptr;
    BLECharacteristic* pChar_ = nullptr;
    volatile bool connected_ = false;
    volatile bool has_rx_ = false;
    String rx_buf_;
    portMUX_TYPE mux_ = portMUX_INITIALIZER_UNLOCKED;
};

extern OmniBandBleServer g_ble;
