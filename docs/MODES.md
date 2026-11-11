# OmniBand V2 — Six Operating Modes

All timing/threshold values come from `protocol/constants.json` (single source
of truth; generated into `constants_gen.h` / `constants_gen.py`). Do not
hand-edit generated files — run `python tools/gen_constants.py`.

| ID | Name | Short | Fast events | SOS | SOS confirm | Long hold | E-stop | Night | Mode change |
|----|------|-------|-------------|-----|-------------|-----------|--------|-------|-------------|
| 0 | ESPORTS_GAMING | ESPORTS | yes | no | — | 3000 ms | no | no | device OK |
| 1 | SURGICAL_STERILE_OT_WORKSTATION | OT/WORK | no | no | — | 1500 ms | no | no | device OK |
| 2 | ASSISTIVE_ACCESSIBILITY | ASSIST | no | yes | 3000 ms | 1500 ms | no | no | device OK |
| 3 | INDUSTRIAL_FIREFIGHTER | INDUSTRY | no | yes | 1500 ms | 2000 ms | yes | no | device OK |
| 4 | ICU_SILENT_SOS | ICU/SOS | no | yes | 3000 ms | disabled | no | no | **PC only** |
| 5 | DAILY_LIFE_RIDER_MEDIA | DAILY | no | no | — | 1500 ms | no | yes | device OK |

## Gesture vocabulary (device `ges` lines)

`SINGLE` `DOUBLE` `TRIPLE` `LONG` `BRUX` `BRUX_END` `REJECT` `ESTOP`
`SOS_PENDING` `SOS_CONFIRMED` `SOS_CANCELLED` `PICKER_OPEN` `PICKER_CANCEL`
`MODE_SET` `NOISE_BURST`

- CH4 tap sequences: SINGLE (1 tap), DOUBLE (2 taps), TRIPLE (3 taps within
  `triple_total_max_ms`). In SOS-capable modes TRIPLE raises `SOS_PENDING`
  and confirms after the mode's confirm window unless cancelled (800 ms hold
  or `sos_cancel` host command).
- `LONG`: CH4 hold >= mode `long_hold_ms` (disabled in mode 4).
- `PICKER_OPEN`/`PICKER_CANCEL`: mode picker after LONG hold; CH1–CH3 select,
  CH4 confirms, 5000 ms timeout cancels.
- `ESTOP`: CH1+CH2+CH3 chord sustained >= 150 ms with CH4 envelope >= 1.5×
  chord envelope (mode 3 only).
- `BRUX`/`BRUX_END`: CH4 hold >= 2500 ms clench epoch (any mode with CH4
  activity; host logs to CSV + optional cue).
- `NOISE_BURST`: >40 ms saturated burst — suppressed, not routed.
- `REJECT`: chew-guard rejection (3 taps inside 2000 ms followed by lockout).

## Host safety precedence

Emergency release (`release_all`) dominates all routing: face/link loss,
watchdog expiry (engine deadman 250 ms), SOS/e-stop events, foreground-window
change (optional `--focus-release`), and process exit all release held outputs
idempotently.
