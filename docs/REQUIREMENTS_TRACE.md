# OmniBand V2 — Requirements Trace

Every requirement → where it lives → how it was verified. Commands were
actually executed; results in `docs/WALKTHROUGH.md` §8 and this file's
"Proof" column.

## Firmware

| Requirement | Implementation | Proof |
|---|---|---|
| Portable C++17 core, native-testable | `firmware/omniband_v2_firmware/omniband_core.h` | `tools/run_native_tests.py` → 88 checks, 0 failures |
| 500 Hz DSP, 30 Hz tele/UI | `omniband_v2_firmware.ino` tasks; `timing` in constants.json | native tests tick at spec rates; compile clean |
| ADC 12-bit/11 dB/oversample 4/pins 32-35 | `adc_front.cpp` | ESP32 compile OK; runtime unverified (no HW) |
| Gated baseline EMA, hysteresis, debounce | `omniband_core.h` | native unit tests + 16 shared vectors |
| Crosstalk lookahead/retro-cancel, noise burst | `omniband_core.h` | vectors `xtalk_*`, `noise_burst` |
| CH4 gestures: tap/double/triple/long/picker | `omniband_core.h` GestureFsm | 16 vectors, Python+C++ parity |
| SOS pending/confirm/cancel; night; bruxism | `omniband_core.h` | vectors `sos_*`, `night_*`, `brux_*` |
| E-stop chord (CH1+2+3 + strong CH4) | `omniband_core.h` | vector `estop_chord_mode3` |
| Bounded safety queue (32, preserve estop/SOS) | `omniband_core.h` EventQueue | native queue test |
| NDJSON link, seq dedupe, rate limit, malformed handling | `link.cpp` | compile OK; host `test_omniband_host.py` |
| USB Serial + BT SPP dual transport | `link.cpp` | compile OK; BT untested on air |
| Baud 115200→921600 negotiate/revert | `link.cpp`, `set_baud` cmd | compile OK |
| NVS config persistence | `config_store.cpp` (Preferences) | compile OK |
| OLED 30 Hz UI | `oled_ui.cpp` (SSD1306) | compile OK; not visually verified |
| Calibration wizard + `cal` reporting | `cal.cpp` | compile OK |
| ESP32 firmware compile | arduino-cli esp32:esp32:esp32 | **87% flash, 19% RAM — PASS** |

## Host

| Requirement | Implementation | Proof |
|---|---|---|
| Protocol parse/serialize, seq window 256 | `host/omniband/protocol.py` | `test_omniband_host.py` |
| USB + BT transports, reconnect/backoff/failover | `host/omniband/link.py` Arbiter | host tests (failover case) |
| Command ack/timeout/error | `protocol.py`, engine | host tests |
| Arbiter/watchdogs: host-silence, deadman 250 ms | `engine.py` | host tests + sim |
| Emergency release: stop/disconnect/foreground/SOS/e-stop | `engine.py` `release_all` | self-test + host tests |
| Win32 SendInput injection, held tracking | `host/omniband/win32_sink.py` | compile/import guarded; dry-run proven |
| Dry-run sink honesty (no fake success) | `out_sinks.py` DryRunSink | self-test logs DRYRUN lines |
| Six modes routing | `engine.py` MODES/ROUTES | vectors + host tests |
| Calibration host side (thr/recal/cfg/save) | `protocol.py` cmd builders | host tests |
| Latency probe + audit log | `tools/omniband_ping.py`, engine stats | ping sim run |
| Keyboard/mouse/gamepad/TTS/siren/ICU/bruxism sinks | `out_sinks.py` | host tests; optional deps lazy |
| Simulator + `--self-test` | `sim_device.py`, engine CLI | `gesture routed to sink: YES` |
| Optical fallback/hybrid, honest source tagging | `host/omniband/optical.py` | optical tests in host suite |
| Dashboard read-only telemetry | `handsfree/dashboard` + omniband card | smoke test: index 12419 B, card present |

## Cross-cutting verification actually run

- `python -m pytest tests/ -q` → **601 passed, 5 skipped** (21.4 s)
- `python tools/run_native_tests.py` → **88 checks, 0 failures**; 16 vectors
- `python tools/gen_constants.py --check` → up to date
- `python tools/audit_no_placeholders.py` → clean
- `python host/omniband_engine.py --simulate --self-test --duration 20` →
  gesture routed, stats sane, exit 0
- `python tools/omniband_ping.py --simulate --count 10` → 10/10 pong,
  tele ~28.8/s
- `arduino-cli compile --fqbn esp32:esp32:esp32` → success (1141540 B)

## Not proven (requires hardware — see BRINGUP_CHECKLIST.md)

- Real ADC behavior, thresholds, crosstalk on physical sensors
- BT SPP over the air, OLED visuals, 921600 baud hot path
- End-to-end gesture→game latency on hardware
