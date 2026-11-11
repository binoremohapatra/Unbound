// omniband_v2_firmware.ino - OmniBand V2 firmware.
//
// Architecture (see docs/PLAN.md + protocol/PROTOCOL.md):
//   core 1 (loopTask): link RX/TX, telemetry, OLED
//   core 0 (dsp_task): 500 Hz ADC -> omniband::Core -> event queue
// Commands that touch device state travel link -> g_cmd_queue -> dsp_task;
// replies travel dsp_task -> g_reply_queue -> link (ack lines).
//
// No placeholders: every path below is implemented.

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <esp_rom_sys.h>
#include <esp_timer.h>
#include <array>
#include <cstring>
#include "constants_gen.h"
#include "omniband_core.h"
#include "shared_state.h"
#include "adc_front.h"
#include "config_store.h"
#include "link.h"
#include "oled_ui.h"
#include "cal.h"

namespace k = omniband::constants;
using omniband::Core;
using omniband::OutEvent;

// ------------------------------------------------------------------ globals
SharedState g_state = {};
portMUX_TYPE g_state_mux = portMUX_INITIALIZER_UNLOCKED;
volatile int g_scope_div = 4;                    // 4 -> 125 Hz publish
QueueHandle_t g_cmd_queue = nullptr;
QueueHandle_t g_reply_queue = nullptr;
QueueHandle_t g_evt_queue = nullptr;

AdcFront g_adc;
static PersistCfg g_persist;
static volatile bool evt_overflow_ = false;

// Core does not expose thr_on; this task is the only writer of thresholds, so
// a mirror kept in sync here is the single source for read-modify-write.
static std::array<double, 4> g_thr_mirror = {};

static int64_t now_ms() { return (int64_t)(esp_timer_get_time() / 1000); }

static const char* ges_name(int g) {
    if (g < 0 || g >= (int)(sizeof(k::kGestures) / sizeof(k::kGestures[0]))) return "?";
    return k::kGestures[g];
}

static void apply_thresholds(Core& core, const std::array<double, 4>& th,
                             int64_t t) {
    core.set_thresholds(th, t);
    g_thr_mirror = th;
}

static void publish_state(Core& core, const int32_t raw[4], int64_t t) {
    portENTER_CRITICAL(&g_state_mux);
    g_state.mode = core.mode();
    g_state.fsm_state = (int)core.fsm_state();
    int fl = 0;
    if (core.night()) fl |= k::kTeleFlagsNight;
    if (core.fsm_state() == omniband::GestureFsm::St::SOS_PENDING)
        fl |= k::kTeleFlagsSosPending;
    if (core.fsm_state() == omniband::GestureFsm::St::PICKER)
        fl |= k::kTeleFlagsPicker;
    if (core.pending_onsets() > 0 || t < core.xt_until())
        fl |= k::kTeleFlagsXtalkSuppress;
    if (evt_overflow_) fl |= k::kTeleFlagsOverrun;
    if (g_persist.compat_v1) fl |= k::kTeleFlagsCompatV1;
    int64_t silence = t - g_state.last_rx_ms;
    int32_t hs = omniband::mode_cfg(core.mode()).host_silence_ms;
    if (hs > 0 && silence > hs) fl |= k::kTeleFlagsHostSilent;
    g_state.flags = fl;
    auto env = core.envelopes();
    auto act = core.active();
    for (int i = 0; i < 4; ++i) {
        g_state.env[i] = env[i];
        g_state.raw[i] = raw[i];
        g_state.active[i] = act[i];
        g_state.thr_on[i] = (float)g_thr_mirror[i];
    }
    g_state.dsp_tick_ms = t;
    g_state.cal_step = cal::step_index();
    for (int i = 0; i < 4; ++i) g_state.cal_act[i] = cal::activations(i);
    portEXIT_CRITICAL(&g_state_mux);
}

// ------------------------------------------------------------------ dsp task
static void dsp_task(void*) {
    Core core(g_persist.mode);
    apply_thresholds(core, g_thr_mirror, 0);

    double sacc[4] = {0, 0, 0, 0};
    int scnt = 0;
    const int64_t period_us = k::kTimingDspTickUs;
    int64_t next = esp_timer_get_time();
    int32_t raw[4] = {0, 0, 0, 0};

    for (;;) {
        // ---- commands from the link task ----
        HostCmd c;
        while (g_cmd_queue && xQueueReceive(g_cmd_queue, &c, 0) == pdTRUE) {
            HostReply r{}; strlcpy(r.id, c.id, sizeof(r.id)); r.ok = true;
            const char* err = "";
            int64_t t = now_ms();
            if (!strcmp(c.name, "set_mode")) {
                if (c.i0 < 0 || c.i0 >= k::kModeCount) { r.ok = false; err = "bad_mode"; }
                else { core.set_mode(c.i0, t); g_persist.mode = c.i0; }
            } else if (!strcmp(c.name, "set_thr")) {
                if (c.i0 < 1 || c.i0 > 4 || c.d0 < (double)k::kThresholdMinOnCounts) {
                    r.ok = false; err = "bad_thr";
                } else {
                    auto th = g_thr_mirror;
                    th[c.i0 - 1] = c.d0;
                    apply_thresholds(core, th, t);
                    g_persist.thr_on[c.i0 - 1] = (float)c.d0;
                }
            } else if (!strcmp(c.name, "recal")) {
                if (!strcmp(c.s0, "baseline")) {
                    core.recal_baseline(t);
                } else if (!strcmp(c.s0, "thresholds") || !strcmp(c.s0, "all")) {
                    std::array<double, 4> th{};
                    for (int i = 0; i < 4; ++i) th[i] = k::kThresholdDefaultOn[i];
                    apply_thresholds(core, th, t);
                    for (int i = 0; i < 4; ++i) g_persist.thr_on[i] = (float)th[i];
                    if (!strcmp(c.s0, "all")) core.recal_baseline(t);
                } else { r.ok = false; err = "bad_what"; }
            } else if (!strcmp(c.name, "night")) {
                core.night(c.i0 != 0, t);
            } else if (!strcmp(c.name, "sos_cancel")) {
                core.sos_cancel(t);
            } else if (!strcmp(c.name, "cal_step")) {
                if (!strcmp(c.s0, "idle") || c.s0[0] == '\0') cal::abort();
                else if (!cal::start(c.s0)) { r.ok = false; err = "bad_step"; }
            } else if (!strcmp(c.name, "set_cfg")) {
                if (!strcmp(c.s0, "compat_v1")) {
                    g_persist.compat_v1 = c.d0 != 0.0;
                } else if (!strcmp(c.s0, "night")) {
                    core.night(c.d0 != 0.0, t);
                } else { r.ok = false; err = "bad_key"; }
            } else if (!strcmp(c.name, "save")) {
                cfgstore::save(g_persist);
            } else if (!strcmp(c.name, "defaults")) {
                cfgstore::set_defaults(g_persist);
                core.set_mode(g_persist.mode, t);
                for (int i = 0; i < 4; ++i) g_thr_mirror[i] = g_persist.thr_on[i];
                apply_thresholds(core, g_thr_mirror, t);
            } else {
                r.ok = false; err = "unknown";
            }
            if (!r.ok) strlcpy(r.err, err, sizeof(r.err));
            if (g_reply_queue) xQueueSend(g_reply_queue, &r, 0);
        }

        // ---- DSP tick ----
        int64_t t = now_ms();
        for (int ch = 0; ch < 4; ++ch) {
            raw[ch] = g_adc.read(ch);
            core.feed_sample(ch + 1, raw[ch], t);
        }
        core.tick(t);

        {
            auto env = core.envelopes();
            auto act = core.active();
            std::array<bool, 4> b{};
            for (int i = 0; i < 4; ++i) b[i] = act[i];
            cal::feed(env.data(), b.data(), t);
        }

        // scope accumulator: peak env per channel
        {
            auto env = core.envelopes();
            for (int i = 0; i < 4; ++i)
                if (env[i] > sacc[i]) sacc[i] = env[i];
            if (++scnt >= (g_scope_div > 0 ? g_scope_div : 4)) {
                scnt = 0;
                portENTER_CRITICAL(&g_state_mux);
                for (int i = 0; i < 4; ++i) {
                    uint16_t v = sacc[i] > 4095 ? 4095 : (sacc[i] < 0 ? 0 : (uint16_t)sacc[i]);
                    g_state.scope_hist[i][g_state.scope_head] = v;
                    sacc[i] = 0;
                }
                g_state.scope_head = (uint8_t)((g_state.scope_head + 1) & 63);
                if (g_state.scope_count < 64) g_state.scope_count++;
                portEXIT_CRITICAL(&g_state_mux);
            }
        }

        // drain core events -> link queue; gestures also flash the OLED
        {
            auto& evs = core.events();
            for (auto& e : evs) {
                if (e.kind == omniband::OUT_GES)
                    oled_set_gesture(ges_name((int)e.g));
                if (g_evt_queue &&
                    xQueueSend(g_evt_queue, &e, 0) != pdTRUE) {
                    evt_overflow_ = true;
                }
            }
            core.drain();
        }

        publish_state(core, raw, t);

        // precise 2 ms cadence
        next += period_us;
        int64_t delay = next - esp_timer_get_time();
        if (delay > 0) esp_rom_delay_us((uint32_t)delay);
        else next = esp_timer_get_time();   // overrun: resync, don't spiral
    }
}

// ------------------------------------------------------------------ sketch
void setup() {
    Serial.begin(k::kLinkBaudBoot);
    Serial.setTimeout(0);

    cfgstore::begin();
    cfgstore::load(g_persist);
    for (int i = 0; i < 4; ++i) g_thr_mirror[i] = g_persist.thr_on[i];

    g_adc.begin();
    cal::begin();
    oled_begin();

    g_cmd_queue = xQueueCreate(24, sizeof(HostCmd));
    g_reply_queue = xQueueCreate(24, sizeof(HostReply));
    g_evt_queue = xQueueCreate(48, sizeof(OutEvent));

    xTaskCreatePinnedToCore(dsp_task, "dsp", 8192, nullptr, 20, nullptr, 0);

    g_link.begin(k::kLinkBaudBoot);
    g_link.send_hello();
}

void loop() {
    g_link.poll();                       // RX + replies + tele/scope/v1 TX
    if (g_evt_queue) {
        OutEvent e;
        while (xQueueReceive(g_evt_queue, &e, 0) == pdTRUE) {
            evt_overflow_ = false;
            if (e.kind == omniband::OUT_EVT_DOWN || e.kind == omniband::OUT_EVT_UP ||
                e.kind == omniband::OUT_EVT_VETO) g_link.send_evt(e);
            else if (e.kind == omniband::OUT_GES) {
                if (e.g == omniband::G_MODE_SET) g_link.send_mode_set(e.m);
                else g_link.send_ges(e);
            }
        }
    }
    oled_update();
    delay(1);
}
