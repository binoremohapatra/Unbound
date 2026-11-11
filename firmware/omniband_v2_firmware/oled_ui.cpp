// oled_ui.cpp - status pages: mode, link, env bars, last gesture, msg overlay.
#include "oled_ui.h"
#include <Wire.h>
#include <esp_timer.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "constants_gen.h"
#include "shared_state.h"

namespace k = omniband::constants;

namespace {
Adafruit_SSD1306 display(128, 64, &Wire, -1);
bool ok = false;
char msg_buf[24] = "";
int64_t msg_until_ms = 0;
char ges_buf[16] = "";
int ges_t_ms = 0;
int64_t last_draw_ms = 0;

const char* mode_tag(int m) {
    if (m < 0 || m >= k::kModeCount) return "?";
    return k::kModes[m].tag;
}
const char* fsm_name(int st) {
    static const char* n[] = {"IDLE","PRESSED","GAP_WAIT","LONG_FIRED",
                              "PICKER","SOS_PENDING","NIGHT"};
    if (st < 0 || st >= 7) return "?";
    return n[st];
}
}  // namespace

void oled_begin() {
    Wire.begin(21, 22);   // SDA, SCL (ESP32 default I2C)
    ok = display.begin(SSD1306_SWITCHCAPVCC, 0x3C);
    if (!ok) return;
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.display();
}

void oled_show_msg(const char* text, int ms) {
    strlcpy(msg_buf, text ? text : "", sizeof(msg_buf) - 1);
    msg_until_ms = (int64_t)(esp_timer_get_time() / 1000) + ms;
}

void oled_set_gesture(const char* name) {
    strlcpy(ges_buf, name ? name : "", sizeof(ges_buf) - 1);
    ges_t_ms = (int)(esp_timer_get_time() / 1000);
}

void oled_update() {
    if (!ok) return;
    int64_t now = (int64_t)(esp_timer_get_time() / 1000);
    if (now - last_draw_ms < (1000 / k::kLinkUiRateHz)) return;
    last_draw_ms = now;

    SharedState st; state_copy(st);
    display.clearDisplay();
    display.setTextSize(1);
    display.setCursor(0, 0);

    // header: mode tag + fsm state + link
    display.print(mode_tag(st.mode));
    display.print(" ");
    display.print(fsm_name(st.fsm_state));
    display.setCursor(96, 0);
    display.print(st.bt_connected ? "BT" : "USB");

    // env bars CH1-4 (scaled to thr_on*2)
    for (int i = 0; i < 4; ++i) {
        int y = 10 + i * 9;
        display.setCursor(0, y);
        display.print("C"); display.print(i + 1);
        display.drawRect(14, y, 100, 7, SSD1306_WHITE);
        float scale = st.thr_on[i] * 2.0f;
        int w = scale > 0 ? (int)(st.env[i] / scale * 100.0) : 0;
        if (w > 100) w = 100; if (w < 0) w = 0;
        if (w > 0) display.fillRect(15, y + 1, w, 5, SSD1306_WHITE);
        if (st.active[i]) display.drawPixel(116, y + 3, SSD1306_WHITE);
    }

    // last gesture + transient msg
    display.setCursor(0, 48);
    if (now < msg_until_ms && msg_buf[0]) {
        display.print(msg_buf);
    } else {
        display.print(ges_buf[0] ? ges_buf : "-");
        if (st.flags & k::kTeleFlagsNight) display.print(" [NIGHT]");
        if (st.flags & k::kTeleFlagsSosPending) display.print(" [SOS!]");
        if (st.flags & k::kTeleFlagsHostSilent) display.print(" [HOST?]");
        if (st.cal_step >= 0) display.print(" [CAL]");
    }
    display.display();
}
