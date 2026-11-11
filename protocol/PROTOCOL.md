# OmniBand V2 Protocol (NDJSON over USB Serial and Bluetooth SPP)

Single source of truth for constants: `protocol/constants.json`
(generator: `tools/gen_constants.py`; stale check: `--check`).

## Transport

- USB Serial **and** Classic Bluetooth SPP (`BluetoothSerial`, device name
  `OmniBand-V2`) are both active simultaneously at 115200 baud (boot) with
  optional negotiated 921600 (`set_baud`). Every line is duplicated to both
  transports with one global `seq`; the host dedupes by `seq` (window 256).
- Framing: UTF-8/ASCII, one JSON object per line, `\n` terminated.
  Host→device lines ≤ 160 bytes; device→host ≤ 128 bytes (`hello`/`cfg` ≤ 200).
- `t` is `esp_timer_get_time()` µs since boot (int64 — `micros()` wraps ~71 min).

## Device → host

| `k` | Fields | Meaning |
|---|---|---|
| `hello` | `v,seq,t,fw,rr,mode,hz,ov,thr[4],caps[],baud` | boot banner; `rr` = reset reason; `ov` = oversample count actually used |
| `tele` | `v,seq,t,m,e[4],s,st` | 30 Hz telemetry: mode, envelopes, flag bitmask, FSM state |
| `evt` | `v,seq,t,ch,ty,a[,d][,veto]` | raw channel edge: `ty` = down/up, `a` = peak counts, `d` = duration ms |
| `ges` | `v,seq,t,g,a,d,m` | decoded gesture `g`, amplitude, duration, mode |
| `scope` | `v,seq,t,r,x[[4]..]` | opt-in oscilloscope frames (125 Hz peak-decimated default) |
| `cal` | `v,seq,t,step,pk[3],np99,thr,snr` | per-step calibration result |
| `ack` | `id,ok,err` | command acknowledgement |
| `pong` | `id,th,t` | ping reply (echoes `th`, device `t`) |
| `diag` | `v,seq,t,adc_us,tick_us,ovr,drop,veto,burst,np99[4]` | health counters |

`s` flag bits: 0 night, 1 sos_pending, 2 picker, 3 xtalk_suppress, 4 overrun,
5 compat_v1, 6 host_silent (no host command within the active mode's
`host_silence_ms`; see `constants.json → tele_flags`).

Gesture names (`constants.json → gestures`): `SINGLE DOUBLE TRIPLE LONG BRUX
BRUX_END REJECT ESTOP SOS_PENDING SOS_CONFIRMED SOS_CANCELLED PICKER_OPEN
PICKER_CANCEL MODE_SET NOISE_BURST`.

## Host → device

`hello`, `set_mode{id,mode}`, `set_thr{id,ch,on[,off]}`, `recal{id,what}`,
`cal_step{id,step}`, `set_cfg{id,key,val}`, `save{id}`, `defaults{id}`,
`scope{id,on,rate}`, `ping{id,th}`, `set_baud{id,baud}`,
`night{id,on}`, `sos_cancel{id}`, `oled_msg{id,text≤20chars,ms}`,
`diag{id}`. Every command gets `ack{id,ok,err}`; malformed/oversized/binary
input is counted in `diag`, never crashes the parser; commands rate-limited
(~50/s).

## Byte-budget math (115200 baud ≈ 11.5 kB/s ≈ 1.15 ms/byte)

- `tele` ≈ 60 B → ~5.2 ms on wire; at 30 Hz ≈ 156 ms/s ≈ 15.6 % of link.
- `evt`/`ges` ≈ 60–75 B → ~6 ms each; gesture bursts are rare.
- `scope` frame ≈ 110 B → ~9.6 ms; opt-in only, off by default.
- `compat_v1` line ≈ 90 B @ 30 Hz ≈ 234 ms/s ≈ 23 % of link — acceptable only
  because it is off by default.
- A 100-byte line takes ~8.7 ms to transmit: "sub-3 ms" is NOT achievable
  end-to-end at 115200; use `set_baud 921600` (~0.95 ms/100 B) for the hot path.

## compat_v1 (default OFF; `set_cfg compat_v1:true`)

Additionally emits legacy lines at 30 Hz:
`{"piezo_raw":<CH4 avg counts>,"pulse":<env4/thr_on(CH4) clamped 0..2>,
"clench":<1 if CH4 active else 0>,"lat_ms":<tick→TX ms>}`.
Field semantics per RECON (no in-repo consumer found).
