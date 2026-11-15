# OmniBand 4-Channel Split-Pod Node: Build, Flash and Bring-up Guide

Firmware: `esp32_omniband_4channel/esp32_omniband_4channel.ino` + `omniband_dsp.h`
Target: 30-pin ESP32-WROOM-32 DevKit, Arduino-ESP32 core 2.0.x or 3.x.

---

## 0. Read this first: what this device really is

| Claim in the brief | What is physically true |
|---|---|
| "neural" | It senses **muscle vibration and bone-conducted transients** with piezo discs. It does not read the brain. Call it a *myo-mechanical* or *bio-signal* sensor in front of judges. |
| "sustained focus pressure" (forehead) | A piezo is a **dynamic** sensor. It reports *change* (taps, muscle buzz), not static pressure. Ch3 therefore outputs an **activity envelope**. For true sustained pressure, swap Ch3 for an FSR-402 (see section 9). |
| "<3 ms" | The firmware samples every 2 ms and writes the packet within about 0.1 to 1 ms of detection (`lat_ms`). That is **device-side only**. USB-serial bridge, Windows and Python add more (typically a few ms, sometimes more). Measure end to end with the LED + slow-motion method in section 7. |
| Steer left/right with temples | The temporalis muscle at the temples also contracts when you **clench your jaw**. Expect cross-talk between Ch1/Ch2 and Ch4. See section 8. |

Nothing here is a medical device. Do not use it for diagnosis or as the only control path for safety-critical equipment.

---

## 1. Bill of materials

| Qty | Part | Notes |
|---|---|---|
| 1 | ESP32-WROOM-32 DevKit, 30-pin | CP2102 or CH340 USB bridge both work |
| 4 | 35 mm piezo disc (brass + ceramic) | typically 20 to 30 nF each; measure if your meter has a capacitance range |
| 4 | 1 MΩ resistor, 1/4 W | bleeder |
| 4 | 1N4148 diode | negative-swing clamp |
| 4 | 100 nF ceramic capacitor | at the ADC pin |
| 4 | **10 kΩ resistor** (recommended, not in the original spec) | series protection |
| 4 | **1 MΩ resistor** (optional bias mod, section 3.2) | one extra per channel (node to 3V3); the bleeders above become the other half (node to GND) |
| 1 | 0.96" SSD1306 I2C OLED, 128x64, address 0x3C | |
| 1 | Perfboard 7 cm x 5 cm | Part B |
| 1 | 9-core thin ribbon or 4 thin shielded cables (about 35 cm) | Part A to Part B |
| 1 | Elastic or Velcro headband, 25 mm wide | Part A |
| - | Double-sided foam tape (2 mm), Kapton or electrical tape, heat-shrink, female pin headers, 6-pin screw terminal or JST-XH | |
| 1 | USB cable that carries data (not charge-only) | |

---

## 2. Pin map

| Function | ESP32 pin | Note |
|---|---|---|
| Ch1 left temple | GPIO 32 | ADC1_CH4 |
| Ch2 right temple | GPIO 33 | ADC1_CH5 |
| Ch3 forehead | GPIO 34 | ADC1_CH6, input-only, no internal pull-up |
| Ch4 piezo jaw clench | GPIO 35 | ADC1_CH7, input-only, no internal pull-up |
| OLED SDA / SCL | GPIO 21 / 22 | 3V3, GND, address 0x3C |
| Status LED | GPIO 2 | on-board LED, lit during a clench |

Only ADC1 pins are used, so analog reads stay valid regardless of radio use.

---

## 3. Per-channel conditioning circuit

### 3.1 As specified (build this first)

```
 piezo ceramic (+) ──┬───────────┬───────────┬──────────┬──── GPIO
                     │           │           │          │
                    1 MΩ      1N4148      (node)      100 nF
                     │      cathode→node                │
                    GND     anode→GND                  GND
 piezo brass (−) ───────────────────────────────────────  GND
```

What each part does, with real numbers:

* **1 MΩ bleeder** gives the piezo's charge a path to ground so the node does not float and drift. Floating high-impedance inputs pick up mains hum and wander.
* **1N4148 (cathode to node, anode to GND)** clamps the node at about −0.6 V when a negative swing arrives. **It does not limit positive over-voltage.** A hard knock can still push the pin above 3.3 V. This is why the series resistor below is recommended.
* **100 nF at the pin** gives the ESP32's sampling capacitor a charge reservoir (the ADC wants a low-impedance source, and a 1 MΩ source is not) and filters wideband noise.
* **Consequence you must know about:** the piezo is a capacitor (about 25 nF). With 100 nF added at the pin, the signal is divided by roughly 25 / (25 + 100) = **0.2 (about 5x smaller)**, and the node decays with **τ = 1 MΩ x 125 nF ≈ 125 ms**. Transients still get through; they are just smaller and tail off over about a tenth of a second. The firmware's 120 ms refractory and 40 ms hold were chosen with this in mind. If signals are too small, use 10 nF instead of 100 nF (bigger signal, shorter τ, noisier).

### 3.2 Recommended upgrades (cheap, worth doing)

1. **10 kΩ series resistor** between the node and the GPIO pin (put the 100 nF on the pin side). It limits current into the ESP32's internal protection diodes during a hard hit, and with the 100 nF it forms a ~160 Hz low-pass, which is a free anti-alias filter for the 500 Hz sampling.
2. **Bias the node to mid-rail** (1.65 V): replace the single 1 MΩ-to-GND with **1 MΩ to 3V3 plus 1 MΩ to GND** at the node. Reason: the ESP32 ADC reads about 0 below roughly 0.1 V, so an un-biased piezo loses small signals in that dead zone, and the 1N4148 throws away the negative half. With the bias, both swing directions are visible and the firmware (which uses |deviation| from a rolling baseline) handles it without any code change. New time constant: 500 kΩ x 125 nF ≈ 62 ms.

Do **not** use a 3.3 V zener as the positive clamp. Its leakage (microamps) into a 1 MΩ node causes volts of error. If you want a positive clamp, use an ultra-low-leakage diode such as BAS116 from the node to 3V3, in addition to the series resistor.

---

## 4. Part A: headband (sensor side)

1. **Disc prep.** Solder a 30 cm thin wire to the ceramic face (signal) and one to the brass ring (GND). Keep soldering quick and use the edge of the ceramic only, or it cracks. Strain-relieve each wire with a drop of hot glue.
2. **Mechanical amplifier.** Stick a small foam bump (about 8 mm diameter, 2 mm thick, from double-sided foam tape) in the **centre of the face that touches the skin**. It concentrates pressure on the disc's centre so it flexes more. Orientation matters: try brass-to-skin and ceramic-to-skin during bring-up and keep whichever gives the larger readings.
3. **Insulation.** Cover every exposed metal surface (including the brass) with Kapton or electrical tape. Nothing conductive may touch skin.
4. **Placement** (headband holds the discs under light, constant pressure):

   | Ch | Disc position |
   |---|---|
   | Ch1 | left temple, over the temporalis, about 1 to 2 cm above and behind the outer corner of the eye |
   | Ch2 | right temple, mirror of Ch1 |
   | Ch3 | centre forehead, above the eyebrows |
   | Ch4 | bone behind the left ear (mastoid) or just in front of the ear over the jaw joint. Test both; keep the one with the larger clean spike on a deliberate clench |

5. **Cable.** Run all four signals and the shared ground in one ribbon. With high-impedance (1 MΩ) signals the cable picks up mains hum and channels couple into each other. Use **interleaved ground** (signal, ground, signal, ground...) or short thin shielded cable per channel (shield to GND at the ESP32 end only). Keep it under about 35 cm.

---

## 5. Part B: collar/shoulder pod (7 cm x 5 cm perfboard)

Layout (top view):

```
 ┌─────────────────────────────────────────────┐
 │  [OLED 0.96" on 4-pin female header]        │
 │                                             │
 │   ┌───────────────────────────────┐         │
 │   │      ESP32 DevKit (30-pin)    │  ← USB  │
 │   │   on 2 x 15 female headers    │         │
 │   └───────────────────────────────┘         │
 │  ch1  ch2  ch3  ch4   ← 4 protection rows   │
 │  [1M][D][100n][10k]  (one row per channel)  │
 │  [ 6-pin screw terminal: S1 S2 S3 S4 G G ]  │
 └─────────────────────────────────────────────┘
```

Build steps:

1. Solder the two 15-pin female headers for the DevKit, leaving the antenna end of the module free of copper.
2. Wire the OLED header: VCC→3V3, GND→GND, SDA→GPIO21, SCL→GPIO22.
3. For each channel, build one protection row as in section 3 (and 3.2 if you chose it): node connects to the screw terminal, then 10 kΩ, then the GPIO pin with the 100 nF to GND at the pin. Orient the 1N4148 so the **cathode (banded end) is on the node** and the anode on GND.
4. Tie all grounds to **one** ground rail. Run it to the DevKit GND and to the terminal's ground pins.
5. Keep the four protection rows physically away from the OLED wiring and the ESP32 antenna, and keep the signal traces short.
6. Before plugging in: continuity-check that no GPIO pin is shorted to GND or 3V3, and that GND ≠ 3V3.

---

## 6. Flash the firmware

1. Install **Arduino IDE 2.x**. Boards Manager: install **"esp32 by Espressif Systems"** (2.0.17 or any 3.x).
2. Library Manager: install **Adafruit GFX Library** and **Adafruit SSD1306**.
3. Put `esp32_omniband_4channel.ino` and `omniband_dsp.h` **together in a folder named `esp32_omniband_4channel`** (the Arduino IDE requires the folder name to match the sketch name).
4. Board: **ESP32 Dev Module**. Select the COM port. Click Upload (hold the BOOT button while it says "Connecting..." if it times out).
5. Serial Monitor at **921600** baud. You should see:
   ```
   # omniband-fw 1.0 baud=921600 oversample=4 pass_us=<number> telemetry_div=5
   # calibrating 2 s: keep still and relaxed. Send 'c' to recalibrate.
   {"piezo_raw":...}
   ```
   `pass_us` is the measured cost of one 4-channel read. The firmware picks oversample 4, 2 or 1 to keep it under 1 ms. If you see the "exceeds 1 ms" warning, the loop will jitter.

If your USB bridge cannot do 921600, change `SERIAL_BAUD` to 115200 in the `.ino`; telemetry automatically drops to 50 Hz (100 Hz of ~125-byte packets does not fit through 115200 baud) and the PC tool must use `--baud 115200`.

---

## 7. Bring-up checklist (do these in order)

Install `pyserial`, then use `tools/read_omniband.py` (close the Arduino Serial Monitor first; only one program can hold the port).

| # | Test | Expected |
|---|---|---|
| 1 | `python read_omniband.py --seconds 15` with the band **on the desk** | about 100 packets/s, 0 parse errors, 0 dropped, no clench edges, bars near 0 |
| 2 | Put the band on, sit still, 2 s calibration, then relax | bars stay near 0, no phantom clench |
| 3 | Tap each disc gently with a fingertip | the matching bar rises and falls back within about 0.5 s; the other bars stay low |
| 4 | Clench your jaw firmly, 10 times | about 10 `>>> CLENCH` lines; the on-board LED lights each time; OLED shows `CLN` |
| 5 | Talk, chew, nod, walk around for 2 minutes | note any false clench edges; if there are any, raise `kTrigK` or `kMinTrig` in `omniband_dsp.h` |
| 6 | Press `c` (or `--recal`) | "recalibrating" banner, outputs zero for 2 s |
| 7 | Pull the USB cable and plug it back | the tool reports "No ESP32 serial port" or errors cleanly; the board re-calibrates on boot |
| 8 | **End-to-end latency:** film the LED and the PC screen together with a phone in slow motion (240 fps) while clenching | count the frames between the LED turning on and the screen showing `>>> CLENCH`. This is your honest number. |

OLED bottom line (when not calibrating): `j<jitter µs> e<exec µs> d<dropped>`. Healthy: j under about 1000, e under about 1200, d = 0.

---

## 8. Using it with the Unbound engine (not built here)

* A thin PC adapter would read these JSON lines and emit `SignalEvent`s (suggested `src="switch"`). Names to agree with Dishti, for example `clench` (edge) for the action trigger and `omni_left` / `omni_right` / `omni_fwd` (0..1) for steering and latch.
* **Cross-talk rule:** a real clench activates **both** temple channels. Treat "both temples high and Ch4 edge" as a fire, and suppress the steering outputs for about 200 ms after a clench edge. Voluntary one-sided temple tension is hard for most people; test with real users before promising left/right steering from the temples.
* The safety brain (face-loss, F12, deadman) stays the authority. A hardware clench must go through the same state machine, never straight to the output gateway. If the cable is unplugged, the adapter must report "face/signal lost" so keys release.

---

## 9. Tuning reference (`omniband_dsp.h`)

| Constant | Default | Raise it when | Lower it when |
|---|---|---|---|
| `ClenchDetector::kTrigK` | 6.0 | false clenches while talking/chewing | clenches are missed |
| `ClenchDetector::kMinTrig` | 35 counts | idle noise triggers | small clenches are missed |
| `kRefractoryUs` | 120 ms | double triggers on one clench | rapid repeated clenches get swallowed |
| `kMinHoldUs` | 40 ms | the PC sometimes misses a short `clench=1` | you want the level to drop sooner |
| `ActivityChannel::kDeadbandK` | 2.0 | bars flicker at rest | small tension is not registering |
| `ActivityChannel::kMinScale` | 60 counts | a light touch already reads 1.0 | a strong effort cannot reach 1.0 |
| `ActivityChannel::kBaseRun` | 0.0005 | the baseline lags real drift | sustained effort fades too quickly |

Optional swap for Ch3: an FSR-402 in a voltage divider (FSR to 3V3, 10 kΩ to GND, tap to GPIO34, 100 nF at the pin) reads true static pressure. In that case change Ch3 to a plain smoothed value instead of the activity envelope.

---

## 10. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| No serial port | charge-only USB cable, missing CP210x/CH340 driver, or the Arduino Serial Monitor is open |
| Packets arrive at ~50/s, not ~100/s | baud is 115200 (expected), or the PC is dropping; use 921600 |
| `dropped` keeps rising | USB hub or cable problem, or the reader is blocked; try a direct port |
| Constant 50 Hz ripple on all channels | mains pickup on the high-impedance cable: shorten it, interleave grounds or use shielded cable, keep away from chargers |
| Ch4 always near 0 | un-biased front end and ADC dead zone: add the bias mod (3.2) |
| Spurious clenches when moving | cable motion (triboelectric/piezo effect in the wires) or a loose disc: strain-relieve and fix the disc to the headband |
| One channel reads all 4095 or all 0 | wiring fault (short or open); the pin must never be tied directly to 3V3 |
| OLED blank | wrong address or wiring; the firmware prints `# OLED not found` and keeps running without it |
| Boot loop / brownout | weak USB port or cable; use a powered port |

---

## 11. Safety

* Power the node **only from the laptop's USB port or a USB power bank**. Do not wear it while connected to any mains-powered device through a non-isolated path.
* Nothing conductive may touch skin. Cover the brass discs and all solder joints with insulation.
* Stop and remove the band if you feel skin irritation, numbness or a headache from the headband pressure. Keep sessions short at first.
* The 3V3 rail and GND are the only voltages on the head; never connect anything on the head to 5 V.

---

## 12. What was and was not verified (honest status)

Verified on a PC (this was actually run):
* `omniband_dsp.h` compiled with g++ (C++11 and C++17, `-Wall -Wextra`) and passed 9 test groups on synthetic signals: noise-only (no false triggers), calibration, slow drift, spike with edge on the first sample, refractory and re-arm, 1-sample taps, small bump rejection, activity envelope rise/fall, negative swing on a biased input, and `micros()` wraparound. The tests were mutation-checked: deliberately wrong constants make them fail.
* `esp32_omniband_4channel.ino` passes a g++ **syntax** check against stub Arduino headers.
* `read_omniband.py --selftest` passes (parser, edge counting, drop estimate).

**Not** verified (needs your bench):
* The firmware has **not been compiled with the real ESP32 toolchain or flashed** to a board. Expect that a first compile might need a small fix (library versions differ).
* Real piezo signal levels on a human head, the best disc positions, false-trigger rate during talking/chewing, and the true end-to-end latency are all unmeasured. The default thresholds are starting points.
