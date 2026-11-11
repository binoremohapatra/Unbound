# PLAN — OmniBand V2 execution order

Phases follow the master prompt §13. Each phase ends with the §12 verification
subset that is runnable on this machine; hardware/toolchain gaps are labelled,
never faked.

- **P0 (done):** recon → `docs/RECON.md`, this file, `docs/ASSUMPTIONS.md`,
  baseline pytest (585 passed / 5 skipped / 20 orphan failures → orphans excluded).
- **P1:** `protocol/constants.json` (all §4 values + mode policy table + event
  names), `tools/gen_constants.py` → `firmware/.../constants_gen.h` +
  `host/omniband/constants_gen.py`, `--check` mode + pytest, `protocol/PROTOCOL.md`,
  `tests/vectors/gesture_vectors.json` shared vectors.
- **P2:** `firmware/omniband_v2_firmware/omniband_core.h` — portable C++17 DSP +
  CH4 gesture FSM + mode policy + crosstalk + noise-burst + queue policy, no
  Arduino includes; `firmware/tests/native/test_core.cpp` + synthetic piezo
  model; `tools/run_native_tests.py` (finds g++/clang; portable winlibs toolchain
  attempted — see WALKTHROUGH for what actually ran).
- **P3:** firmware glue — `hw_config.h`, `adc_frontend`, `link_io` (USB+BT SPP,
  NDJSON, TX scheduler, baud negotiation), `oled_hud`, `config_store`,
  `omniband_v2_firmware.ino` (FreeRTOS task layout per §5.5), `libs.lock.md`.
  Compile via `tools/build_firmware.ps1` if arduino-cli obtainable; else
  `VERIFIED_COMPILE_ONLY`→`NEEDS_TOOLCHAIN` honestly.
- **P4:** `host/omniband/` — `protocol.py` (hardened NDJSON), `transport/`
  (base, serial_usb, bt_spp, simulator, arbiter), `win32/` (sendinput/scancodes/
  sink/foreground/elevation/console_ctrl — reusing `gateway.py` primitives),
  `safety.py`, `config.py`, `audit.py`, `latency.py`, `out_sinks.py`,
  `omniband_engine.py` entry + `--doctor`.
- **P5:** `router/` + `host/profiles/mode0..5*.json` implementing §7 rows +
  `calibration.py` (§6.4) + tests per mode.
- **P6:** `tts/` (worker, siren WAV, phrases, ICU board state machine in
  `icu_board.py`) + `profiles/icu_phrases.json` + bruxism CSV logger.
- **P7:** simulator transport + `--simulate --dry-run --self-test`, shared
  `gesture_vectors.json` parity test.
- **P8:** `dashboard/server.py` (stdlib SSE + token/Host/Origin/rate-limit
  security) + `static/` UI (6 views) + `tools/smoke_dashboard.py` +
  `tests/test_dashboard_security.py` + `tests/test_contrast.py`.
- **P9:** `optical/` backends (MediaPipe Tasks/legacy detect, head-pose cursor,
  hybrid/fallback) + tests with fakes.
- **P10:** docs (`MODES.md`, `BRINGUP_CHECKLIST.md`, README upgrade,
  `SAFETY_AND_CLAIMS.md`, `SENSOR_PHYSICS.md`, `REQUIREMENTS_TRACE.md`,
  `DEVIATIONS.md`, `WALKTHROUGH.md`), `audit_no_placeholders.py` gate, final
  full-suite run.

## Verification policy

Every claim in code comments/docs is either measured by a command recorded in
`docs/WALKTHROUGH.md` or labelled `NEEDS_HARDWARE` / `NEEDS_WINDOWS_MANUAL` /
`NEEDS_TOOLCHAIN`. `python -m pytest -q` must stay green (orphan folder excluded)
after every phase.
