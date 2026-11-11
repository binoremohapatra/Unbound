// shared_state.h - telemetry snapshot shared between the DSP task (writer,
// 500 Hz) and the link/UI tasks (readers, 30 Hz). Written under a spinlock;
// readers take a consistent copy. All fields are plain POD.
#pragma once
#include <stdint.h>
#include <freertos/FreeRTOS.h>
#include <freertos/portmacro.h>

struct SharedState {
    int32_t mode;
    int32_t fsm_state;      // GestureFsm::St as int
    int32_t flags;          // tele_flags bitmask
    double env[4];
    bool active[4];
    float thr_on[4];
    int64_t dsp_tick_ms;    // last completed DSP tick (ms since boot)
    int64_t last_rx_ms;     // last host command received (ms since boot)
    char last_ges[16];      // last decoded gesture name ("")
    int32_t last_ges_t;     // ms
    int32_t link_bps;       // negotiated baud (0 = unknown)
    bool bt_connected;
    int32_t raw[4];         // last ADC counts per channel
    // scope history: ring of peak-decimated env values, newest at head-1
    uint16_t scope_hist[4][64];
    uint8_t scope_head;
    uint8_t scope_count;
    // calibration progress (written by dsp task)
    int32_t cal_step;       // -1 idle, 0 rest, 1..4 ch1..ch4
    int32_t cal_act[4];     // activations seen per channel this step
};

extern SharedState g_state;
extern portMUX_TYPE g_state_mux;
// scope publish decimation (2 ms tick units): 1->250 Hz, 2->125 Hz, 4->62.5 Hz
extern volatile int g_scope_div;

inline void state_write(void (*fn)(SharedState&)) {
    portENTER_CRITICAL(&g_state_mux);
    fn(g_state);
    portEXIT_CRITICAL(&g_state_mux);
}
inline void state_copy(SharedState& dst) {
    portENTER_CRITICAL(&g_state_mux);
    dst = g_state;
    portEXIT_CRITICAL(&g_state_mux);
}
