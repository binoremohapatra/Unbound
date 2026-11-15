# OmniBand V2 — Hardware Assembly & Firmware Deployment Reference

Firmware `omniband-fw 2.0` · Target: ESP32-WROOM-32 (classic ESP32) · Status: **firmware verified only on a PC with stubs; nothing here has been run on real hardware** (see §10).

---

## 1. What it is
A hands-free input band worn on the head. Four piezo discs pick up (1) muscle vibration at the left temple, (2) right temple, (3) forehead, and (4) a jaw-clench tap. An ESP32 turns them into a clean JSON stream. A Python backend turns that stream into Windows keyboard/mouse input with `SendInput`. If the band disappears (unplugged, battery dead, Bluetooth drops), the backend falls back to the webcam/optical pipeline.

| Channel | Pin | Meaning | Output |
|---|---|---|---|
| Ch1 left temple | GPIO32 | steer left | `ch_left` 0..1 (activity envelope) |
| Ch2 right temple | GPIO33 | steer right | `ch_right` 0..1 |
| Ch3 forehead | GPIO34 | forward latch | `ch_fwd` 0..1 |
| Ch4 jaw clench | GPIO35 | instant fire | `clench` 0/1 + `pulse` 0..1 + `piezo_raw` |

**Physics reminder:** a piezo disc is a *dynamic* sensor. It reports change (vibration, taps, muscle buzz), never steady pressure. Holding a clench still for a second will fade out; the firmware therefore uses an envelope for Ch1–3 and a transient detector for Ch4.

## 2. How it works
- **Core 1 (high priority):** one task, exactly every 2 ms (500 Hz, `vTaskDelayUntil`, needs the default 1 kHz FreeRTOS tick — checked by `static_assert`). Oversampled `analogRead` of 4 ADC1 pins → `omniband_dsp.h` → JSON line. It never touches I²C, Bluetooth or a full UART: output is drop-and-count.
- **Core 0 (low priority):** OLED task (150 ms refresh) and the **Bluetooth writer task** (V2). The 500 Hz loop hands packets to the BT task through a 16-slot queue with zero timeout; a stalled radio only drops packets, it cannot delay sampling.
- **DSP (pure C++, unit-tested on a PC):** rolling baseline, learned noise floor, rectified-deviation envelope (fast attack / slow release, adaptive scale) for Ch1–3; threshold-with-hysteresis transient detector for Ch4 (trigger = max(35 counts, 6× noise), release 2.5× noise, 120 ms refractory, ≥ 40 ms hold, wrap-safe `micros()`). 2 s calibration at boot (stay relaxed); send `c` to recalibrate.
- **Packet:** `{"piezo_raw":int,"pulse":f,"clench":0|1,"ch_left":f,"ch_right":f,"ch_fwd":f,"lat_ms":f,"seq":int}` + `\n`. A packet is sent immediately on a clench rising edge, otherwise at 100 Hz (≥ 460800 baud) or 50 Hz (115200). Lines starting with `#` are diagnostics: ignore them.
- **Why 921600 baud:** 100 Hz × ~125 bytes = 12.5 kB/s; 115200 baud carries only 11.5 kB/s.
- `lat_ms` is device-side only (sample start → packet write). It does not include USB, Bluetooth, OS or Python time.

## 3. Mechanical build
| Item | Spec |
|---|---|
| Band | 30 mm elastic, adjustable 520–640 mm |
| Sensors | 4 × 35 mm piezo discs, brass side = GND ring, ceramic side = signal |
| Pod | right side, flush, 60 × 30 × 12 mm outer, 1.2–1.5 mm walls |
| Wiring | thin wires in 5–7 mm fabric tunnels, with slack loops across stretch zones |
| Placement | disc 1 left temple, 2 right temple, 3 forehead centre, 4 over the masseter / jaw hinge (a clench bulges the muscle against the disc) |

**Fit check (my arithmetic, not a test).** Inside the pod you have about 57 × 27 × 9 mm. A 30-pin DevKit is ~51 × 28 mm and ~13 mm tall with headers, so **it does not fit**. Use the bare **ESP32-WROOM-32 module** (18 × 25.5 × 3.1 mm) on the 54 × 24 mm perfboard strip, plus a 0.96" OLED (~27 × 27 mm PCB, ~4 mm), plus a 1S pouch cell (e.g. 502030, ~5 × 20 × 30 mm) and a TP4056 board (~26 × 17 mm). That is a very tight layout: plan on a 3D-printed prototype before committing, or grow the pod to ~70 × 35 × 16 mm. A bare module also needs a programming header (3V3, GND, TX, RX, EN, IO0) because there is no USB bridge.

## 4. Wiring (per sensor and power)
Per sensor pin (35 mm disc → ESP32 ADC pin):
```
disc ceramic (signal) ──┬── 10 kΩ series ── ADC pin ──┬── 100 nF ── GND
                        └── 1 MΩ bleeder ── GND       └── 1N4148, ANODE to GND, CATHODE to the pin
disc brass ─────────────── GND (common ring)
```
- **1N4148 orientation as specified** (cathode toward the ESP32) clamps only *negative* swings to about −0.6 V. It does **not** protect against positive spikes above 3.3 V. A hard tap on a 35 mm disc can exceed that. Add the 10 kΩ series resistor (recommended) and a second clamp to 3V3 (e.g. BAT54 Schottky, anode at the pin, cathode at 3V3). Do not use a zener on these 1 MΩ nodes (leakage).
- With 100 nF at the pin the signal is divided ~5× and decays with ~125 ms time constant. That is why the firmware thresholds are in counts, learned from your noise, not fixed volts.
- If the signal is unipolar-clamped, bias each pin to ~1.65 V with a 2 × 1 MΩ divider (the DSP handles a biased baseline; test 7). The ESP32 ADC is poor near 0 V, so bias also avoids the dead zone.

| ESP32 | Connects to |
|---|---|
| GPIO32 / 33 / 34 / 35 | Ch1 / Ch2 / Ch3 / Ch4 sensor nodes (all ADC1) |
| GND | common brass ring, OLED GND, battery −, TP4056 OUT− |
| 3V3 | OLED VCC |
| GPIO21 / GPIO22 | OLED SDA / SCL (0x3C) |
| GPIO2 | on-board LED (lit while clench active) |
| USB | 921600 baud serial (or 115200 with 50 Hz telemetry) |

## 5. Power design (needs your decision)
The spec feeds the **LiPo straight into VIN** through a slide switch. On a DevKit, VIN goes to an AMS1117-3.3 regulator whose dropout is roughly 1 V at load, and a LiPo runs 4.2 → 3.0 V. At 4.2 V you get about 3.2 V out (marginal), and below ~3.9 V the ESP32 can brown out, especially during Bluetooth transmit peaks (a few hundred mA). So:
1. **Preferred:** LiPo → boost converter to 5 V (with a Schottky if USB is also present) → VIN. Or
2. **Bare-module build:** LiPo → a low-dropout 3.3 V LDO rated ≥ 500 mA → module 3V3 (bypass the AMS1117 entirely).
3. Do **not** power from USB and the battery at the same time without an ideal-diode / load-sharing arrangement.
4. Use a **protected** cell and a TP4056 board with the DW01 protection IC (over-discharge, short circuit). A bare TP4056 does not stop over-discharge.
5. A battery on the head: no exposed cell, no sharp edges, a fuse or protection board, rounded pod, and keep the cell away from sweat. Never use an unprotected cell.

## 6. Firmware deployment
1. Arduino IDE → install **ESP32 board package (Arduino-ESP32 2.0.x or 3.x)**. Libraries: *Adafruit GFX*, *Adafruit SSD1306*. `BluetoothSerial` ships with the core.
2. Board: *ESP32 Dev Module*. **Partition scheme: "Minimal SPIFFS (1.9MB APP with OTA)" or "Huge APP"** (Bluetooth makes the default 1.2 MB too small).
3. Open `esp32_omniband_4channel/` (the `.ino` and `omniband_dsp.h` must sit together). Options at the top of the sketch: `SERIAL_BAUD` (921600), `ENABLE_BLUETOOTH` (1; set to 0 for a USB-only build), `BT_DEVICE_NAME` ("OmniBand").
4. Upload. Open the serial monitor at 921600: you should see `# omniband-fw 2.0 baud=921600 oversample=… pass_us=…`, a bluetooth line, then the 2 s calibration message. `pass_us` over 1000 triggers a warning.
5. PC bring-up: `pip install pyserial` then `python tools/read_omniband.py --seconds 30`. It prints bars, clench edges, packet rate, estimated drops, device-side latency.
6. **Bluetooth:** pair "OmniBand" in Windows. Windows creates two COM ports; open the **outgoing** one at any baud (SPP ignores it) with the same reader: `python tools/read_omniband.py --port COMx`. Expect 10–30 ms jitter and occasional stalls; USB stays the reference for fast play.
7. Recalibrate any time: `python tools/read_omniband.py --recal` (or send `c` over USB or Bluetooth), then stay relaxed for 2 s.

## 7. Backend contract (PC side, not in firmware)
- Parse only lines starting with `{`; ignore `#`.
- Fire on a `clench` 0 → 1 edge. Treat `seq` gaps (steps of the telemetry divider, 5) as drops.
- **Hot-unplug watchdog lives on the PC:** if no valid packet arrives for ~250–500 ms, release all held keys (your Binore gateway's `release_all`) and switch to the optical/webcam fallback. The firmware cannot do this by itself.
- Both USB and Bluetooth can be open at once; use one as primary and drop duplicates by `seq`.

## 8. Test plan
| Test | Pass |
|---|---|
| PC: `g++ -std=c++11 … tools/test_dsp.cpp` | 9 groups pass |
| Reader: `read_omniband.py --selftest` | PASSED |
| Boot banner shows `pass_us` ≤ 1000 | yes |
| 30 s still: clench edges | 0 |
| 20 deliberate clenches | 20 edges (±1) |
| Packet rate at 921600 | ≈ 100/s, drops ≈ 0 |
| Pull USB cable | backend releases keys and switches to webcam |
| Walk 5 m away on Bluetooth | graceful drops, no stuck keys |
| 30 min wear | no hot spots, pod stays flush, wires intact |

## 9. Bill of materials (single unit)
4 × 35 mm piezo discs · 4 × 1 MΩ (bleeder) + 4 × 1 MΩ (only if you add bias) · 4 × 10 kΩ · 4 × 100 nF · 4 × 1N4148 (+ 4 × BAT54 optional) · ESP32-WROOM-32 (DevKit for the bench, bare module for the pod) · 0.96" SSD1306 I²C OLED · 1S LiPo with protection · TP4056 with DW01 · boost (or LDO) regulator · slide switch · 54 × 24 mm perfboard · 30 mm elastic, hook-and-loop, thin multi-strand wire, heat-shrink.

## 10. Honest status
**Verified (on a PC):** DSP unit tests (9 groups, mutation-checked), reader self-test, firmware syntax check against stubs for `ENABLE_BLUETOOTH` 1 and 0 under gnu++11 and gnu++17.
**Not verified:** compilation with the real Arduino-ESP32 toolchain; any run on an ESP32; real Bluetooth throughput/latency; piezo thresholds on a real face; the pod fit (calculated only); battery/regulator behaviour. Expect to tune `kTrigK`, `kMinTrig`, `kMinScale` in `omniband_dsp.h` after the first real recording.
