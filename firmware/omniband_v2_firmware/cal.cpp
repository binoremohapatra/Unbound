// cal.cpp - calibration collector. Env samples are quantized to uint16 and
// kept in a 2048-deep ring per channel (~40 s at the 50 Hz publish rate).
#include "cal.h"
#include "constants_gen.h"
#include "link.h"
#include <string.h>
#include <algorithm>

namespace k = omniband::constants;

namespace {
constexpr int CH = 4;
constexpr int RING = 2048;
constexpr int PUBLISH_EVERY = 10;         // ticks -> ~50 Hz sample rate

struct Cal {
    int step = -1;                        // -1 idle, 0 rest, 1..4
    int target = -1;                      // channel index for chN steps
    int64_t step_t0 = 0;
    int tick_n = 0;
    uint16_t ring[CH][RING];
    uint16_t head[CH] = {0, 0, 0, 0};
    uint16_t count[CH] = {0, 0, 0, 0};
    float pk[CH] = {0, 0, 0, 0};
    int act[CH] = {0, 0, 0, 0};
    bool was_active[CH] = {false, false, false, false};
    bool finished_flag = false;
    char last_step_name[8] = "";
};
Cal g;

const char* step_names[] = {"rest", "ch1", "ch2", "ch3", "ch4"};

void clear_channel(int ch) {
    g.head[ch] = 0; g.count[ch] = 0; g.pk[ch] = 0; g.act[ch] = 0;
    g.was_active[ch] = false;
}

void finish_step() {
    // report: pk[3] = peaks of the three NON-target channels during the step
    // (crosstalk view); np99/thr/snr describe the target channel (or ch4 for rest).
    int tgt = g.target >= 0 ? g.target : 3;
    float pk3[3]; int n = 0;
    for (int ch = 0; ch < CH && n < 3; ++ch) {
        if (ch == tgt) continue;
        pk3[n++] = g.pk[ch];
    }
    while (n < 3) pk3[n++] = 0.0f;

    // np99 over the ring
    float np99 = 0.0f;
    int cnt = g.count[tgt];
    if (cnt > 0) {
        // copy ring to a temp buffer and pick the 99th percentile by sort
        static uint16_t tmp[RING];
        int m = cnt < RING ? cnt : RING;
        for (int i = 0; i < m; ++i) {
            int idx = (g.head[tgt] + RING - m + i) & (RING - 1);
            tmp[i] = g.ring[tgt][idx];
        }
        std::sort(tmp, tmp + m);
        np99 = (float)tmp[(int)((double)m * 0.99)];
    }
    float thr = 0.0f, snr = 0.0f;
    if (g.pk[tgt] > 0) {
        float lo = np99 * (float)k::kCalibrationThrOnLoFracNp99;
        float hi = g.pk[tgt] * (float)k::kCalibrationThrOnHiFrac;
        thr = g.pk[tgt] * (float)k::kCalibrationThrOnFrac;
        if (thr < lo) thr = lo;
        if (thr > hi) thr = hi;
        snr = g.pk[tgt] / (np99 > 1.0f ? np99 : 1.0f);
    }
    g_link.send_cal(g.last_step_name, pk3, 3, np99, thr, snr);
    g.finished_flag = true;
    g.step = -1; g.target = -1;
}
}  // namespace

namespace cal {
void begin() {
    memset(&g, 0, sizeof(g));
    g.step = -1; g.target = -1;
}

bool start(const char* step) {
    int idx = -1;
    for (int i = 0; i < 5; ++i) if (strcmp(step, step_names[i]) == 0) idx = i;
    if (idx < 0) return false;
    for (int ch = 0; ch < CH; ++ch) clear_channel(ch);
    g.step = idx;
    g.target = idx > 0 ? idx - 1 : -1;
    g.tick_n = 0;
    g.step_t0 = 0;
    g.finished_flag = false;
    strlcpy(g.last_step_name, step_names[idx], sizeof(g.last_step_name) - 1);
    return true;
}

void feed(const double env[4], const bool active[4], int64_t now_ms) {
    if (g.step < 0) return;
    if (g.step_t0 == 0) g.step_t0 = now_ms;
    g.tick_n++;
    int dur = (int)(now_ms - g.step_t0);

    // publish ~50 Hz env samples into rings (all channels: crosstalk view)
    if ((g.tick_n % PUBLISH_EVERY) == 0) {
        for (int ch = 0; ch < CH; ++ch) {
            uint16_t v = env[ch] > 4095.0 ? 4095 : (env[ch] < 0 ? 0 : (uint16_t)env[ch]);
            g.ring[ch][g.head[ch]] = v;
            g.head[ch] = (uint16_t)((g.head[ch] + 1) & (RING - 1));
            if (g.count[ch] < RING) g.count[ch]++;
            if (env[ch] > g.pk[ch]) g.pk[ch] = (float)env[ch];
        }
    }
    for (int ch = 0; ch < CH; ++ch) {
        if (active[ch] && !g.was_active[ch]) g.act[ch]++;
        g.was_active[ch] = active[ch];
    }

    // step durations: rest = kCalibrationRestMs quiet; chN = enough activations
    // or a generous window, whichever first.
    bool done = false;
    if (g.step == 0) {
        done = dur >= k::kCalibrationRestMs;
    } else {
        int tgt = g.target;
        int need = k::kCalibrationActivationsPerChannel;
        // channel steps end after `need` activations AND a short tail, or
        // after 15 s regardless (honest timeout, reported via cal line).
        done = (g.act[tgt] >= need && dur >= 1500) || dur >= 15000;
    }
    if (done) finish_step();
}

void abort() {
    if (g.step >= 0) {
        g.step = -1; g.target = -1; g.finished_flag = false;
    }
}
bool running() { return g.step >= 0; }
int step_index() { return g.step; }
float peak(int ch) { return (ch >= 0 && ch < CH) ? g.pk[ch] : 0.0f; }
float np99(int ch) {
    if (ch < 0 || ch >= CH || g.count[ch] == 0) return 0.0f;
    static uint16_t tmp[RING];
    int m = g.count[ch] < RING ? g.count[ch] : RING;
    for (int i = 0; i < m; ++i) {
        int idx = (g.head[ch] + RING - m + i) & (RING - 1);
        tmp[i] = g.ring[ch][idx];
    }
    std::sort(tmp, tmp + m);
    return (float)tmp[(int)((double)m * 0.99)];
}
int activations(int ch) { return (ch >= 0 && ch < CH) ? g.act[ch] : 0; }
bool just_finished() { return g.finished_flag; }
void clear_finished() { g.finished_flag = false; }
}  // namespace cal
