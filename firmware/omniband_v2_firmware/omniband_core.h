// omniband_core.h - portable C++17 core for OmniBand V2.
// NO Arduino/FreeRTOS/ESP-IDF includes: time is injected as int64 ms, output is a
// caller-drained vector. Compiled by firmware/tests/native/test_core.cpp on host
// and included by the Arduino glue. All constants come from constants_gen.h
// (generated from protocol/constants.json - never hand-copy values).
#pragma once

#include <algorithm>
#include <array>
#include <cstdint>
#include <deque>
#include <vector>

#include "constants_gen.h"

namespace omniband {
namespace k = constants;

// ---------------------------------------------------------------- output event
enum OutKind : uint8_t {
    OUT_EVT_DOWN = 0,   // raw channel edge (ty='d')
    OUT_EVT_UP = 1,     // raw channel edge (ty='u'), d=duration ms, a=peak
    OUT_EVT_VETO = 2,   // retro-cancelled down (fast modes), veto=1
    OUT_GES = 3,        // decoded gesture
    OUT_MODE_SET = 4,   // picker-confirmed mode change, m=new mode
};

enum GesId : uint8_t {
    G_SINGLE, G_DOUBLE, G_TRIPLE, G_LONG, G_BRUX, G_BRUX_END, G_REJECT,
    G_ESTOP, G_SOS_PENDING, G_SOS_CONFIRMED, G_SOS_CANCELLED, G_PICKER_OPEN,
    G_PICKER_CANCEL, G_MODE_SET, G_NOISE_BURST
};

inline const char* ges_name(GesId g) {
    static const char* names[] = {
        "SINGLE", "DOUBLE", "TRIPLE", "LONG", "BRUX", "BRUX_END", "REJECT",
        "ESTOP", "SOS_PENDING", "SOS_CONFIRMED", "SOS_CANCELLED", "PICKER_OPEN",
        "PICKER_CANCEL", "MODE_SET", "NOISE_BURST"};
    return names[static_cast<int>(g)];
}

struct OutEvent {
    uint8_t kind;
    int64_t t_ms;
    int32_t ch;        // 1..4 for evt, -1 for ges
    char ty;           // 'd' | 'u' | 0
    int32_t a;         // peak amplitude (counts) or gesture amp
    int32_t d;         // duration ms (up / gesture)
    int32_t m;         // mode for ges/mode_set
    int32_t veto;
    GesId g;           // valid when kind==OUT_GES/OUT_MODE_SET
};

// ------------------------------------------------------------------ mode policy
struct ModeCfg {
    bool fast_events;
    bool sos;
    bool estop;
    bool night_mode;
    bool pc_only;
    int32_t sos_confirm_ms;
    int32_t long_hold_ms;
    int32_t xt_lookahead_ms;
    int32_t host_silence_ms;
};

inline constexpr int MODE_COUNT = 6;
inline ModeCfg mode_cfg(int mode) {
    if (mode < 0 || mode >= MODE_COUNT) mode = 0;
    const k::ModePolicy& p = k::kModes[mode];
    return {p.fast_events, p.sos, p.estop, p.night_mode,
            p.mode_change_pc_only, p.sos_confirm_ms, p.long_hold_ms,
            p.xt_lookahead_ms, p.host_silence_ms};
}

// ================================================================ gesture FSM
// Edge-driven decoder. Fed by inject_edge() (test/simulator path) or by the DSP
// layer inside Core. Times are int64 ms. Policy comes from ModeCfg + constants.
class GestureFsm {
public:
    enum class St : uint8_t { IDLE, PRESSED, GAP_WAIT, LONG_FIRED, PICKER, SOS_PENDING, NIGHT };

    explicit GestureFsm(std::vector<OutEvent>& out) : out_(out) {}

    void set_mode(int mode, int64_t now) {
        mode_ = mode;
        cfg_ = mode_cfg(mode);
        reset(now);
    }
    int mode() const { return mode_; }
    ModeCfg cfg() const { return cfg_; }
    St state() const { return st_; }
    bool night() const { return night_; }

    void night_on(int64_t now) {
        if (!cfg_.night_mode) return;      // night monitor exists only in M5
        night_ = true;
        st_ = St::NIGHT;
        reset_taps(now);
        brux_clear();
    }
    void night_off(int64_t now) {
        night_ = false;
        if (st_ == St::NIGHT) st_ = St::IDLE;
        reset_taps(now);
        brux_clear();
    }
    void sos_cancel(int64_t now) {
        if (st_ == St::SOS_PENDING) {
            emit_g(G_SOS_CANCELLED, now, 0, 0);
            st_ = St::IDLE;
        }
    }
    void host_set_mode(int mode, int64_t now) { set_mode(mode, now); }

    // ---- channel edges: ch 1..4, down bool, amp peak counts, dur_ms on up ----
    void edge(int ch, bool down, int32_t amp, int32_t dur_ms, int64_t now) {
        if (ch < 1 || ch > 4) return;
        if (night_) { night_edge(ch, down, amp, dur_ms, now); return; }
        if (st_ == St::PICKER) { picker_edge(ch, down, amp, dur_ms, now); return; }
        if (st_ == St::SOS_PENDING) { sos_edge(ch, down, amp, dur_ms, now); return; }
        if (ch == 4) ch4_edge(down, amp, dur_ms, now);
    }

    // timers: gap expiry, sos countdown + hold-cancel, picker timeout + confirm,
    // long-hold fire, night bruxism bridging, night-exit chord
    void tick(int64_t now) {
        if (chew_lock_until_ > 0 && now >= chew_lock_until_) chew_lock_until_ = 0;
        switch (st_) {
            case St::GAP_WAIT:
                if (now >= gap_deadline_) resolve_gap(now);
                break;
            case St::PRESSED:
                if (cfg_.long_hold_ms > 0 &&
                    now - press_start_ >= cfg_.long_hold_ms) {
                    emit_g(G_LONG, now, press_amp_,
                           static_cast<int32_t>(now - press_start_));
                    open_picker(now);      // LONG -> PICKER (st_ becomes PICKER)
                }
                break;
            case St::SOS_PENDING:
                if (sos_hold_since_ >= 0 &&
                    now - sos_hold_since_ >= k::kCh4GesturesSosHoldCancelMs) {
                    sos_cancel(now);
                } else if (now >= sos_deadline_) {
                    emit_g(G_SOS_CONFIRMED, now, 0, 0);
                    st_ = St::IDLE;
                }
                break;
            case St::PICKER:
                if (picker_confirm_pending_ && now >= gap_deadline_) {
                    picker_confirm_pending_ = false;
                    int m = picker_sel_;
                    set_mode(m, now);                    // resets decoder, applies policy
                    emit_mode_set(now, m);
                } else if (now >= picker_deadline_) {
                    emit_g(G_PICKER_CANCEL, now, 0, 0);
                    st_ = St::IDLE;
                }
                break;
            case St::NIGHT:
                night_tick(now);
                break;
            default: break;
        }
    }

    // thresholds (counts) - set by Core from calibration/defaults
    std::array<double, 4> thr_on_ = {k::kThresholdDefaultOn[0], k::kThresholdDefaultOn[1],
                                     k::kThresholdDefaultOn[2], k::kThresholdDefaultOn[3]};

private:
    std::vector<OutEvent>& out_;
    ModeCfg cfg_{};
    int mode_ = 0;
    St st_ = St::IDLE;
    bool night_ = false;

    // CH4 press / multi-tap
    int64_t press_start_ = -1;
    int32_t press_amp_ = 0;
    int32_t press_dur_ = 0;
    std::vector<std::pair<int64_t, int32_t>> taps_;     // pending sequence taps
    std::deque<std::pair<int64_t, int32_t>> recent_;    // chew-guard window taps
    int64_t first_press_t_ = -1;
    int64_t gap_deadline_ = -1;
    int64_t chew_lock_until_ = 0;

    // picker / sos
    int picker_sel_ = 0;
    int64_t picker_deadline_ = -1;
    bool picker_confirm_pending_ = false;
    int64_t sos_deadline_ = -1;
    int64_t sos_hold_since_ = -1;

    // night bruxism + exit chord
    int ch4_pressed_ = 0;                 // night-mode press depth (0/1)
    int64_t brux_epoch_start_ = -1, brux_last_ = -1, brux_end_deadline_ = -1;
    int32_t brux_pk_ = 0;
    bool brux_fired_ = false;
    std::array<int64_t, 16> ch3_pulses_{};
    int ch3_pulse_n_ = 0;

    void reset(int64_t now) {
        st_ = St::IDLE;
        reset_taps(now);
        picker_sel_ = mode_;
        sos_hold_since_ = -1;
        picker_confirm_pending_ = false;
        brux_clear();
        ch4_pressed_ = 0;
        ch3_pulse_n_ = 0;
    }
    void reset_taps(int64_t) {
        taps_.clear(); recent_.clear();
        first_press_t_ = -1; gap_deadline_ = -1;
    }
    void brux_clear() {
        brux_epoch_start_ = brux_last_ = brux_end_deadline_ = -1;
        brux_pk_ = 0; brux_fired_ = false; ch4_pressed_ = 0;
    }

    void emit_g(GesId g, int64_t t, int32_t a, int32_t d) {
        OutEvent e{}; e.kind = OUT_GES; e.t_ms = t; e.ch = -1; e.a = a; e.d = d;
        e.m = mode_; e.g = g; out_.push_back(e);
    }
    void emit_mode_set(int64_t t, int m) {
        OutEvent e{}; e.kind = OUT_MODE_SET; e.t_ms = t; e.ch = -1; e.m = m; e.g = G_MODE_SET;
        out_.push_back(e);
    }

    double thr_on(int ch) const { return ch >= 1 && ch <= 4 ? thr_on_[ch - 1] : 0.0; }

    // ---------------- CH4 decoding ----------------
    void ch4_edge(bool down, int32_t amp, int32_t dur_ms, int64_t now) {
        if (down) {
            press_start_ = now; press_amp_ = amp;
            if (st_ != St::PRESSED && st_ != St::LONG_FIRED) st_ = St::PRESSED;
        } else {
            if (st_ != St::PRESSED && st_ != St::LONG_FIRED) return;  // unmatched up
            press_dur_ = dur_ms > 0 ? dur_ms : static_cast<int32_t>(now - press_start_);
            int64_t ps = press_start_;
            int32_t pa = press_amp_;
            if (st_ == St::LONG_FIRED) { st_ = St::IDLE; return; }
            st_ = St::IDLE;
            if (press_dur_ < k::kCh4GesturesPressMinMs) return;          // noise
            if (cfg_.long_hold_ms > 0 && press_dur_ >= cfg_.long_hold_ms) return; // LONG fired
            if (press_dur_ > k::kCh4GesturesTapMaxMs) return;             // medium hold
            if (now < chew_lock_until_) return;                            // locked
            tap_release(now, pa, ps);
        }
    }

    void tap_release(int64_t now, int32_t amp, int64_t press_t) {
        // chew guard runs on ALL taps in the sliding window, independent of
        // multi-tap sequence state
        while (!recent_.empty() && now - recent_.front().first > k::kCh4GesturesChewGuardWindowMs)
            recent_.pop_front();
        recent_.push_back({now, amp});
        if (static_cast<int>(recent_.size()) > k::kCh4GesturesChewGuardTaps) {
            emit_g(G_REJECT, now, amp, press_dur_);
            chew_lock_until_ = now + k::kCh4GesturesChewGuardLockMs;
            taps_.clear(); recent_.clear();
            first_press_t_ = -1; gap_deadline_ = -1;
            return;
        }
        taps_.push_back({now, amp});
        if (first_press_t_ < 0) first_press_t_ = press_t;
        // 3rd tap released within total window & every tap >= ratio*thr_on -> TRIPLE
        if (taps_.size() == 3 &&
            now - first_press_t_ <= k::kCh4GesturesTripleTotalMaxMs) {
            bool strong = true;
            for (auto& tp : taps_)
                if (tp.second < k::kCh4GesturesSosAmplitudeRatio * thr_on(4)) strong = false;
            if (strong) {
                emit_g(G_TRIPLE, now, amp, press_dur_);
                taps_.clear();   // recent_ keeps counting for the chew guard
                first_press_t_ = -1; gap_deadline_ = -1;
                after_triple(now);
                return;
            }
            // weak 3rd tap: resolve the strong pair as DOUBLE, restart seq with tap3
            emit_g(G_DOUBLE, now, taps_[1].second, 0);
            taps_.erase(taps_.begin(), taps_.end() - 1);
            taps_.front() = {now, amp};
            first_press_t_ = press_t;
        }
        gap_deadline_ = now + k::kCh4GesturesMultiGapMs;
        st_ = St::GAP_WAIT;
    }

    void resolve_gap(int64_t now) {
        if (taps_.size() == 1) emit_g(G_SINGLE, now, taps_[0].second, 0);
        else if (taps_.size() == 2) emit_g(G_DOUBLE, now, taps_[1].second, 0);
        taps_.clear();
        first_press_t_ = -1; gap_deadline_ = -1;
        st_ = St::IDLE;
    }

    void after_triple(int64_t now) {
        if (cfg_.sos) {
            emit_g(G_SOS_PENDING, now, 0, 0);
            st_ = St::SOS_PENDING;
            sos_deadline_ = now + cfg_.sos_confirm_ms;
            sos_hold_since_ = -1;
        } else {
            st_ = St::IDLE;    // plain TRIPLE already emitted
        }
    }

    // ---------------- SOS_PENDING ----------------
    void sos_edge(int ch, bool down, int32_t amp, int32_t dur_ms, int64_t now) {
        if (ch == 4) {
            if (down) { press_start_ = now; press_amp_ = amp; sos_hold_since_ = now; }
            else {
                press_dur_ = dur_ms > 0 ? dur_ms : static_cast<int32_t>(now - press_start_);
                sos_hold_since_ = -1;
                // a full CH4 tap while pending is consumed (no re-trigger)
            }
        }
        (void)amp;
    }

    // ---------------- PICKER ----------------
    void open_picker(int64_t now) {
        st_ = St::PICKER;
        picker_sel_ = mode_;
        picker_deadline_ = now + k::kPickerTimeoutMs;
        picker_confirm_pending_ = false;
        emit_g(G_PICKER_OPEN, now, 0, 0);
    }
    void picker_edge(int ch, bool down, int32_t amp, int32_t dur_ms, int64_t now) {
        if (ch == 1 && down) {
            picker_sel_ = (picker_sel_ + MODE_COUNT - 1) % MODE_COUNT;
            picker_deadline_ = now + k::kPickerTimeoutMs;
        } else if (ch == 2 && down) {
            picker_sel_ = (picker_sel_ + 1) % MODE_COUNT;
            picker_deadline_ = now + k::kPickerTimeoutMs;
        } else if (ch == 4) {
            if (down) { press_start_ = now; press_amp_ = amp; }
            else {
                int32_t d = dur_ms > 0 ? dur_ms : static_cast<int32_t>(now - press_start_);
                if (d >= k::kCh4GesturesPressMinMs && d <= k::kCh4GesturesTapMaxMs) {
                    gap_deadline_ = now + k::kCh4GesturesMultiGapMs;
                    picker_confirm_pending_ = true;
                }
            }
        }
        (void)amp;
    }

    // ---------------- NIGHT ----------------
    void night_edge(int ch, bool down, int32_t amp, int32_t dur_ms, int64_t now) {
        if (ch == 4) {
            if (down) {
                ch4_pressed_++;
                if (brux_epoch_start_ < 0) {
                    brux_epoch_start_ = now; brux_pk_ = 0; brux_fired_ = false;
                }
                if (amp > brux_pk_) brux_pk_ = amp;
            } else {
                if (ch4_pressed_ > 0) ch4_pressed_--;
                if (amp > brux_pk_) brux_pk_ = amp;
                brux_last_ = now;
            }
        } else if (ch == 3 && down &&
                   amp >= k::kNightStrongRatio * thr_on(3)) {
            ch3_pulses_[ch3_pulse_n_ % 16] = now;
            ch3_pulse_n_++;
            if (night_exit_ready(now)) night_off(now);
        }
        (void)dur_ms;
    }

    void night_tick(int64_t now) {
        // last-activity = now while CH4 held (bridged press is continuous),
        // else the last edge time
        int64_t last = ch4_pressed_ > 0 ? now : brux_last_;
        if (brux_epoch_start_ >= 0 && last >= 0 &&
            now - last > k::kActivityDropoutBridgeMs && brux_end_deadline_ < 0) {
            brux_end_deadline_ = last + k::kActivityDropoutBridgeMs;
        }
        if (brux_end_deadline_ >= 0 && now >= brux_end_deadline_) {
            if (brux_fired_) {
                int64_t span = brux_last_ - brux_epoch_start_;
                if (span < 0) span = brux_end_deadline_ - brux_epoch_start_;
                emit_g(G_BRUX_END, now, brux_pk_, static_cast<int32_t>(span));
            }
            brux_clear();
        }
        if (!brux_fired_ && brux_epoch_start_ >= 0 &&
            now - brux_epoch_start_ >= k::kHoldBruxismMs) {
            brux_fired_ = true;
            emit_g(G_BRUX, now, brux_pk_,
                   static_cast<int32_t>(now - brux_epoch_start_));
        }
    }

    bool night_exit_ready(int64_t now) {
        int cnt = 0;
        int n = std::min(ch3_pulse_n_, 16);
        for (int i = 0; i < n; ++i) {
            int64_t t = ch3_pulses_[(ch3_pulse_n_ - 1 - i + 16) % 16];
            if (now - t <= k::kNightWindowMs) cnt++; else break;
        }
        return cnt >= k::kNightStrongCh3Pulses;
    }
};


// ================================================================ DSP + Core
// Per-channel pipeline at 500 Hz: gated EMA baseline -> rectify -> peak envelope
// -> hysteresis -> debounce -> (CH1/2 xtalk lookahead / fast retro-cancel) ->
// raw edges -> GestureFsm. feed_sample() is the real path; inject_edge() bypasses
// DSP for shared-vector and simulator use. All times int64 ms.

struct ChanDsp {
    double baseline = 0.0;
    double env = 0.0;
    double env_peak = 0.0;       // peak env since last down edge (press amplitude)
    bool active = false;
    bool initialized = false;
    double dev = 0.0;            // last raw deviation from baseline
    int over_count = 0, under_count = 0;
    int64_t down_t = -1;
    int32_t down_amp = 0;
    // lookahead-pending onset (non-fast CH1/2)
    bool pending_onset = false;
    int64_t pending_t = 0;
    int64_t pending_emit_at = 0;
    double pending_env = 0.0;
    // fast-mode retro-cancel bookkeeping
    int64_t last_down_t = -1;
    // noise-burst tracking (CH1-3)
    bool burst_risen = false;
    int64_t burst_rise_t = 0;
};

class Core {
public:
    explicit Core(int mode = 0) : fsm_(out_) {
        thr_on_ = {k::kThresholdDefaultOn[0], k::kThresholdDefaultOn[1],
                   k::kThresholdDefaultOn[2], k::kThresholdDefaultOn[3]};
        for (int i = 0; i < 4; ++i) thr_off_[i] = thr_on_[i] * k::kThresholdOffRatio;
        set_mode(mode, 0);
    }

    void set_mode(int mode, int64_t now) {
        mode_ = mode;
        cfg_ = mode_cfg(mode);
        fsm_.set_mode(mode, now);
        for (auto& c : ch_) { c.pending_onset = false; c.burst_risen = false; }
        xt_until_ = -1;
        estop_since_ = -1;
        estop_fired_ = false;
        burst_open_ = false;
    }
    int mode() const { return mode_; }
    ModeCfg cfg() const { return cfg_; }

    void set_thresholds(const std::array<double, 4>& on, int64_t now) {
        for (int i = 0; i < 4; ++i) {
            thr_on_[i] = on[i];
            thr_off_[i] = on[i] * k::kThresholdOffRatio;
        }
        fsm_.thr_on_ = on;
        (void)now;
    }
    void set_threshold(int ch, double on, int64_t now) {
        if (ch < 1 || ch > 4) return;
        auto t = thr_on_; t[ch - 1] = on; set_thresholds(t, now);
    }
    void recal_baseline(int64_t now) {
        for (auto& c : ch_) {
            c.initialized = false; c.env = 0; c.env_peak = 0;
            c.active = false; c.pending_onset = false;
            c.over_count = c.under_count = 0; c.down_t = -1;
        }
        (void)now;
    }
    void night(bool on, int64_t now) {
        if (on) fsm_.night_on(now); else fsm_.night_off(now);
    }
    void sos_cancel(int64_t now) { fsm_.sos_cancel(now); }

    // ---------------- real DSP path (500 Hz per channel) ----------------
    void feed_sample(int ch, int32_t x, int64_t t_ms) {
        if (ch < 1 || ch > 4) return;
        ChanDsp& c = ch_[ch - 1];
        const double neu = k::kBaselineEmaNew;
        const double thr_on = thr_on_[ch - 1], thr_off = thr_off_[ch - 1];

        // 5.3.2 gated baseline: EMA only while the channel is idle - env below
        // half of thr_off, no active gesture, outside CH1/2 xtalk suppress window
        bool idle = c.env < 0.5 * thr_off && !c.active &&
                    !c.pending_onset && !(ch <= 2 && t_ms < xt_until_);
        if (!c.initialized) { c.baseline = x; c.initialized = true; }
        else if (idle) c.baseline += neu * (x - c.baseline);

        double dev = x - c.baseline;
        c.dev = dev;
        double d = dev > 0 ? dev : 0.0;
        c.env = std::max(d, c.env * k::kEnvelopeReleasePerSample);
        c.env_peak = std::max(c.env_peak, c.env);

        bool over = c.env >= thr_on;
        bool under = c.env < thr_off;
        int need = cfg_.fast_events ? k::kDebounceFastConsecutive
                                    : k::kDebounceNormalConsecutive;

        if (!c.active) {
            if (over) {
                if (++c.over_count >= need) try_onset(ch, c, t_ms);
                c.under_count = 0;
            } else if (under) {
                c.over_count = 0;
            }
        } else {
            if (under) {
                if (++c.under_count >= need) onset_release(ch, c, t_ms);
                c.over_count = 0;
            } else if (over) {
                c.under_count = 0;
            }
        }

        track_noise_burst(ch, c, t_ms);
        track_xtalk(ch, c, t_ms);
        flush_pending_onsets(t_ms);
        track_estop(t_ms);
        fsm_.tick(t_ms);
    }

    // ------------- test/simulator hook: bypass DSP, feed FSM edges -------------
    void inject_edge(int ch, bool down, int32_t amp, int32_t dur_ms, int64_t t_ms) {
        if (ch < 1 || ch > 4) return;
        ChanDsp& c = ch_[ch - 1];
        if (down) { c.env = std::max(c.env, (double)amp); c.active = true; c.down_amp = amp; }
        else { c.env = 0.0; c.active = false; }
        fsm_.edge(ch, down, amp, dur_ms, t_ms);
        if (down) emit_evt(ch, 'd', amp, 0, t_ms, 0);
        else emit_evt(ch, 'u', amp, dur_ms, t_ms, 0);
        fsm_.tick(t_ms);
    }
    void inject_night(bool on, int64_t t_ms) { night(on, t_ms); }
    void inject_sos_cancel(int64_t t_ms) { sos_cancel(t_ms); }
    void inject_set_mode(int mode, int64_t t_ms) { set_mode(mode, t_ms); }
    void tick(int64_t t_ms) { fsm_.tick(t_ms); }

    const std::vector<OutEvent>& events() const { return out_; }
    void drain() { out_.clear(); }

    std::array<double, 4> envelopes() const {
        std::array<double, 4> e{};
        for (int i = 0; i < 4; ++i) e[i] = ch_[i].env;
        return e;
    }
    std::array<bool, 4> active() const {
        std::array<bool, 4> a{};
        for (int i = 0; i < 4; ++i) a[i] = ch_[i].active;
        return a;
    }
    int veto_count() const { return veto_count_; }
    int burst_count() const { return burst_count_; }
    int drop_count() const { return drop_count_; }
    bool night() const { return fsm_.night(); }
    GestureFsm::St fsm_state() const { return fsm_.state(); }
    int64_t xt_until() const { return xt_until_; }
    int pending_onsets() const {
        int n = 0;
        for (auto& c : ch_) if (c.pending_onset) n++;
        return n;
    }

private:
    ModeCfg cfg_{};
    int mode_ = 0;
    std::vector<OutEvent> out_;   // declared before fsm_: fsm_ emits into it
    GestureFsm fsm_;
    std::array<ChanDsp, 4> ch_{};
    std::array<double, 4> thr_on_ = {k::kThresholdDefaultOn[0], k::kThresholdDefaultOn[1],
                                     k::kThresholdDefaultOn[2], k::kThresholdDefaultOn[3]};
    std::array<double, 4> thr_off_ = {k::kThresholdDefaultOn[0] * k::kThresholdOffRatio,
                                      k::kThresholdDefaultOn[1] * k::kThresholdOffRatio,
                                      k::kThresholdDefaultOn[2] * k::kThresholdOffRatio,
                                      k::kThresholdDefaultOn[3] * k::kThresholdOffRatio};
    int64_t xt_until_ = -1;
    int64_t estop_since_ = -1;
    bool estop_fired_ = false;
    int veto_count_ = 0, burst_count_ = 0, drop_count_ = 0;
    // noise-burst window state (CH1-3)
    bool burst_open_ = false;
    int64_t burst_t0_ = 0;

    void emit_evt(int ch, char ty, int32_t a, int32_t d, int64_t t, int32_t veto) {
        OutEvent e{};
        e.kind = ty == 'd' ? OUT_EVT_DOWN : (veto ? OUT_EVT_VETO : OUT_EVT_UP);
        e.t_ms = t; e.ch = ch; e.ty = ty; e.a = a; e.d = d; e.veto = veto;
        out_.push_back(e);
    }
    void emit_g(GesId g, int64_t t, int32_t a, int32_t d) {
        OutEvent e{}; e.kind = OUT_GES; e.t_ms = t; e.ch = -1; e.a = a; e.d = d;
        e.m = mode_; e.g = g; out_.push_back(e);
    }

    // ---------------- onset ----------------
    void try_onset(int ch, ChanDsp& c, int64_t t) {
        if (ch == 1 || ch == 2) {
            if (cfg_.fast_events) {
                // M0: emit immediately; CH4 hard-cross within 20 ms retro-cancels
                c.active = true; c.down_t = t;
                c.down_amp = static_cast<int32_t>(c.env_peak);
                c.env_peak = 0; c.last_down_t = t;
                emit_evt(ch, 'd', c.down_amp, 0, t, 0);
                fsm_.edge(ch, true, c.down_amp, 0, t);
                return;
            }
            // non-fast: hold onset for xt_lookahead so CH4 can veto first.
            // Guard: over_count keeps rising while env stays high, so without
            // this the emit deadline would be re-armed every sample and the
            // onset could never flush.
            if (c.pending_onset) return;
            c.pending_onset = true; c.pending_t = t; c.pending_env = c.env;
            c.pending_emit_at = t + cfg_.xt_lookahead_ms;
            return;
        }
        // CH3 exempt from crosstalk; CH4 always immediate
        c.active = true; c.down_t = t;
        c.down_amp = static_cast<int32_t>(c.env_peak);
        c.env_peak = 0;
        emit_evt(ch, 'd', c.down_amp, 0, t, 0);
        fsm_.edge(ch, true, c.down_amp, 0, t);
    }

    void onset_release(int ch, ChanDsp& c, int64_t t) {
        c.pending_onset = false;
        if (!c.active) return;
        int32_t dur = static_cast<int32_t>(t - c.down_t);
        int32_t amp = c.down_amp;
        c.active = false;
        emit_evt(ch, 'u', amp, dur, t, 0);
        fsm_.edge(ch, false, amp, dur, t);
        c.down_t = -1; c.env_peak = 0; c.last_down_t = -1;
    }

    void flush_pending_onsets(int64_t t) {
        for (int ch = 1; ch <= 2; ++ch) {
            ChanDsp& c = ch_[ch - 1];
            if (!c.pending_onset || t < c.pending_emit_at) continue;
            c.pending_onset = false;
            if (c.env < thr_on_[ch - 1]) continue;                    // fizzled
            // A deliberate multi-channel chord (>=3 channels simultaneously
            // above the chord ratio, e.g. the M3 E-STOP grip) is genuine
            // activity, not crosstalk - do not suppress it.
            int chord_high = 0;
            for (int i = 0; i < 4; ++i)
                if (ch_[i].env >= k::kEstopChordEnvRatio * thr_on_[i]) chord_high++;
            bool suppressed = chord_high < 3 && t < xt_until_ &&
                c.env < k::kCrosstalkGenuineOverrideRatio * ch_[3].env;
            if (suppressed) { veto_count_++; continue; }
            c.active = true; c.down_t = c.pending_t;
            c.down_amp = static_cast<int32_t>(c.pending_env);
            c.env_peak = 0;
            emit_evt(ch, 'd', c.down_amp, 0, c.pending_t, 0);
            fsm_.edge(ch, true, c.down_amp, 0, c.pending_t);
        }
    }

    // ---------------- crosstalk ----------------
    void track_xtalk(int ch, ChanDsp& c, int64_t t) {
        if (ch != 4) return;
        if (c.env >= k::kCrosstalkHardRatio * thr_on_[3]) {
            xt_until_ = t + k::kCrosstalkSuppressMs;
            if (cfg_.fast_events) {
                // retro-cancel CH1/2 downs emitted within the lookback window
                for (int c12 = 1; c12 <= 2; ++c12) {
                    ChanDsp& o = ch_[c12 - 1];
                    if (o.active && o.last_down_t >= 0 &&
                        t - o.last_down_t <= k::kDebounceRetroCancelMs) {
                        veto_count_++;
                        int32_t dur = static_cast<int32_t>(t - o.down_t);
                        emit_evt(c12, 'u', o.down_amp, dur, t, 1);
                        fsm_.edge(c12, false, o.down_amp, dur, t);
                        o.active = false; o.last_down_t = -1; o.down_t = -1;
                    }
                }
            }
        }
    }

    // ---------------- impact / noise burst ----------------
    // CH1-3 all rising within burst_window_ms and the whole event < burst_max_ms
    // with CH4 not dominant -> NOISE_BURST; pending onsets are dropped and any
    // already-emitted downs get a matching up so no key is left held.
    void track_noise_burst(int ch, ChanDsp& c, int64_t t) {
        if (ch < 1 || ch > 3) return;
        double thr = thr_on_[ch - 1];
        if (c.env >= thr && !c.burst_risen) {
            c.burst_risen = true;
            c.burst_rise_t = t;
            if (!burst_open_) { burst_open_ = true; burst_t0_ = t; }
        }
        if (c.env < 0.5 * thr) { c.burst_risen = false; }
        if (!burst_open_) return;
        // all three risen?
        int risen = 0; int64_t lo = 0, hi = 0;
        for (int i = 0; i < 3; ++i) {
            if (ch_[i].burst_risen) {
                risen++;
                if (lo == 0 || ch_[i].burst_rise_t < lo) lo = ch_[i].burst_rise_t;
                if (ch_[i].burst_rise_t > hi) hi = ch_[i].burst_rise_t;
            }
        }
        if (risen < 3 || hi - lo > k::kCrosstalkBurstWindowMs) {
            if (t - burst_t0_ > k::kCrosstalkBurstMaxMs) burst_open_ = false;
            return;
        }
        // candidate window: CH4 dominant means a real chord attempt, not noise
        double env4 = ch_[3].env;
        double env123 = std::max({ch_[0].env, ch_[1].env, ch_[2].env});
        if (env4 >= env123 && env4 >= thr_on_[3]) return;    // CH4 dominant -> not burst
        // burst ends when the raw signal settles on all three (dev ~ 0) or the
        // window exceeds burst_max. dev is used rather than env: the envelope
        // release constant (~66 ms tau) is far slower than burst_max_ms.
        bool all_down = true;
        for (int i = 0; i < 3; ++i)
            if (ch_[i].dev >= 0.5 * thr_on_[i]) all_down = false;
        int64_t dur = t - burst_t0_;
        if (all_down && dur < k::kCrosstalkBurstMaxMs) {
            burst_count_++;
            burst_open_ = false;
            emit_g(G_NOISE_BURST, t, 0, static_cast<int32_t>(dur));
            for (int i = 0; i < 3; ++i) {
                ChanDsp& o = ch_[i];
                o.pending_onset = false;
                if (o.active) onset_release(i + 1, o, t);   // safe: matching up
                o.burst_risen = false;
            }
        } else if (dur >= k::kCrosstalkBurstMaxMs) {
            burst_open_ = false;                            // real sustained event
        }
    }

    // ---------------- E-STOP chord (mode 3 only) ----------------
    // All four channels above chord_env_ratio*thr_on, CH4 dominant, sustained
    // >= chord_sustain_ms -> ESTOP once. Sub-40 ms impacts never reach 150 ms.
    void track_estop(int64_t t) {
        if (!cfg_.estop) { estop_since_ = -1; estop_fired_ = false; return; }
        bool all = true;
        for (int i = 0; i < 4; ++i) {
            if (!ch_[i].active || ch_[i].env < k::kEstopChordEnvRatio * thr_on_[i]) {
                all = false; break;
            }
        }
        if (!all) { estop_since_ = -1; estop_fired_ = false; return; }
        // CH4 must dominate
        for (int i = 0; i < 3; ++i)
            if (ch_[3].env < ch_[i].env) { estop_since_ = -1; estop_fired_ = false; return; }
        if (estop_since_ < 0) estop_since_ = t;
        int64_t dur = t - estop_since_;
        if (!estop_fired_ && dur >= k::kEstopChordSustainMs) {
            estop_fired_ = true;
            emit_g(G_ESTOP, t, static_cast<int32_t>(ch_[3].env), static_cast<int32_t>(dur));
        }
    }
};

// ================================================================ event queue
// Bounded dsp->others queue (depth 32 per spec). The firmware wraps this policy
// around a FreeRTOS queue; this portable version is what native tests exercise.
// Drop policy: when full, drop the OLDEST non-critical item; ESTOP, SOS_* and
// MODE_SET are critical and are never dropped to make room - a critical push
// into an all-critical full queue is itself counted as a drop (unreachable in
// practice: critical events are rare and self-limiting).
class EventQueue {
public:
    static constexpr size_t CAP = 32;

    static bool is_critical(const OutEvent& e) {
        if (e.kind == OUT_MODE_SET) return true;
        if (e.kind != OUT_GES) return false;
        return e.g == G_ESTOP || e.g == G_SOS_PENDING || e.g == G_SOS_CONFIRMED ||
               e.g == G_SOS_CANCELLED || e.g == G_PICKER_OPEN;
    }

    bool push(const OutEvent& e) {
        if (size_ < CAP) {
            buf_[(head_ + size_) % CAP] = e;
            size_++;
            return true;
        }
        // full: find oldest non-critical slot and overwrite it
        for (size_t i = 0; i < size_; ++i) {
            size_t idx = (head_ + i) % CAP;   // i=0 is oldest
            if (!is_critical(buf_[idx])) {
                buf_[idx] = e;
                drops_++;
                return true;
            }
        }
        drops_++;          // all-critical full queue: cannot happen in practice
        return false;
    }

    bool pop(OutEvent& e) {
        if (size_ == 0) return false;
        e = buf_[head_];
        head_ = (head_ + 1) % CAP;
        size_--;
        return true;
    }
    size_t size() const { return size_; }
    uint64_t drops() const { return drops_; }

private:
    std::array<OutEvent, CAP> buf_{};
    size_t head_ = 0, size_ = 0;
    uint64_t drops_ = 0;
};

}  // namespace omniband
