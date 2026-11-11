# OmniBand V2 — Bring-up Checklist (real hardware)

Everything below requires physical OmniBand V2 hardware. The simulator proves
the software path only; it does NOT prove sensor behavior.

## 1. Flash firmware

```
"C:\Program Files\Arduino CLI\arduino-cli.exe" compile --fqbn esp32:esp32:esp32 firmware/omniband_v2_firmware
"C:\Program Files\Arduino CLI\arduino-cli.exe" upload  --fqbn esp32:esp32:esp32 -p COMx firmware/omniband_v2_firmware
```

Expected boot: `hello` line within 500 ms on USB serial @115200, then
`tele` @30 Hz. Device offers `set_baud 921600`; host negotiates automatically.

## 2. Wiring sanity (no firmware change needed)

- CH1–CH4 on GPIO 32/33/34/35, 11 dB attenuation, ~1 MΩ bias to GND per spec.
- Quiet baseline: with sensors still, `tele.env` should sit near boot baseline
  (< ~50 counts). Tap each sensor firmly: env should exceed `thr_on`
  (defaults 220/220/220/260).

## 3. Host link

```
python host/omniband_engine.py --port COMx --baud 115200 --dry-run
python tools/omniband_ping.py --port COMx --count 20
```

Expect: hello ack, tele flowing, ping RTT reported. At 115200 baud expect
RTT ≳ 9 ms for a 100-byte line; after `set_baud` RTT should drop.

## 4. Calibration

```
python host/omniband_engine.py --port COMx
# then send: {"k":"cal_step","s":"rest"} ... "ch1".."ch4" via the link,
# or use the OLED wizard on-device.
```

Each `cal` result reports peak / np99 / suggested threshold / SNR. Apply with
`{"k":"set_thr","s":"..."}` and persist with `{"k":"save"}`.

## 5. Safety drill (do this before trusting the device)

1. Cover/unplug a sensor mid-hold → all outputs release immediately.
2. Pull USB → `release_all` within one engine tick; reconnect → hello re-sync.
3. Mode 3: CH1+CH2+CH3+CH4 chord → ESTOP + release_all.
4. Mode 2/3: triple-tap CH4 → SOS_PENDING; hold CH4 800 ms → SOS_CANCELLED;
   do nothing → SOS_CONFIRMED after confirm window.
5. Switch foreground window (with `--focus-release`) → outputs release.

## 6. What is NOT proven without hardware

- Real ADC noise floor, crosstalk ratios, threshold feel.
- BT SPP range/reconnect behavior (code path exists; untested on air).
- OLED rendering (compiled, not visually verified).
- End-to-end gesture→game latency at 921600 baud.
