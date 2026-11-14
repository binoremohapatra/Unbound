// link.h - NDJSON transport: USB Serial + Classic BT SPP, one global seq,
// every line duplicated to both. See protocol/PROTOCOL.md.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include "omniband_core.h"
#include "shared_state.h"

// ---- host -> device command (decoded; dsp-state commands forwarded) ----
struct HostCmd {
    char id[16];
    char name[16];
    int32_t i0 = 0, i1 = 0;
    double d0 = 0.0;
    char s0[24] = "";
    bool needs_dsp = false;   // true -> forward to DSP command queue
};

// ---- device -> host reply for dsp-handled commands ----
struct HostReply {
    char id[16];
    bool ok;
    char err[32];
};

class Link {
public:
    void begin(uint32_t baud);
    void set_baud(uint32_t baud);       // re-begin both transports
    // call from loop()/link task: RX poll + periodic TX (tele/scope/compat)
    void poll();
    // ---- TX helpers (any task; internally locked) ----
    void send_raw(const String& line);
    void send_hello();
    void send_ack(const char* id, bool ok, const char* err = "");
    void send_pong(const char* id, int64_t th);
    void send_diag();
    void send_cal(const char* step, const float* pk, int n, float np99,
                  float thr, float snr);
    void send_evt(const omniband::OutEvent& e);
    void send_ges(const omniband::OutEvent& e);
    void send_mode_set(int mode);
    // events drained from the DSP queue by the caller task
    void flush_events(omniband::EventQueue& q);
    void send_tele(const SharedState& st);
    void send_ble_somatic(int32_t ch1, int32_t ch2, int32_t ch3, int32_t ch4, float dx = 0.0f, float dy = 0.0f);
    void send_ble_click();
    uint32_t rx_count() const { return rx_lines_; }
    uint32_t drop_count() const { return drop_lines_; }
    uint32_t cmd_rate_limited() const { return rate_limited_; }
private:
    void handle_line(const String& line, const char* src);
    void tx(const String& line);
    String ser_buf_, bt_buf_;
    uint32_t seq_ = 0;
    uint32_t baud_ = 115200;
    uint32_t rx_lines_ = 0, drop_lines_ = 0, rate_limited_ = 0;
    int64_t last_cmd_ms_ = -100000;
    bool hello_sent_ = false;
    // scope + compat_v1 opt-in state
    bool scope_on_ = false;
    int64_t last_scope_ms_ = 0;
    int64_t last_tele_ms_ = 0;
};

extern Link g_link;

// ---- inter-task queues (created in setup, consumed by dsp/link tasks) ----
#include <freertos/queue.h>
extern QueueHandle_t g_cmd_queue;    // HostCmd  dsp <- link
extern QueueHandle_t g_reply_queue;  // HostReply link <- dsp
extern QueueHandle_t g_evt_queue;    // omniband::OutEvent  link <- dsp
