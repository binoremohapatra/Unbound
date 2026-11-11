# OmniBand V2 — Implementation Walkthrough

How the pieces fit, in execution order. All paths relative to repo root
`D:\unbound\unbound`.

## 1. Single source of truth

`protocol/constants.json` → `tools/gen_constants.py` emits:

- `firmware/omniband_v2_firmware/constants_gen.h` (C++17, `omniband::constants`)
- `host/omniband/constants_gen.py` (Python `CONSTANTS` dict)

`python tools/gen_constants.py --check` fails on drift. Never hand-edit
generated files.

## 2. Firmware (`firmware/omniband_v2_firmware/`)

- `omniband_core.h` — portable C++17 DSP/FSM core, shared with native tests:
  ADC→DC-remove→500 Hz Goertzel-ish envelope→gated baseline EMA→hysteresis
  thresholds→debounce→crosstalk lookahead/retro-cancel→noise-burst guard→
  CH4 gesture FSM (taps/holds/SOS/night/bruxism)→e-stop chord→bounded
  safety-aware event queue (depth 32, preserves ESTOP/SOS/MODE_SET).
- `adc_front.*` — ESP32 ADC: 12-bit, 11 dB, 1 dummy + 30 µs settle, 4×
  oversample (min 2), pins 32–35, 1500 µs tick budget.
- `link.*` — NDJSON over USB Serial + BT SPP; global seq; `hello`/`tele`/`evt`/
  `ges`/`scope`/`cal`/`ack`/`pong`/`diag`; baud negotiation 115200→921600;
  command rate limit 50 Hz; malformed/oversize line handling.
- `config_store.*` — Preferences/NVS persistence (mode, thresholds, compat).
- `oled_ui.*` — SSD1306 30 Hz status/UI task.
- `cal.*` — guided calibration collector (rest + ch1..ch4 steps → `cal` lines
  with peak/np99/threshold/SNR).
- `shared_state.h` — lock-free-ish snapshot struct between tasks.
- `omniband_v2_firmware.ino` — setup/loop: ADC task @500 Hz (2 ms),
  telemetry/UI @30 Hz, link RX dispatch, watchdog feed.

Native tests (`firmware/tests/native/test_core.cpp`) run the same
`omniband_core.h` on the host: `python tools/run_native_tests.py`
(LLVM-MinGW, `-std=c++17 -Wall -Wextra -Werror`).

## 3. Protocol (`host/omniband/`)

- `protocol.py` — line parse/serialize, seq dedupe window (256), command
  builders (`cmd_ping`, `cmd_set_mode`, ...).
- `link.py` — `SerialTransport` (pyserial), `SimTransport`, `Arbiter`
  (dual-link dedupe, primary-silence failover 250 ms, exponential backoff
  250→2000 ms, freshest-link command routing).
- `fsm_core.py` — Python port of GestureFsm used by simulator/optical/tests.
- `sim_device.py` — `SimDevice`: deterministic-ish device emulator emitting
  hello/tele/ges + answering set_mode/night/threshold/ping commands.

## 4. Host engine (`host/omniband/engine.py` + `host/omniband_engine.py`)

`OmniEngine` owns: arbiter, output sink collection, mode table (from
constants), host-silence watchdog (150 ms M0 / 400 ms others), engine deadman
(250 ms), SOS/e-stop escalation, emergency `release_all` (idempotent,
exception-safe, foreground-change aware), audit log, latency stats.

CLI `host/omniband_engine.py`:

```
python host/omniband_engine.py --simulate --self-test      # no hardware
python host/omniband_engine.py --simulate --duration 60    # live sim
python host/omniband_engine.py --port COM5 --baud 115200   # real device
python host/omniband_engine.py --simulate --dashboard      # HUD on :8080
python host/omniband_engine.py --optical --camera 0        # camera fallback
```

## 5. Output sinks (`host/omniband/out_sinks.py`)

`DryRunSink` (logs every command — default safe), `Win32Sink`
(`SendInput` scancodes + mouse, held-output tracking, foreground watcher),
`GamepadSink` (vgamepad if present), `TTSSink`, `SirenSink` (22.05 kHz
alternating 800/1200 Hz), `BruxismLogger` (CSV per day).

## 6. Routing & modes

Gesture→action table per mode lives in `engine.py` (`MODES`/`ROUTES` derived
from constants). Safety precedence: `ESTOP`/`SOS_CONFIRMED`/watchdog/link-loss
always call `release_all` before any routing.

## 7. Optical fallback (`host/omniband/optical.py`)

Maps camera blendshape/pose events → gesture vocabulary with
`source="optical"` tagging and confidence gating. DOUBLE resolution is delayed
one tap-window so optical doubles don't fire premature SINGLEs. Honest: if
OpenCV/MediaPipe missing, it reports unavailable — never fabricates frames.

## 8. Verification surface

| Command | Proves |
|---|---|
| `python -m pytest tests/ -q` | 601 tests incl. host/vectors |
| `python tools/run_native_tests.py` | C++ core: 88 checks, 0 failures |
| `python tools/gen_constants.py --check` | no generated drift |
| `python tools/audit_no_placeholders.py` | no stubs/TODOs |
| `python host/omniband_engine.py --simulate --self-test` | gesture→sink E2E |
| `python tools/omniband_ping.py --simulate` | link RTT/tele rate |
| `arduino-cli compile --fqbn esp32:esp32:esp32 firmware/omniband_v2_firmware` | firmware builds |
