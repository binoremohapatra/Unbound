# ASSUMPTIONS — OmniBand V2

1. **Repo identity:** work happens inside `D:\unbound\unbound` (the existing
   `unbound` repo). The standalone `D:\unbound\omnicontrol_os` prototype is NOT
   part of this tree; V2 host code lives in `host/omniband/` + `host/omniband_engine.py`.
2. **No legacy piezo consumer:** RECON found none, so `compat_v1` uses the §5.6
   best-effort field semantics.
3. **Orphan tests:** `my_project(UnBound)/test_webcam_adapter.py` fails at
   baseline (targets a nonexistent API). Excluded from pytest via
   `norecursedirs`; recorded in DEVIATIONS.md. Everything else must stay green.
4. **Toolchain:** no g++/arduino-cli at recon. Attempt portable winlibs g++ for
   native tests; arduino-cli compile is `NEEDS_TOOLCHAIN` if unobtainable —
   firmware code is written complete and compile-reviewed but not claimed built.
5. **Hardware locked:** no IMU/buzzer/vibration/GPIO beyond §2 — no battery %,
   no haptics, no on-device audio. Cues go to OLED + PC only.
6. **Frozen contract:** `core/events.py` untouched; V2 bridges via
   `adapters/compat_v1.py` rather than editing it.
7. **Teammate files:** `handsfree/**`, `adapters/{audio,switch}.py`,
   `profiles/*.json` (camera-engine profiles), `core/mapping.py` untouched.
   V2 profiles live in `host/profiles/`.
8. **Clock rule:** all intervals `time.perf_counter()`; `datetime` only for
   log/file-name wall-clock labels.
9. **Dependencies:** pyserial + pyttsx3 required (both import OK here);
   opencv/mediapipe/numpy optional; no pywin32/vgamepad/admin for core.
10. **Dashboard binding:** `127.0.0.1` only, random per-session token, SSE for
    telemetry, POST for commands — per §9 security block.
11. **Simulator keys Z/X/C/V are global** (`GetAsyncKeyState` ≥500 Hz) —
    documented in README; they only run in `--simulate` mode.
12. **SOS fail direction:** per spec, no cancel → `SOS_CONFIRMED` (fails toward
    alerting). Host siren stops only on PC-side ACK, never patient channel.
