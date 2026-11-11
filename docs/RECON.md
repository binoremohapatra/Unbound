# RECON — OmniBand V2 in-place upgrade of `unbound`

Date: 2026-10-11. Scope: `D:\unbound\unbound` (git `main`, clean tree at recon time).

## 1. Workspace inventory

Languages: Python 3.11 (host), no firmware/C++ present. Entry points: `main.py`
(60 Hz hands-free engine), `omnicontrol.py` (console shim), `handsfree/main.py`
(standalone pipeline). Test suite: `pytest` — **baseline: 585 passed, 5 skipped,
20 failed**. All 20 failures are inside `my_project(UnBound)/test_webcam_adapter.py`,
an orphaned scratch folder whose tests target a `WebcamAdapter`/`WebcamConfig` API
that does not exist anywhere in the repo (`AttributeError: module
'handsfree.adapters.webcam' has no attribute 'WebcamConfig'`). It is not part of
`tests/` and cannot pass against any current code. Decision: exclude it from
collection via `pyproject.toml` `norecursedirs` (recorded in DEVIATIONS.md);
leaving it red would mask real regressions.

Merged upstream work since last session: PR #5/#6 added
`tests/test_omnicontrol_system.py`, which imports the *external*
`D:\unbound\omnicontrol_os` package (the standalone HUD/pipelines prototype).
That package stays external; this upgrade does not depend on it.

Toolchain on this machine: Python 3.11 64-bit, numpy 1.26.4, pyserial, pyttsx3
present. **No g++, gcc, clang++, arduino-cli** at recon time — see PLAN for how
native/firmware verification is handled (portable toolchain download attempted;
otherwise labelled NEEDS_TOOLCHAIN honestly).

## 2. In-Place Decision Map

| Path | Decision | Reason / planned change |
|---|---|---|
| `core/events.py` | KEEP AS-IS | Frozen shared contract (`SignalEvent`, `Intent`, `OutputCommand`). V2 bridges into it via `adapters/compat_v1.py`. |
| `gateway.py` | UPGRADE-IN-PLACE (wrap, not edit) | Already implements the §6.2 hard parts: x64 `INPUT` (40 B asserted), Set-1 scancode `KEYEVENTF_SCANCODE` table, `SendInputBackend`, `RecordingBackend`, `Gateway` desired-vs-held diff with release-first ordering, `release_all`, `ForegroundWatcher`, `KillSwitch`, `atexit`/console-ctrl handlers. The V2 engine reuses `SendInputBackend`/`RecordingBackend`/`Gateway` inside `host/omniband/win32/sink.py`. Media-key VK path and wheel support are added in the new `win32/` package (gateway has no media keys; extending it would churn teammate-reviewed code). |
| `state_machine.py` | KEEP AS-IS | Face-loss safety FSM for the camera pipeline; unrelated to piezo FSM. Reused unchanged by `main.py`. |
| `watchdog.py` | KEEP AS-IS | `Watchdog` deadman + `LatencyLog` + `FrameClock` are exactly what §6.2/§6.5 need; V2 engine instantiates them. |
| `elevation.py` | UPGRADE-IN-PLACE (extend) | Already has `is_admin`, token-elevation probing, UAC relaunch. V2 adds foreground-process elevation classification (`omniband/win32/elevation.py` wraps/extends, reusing ctypes helpers where possible). |
| `doctor.py` | UPGRADE-IN-PLACE (extend) | Existing `--doctor` covers camera/mic/SendInput/deps. V2 adds COM-port, TTS-voice, elevation and INPUT-size checks via `omniband_engine.py --doctor`; root doctor.py gains an OmniBand section only if cheap — otherwise the engine doctor is authoritative for V2. |
| `config.py`, `logsetup.py`, `single_instance.py`, `tray.py` | KEEP AS-IS | Hot-reload config, logging, mutex, tray all reusable concepts; V2 engine has its own `omniband/config.py` (per-user JSON) to avoid coupling the two products. |
| `adapters/replay.py` | KEEP AS-IS | JSONL recorder/replay for the camera engine; V2 simulator is a separate transport. |
| `adapters/{audio,switch}.py`, `handsfree/**`, `profiles/*.json`, `dashboard` (handsfree) | KEEP AS-IS (teammate-owned) | Not touched. V2 gets its own `host/profiles/` and `dashboard/` at repo root per spec. |
| `main.py` | KEEP AS-IS | Existing product entry point. V2 entry point is the new `host/omniband_engine.py`. |
| `tools/{acceptance,camera_bench,soak,latency_probe,verify_bundle,make_icon}.py` | KEEP AS-IS | `latency_probe.py` stays for the camera engine; V2 gets its own stage-latency instrumentation (§6.5) inside `omniband/latency.py`. |
| `my_project(UnBound)/`, `try_mapping.py`, `omnicontrol.egg-info/` | KEEP AS-IS (orphan) | Stray scratch artifacts; not part of the build. pytest collection of the orphan test dir is disabled (see DEVIATIONS). |
| `firmware/` | CREATE | Absent. Full Arduino-ESP32 firmware per §5. |
| `protocol/` | CREATE | `constants.json` single source of truth + `PROTOCOL.md`. |
| `host/omniband*` | CREATE | V2 host engine package + entry point. Reuses root `gateway.py` primitives. |
| `dashboard/` (root) | CREATE | Spec'd stdlib SSE dashboard. `handsfree/dashboard/` untouched. |
| `docs/` | CREATE | All required documents. |

## 3. compat_v1 / legacy consumer search (required by §1)

Searched the whole tree for `piezo_raw`, `lat_ms`, `PsychicIntent`, `serial`,
`clench`: **no host-side consumer exists in `unbound`**. The only matches are in
the *external* `omnicontrol_os` prototype's tests (`tests/test_omnicontrol_system.py`
imports `D:\unbound\omnicontrol_os`, outside this repo). The V2 `compat_v1` stream
(§5.6) is therefore implemented per the spec's best-effort field semantics
(`piezo_raw` = CH4 averaged counts, `pulse` = env/thr_on clamped 0..2,
`clench` = CH4 active, `lat_ms` = tick→TX ms) and flagged here as RECON-mandated.

## 4. Contract bridging

`core/events.py` is frozen. V2 does not emit `SignalEvent`s (those are
webcam/mic/switch-typed). Instead `omniband/adapters/compat_v1.py` can translate
V2 gesture events into `Intent`/`OutputCommand` for the *existing* engine if a
deployment wants piezo-driven camera-engine intents; the standalone V2 engine
uses its own router. Both paths are tested.
