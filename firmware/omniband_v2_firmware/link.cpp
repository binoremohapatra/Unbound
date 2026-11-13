// link.cpp - NDJSON link layer. See protocol/PROTOCOL.md for wire format.
#include "link.h"
#include "ble_transport.h"
#include <BluetoothSerial.h>
#include <Preferences.h>
#include <esp_system.h>
#include <esp_timer.h>
#include "constants_gen.h"
#include "config_store.h"
#include "oled_ui.h"

namespace k = omniband::constants;

Link g_link;
BluetoothSerial SerialBT;

static const int SER_BAUD_BOOT = k::kLinkBaudBoot;

// ------------------------------------------------------------------ helpers
static String js_escape(const char* s) {
    String o;
    for (const char* p = s; *p; ++p) {
        char c = *p;
        if (c == '"' || c == '\\') { o += '\\'; o += c; }
        else if ((uint8_t)c < 0x20) { o += ' '; }
        else o += c;
    }
    return o;
}

static const char* ges_name(int g) {
    if (g < 0 || g >= (int)(sizeof(k::kGestures) / sizeof(k::kGestures[0]))) return "?";
    return k::kGestures[g];
}
static const char* fsm_name(int st) {
    static const char* n[] = {"IDLE","PRESSED","GAP_WAIT","LONG_FIRED",
                              "PICKER","SOS_PENDING","NIGHT"};
    if (st < 0 || st >= 7) return "?";
    return n[st];
}

// ------------------------------------------------------------------ Link
void Link::begin(uint32_t baud) {
    baud_ = baud;
    Serial.begin(baud);
    Serial.setTimeout(0);
    g_ble.begin();
    if (SerialBT.begin("OmniBand-V2")) {
        SerialBT.setTimeout(0);
    }
    hello_sent_ = false;
}

void Link::send_ble_somatic(int32_t ch1, int32_t ch2, int32_t ch3, int32_t ch4) {
    g_ble.notify_frame(ch1, ch2, ch3, ch4, seq_++);
}

void Link::set_baud(uint32_t baud) {
    // brief settle so the host sees the switch; both transports move together
    delay(k::kLinkBaudSwitchDelayMs);
    begin(baud);
    send_hello();
}

void Link::tx(const String& line) {
    Serial.println(line);
    if (SerialBT.hasClient()) SerialBT.println(line);
    if (g_ble.is_connected()) g_ble.notify_line(line + "\n");
}

void Link::send_raw(const String& line) { tx(line); }

void Link::send_hello() {
    SharedState st; state_copy(st);
    JsonDocument doc;
    doc["k"] = "hello"; doc["v"] = 2; doc["seq"] = seq_++;
    doc["t"] = (int64_t)esp_timer_get_time();
    doc["fw"] = "2.0.0";
    doc["rr"] = (int)esp_reset_reason();
    doc["mode"] = st.mode;
    doc["hz"] = k::kTimingDspHz;
    doc["ov"] = 4;
    JsonArray th = doc["thr"].to<JsonArray>();
    for (int i = 0; i < 4; ++i) th.add(st.thr_on[i]);
    JsonArray caps = doc["caps"].to<JsonArray>();
    for (const char* c : k::kCaps) caps.add(c);
    doc["baud"] = baud_;
    String line; serializeJson(doc, line);
    tx(line);
    hello_sent_ = true;
}

void Link::send_ack(const char* id, bool ok, const char* err) {
    JsonDocument doc;
    doc["k"] = "ack"; doc["id"] = id; doc["ok"] = ok;
    if (!ok && err && *err) doc["err"] = err;
    String line; serializeJson(doc, line); tx(line);
}

void Link::send_pong(const char* id, int64_t th) {
    JsonDocument doc;
    doc["k"] = "pong"; doc["id"] = id; doc["th"] = th;
    doc["t"] = (int64_t)esp_timer_get_time();
    String line; serializeJson(doc, line); tx(line);
}

void Link::send_diag() {
    SharedState st; state_copy(st);
    JsonDocument doc;
    doc["k"] = "diag"; doc["v"] = 2; doc["seq"] = seq_++;
    doc["t"] = (int64_t)esp_timer_get_time();
    doc["rx"] = rx_lines_; doc["drop"] = drop_lines_;
    doc["rl"] = rate_limited_; doc["baud"] = baud_;
    doc["bt"] = st.bt_connected ? 1 : 0;
    String line; serializeJson(doc, line); tx(line);
}

void Link::send_cal(const char* step, const float* pk, int n, float np99,
                    float thr, float snr) {
    JsonDocument doc;
    doc["k"] = "cal"; doc["v"] = 2; doc["seq"] = seq_++;
    doc["t"] = (int64_t)esp_timer_get_time();
    doc["step"] = step;
    JsonArray p = doc["pk"].to<JsonArray>();
    for (int i = 0; i < n && i < 3; ++i) p.add(pk[i]);
    doc["np99"] = np99; doc["thr"] = thr; doc["snr"] = snr;
    String line; serializeJson(doc, line); tx(line);
}

void Link::send_evt(const omniband::OutEvent& e) {
    JsonDocument doc;
    doc["k"] = "evt"; doc["v"] = 2; doc["seq"] = seq_++;
    doc["t"] = (int64_t)e.t_ms * 1000; doc["ch"] = e.ch;
    doc["ty"] = e.ty == 'd' ? "down" : (e.veto ? "veto" : "up");
    doc["a"] = e.a; if (e.d) doc["d"] = e.d; if (e.veto) doc["veto"] = 1;
    String line; serializeJson(doc, line); tx(line);
}

void Link::send_ges(const omniband::OutEvent& e) {
    JsonDocument doc;
    doc["k"] = "ges"; doc["v"] = 2; doc["seq"] = seq_++;
    doc["t"] = (int64_t)e.t_ms * 1000; doc["g"] = ges_name((int)e.g);
    if (e.a) doc["a"] = e.a; if (e.d) doc["d"] = e.d; doc["m"] = e.m;
    String line; serializeJson(doc, line); tx(line);
}

void Link::send_mode_set(int mode) {
    JsonDocument doc;
    doc["k"] = "ges"; doc["v"] = 2; doc["seq"] = seq_++;
    doc["t"] = (int64_t)esp_timer_get_time();
    doc["g"] = "MODE_SET"; doc["m"] = mode;
    String line; serializeJson(doc, line); tx(line);
}

void Link::flush_events(omniband::EventQueue& q) {
    omniband::OutEvent e;
    while (q.pop(e)) {
        if (e.kind == omniband::OUT_EVT_DOWN || e.kind == omniband::OUT_EVT_UP ||
            e.kind == omniband::OUT_EVT_VETO) send_evt(e);
        else if (e.kind == omniband::OUT_GES) {
            if (e.g == omniband::G_MODE_SET) send_mode_set(e.m);
            else send_ges(e);
        }
    }
}

void Link::send_tele(const SharedState& st) {
    JsonDocument doc;
    doc["k"] = "tele"; doc["v"] = 2; doc["seq"] = seq_++;
    doc["t"] = (int64_t)esp_timer_get_time();
    doc["m"] = st.mode;
    JsonArray ev = doc["e"].to<JsonArray>();
    for (int i = 0; i < 4; ++i) ev.add(st.env[i]);
    doc["s"] = st.flags;
    doc["st"] = fsm_name(st.fsm_state);
    String line; serializeJson(doc, line); tx(line);
}

// ------------------------------------------------------------------ RX path
static bool json_get_str(JsonObjectConst o, const char* k, char* dst, size_t n) {
    if (!o[k].is<const char*>()) return false;
    strlcpy(dst, o[k].as<const char*>(), n);
    return true;
}

void Link::handle_line(const String& line, const char* src) {
    (void)src;
    if (line.length() == 0 || line.length() > 220) { drop_lines_++; return; }
    rx_lines_++;
    {
        int64_t now_ms = (int64_t)(esp_timer_get_time() / 1000);
        portENTER_CRITICAL(&g_state_mux);
        g_state.last_rx_ms = now_ms;
        portEXIT_CRITICAL(&g_state_mux);
    }
    JsonDocument doc;
    if (deserializeJson(doc, line)) { drop_lines_++; return; }
    JsonObject o = doc.as<JsonObject>();
    const char* k = o["k"] | "";
    if (!k[0]) { drop_lines_++; return; }

    // rate limit ~50 cmd/s
    int64_t now = (int64_t)(esp_timer_get_time() / 1000);
    if (now - last_cmd_ms_ < (1000 / k::kLinkCmdRateLimitHz)) {
        rate_limited_++;
        const char* id = o["id"] | "";
        if (id[0]) send_ack(id, false, "rate_limited");
        return;
    }
    last_cmd_ms_ = now;

    HostCmd c{};
    json_get_str(o, "id", c.id, sizeof(c.id));
    strlcpy(c.name, k, sizeof(c.name));

    // per-command argument decode (wire fields per PROTOCOL.md)
    if (strcmp(k, "set_mode") == 0)        c.i0 = (int32_t)(o["mode"] | 0);
    else if (strcmp(k, "set_thr") == 0) {
        c.i0 = (int32_t)(o["ch"] | 0);
        c.d0 = o["on"].is<float>() ? (double)o["on"] : 0.0;
        c.i1 = o["off"].is<float>() ? (int32_t)(o["off"].as<float>() + 0.5f) : -1;
    }
    else if (strcmp(k, "recal") == 0)      json_get_str(o, "what", c.s0, sizeof(c.s0));
    else if (strcmp(k, "cal_step") == 0)   json_get_str(o, "step", c.s0, sizeof(c.s0));
    else if (strcmp(k, "set_cfg") == 0) {
        json_get_str(o, "key", c.s0, sizeof(c.s0));
        c.d0 = o["val"].is<float>() ? (double)o["val"] : 0.0;
    }
    else if (strcmp(k, "scope") == 0) {
        c.i0 = (int32_t)(o["on"] | 0);
        c.i1 = (int32_t)(o["rate"] | 1);
    }
    else if (strcmp(k, "ping") == 0)       c.d0 = o["th"].is<float>() ? (double)o["th"] : 0.0;
    else if (strcmp(k, "set_baud") == 0)   c.i0 = (int32_t)(o["baud"] | 0);
    else if (strcmp(k, "night") == 0)      c.i0 = (int32_t)(o["on"] | 0);
    else if (strcmp(k, "oled_msg") == 0) {
        json_get_str(o, "text", c.s0, sizeof(c.s0));
        c.i0 = o["ms"].is<int>() ? (int)o["ms"] : 2000;
    }

    // ---- link-local commands (no dsp state) ----
    if (strcmp(k, "hello") == 0) { send_hello(); return; }
    if (strcmp(k, "ping") == 0) { send_pong(c.id, (int64_t)c.d0); return; }
    if (strcmp(k, "scope") == 0) {
        scope_on_ = c.i0 != 0;
        for (int32_t r : k::kScopeRates) if (r == c.i1) g_scope_div = (int)r;
        send_ack(c.id, true); return;
    }
    if (strcmp(k, "set_baud") == 0) {
        int32_t b = c.i0;
        bool ok = false;
        for (int32_t bb : k::kBaudRates) if (bb == b) ok = true;
        if (!ok) { send_ack(c.id, false, "bad_baud"); return; }
        set_baud((uint32_t)b);
        send_ack(c.id, true); return;
    }
    if (strcmp(k, "diag") == 0) { send_diag(); send_ack(c.id, true); return; }
    if (strcmp(k, "oled_msg") == 0) {
        oled_show_msg(c.s0, c.i0 > 0 ? c.i0 : 2000);
        send_ack(c.id, true); return;
    }

    // ---- everything else goes to the DSP task ----
    c.needs_dsp = true;
    if (g_cmd_queue && xQueueSend(g_cmd_queue, &c, 0) != pdTRUE) {
        send_ack(c.id, false, "busy");
        return;
    }
    // ack is sent by the dsp task via g_reply_queue
}

void Link::poll() {
    // --- replies from the dsp task -> ack lines ---
    if (g_reply_queue) {
        HostReply r;
        while (xQueueReceive(g_reply_queue, &r, 0) == pdTRUE)
            send_ack(r.id, r.ok, r.err);
    }
    // --- RX: assemble lines from both transports ---
    while (Serial.available()) {
        char ch = (char)Serial.read();
        if (ch == '\n') {
            String l = ser_buf_; ser_buf_ = "";
            l.trim(); handle_line(l, "usb");
        } else if (ch != '\r' && ser_buf_.length() < 240) ser_buf_ += ch;
    }
    if (SerialBT.hasClient()) {
        while (SerialBT.available()) {
            char ch = (char)SerialBT.read();
            if (ch == '\n') {
                String l = bt_buf_; bt_buf_ = "";
                l.trim(); handle_line(l, "bt");
            } else if (ch != '\r' && bt_buf_.length() < 240) bt_buf_ += ch;
        }
    }
    if (g_ble.has_rx()) {
        String rx = g_ble.pop_rx();
        rx.trim();
        if (rx.length() > 0) handle_line(rx, "ble");
    }

    int64_t now = (int64_t)(esp_timer_get_time() / 1000);
    SharedState st; state_copy(st);

    // hello on boot and on BT connect edge
    static bool bt_was = false;
    bool bt_now = SerialBT.hasClient();
    if (!hello_sent_ || (bt_now && !bt_was)) send_hello();
    bt_was = bt_now;

    // 30 Hz telemetry
    if (now - last_tele_ms_ >= (1000 / k::kLinkTeleRateHz)) {
        last_tele_ms_ = now;
        send_tele(st);
        // opt-in scope frame: up to 16 newest peak-decimated samples per ch
        if (scope_on_ && now - last_scope_ms_ >= 66) {
            last_scope_ms_ = now;
            JsonDocument doc;
            doc["k"] = "scope"; doc["v"] = 2; doc["seq"] = seq_++;
            doc["t"] = (int64_t)esp_timer_get_time(); doc["r"] = g_scope_div;
            JsonArray x = doc["x"].to<JsonArray>();
            int n = st.scope_count < 16 ? st.scope_count : 16;
            for (int ch = 0; ch < 4; ++ch) {
                JsonArray xs = x.add<JsonArray>();
                for (int i = 0; i < n; ++i) {
                    int idx = (st.scope_head + 64 - n + i) & 63;
                    xs.add(st.scope_hist[ch][idx]);
                }
            }
            String line; serializeJson(doc, line); tx(line);
        }
        // compat_v1 legacy line (flag bit set by dsp when enabled)
        if (st.flags & k::kTeleFlagsCompatV1) {
            JsonDocument doc;
            doc["piezo_raw"] = st.raw[3];
            float pulse = st.thr_on[3] > 1.0f
                ? (float)st.env[3] / st.thr_on[3] : 0.0f;
            if (pulse < 0) pulse = 0; if (pulse > 2) pulse = 2;
            doc["pulse"] = pulse;
            doc["clench"] = st.active[3] ? 1 : 0;
            doc["lat_ms"] = (int32_t)(now - st.dsp_tick_ms);
            String line; serializeJson(doc, line); tx(line);
        }
    }
}
