// test_core.cpp - native host-side tests for omniband_core.h (no Arduino, no
// hardware). Builds with: g++ -std=c++17 -Wall -Wextra -Werror -I<firmware dir>
// Run from repo root via tools/run_native_tests.py.
//
// Covers: shared gesture_vectors.json parity, baseline gate, hysteresis,
// debounce fast/normal + retro-cancel, crosstalk veto/lookahead/override,
// noise-burst rejection, E-STOP vs impact, night bruxism + exit chord,
// mode-policy enforcement, EventQueue drop policy, int64 timestamps.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "omniband_core.h"

using namespace omniband;

static int failures = 0;
static int checks = 0;

#define CHECK(cond, msg) do { ++checks; if (!(cond)) { ++failures; \
    std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, msg); } } while (0)

// ------------------------------------------------------------------ tiny JSON
// Minimal parser for the vector file shape: objects/arrays/strings/numbers/bools.
struct J {
    enum T { NUL, NUM, STR, BOOL, ARR, OBJ } t = NUL;
    double num = 0; bool b = false; std::string s;
    std::vector<J> a; std::vector<std::pair<std::string, J>> o;
    const J* find(const char* k) const {
        for (auto& p : o) if (p.first == k) return &p.second;
        return nullptr;
    }
    double num_or(double d) const { return t == NUM ? num : d; }
};

struct P {
    const char* p;
    void ws() { while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') ++p; }
    bool eat(char c) { ws(); if (*p == c) { ++p; return true; } return false; }
    J val() {
        ws();
        if (*p == '{') return obj();
        if (*p == '[') return arr();
        if (*p == '"') { J j; j.t = J::STR; j.s = str(); return j; }
        if (!std::strncmp(p, "true", 4)) { p += 4; J j; j.t = J::BOOL; j.b = true; return j; }
        if (!std::strncmp(p, "false", 5)) { p += 5; J j; j.t = J::BOOL; return j; }
        if (!std::strncmp(p, "null", 4)) { p += 4; return J(); }
        return num();
    }
    std::string str() {
        ++p; std::string r;
        while (*p && *p != '"') {
            if (*p == '\\') { ++p; if (*p == 'n') r += '\n'; else if (*p == 't') r += '\t'; else r += *p; }
            else r += *p;
            ++p;
        }
        if (*p == '"') ++p;
        return r;
    }
    J num() {
        J j; j.t = J::NUM; j.num = std::strtod(p, (char**)&p); return j;
    }
    J arr() {
        J j; j.t = J::ARR; ++p; ws();
        if (eat(']')) return j;
        while (true) { j.a.push_back(val()); if (eat(']')) break; eat(','); }
        return j;
    }
    J obj() {
        J j; j.t = J::OBJ; ++p; ws();
        if (eat('}')) return j;
        while (true) {
            ws(); std::string k = str(); eat(':');
            j.o.push_back({k, val()});
            if (eat('}')) break; eat(',');
        }
        return j;
    }
};

// ------------------------------------------------------------------ vector run
struct Step { int64_t t; int ch; bool down; int32_t amp, dur; std::string cmd; int mode; bool on; };
struct Expect { int64_t t; std::string g; int32_t d; };

static std::vector<J> load_vectors(const char* path, J& root) {
    std::ifstream f(path);
    if (!f) { std::fprintf(stderr, "cannot open %s\n", path); std::exit(2); }
    std::stringstream ss; ss << f.rdbuf();
    std::string txt = ss.str();
    P p{txt.c_str()};
    root = p.val();
    const J* vs = root.find("vectors");
    if (!vs) return {};
    return vs->a;
}

static void run_vector(const J& v) {
    int mode = (int)v.find("mode")->num;
    Core core(mode);
    std::vector<Step> steps;
    std::vector<Expect> expects;
    int64_t t_end = 0;      // max step time
    int64_t t_expect = 0;   // max expected gesture time
    for (const J& in : v.find("in")->a) {
        Step s{}; s.t = (int64_t)in.find("t")->num;
        if (const J* c = in.find("cmd")) {
            s.cmd = c->s;
            if (const J* m = in.find("mode")) s.mode = (int)m->num;
            if (const J* o = in.find("on")) s.on = o->b;
        } else {
            s.ch = (int)in.find("ch")->num;
            s.down = in.find("down")->b;
            s.amp = (int32_t)in.find("a")->num;
            if (const J* d = in.find("d")) s.dur = (int32_t)d->num;
        }
        steps.push_back(s);
        t_end = std::max(t_end, s.t);
    }
    for (const J& e : v.find("expect")->a) {
        Expect x{}; x.t = (int64_t)e.find("t")->num;
        x.g = e.find("g")->s;
        if (const J* d = e.find("d")) x.d = (int32_t)d->num;
        expects.push_back(x);
        t_expect = std::max(t_expect, x.t);
    }
    t_end = std::max(t_end + 700, t_expect + 50);   // let timers (gap/sos/picker/bruxism) fire

    // merge-sort steps by t (stable)
    std::stable_sort(steps.begin(), steps.end(), [](const Step&a, const Step&b){return a.t<b.t;});
    size_t si = 0;
    std::vector<OutEvent> ges;
    for (int64_t t = 0; t <= t_end; ++t) {
        while (si < steps.size() && steps[si].t == t) {
            const Step& s = steps[si++];
            if (!s.cmd.empty()) {
                if (s.cmd == "set_mode") core.inject_set_mode(s.mode, t);
                else if (s.cmd == "night") core.inject_night(s.on, t);
                else if (s.cmd == "sos_cancel") core.inject_sos_cancel(t);
            } else {
                core.inject_edge(s.ch, s.down, s.amp, s.dur, t);
            }
        }
        core.tick(t);
        for (const OutEvent& e : core.events())
            if (e.kind == OUT_GES || e.kind == OUT_MODE_SET) ges.push_back(e);
        core.drain();
    }
    // verify: every expected gesture produced at exact t (kind ges) ; nothing extra
    std::vector<bool> got(expects.size(), false);
    for (const OutEvent& e : ges) {
        const char* name = e.kind == OUT_MODE_SET ? "MODE_SET" : ges_name(e.g);
        for (size_t i = 0; i < expects.size(); ++i) {
            if (!got[i] && expects[i].t == e.t_ms && expects[i].g == name &&
                (expects[i].d == 0 || expects[i].d == e.d)) {
                got[i] = true; break;
            }
        }
    }
    for (size_t i = 0; i < expects.size(); ++i) {
        CHECK(got[i], (std::string("vector '") + v.find("name")->s + "' missing ges " +
                       expects[i].g + " @" + std::to_string(expects[i].t)).c_str());
    }
    // unexpected gestures?
    for (const OutEvent& e : ges) {
        const char* name = e.kind == OUT_MODE_SET ? "MODE_SET" : ges_name(e.g);
        bool expected = false;
        for (auto& x : expects)
            if (x.t == e.t_ms && x.g == name && (x.d == 0 || x.d == e.d)) expected = true;
        CHECK(expected, (std::string("vector '") + v.find("name")->s + "' unexpected ges " +
                         name + " @" + std::to_string(e.t_ms)).c_str());
    }
}

// ------------------------------------------------------------------ DSP helpers
// Synthetic piezo: quiet baseline B, press = B+AMP counts, release = B.
struct Sim {
    Core core;
    int ch;
    int32_t base, amp;
    Sim(int mode, int c, int32_t b = 512, int32_t a = 700)
        : core(mode), ch(c), base(b), amp(a) {}
    void quiet(int64_t& t, int ms) { for (int i = 0; i < ms; ++i, t += 2) core.feed_sample(ch, base, t); }
    void press(int64_t& t, int ms) { for (int i = 0; i < ms / 2; ++i, t += 2) core.feed_sample(ch, base + amp, t); }
    void release(int64_t& t, int ms) { for (int i = 0; i < ms / 2; ++i, t += 2) core.feed_sample(ch, base, t); }
    std::vector<OutEvent> drain() { auto e = core.events(); core.drain(); return e; }
};

static int count_kind(const std::vector<OutEvent>& ev, OutKind k) {
    int n = 0; for (auto& e : ev) if (e.kind == k) n++; return n;
}
static const OutEvent* find_ges(const std::vector<OutEvent>& ev, GesId g) {
    for (auto& e : ev) if (e.kind == OUT_GES && e.g == g) return &e;
    return nullptr;
}

// ------------------------------------------------------------------ tests
static void test_baseline_gate() {
    // 3 s sustained clench must NOT be absorbed by the baseline (gated EMA).
    // Without the gate, env decays below thr_off and hold detection fails.
    Sim s(0, 4);
    int64_t t = 0;
    s.quiet(t, 1200);                       // boot quiet -> baseline ~512
    s.press(t, 3000);                       // 3 s "clench" bursty? use constant+noise
    auto env = s.core.envelopes();
    CHECK(env[3] >= 200.0, "baseline gate: env should stay high during 3s press");
    CHECK(s.core.active()[3], "baseline gate: ch4 should still be active after 3 s");
    s.release(t, 200);
}

static void test_hysteresis() {
    // env between thr_off and thr_on keeps prior state (no chatter)
    Sim s(1, 4, 512, 400);
    int64_t t = 0;
    s.quiet(t, 500);
    // partial press: amp 400 -> env ~400 > thr_on 260 -> active
    s.press(t, 100);
    CHECK(s.core.active()[3], "hysteresis: should be active above thr_on");
    // drop to mid-level: env decays; feed mid signal env~150 (between off=156? off=156, on=260)
    // thr_off = 156; feed amp giving env ~180 (between) -> state must persist
    for (int i = 0; i < 60; ++i, t += 2) s.core.feed_sample(4, 512 + 180, t);
    CHECK(s.core.active()[3], "hysteresis: mid-level env must hold active state");
    s.release(t, 300);
    CHECK(!s.core.active()[3], "hysteresis: release below thr_off must deactivate");
}

static void test_debounce_modes() {
    // normal mode (M1): single-sample blip below debounce must not emit down
    {
        Sim s(1, 1, 512, 800);
        int64_t t = 0; s.quiet(t, 400);
        s.core.feed_sample(1, 512 + 800, t); t += 2;   // 1 sample over
        s.core.feed_sample(1, 512, t); t += 2;
        auto ev = s.drain();
        CHECK(count_kind(ev, OUT_EVT_DOWN) == 0, "normal debounce: 1-sample blip must not emit down");
    }
    // fast mode (M0): same blip emits down then immediate up (tap)
    {
        Sim s(0, 1, 512, 800);
        int64_t t = 0; s.quiet(t, 400);
        s.core.feed_sample(1, 512 + 800, t); t += 2;
        auto ev1 = s.drain();
        CHECK(count_kind(ev1, OUT_EVT_DOWN) == 1, "fast debounce: first sample emits down");
        // envelope release tau ~66 ms: feed enough quiet samples to fall below thr_off
        for (int i = 0; i < 90; ++i, t += 2) s.core.feed_sample(1, 512, t);
        auto ev2 = s.drain();
        CHECK(count_kind(ev2, OUT_EVT_UP) == 1, "fast debounce: fall below off emits up");
    }
}

static void test_crosstalk_retro_cancel() {
    // M0 fast: CH1 down emitted, CH4 hard-cross within 20 ms -> veto up
    Sim s(0, 1, 512, 800);
    int64_t t = 0;
    // quiet ALL channels so every baseline is established (Sim only feeds ch1)
    for (int i = 0; i < 200; ++i, t += 2) {
        s.core.feed_sample(1, 512, t); s.core.feed_sample(2, 512, t);
        s.core.feed_sample(3, 512, t); s.core.feed_sample(4, 512, t);
    }
    s.drain();
    // CH1 press (2 samples to be safe even in fast mode)
    for (int i = 0; i < 4; ++i, t += 2) s.core.feed_sample(1, 512 + 800, t);
    auto ev1 = s.drain();
    CHECK(count_kind(ev1, OUT_EVT_DOWN) == 1, "xtalk: ch1 down emitted in fast mode");
    // CH4 slams hard 10 ms later
    for (int i = 0; i < 4; ++i, t += 2) s.core.feed_sample(4, 512 + 1500, t);
    auto ev2 = s.drain();
    int vetoes = 0;
    for (auto& e : ev2) if (e.kind == OUT_EVT_VETO && e.ch == 1) vetoes++;
    CHECK(vetoes == 1, "xtalk: ch1 down must be retro-cancelled with veto");
    CHECK(s.core.veto_count() >= 1, "xtalk: veto counter incremented");
}

static void test_crosstalk_lookahead_veto() {
    // M1 non-fast: CH1 onset held 10 ms; CH4 crosses hard inside window -> veto
    Sim s4(1, 4, 512, 1500);
    int64_t t = 0;
    // quiet both channels first (Sim only feeds one channel; feed ch4 quiet manually)
    for (int i = 0; i < 200; ++i, t += 2) { s4.core.feed_sample(4, 512, t); s4.core.feed_sample(1, 512, t); }
    // CH1 rises (needs 2 consecutive over thr)
    for (int i = 0; i < 3; ++i, t += 2) { s4.core.feed_sample(1, 512 + 800, t); s4.core.feed_sample(4, 512, t); }
    // within lookahead (10 ms), CH4 hard cross
    for (int i = 0; i < 3; ++i, t += 2) { s4.core.feed_sample(4, 512 + 1500, t); s4.core.feed_sample(1, 512 + 800, t); }
    // run out the lookahead window
    for (int i = 0; i < 20; ++i, t += 2) { s4.core.feed_sample(1, 512 + 800, t); s4.core.feed_sample(4, 512 + 1500, t); }
    auto ev = s4.drain();
    int d1 = 0;
    for (auto& e : ev) if (e.kind == OUT_EVT_DOWN && e.ch == 1) d1++;
    // ch1 down may or may not appear depending on genuine-override; env1=800 vs 1.5*env4=2250 -> suppressed
    CHECK(d1 == 0, "xtalk lookahead: ch1 onset must be vetoed by CH4 hard cross");
    CHECK(s4.core.veto_count() >= 1, "xtalk lookahead: veto counted");
}

static void test_noise_burst() {
    // M3: CH1-3 rise together <6 ms, event <40 ms, CH4 quiet -> NOISE_BURST ges
    Core core(3);
    int64_t t = 0;
    for (int i = 0; i < 300; ++i, t += 2) {
        core.feed_sample(1, 512, t); core.feed_sample(2, 512, t);
        core.feed_sample(3, 512, t); core.feed_sample(4, 512, t);
    }
    core.drain();
    // all three rise within ~4 ms (2 samples each)
    for (int i = 0; i < 3; ++i, t += 2) {
        core.feed_sample(1, 512 + 800, t);
        core.feed_sample(2, 512 + 800, t);
        core.feed_sample(3, 512 + 800, t);
        core.feed_sample(4, 512, t);
    }
    for (int i = 0; i < 8; ++i, t += 2) {   // ~16 ms total < 40 ms
        core.feed_sample(1, 512, t); core.feed_sample(2, 512, t);
        core.feed_sample(3, 512, t); core.feed_sample(4, 512, t);
    }
    for (int i = 0; i < 30; ++i, t += 2) {
        core.feed_sample(1, 512, t); core.feed_sample(2, 512, t);
        core.feed_sample(3, 512, t); core.feed_sample(4, 512, t);
    }
    auto ev = core.events();
    CHECK(find_ges(ev, G_NOISE_BURST) != nullptr, "noise burst: NOISE_BURST gesture emitted");
    CHECK(core.burst_count() >= 1, "noise burst: counter incremented");
    core.drain();
}

static void test_estop_vs_impact() {
    // sustained chord >=150 ms in M3 -> ESTOP; short impact -> nothing
    {
        Core core(3);
        int64_t t = 0;
        for (int i = 0; i < 200; ++i, t += 2) {
            core.feed_sample(1, 512, t); core.feed_sample(2, 512, t);
            core.feed_sample(3, 512, t); core.feed_sample(4, 512, t);
        }
        core.drain();
        for (int i = 0; i < 100; ++i, t += 2) {   // 200 ms sustained chord
            core.feed_sample(1, 512 + 800, t); core.feed_sample(2, 512 + 800, t);
            core.feed_sample(3, 512 + 800, t); core.feed_sample(4, 512 + 1600, t);
        }
        auto ev = core.events();
        CHECK(find_ges(ev, G_ESTOP) != nullptr, "estop: sustained chord emits ESTOP");
    }
    {
        Core core(3);
        int64_t t = 0;
        for (int i = 0; i < 200; ++i, t += 2) {
            core.feed_sample(1, 512, t); core.feed_sample(2, 512, t);
            core.feed_sample(3, 512, t); core.feed_sample(4, 512, t);
        }
        core.drain();
        for (int i = 0; i < 10; ++i, t += 2) {    // 20 ms impact
            core.feed_sample(1, 512 + 800, t); core.feed_sample(2, 512 + 800, t);
            core.feed_sample(3, 512 + 800, t); core.feed_sample(4, 512 + 1600, t);
        }
        for (int i = 0; i < 30; ++i, t += 2) {
            core.feed_sample(1, 512, t); core.feed_sample(2, 512, t);
            core.feed_sample(3, 512, t); core.feed_sample(4, 512, t);
        }
        auto ev = core.events();
        CHECK(find_ges(ev, G_ESTOP) == nullptr, "estop: 20 ms impact must NOT emit ESTOP");
    }
}

static void test_mode_policy() {
    // M4: long_hold disabled -> no LONG, no picker via hold
    {
        Core core(4);
        int64_t t = 100;
        for (int i = 0; i < 2000; ++i) { core.tick(t); t += 2; }
        core.inject_edge(4, true, 900, 0, 100);
        for (int i = 0; i < 2000; ++i) { core.tick(t); t += 2; }  // 4 s held
        core.inject_edge(4, false, 900, 4000, t);
        auto ev = core.events();
        CHECK(find_ges(ev, G_LONG) == nullptr, "policy M4: LONG must be disabled");
        CHECK(find_ges(ev, G_PICKER_OPEN) == nullptr, "policy M4: no picker without LONG");
    }
    // M0: SOS disabled -> TRIPLE is plain gesture (no SOS_PENDING)
    {
        Core core(0);
        int downs[] = {100, 480, 860}, ups[] = {280, 660, 1040};
        for (int k = 0; k < 3; ++k) {
            core.inject_edge(4, true, 950, 0, downs[k]);
            for (int64_t u = downs[k]; u < ups[k]; ++u) core.tick(u);
            core.inject_edge(4, false, 950, (int32_t)(ups[k] - downs[k]), ups[k]);
        }
        for (int64_t u = 1040; u < 5000; ++u) core.tick(u);
        auto ev = core.events();
        CHECK(find_ges(ev, G_TRIPLE) != nullptr, "policy M0: TRIPLE emitted as plain gesture");
        CHECK(find_ges(ev, G_SOS_PENDING) == nullptr, "policy M0: SOS must be disabled");
    }
}

static void test_queue_drop_policy() {
    EventQueue q;
    OutEvent filler{}; filler.kind = OUT_EVT_DOWN; filler.ch = 1; filler.ty = 'd';
    for (int i = 0; i < 32; ++i) q.push(filler);
    CHECK(q.size() == 32, "queue: fills to capacity");
    // push 40 non-critical -> oldest dropped, count == 40
    for (int i = 0; i < 40; ++i) q.push(filler);
    CHECK(q.drops() == 40, "queue: non-critical overflow counted");
    // now push critical events - they must all land
    OutEvent crit{}; crit.kind = OUT_GES; crit.g = G_ESTOP; crit.ch = -1;
    for (int i = 0; i < 10; ++i) CHECK(q.push(crit), "queue: critical push accepted");
    // drain and verify no critical was dropped: count ESTOPs present
    int estops = 0; OutEvent e;
    EventQueue q2;
    for (int i = 0; i < 32; ++i) q2.push(filler);
    for (int i = 0; i < 10; ++i) q2.push(crit);
    while (q2.pop(e)) if (e.g == G_ESTOP) estops++;
    CHECK(estops == 10, "queue: all critical events survive overflow");
}

static void test_int64_timestamps() {
    // simulate ~80 min at 500 Hz -> t_ms up to 4.8e6; ensure no wrap/truncation
    Core core(0);
    int64_t t = 0;
    const int64_t end = 80LL * 60 * 1000;   // 80 min in ms
    int64_t last_ges_t = -1;
    // sparse activity so the run is fast: tick every 2 ms but only feed ch4 quiet
    for (int64_t tt = 0; tt < end; tt += 2) {
        core.feed_sample(4, 512, tt);
        core.tick(tt);
        t = tt;
    }
    (void)t; (void)last_ges_t;
    // inject an edge at the far-future timestamp and confirm t_ms is exact
    core.inject_edge(4, true, 900, 0, end);
    core.inject_edge(4, false, 900, 100, end + 100);
    bool found = false;
    for (auto& e : core.events()) if (e.t_ms == end + 100) found = true;
    CHECK(found, "int64: timestamp at 80 min must be exact, no wrap");
}

static void test_gesture_vectors(const char* path) {
    J root;
    auto vecs = load_vectors(path, root);
    CHECK(!vecs.empty(), "vectors: file loaded");
    for (const J& v : vecs) run_vector(v);
    std::printf("vectors: %zu cases run\n", vecs.size());
}

int main(int argc, char** argv) {
    const char* vec = argc > 1 ? argv[1] : "tests/vectors/gesture_vectors.json";
    test_baseline_gate();
    test_hysteresis();
    test_debounce_modes();
    test_crosstalk_retro_cancel();
    test_crosstalk_lookahead_veto();
    test_noise_burst();
    test_estop_vs_impact();
    test_mode_policy();
    test_queue_drop_policy();
    test_int64_timestamps();
    test_gesture_vectors(vec);
    std::printf("native tests: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
