# OmniBand V2 — Sensor Physics, Analog Front End & Electrical Constraints

## 1. Ground-Truth Hardware Configuration

- **Microcontroller:** ESP32-WROOM-32 (30-pin DevKit, 240 MHz dual-core).
- **Sensors:** 4× 35 mm piezoelectric ceramic discs connected to **ADC1 only** (GPIO 32, 33, 34, 35).
- **Analog Front End (per channel):**
  - Piezo transducer in parallel with a 1 MΩ pull-down resistor to GND.
  - Clamping diode D1 (1N4148): Anode at GND, Cathode at signal (clamps undershoot below ≈ −0.3 V).
  - Clamping diode D2 (1N4148): Anode at signal, Cathode at 3V3 (clamps overshoot above ≈ 3.6 V to 3.9 V).
  - **No parallel filter capacitor.**

---

## 2. AC-Coupled Sensor Dynamics

1. **High-Pass Cutoff Frequency:**
   A standard 35 mm brass/ceramic piezo disc exhibits an equivalent capacitance $C_p \approx 25\text{ nF}$. In conjunction with the $R_L = 1\text{ M}\Omega$ load resistance:
   $$\tau = R_L \cdot C_p \approx 1\text{ M}\Omega \times 25\text{ nF} = 25\text{ ms}$$
   $$f_c = \frac{1}{2\pi \tau} = \frac{1}{2\pi \times 0.025\text{ s}} \approx 6.37\text{ Hz}$$
   
2. **Consequences for Signal Morphology:**
   - **No DC steady-state response:** Static, sustained mechanical pressure produces zero ongoing voltage offset. Pressure application generates a sharp positive-going onset spike; pressure release produces an equal-and-opposite negative recovery swing.
   - **Half-Wave Rectification:** Due to diode D1 clamping the negative deflection to approximately −0.3 V (below ADC zero rail), the ADC digitizes almost exclusively the positive half-wave. Software must never attempt negative wave recovery.
   - **Sustained Muscle Clench Detection:** Real jaw clenches (masseter / temporalis activation on CH4) generate bursty mechanical micro-vibrations between 20 Hz and 60 Hz with intermittent dropouts. Detecting a "long hold" or "bruxism" therefore **cannot rely on DC offset**; it requires integrating a rectified peak envelope with a **dropout bridge window (120 ms)**.

---

## 3. ESP32 ADC Non-Linearities & Conversion Budget

1. **Attenuation and Rail Behavior:**
   - Configuration: 12-bit resolution (`0..4095`), 11 dB attenuation (`ADC_ATTEN_DB_11`).
   - The ESP32 successive approximation register (SAR) ADC has known non-linearities:
     - Dead zone below ≈ 100 mV – 150 mV (digital readout hovers near 0–50 counts despite small inputs).
     - Compression/saturation above ≈ 2.8 V – 3.1 V (counts clamp before true 3.3 V rail).
   - **Software mitigation:** All operational thresholds are tracked relative to a dynamically estimated quiet baseline (`b`), never absolute millivolt values.

2. **Source Impedance & Multiplexer Crosstalk:**
   - The ESP32 ADC input sampling capacitor requires low source impedance ($< 10\text{ k}\Omega$). Driving the multiplexer directly from a 1 MΩ source causes high-impedance charge injection and ghosting across channels.
   - **Firmware Mitigation (§5.2):**
     1. Discard the first conversion (`dummy read`) to discharge the sample-and-hold circuit.
     2. Wait `delayMicroseconds(30)` for intermediate settling.
     3. Take 4 back-to-back readings and average them (producing a 14-bit internal sum).

---

## 4. Ambient and Power Rail Noise

1. **Bluetooth SPP Transmit Bursts:**
   - Current pulses up to 250 mA occur during 2.4 GHz RF bursts, producing rail sag and high-frequency spikes on the analog rails.
   - Calibration must always occur with **Bluetooth active** to calibrate against the true operational noise floor (`np99`).
2. **MT3608 Boost Switching:**
   - The 5 V boost converter operates at ≈ 1.2 MHz with ripple that couples onto the 3.3 V LDO output.
   - Baseline EMA time constant (≈ 400 ms) filters out RF/boost ripple while the idle gate ensures actual clench envelopes are not absorbed.

