// cal.h - guided calibration wizard (rest + per-channel activation steps).
// Passive collector driven by the DSP task: cal_feed() once per 2 ms tick with
// the Core's envelope/active arrays. Step machine + stats live here; results
// are pushed to the link as `cal` lines.
#pragma once
#include <stdint.h>

namespace cal {
    void begin();                          // idle
    // step: ""/"idle" -> stop, "rest", "ch1".."ch4"
    bool start(const char* step);
    void feed(const double env[4], const bool active[4], int64_t now_ms);
    void abort();
    bool running();
    int step_index();                // -1 idle, 0 rest, 1..4 ch1..4
    // per-channel stats for the CURRENT/last step (valid after finish)
    float peak(int ch);              // max env observed (counts)
    float np99(int ch);              // 99th percentile env (counts)
    int activations(int ch);         // active rising edges seen
    bool just_finished();            // set once after a step completes
    void clear_finished();
}
