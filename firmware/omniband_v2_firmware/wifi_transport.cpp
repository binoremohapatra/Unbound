// wifi_transport.cpp - Ultra-Low-Latency Wi-Fi UDP Streaming for OmniBand V2 & Unbound Desktop
#include "wifi_transport.h"
#include "hw_config.h"

OmniBandWifiServer g_wifi;

void OmniBandWifiServer::begin() {
    Serial.printf("\n[WIFI] Initializing OmniBand Dual Wi-Fi (SoftAP + STA)...\n");

    // 1. Dual Wi-Fi Mode (AP + STA)
    WiFi.mode(WIFI_AP_STA);

    // 2. Configure dedicated Access Point
    IPAddress apIP(192, 168, 4, 1);
    IPAddress netMsk(255, 255, 255, 0);
    WiFi.softAPConfig(apIP, apIP, netMsk);

    bool ap_ok = WiFi.softAP(omniband::hw::WIFI_AP_SSID, omniband::hw::WIFI_AP_PASS);
    if (ap_ok) {
        ap_active_ = true;
        Serial.printf("[WIFI] SoftAP Online! SSID: '%s' | Pass: '%s' | IP: %s\n",
                      omniband::hw::WIFI_AP_SSID, omniband::hw::WIFI_AP_PASS,
                      WiFi.softAPIP().toString().c_str());
    } else {
        Serial.println("[WIFI] ERROR: Failed to start SoftAP!");
    }

    // 3. Bind UDP Port 4210 for zero-latency bidirectional streaming
    udp_.begin(omniband::hw::WIFI_UDP_PORT);
    Serial.printf("[WIFI] UDP Listening & Broadcasting on Port %u\n", omniband::hw::WIFI_UDP_PORT);

    // 4. Lightweight HTTP Status Endpoint for Windows firewall fallback
    server_.on("/status", HTTP_GET, [this]() {
        char json[256];
        snprintf(json, sizeof(json),
                 "{\"status\":\"ok\",\"device\":\"OmniBand\",\"ap_ip\":\"192.168.4.1\",\"port\":%u,\"packets\":%lu}",
                 omniband::hw::WIFI_UDP_PORT, (unsigned long)pkt_count_);
        server_.send(200, "application/json", json);
    });

    server_.on("/ping", HTTP_GET, [this]() {
        host_unicast_ip_ = server_.client().remoteIP();
        has_host_ip_ = true;
        server_.send(200, "application/json", "{\"pong\":true}");
    });

    server_.begin();
    Serial.println("[WIFI] HTTP Server online at http://192.168.4.1/status");
}

void OmniBandWifiServer::poll() {
    // 1. Process HTTP requests
    server_.handleClient();

    // 2. Process incoming UDP datagrams (e.g. host discovery ping)
    int packetSize = udp_.parsePacket();
    if (packetSize > 0) {
        IPAddress remote = udp_.remoteIP();
        if (remote[0] != 0) {
            host_unicast_ip_ = remote;
            has_host_ip_ = true;
        }

        char buf[64];
        int len = udp_.read(buf, sizeof(buf) - 1);
        if (len > 0) {
            buf[len] = '\0';
            if (strstr(buf, "PING") != nullptr || strstr(buf, "HELLO") != nullptr) {
                // Reply with immediate PONG unicast datagram
                udp_.beginPacket(remote, omniband::hw::WIFI_UDP_PORT);
                udp_.write((const uint8_t*)"PONG\n", 5);
                udp_.endPacket();
            }
        }
    }
}

void OmniBandWifiServer::send_telemetry(int32_t ch1, int32_t ch2, int32_t ch3, int32_t ch4,
                                        uint32_t seq, float dx, float dy,
                                        float pitch, float roll,
                                        const char* gesture,
                                        const char* click_type, int scroll_val) {
    char buf[200];
    const char* gst = (gesture && strlen(gesture) > 0) ? gesture : "NONE";
    const char* clk = (click_type && strlen(click_type) > 0) ? click_type : pending_click_.c_str();
    int scr = (scroll_val != 0) ? scroll_val : pending_scroll_;

    snprintf(buf, sizeof(buf),
             "{\"ch\":[%ld,%ld,%ld,%ld],\"dx\":%.2f,\"dy\":%.2f,\"pitch\":%.2f,\"roll\":%.2f,\"gesture\":\"%s\",\"click\":\"%s\",\"scroll\":%d,\"seq\":%lu}\n",
             (long)ch1, (long)ch2, (long)ch3, (long)ch4, dx, dy, pitch, roll, gst, clk, scr, (unsigned long)seq);

    size_t len = strlen(buf);

    // 1. Global subnet broadcast
    udp_.beginPacket(IPAddress(255, 255, 255, 255), omniband::hw::WIFI_UDP_PORT);
    udp_.write((const uint8_t*)buf, len);
    udp_.endPacket();

    // 2. SoftAP subnet broadcast
    udp_.beginPacket(IPAddress(192, 168, 4, 255), omniband::hw::WIFI_UDP_PORT);
    udp_.write((const uint8_t*)buf, len);
    udp_.endPacket();

    // 3. Direct unicast if host IP has pinged us
    if (has_host_ip_) {
        udp_.beginPacket(host_unicast_ip_, omniband::hw::WIFI_UDP_PORT);
        udp_.write((const uint8_t*)buf, len);
        udp_.endPacket();
    }

    pkt_count_++;
    pending_click_ = "none";
    pending_scroll_ = 0;
}

void OmniBandWifiServer::mouse_click(uint8_t button) {
    const char* c = "click";
    if (button == MOUSE_BTN_LEFT) c = "left";
    else if (button == MOUSE_BTN_RIGHT) c = "right";
    else if (button == MOUSE_BTN_MIDDLE) c = "middle";

    pending_click_ = c;
    char buf[64];
    snprintf(buf, sizeof(buf), "{\"click\":\"%s\",\"evt\":\"click\"}\n", c);
    notify_line(buf);
}

void OmniBandWifiServer::mouse_scroll(int8_t wheel) {
    pending_scroll_ = wheel;
    char buf[64];
    snprintf(buf, sizeof(buf), "{\"scroll\":%d,\"evt\":\"scroll\"}\n", wheel);
    notify_line(buf);
}

void OmniBandWifiServer::mouse_toggle_drag() {
    drag_locked_ = !drag_locked_;
    const char* d = drag_locked_ ? "drag_on" : "drag_off";
    pending_click_ = d;
    char buf[64];
    snprintf(buf, sizeof(buf), "{\"drag\":\"%s\",\"evt\":\"drag\"}\n", d);
    notify_line(buf);
}

void OmniBandWifiServer::notify_gesture(const char* gesture) {
    char buf[80];
    snprintf(buf, sizeof(buf), "{\"gesture\":\"%s\",\"evt\":\"gesture\"}\n", gesture);
    notify_line(buf);
}

void OmniBandWifiServer::notify_line(const String& line) {
    size_t len = line.length();
    const uint8_t* p = (const uint8_t*)line.c_str();

    udp_.beginPacket(IPAddress(255, 255, 255, 255), omniband::hw::WIFI_UDP_PORT);
    udp_.write(p, len);
    udp_.endPacket();

    udp_.beginPacket(IPAddress(192, 168, 4, 255), omniband::hw::WIFI_UDP_PORT);
    udp_.write(p, len);
    udp_.endPacket();

    if (has_host_ip_) {
        udp_.beginPacket(host_unicast_ip_, omniband::hw::WIFI_UDP_PORT);
        udp_.write(p, len);
        udp_.endPacket();
    }
}
