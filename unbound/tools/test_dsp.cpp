// test_dsp.cpp - PC-side unit tests for omniband_dsp.h (no Arduino needed)
// Build & run:
//   g++ -std=c++11 -O2 -Wall -Wextra -I../esp32_omniband_4channel test_dsp.cpp -o test_dsp && ./test_dsp
// (-std=c++11 on purpose: the oldest Arduino-ESP32 2.0.x toolchains default to gnu++11.)
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <vector>
#include "omniband_dsp.h"

static int g_fail = 0;
#define CHECK(cond, msg)                                              \
  do {                                                                \
    if (!(cond)) { std::printf("  FAIL: %s  (%s:%d)\n", msg, __FILE__, __LINE__); ++g_fail; } \
    else { std::printf("  ok  : %s\n", msg); }                        \
  } while (0)

// Small deterministic noise source (uniform in [-amp, +amp]).
struct Rng {
  uint32_t s = 12345u;
  float uni(float amp) {
    s = s * 1664525u + 1013904223u;
    return (((s >> 8) & 0xFFFF) / 32767.5f - 1.0f) * amp;
  }
};

struct Rig {
  omni::OmniDsp dsp;
  Rng rng;
  uint32_t i = 0;                      // sample index; t = i * 2000 us (500 Hz)
  float base = 100.0f, noise = 3.0f;
  Rig() { dsp.begin(1000); }

  // Advance one sample with optional additive offsets per channel.
  omni::Frame step(float l = 0, float r = 0, float f = 0, float p = 0) {
    omni::Sample s;
    s.left = base + rng.uni(noise) + l;
    s.right = base + rng.uni(noise) + r;
    s.fwd = base + rng.uni(noise) + f;
    s.piezo = base + rng.uni(noise) + p;
    s.t_us = i * 2000u;
    ++i;
    return dsp.process(s);
  }
  void calibrate() { for (int k = 0; k < 1000; ++k) step(); }
};

static void test_quiet_noise() {
  std::printf("[1] noise only: no false triggers, activity stays ~0\n");
  Rig r; r.calibrate();
  int rising = 0; float mx = 0;
  for (int k = 0; k < 5000; ++k) {
    omni::Frame f = r.step();
    if (f.clench_rising) ++rising;
    mx = std::fmax(mx, std::fmax(f.left, std::fmax(f.right, f.fwd)));
  }
  CHECK(rising == 0, "no clench in 10 s of noise");
  CHECK(mx < 0.05f, "activity channels < 0.05 in noise");
}

static void test_calibration_flag() {
  std::printf("[2] calibration window\n");
  Rig r;
  omni::Frame f = r.step();
  CHECK(f.calibrating, "first frame is calibrating");
  for (int k = 0; k < 999; ++k) f = r.step();
  CHECK(!r.step().calibrating, "calibration ends after 1000 samples");
  r.dsp.recalibrate();
  CHECK(r.step().calibrating, "recalibrate() restarts calibration");
}

static void test_drift() {
  std::printf("[3] slow drift (+20 counts in 10 s) must not trigger\n");
  Rig r; r.calibrate();
  int rising = 0; float mx = 0;
  for (int k = 0; k < 5000; ++k) {
    float d = 0.004f * k;
    omni::Frame f = r.step(d, d, d, d);
    if (f.clench_rising) ++rising;
    mx = std::fmax(mx, std::fmax(f.left, std::fmax(f.right, f.fwd)));
  }
  CHECK(rising == 0, "no clench from drift");
  CHECK(mx < 0.15f, "drift does not look like activity");
}

static void test_spike_and_refractory() {
  std::printf("[4] spike: instant edge, level hold, refractory, re-arm\n");
  Rig r; r.calibrate();
  for (int k = 0; k < 500; ++k) r.step();        // settle

  int rising = 0, first_rising_at = -1;
  bool level_ok = true;
  const int spike1 = 100, spike2 = 120, spike3 = 600;  // 2nd is 40 ms after 1st, 3rd is ~1 s later
  float amp1 = 300.0f;
  bool level_after_decay = true;
  for (int k = 0; k < 1000; ++k) {
    float p = 0;
    if (k >= spike1)            p += amp1 * std::exp(-(k - spike1) / 60.0f);
    if (k >= spike2 && k < spike3) p += 250.0f * std::exp(-(k - spike2) / 60.0f);
    if (k >= spike3)            p += 250.0f * std::exp(-(k - spike3) / 60.0f);
    omni::Frame f = r.step(0, 0, 0, p);
    if (f.clench_rising) { ++rising; if (first_rising_at < 0) first_rising_at = k; }
    if (k >= spike1 && k < spike1 + 20 && !f.clench) level_ok = false;   // >= 40 ms hold
    if (k == spike3 - 5 && f.clench) level_after_decay = false;          // released before 3rd spike
  }
  CHECK(first_rising_at == spike1, "rising edge fires on the very first sample over threshold (0 samples late)");
  CHECK(rising == 2, "exactly 2 rising edges (2nd spike inside the refractory is ignored)");
  CHECK(level_ok, "clench level stays high for >= 40 ms");
  CHECK(level_after_decay, "clench level released after the transient decays");
}

static void test_small_bump() {
  std::printf("[5] small bump (20 counts) must be ignored\n");
  Rig r; r.calibrate();
  for (int k = 0; k < 300; ++k) r.step();
  int rising = 0;
  for (int k = 0; k < 300; ++k) {
    float p = (k >= 50) ? 20.0f * std::exp(-(k - 50) / 40.0f) : 0.0f;
    if (r.step(0, 0, 0, p).clench_rising) ++rising;
  }
  CHECK(rising == 0, "20-count bump does not trigger");
}

static void test_activity_envelope() {
  std::printf("[6] activity channel: rises with vibration, releases when quiet, others unaffected\n");
  Rig r; r.calibrate();
  for (int k = 0; k < 500; ++k) r.step();
  omni::Frame f{};
  for (int k = 0; k < 500; ++k) f = r.step(r.rng.uni(80.0f), 0, 0, 0);   // 1 s of vibration on LEFT only
  CHECK(f.left > 0.5f, "left > 0.5 during 1 s of vibration");
  CHECK(f.right < 0.05f && f.fwd < 0.05f, "right/forward stay quiet");
  for (int k = 0; k < 1000; ++k) f = r.step();                           // 2 s quiet
  CHECK(f.left < 0.05f, "left falls back below 0.05 within 2 s");
}

static void test_piezo_bias_negative_swing() {
  std::printf("[7] negative-going spike (biased front end) also triggers\n");
  Rig r; r.base = 1600.0f; r.calibrate();
  for (int k = 0; k < 300; ++k) r.step();
  int rising = 0;
  for (int k = 0; k < 200; ++k) {
    float p = (k >= 20) ? -300.0f * std::exp(-(k - 20) / 60.0f) : 0.0f;
    if (r.step(0, 0, 0, p).clench_rising) ++rising;
  }
  CHECK(rising == 1, "one clench from a negative swing around a 1.6 V bias");
}

static void test_timestamp_wrap() {
  std::printf("[8] micros() wraparound does not break the refractory logic\n");
  omni::ClenchDetector d;
  uint32_t t = 0xFFFFFFFFu - 10 * 2000u;          // 10 samples before the 32-bit wrap
  int rising = 0;
  for (int k = 0; k < 600; ++k) {                  // calibrate across the wrap
    d.update(100.0f, t, true); t += 2000u;
  }
  for (int k = 0; k < 100; ++k) { d.update(100.0f, t, false); t += 2000u; }
  omni::ClenchDetector::Out o = d.update(500.0f, t, false); t += 2000u;
  if (o.rising) ++rising;
  for (int k = 0; k < 10; ++k) { o = d.update(500.0f, t, false); t += 2000u; if (o.rising) ++rising; }
  CHECK(rising == 1, "single edge across the timer wrap");
}

static void test_short_tap_hold_and_refractory() {
  std::printf("[9] 1-sample taps: minimum hold and 120 ms refractory are really enforced\n");
  Rig r; r.calibrate();
  for (int k = 0; k < 500; ++k) r.step();
  int rising = 0;
  bool hold_ok = true, released_before_third = false;
  // Taps are a single 400-count sample at k=10, k=50 (80 ms later) and k=90 (160 ms later).
  for (int k = 0; k < 200; ++k) {
    float p = (k == 10 || k == 50 || k == 90) ? 400.0f : 0.0f;
    omni::Frame f = r.step(0, 0, 0, p);
    if (f.clench_rising) ++rising;
    if (k >= 10 && k < 30 && !f.clench) hold_ok = false;      // 20 samples = 40 ms minimum hold
    if (k == 85 && !f.clench) released_before_third = true;   // level must have dropped again
  }
  CHECK(hold_ok, "a 1-sample tap still holds the level for >= 40 ms");
  CHECK(released_before_third, "level releases after a 1-sample tap");
  CHECK(rising == 2, "tap at +80 ms is swallowed by the 120 ms refractory, tap at +160 ms fires");
}

int main() {
  test_quiet_noise();
  test_calibration_flag();
  test_drift();
  test_spike_and_refractory();
  test_small_bump();
  test_activity_envelope();
  test_piezo_bias_negative_swing();
  test_timestamp_wrap();
  test_short_tap_hold_and_refractory();
  std::printf("\n%s (%d failure%s)\n", g_fail ? "DSP TESTS FAILED" : "DSP TESTS PASSED", g_fail, g_fail == 1 ? "" : "s");
  return g_fail ? 1 : 0;
}
