// ble_transport.h - Native Bluetooth HID Mouse + GATT Server for OmniBand V2
#pragma once
#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <BLEHIDDevice.h>
#include <BLECharacteristic.h>

#define MOUSE_BTN_LEFT   0x01
#define MOUSE_BTN_RIGHT  0x02
#define MOUSE_BTN_MIDDLE 0x04

class OmniBandBleServer : public BLEServerCallbacks, public BLECharacteristicCallbacks {
public:
    void begin();
    bool is_connected() const { return connected_; }

    // Native Mouse Actions (sent directly to Windows Kernel — zero PC software needed!)
    void mouse_move(int8_t dx, int8_t dy, int8_t wheel = 0);
    void mouse_click(uint8_t button);
    void mouse_press(uint8_t button);
    void mouse_release(uint8_t button);
    void mouse_toggle_drag();
    void mouse_scroll(int8_t wheel);

    // Frame telemetry & commands
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
    BLEHIDDevice* pHid_ = nullptr;
    BLECharacteristic* pInputMouse_ = nullptr;
    BLECharacteristic* pChar_ = nullptr;
    volatile bool connected_ = false;
    volatile bool has_rx_ = false;
    uint8_t button_mask_ = 0;
    String rx_buf_;
    portMUX_TYPE mux_ = portMUX_INITIALIZER_UNLOCKED;
};

extern OmniBandBleServer g_ble;
