#pragma once
// wifi_transport.h - Ultra-Low-Latency Wi-Fi UDP Streaming for OmniBand V2 & Unbound Desktop
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebServer.h>

#ifndef MOUSE_BTN_LEFT
constexpr uint8_t MOUSE_BTN_LEFT   = 0x01;
constexpr uint8_t MOUSE_BTN_RIGHT  = 0x02;
constexpr uint8_t MOUSE_BTN_MIDDLE = 0x04;
#endif

class OmniBandWifiServer {
public:
    void begin();
    void poll();

    // Telemetry & somatic frames
    void send_telemetry(int32_t ch1, int32_t ch2, int32_t ch3, int32_t ch4,
                        uint32_t seq, float dx = 0.0f, float dy = 0.0f,
                        float pitch = 0.0f, float roll = 0.0f,
                        const char* gesture = "NONE",
                        const char* click_type = "none", int scroll_val = 0);

    // Mouse actions & gestures
    void mouse_click(uint8_t button);
    void mouse_scroll(int8_t wheel);
    void mouse_toggle_drag();
    void notify_gesture(const char* gesture);
    void notify_line(const String& line);

    bool is_ap_active() const { return ap_active_; }
    uint32_t packet_count() const { return pkt_count_; }

private:
    WiFiUDP udp_;
    WebServer server_{80};
    bool ap_active_ = false;
    uint32_t pkt_count_ = 0;
    IPAddress host_unicast_ip_{0, 0, 0, 0};
    bool has_host_ip_ = false;
    portMUX_TYPE mux_ = portMUX_INITIALIZER_UNLOCKED;

    String pending_click_ = "none";
    int pending_scroll_ = 0;
    bool drag_locked_ = false;
};

extern OmniBandWifiServer g_wifi;
