# OmniBand V2 — Deviations & Assumptions

## Assumptions (see also ASSUMPTIONS.md)

- Target MCU: ESP32 classic DevKit (`esp32:esp32:esp32`), Arduino core 3.3.12.
- Sensors: 4× EMG/force channels, 12-bit ADC, GPIO 32–35, 500 Hz DSP tick.
- Host: Windows 11 x64, Python 3.11. USB serial + Bluetooth SPP transports.
- No hardware was available during development; all "device" results come
  from `SimDevice`, which replays the same protocol FSM, not real sensors.

## Deviations from the original master prompt

1. **No separate `omniband_v2/` root** — per explicit instruction, everything
   lives in-place under `D:\unbound\unbound` (`firmware/`, `host/omniband/`,
   `protocol/`, `tools/`).
2. **Existing modules preserved** — `main.py`, `gateway.py`, `state_machine.py`,
   `watchdog.py`, `core/events.py` (frozen contract) untouched in behavior;
   OmniBand host is a parallel engine (`host/omniband_engine.py`) that can
   coexist with the camera pipeline.
3. **Dashboard reused, not duplicated** — extended the existing
   `handsfree/dashboard` with an `omniband` telemetry card rather than
   building a second server. Read-only by construction (WS broadcast only).
4. **TTS/siren are optional sinks** — `pyttsx3` imported lazily; absent =
   silent (honest degradation, `--no-tts`/`--no-siren` flags).
5. **Win32 injection** — `win32_sink.py` uses `SendInput` scancodes directly;
   falls back to `DryRunSink` under `--dry-run` or non-Windows. No fake
   success is reported: dry-run logs every command.
6. **Firmware compile target** — compiled for `esp32:esp32:esp32` (Dev
   Module). 87% flash / 19% RAM. Not yet flashed to physical hardware.
7. **Latency numbers** — `tools/omniband_ping.py --simulate` RTT ≈ 47 ms is a
   *simulator scheduling artifact*, not a hardware measurement. Real RTT must
   be measured on-device per BRINGUP_CHECKLIST.md §3.
8. **Optical fallback** — `host/omniband/optical.py` converts camera blendshape
   events into the same gesture vocabulary when no OmniBand link is present.
   It reports `source="optical"` honestly; it never claims hardware data.

## Known limitations

- `compat_v1` legacy lines implemented in firmware + constants; host parser
  accepts them but routing ignores them (v1 compat is diagnostic only).
- OLED UI compiled but not visually verified (no display attached).
- BT SPP transport implemented; pairing flow is OS-standard, untested on air.
- ICU "board" behavior = mode-4 routing rules + silent cues (no audible alarm
  by design); physical ICU board display not implemented.
