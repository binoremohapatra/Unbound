<div align="center">

# 🎮 OmniControl (Unbound)
### **High-Performance, Safety-Critical Hands-Free Game & Desktop Controller**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Platform Windows 11](https://img.shields.io/badge/platform-Windows%2011%20x64-0078D6.svg?style=for-the-badge&logo=windows&logoColor=white)](https://microsoft.com/windows)
[![Tests Passing](https://img.shields.io/badge/tests-68%2F68%20passed-brightgreen.svg?style=for-the-badge&logo=pytest&logoColor=white)](#automated-test-suite)
[![Latency](https://img.shields.io/badge/tick%20latency-0.034ms%20p50-success.svg?style=for-the-badge&logo=speedtest&logoColor=white)](#performance-benchmarks)
[![Security UIPI](https://img.shields.io/badge/security-UIPI%20%26%20UAC%20Hardened-orange.svg?style=for-the-badge&logo=shield&logoColor=white)](#uipi--permission-architecture)
[![License MIT](https://img.shields.io/badge/license-MIT-purple.svg?style=for-the-badge)](LICENSE)

*Empowering hands-free desktop navigation and gaming through real-time facial blendshapes, head pose tracking, mouth acoustics, and adaptive switches — built with zero-compromise safety guarantees.*

---

</div>

## 📌 Executive Summary

**OmniControl** is the core system brain and output engine of **Unbound**: a next-generation assistive controller enabling individuals with motor limitations or gamers seeking alternate inputs to play modern PC games (e.g. *GTA V*, *SuperTuxKart*, *Cyberpunk 2077*) and control desktop productivity tools hands-free.

Unlike fragile script-based macro solutions, OmniControl is engineered with **industrial systems reliability**:
- **Pure Win32 `SendInput` Engine**: Injects hardware-accurate DirectX Set-1 scancodes and relative sub-pixel mouse deltas without external driver dependencies (no ViGEm / vgamepad required).
- **Derived-State Finite State Machine**: State is computed deterministically from sensory facts (`SAFE_IDLE > CONVERSE_LOCK > LATCHED > ACTIVE`) — impossible to enter contradictory or corrupted states.
- **Fail-Safe Watchdog Deadman Channels**: Any camera freeze (500 ms) or loop stall (250 ms) immediately releases all virtual inputs.
- **Windows UIPI & Permission Intelligence**: Auto-detects elevated target games, requests UAC elevation safely, and warns before User Interface Privilege Isolation drops input.
- **Hardware-Free Emergency Interventions**: Instant release via face loss (1 frame / 16 ms), tongue click ("pop"), F12 global edge switch, or `Ctrl+Alt+Shift+Q` panic chord.

---

## 🏗️ System Architecture & Data Flow

```
                           +--------------------------+
                           |  Physical Input Sources  |
                           |  (Webcam, Mic, Switches) |
                           +-------------+------------+
                                         |
                                         v
                      +------------------------------------+
                      |    core/events.py (SignalEvent)    |
                      |  src, name, value, timestamp, conf |
                      +------------------+-----------------+
                                         |
                                         v
                     +--------------------------------------+
                     | state_machine.py (Permission Filter) |
                     |  • Safety Signal Extraction (pop/F12)|
                     |  • Derived State Calculation         |
                     |  • Timed Hold Expiry & Dwell Control |
                     +-------------------+------------------+
                                         |
                                         v
                     +--------------------------------------+
                     |  producers (Fastpath / Slowpath)     |
                     |  • Head Yaw/Pitch -> Look Stick      |
                     |  • Jaw Open -> Timed Fire Hold       |
                     |  • Brow Raise -> Action Interact     |
                     +-------------------+------------------+
                                         |
                                         v
                     +--------------------------------------+
                     |         gate() Intent Filter         |
                     |  • Drops gesture intents if locked   |
                     |  • Passes cancel_all & release       |
                     +-------------------+------------------+
                                         |
                                         v
                     +--------------------------------------+
                     |    mapping.py (Action -> Output)     |
                     |  • Gamepad button -> Keyboard key    |
                     +-------------------+------------------+
                                         |
                                         v
                     +--------------------------------------+
                     |      gateway.py (Win32 Engine)       |
                     |  • State Diffs (Releases first!)     |
                     |  • Schmitt Trigger Axis Thresholds   |
                     |  • Sub-pixel Mouse Accumulator       |
                     |  • Sliding Window Rate Limiter       |
                     |  • Batched SendInput Execution       |
                     +-------------------+------------------+
                                         |
                                         v
                           +--------------------------+
                           |  Windows OS & Game View  |
                           +--------------------------+
```

---

## ✨ Key Technical Highlights

### 1. Pure Win32 SendInput Gateway (`gateway.py`)
- **40-Byte Struct Alignment**: Uses exact fixed-width C types (`c_uint32`, `c_int32`, `c_uint16`, `c_size_t`) matching Win32 x64 ABI (`sizeof(INPUT) == 40`).
- **Hardware Scancodes**: Utilizes `KEYEVENTF_SCANCODE` with `KEYEVENTF_EXTENDEDKEY` for arrow keys, navigation keys, and function keys to bypass DirectX DirectInput / raw input filters.
- **Differential State Commit**: Output gateway only tracks *desired* state. On each 60 Hz tick, `flush()` diffs desired vs. held state: **releases first, presses second**, and sends all changes in **one atomic `SendInput` batch**.
- **Schmitt Trigger Axis Hysteresis**: Prevents chattering when mapping analog head tilt to W/A/S/D keys (turns ON at `0.35`, turns OFF at `0.21`).
- **Sub-Pixel Mouse Accumulator**: Preserves fractional remainder deltas across ticks, preventing truncation jitter during subtle head-aiming movements.

### 2. State Machine & Safety Invariants (`state_machine.py`)
- **Single Source of Truth**: State is strictly derived via property:
  $$\text{State} = f(\text{face\_ok}, \text{locked}, \text{latches})$$
- **Priority Cascade**: `SAFE_IDLE > CONVERSE_LOCK > LATCHED > ACTIVE`.
- **Zero-Latency Interrupts**:
  - **Face lost**: Emits `cancel_all` on the **very first absent frame**. Returns to `ACTIVE` only after **300 ms continuous dwell** (flickers immediately reset the timer).
  - **Tongue Click ("pop") / Hard Blink**: Releases active latch in one tick without clearing head-aim steering; event is consumed so it never triggers secondary actions.
  - **Converse Lock ("freeze" / F12)**: Halts all injection immediately. Releases and cancels *always pass* the gate — a latch can **never** be trapped.

### 3. Loop Watchdog & Health Daemon (`watchdog.py`)
- Independent background watchdog monitoring named channels:
  - `"loop"`: 250 ms timeout $\rightarrow$ calls `gateway.release_all()` immediately from the watchdog thread if the main loop stalls or blocks.
  - `"webcam"`: 500 ms timeout $\rightarrow$ automatically synthesizes `face=0` if camera feed drops.
- Channels arm on their very first heartbeat to support missing optional hardware gracefully.

### 4. Enterprise Safety Hardening
- **Single-Instance Named Mutex**: `Global\OmniControl_SingleInstance_v1` ensures two background copies never contend for hardware input.
- **Process Deny-List**: Automatically forces `CONVERSE_LOCK` and drops injection when sensitive applications are focused (`1Password`, `Bitwarden`, `KeePass`, `consent.exe`, `LogonUI.exe`).
- **Session Lock Window**: Hidden Win32 message window captures `WM_WTSSESSION_CHANGE` (workstation lock) and `WM_POWERBROADCAST` (sleep/suspend) to release all inputs instantly.
- **Emergency Crash Handler**: Custom `sys.excepthook` and `threading.excepthook` invokes `gateway.release_all()` **before** serializing traceback to `%APPDATA%\OmniControl\logs\crash.log`.
- **Injection Rate Limiting**: Caps maximum key state transitions per second (default 200/s) to neutralize runaway sensor loops.

---

## 🩺 System Diagnostics (`--doctor`)

OmniControl features a built-in diagnostic subsystem that evaluates hardware, permissions, and latency characteristics:

```bash
omnicontrol --doctor
# or
python main.py --doctor
```

### Diagnostic Audit Matrix

```text
==========================OmniControl Doctor==========================
  [PASS] Python version                   Python 3.11.9
  [PASS] 64-bit process                   64-bit OK
  [PASS] INPUT struct size                sizeof(INPUT) = 40 ✓
  [PASS] Admin status                     Running as administrator
  [PASS] Foreground app / UIPI            Foreground app not elevated
  [PASS] Camera                           1280×720 @ 60 fps
  [PASS] Microphone                       1 s audio RMS = 0.0452
  [PASS] Display / DPI                    DPI=per-monitor-v2, 1 monitor(s), primary=1920×1080
  [PASS] SendInput self-test              F24 down+up: 2/2 sent OK
  [PASS] 60 Hz loop p99 jitter            p99 tick = 1.12 ms (threshold 3.0 ms)
  [PASS] Optional: opencv-python          installed — webcam capture
  [PASS] Optional: pystray                installed — system tray icon
  [PASS] Optional: PIL                    installed — image support for tray
======================================================================
  Result: 0 FAIL(s), 0 WARN(s)

All critical checks passed.
```

---

## 🚀 Quick Start & Installation

### Option 1: Editable Local Install

```bash
# Clone the repository
git clone https://github.com/binoremohapatra/Unbound.git
cd Unbound

# Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install OmniControl in editable mode
pip install -e .

# Run hardware and permission doctor
omnicontrol --doctor
```

### Option 2: Standalone Windows Executables

Compile standalone single-folder executables using PyInstaller:

```cmd
build.bat
```

This compiles two distinct targets in `dist\`:
- `dist\OmniControl_Admin\OmniControl.exe`: Embeds `manifest_admin.xml` (`requireAdministrator`), prompting for UAC at startup. Recommended for controlling elevated games without UIPI blocks.
- `dist\OmniControl\OmniControl.exe`: Embeds `manifest_user.xml` (`asInvoker`), running under standard user privileges.

### Option 3: Inno Setup Windows Installer

Compile `installer.iss` using [Inno Setup 6](https://jrsoftware.org/isdl.php) to produce `Output\OmniControl_Setup_v0.1.0.exe`:
- Installs to `C:\Program Files\OmniControl`
- Creates Start Menu and Desktop shortcuts
- Includes uninstaller that cleanly deletes runtime logs and configurations

---

## 🕹️ CLI Options & Modes

```bash
omnicontrol [options]
```

| Flag | Type | Description |
|---|---|---|
| `--doctor` | Flag | Runs full hardware, permission, and timer diagnostics and exits. |
| `--dry-run` | Flag | Simulates input pipeline without sending real OS keystrokes or mouse movements. |
| `--safe` | Flag | Safe mode: starts pipeline with injection disabled for camera calibration. |
| `--elevate` | Flag | Requests UAC administrator elevation if not currently elevated. |
| `--replay <FILE>` | String | Replays deterministic recorded `.jsonl` landmark stream through the pipeline. |
| `--speed <N>` | Float | Speed multiplier for replay (e.g. `2.0` for 2x speed). |
| `--record <FILE>` | String | Records live sensor events into a replayable JSONL session file. |
| `--hz <N>` | Float | Target loop frequency (default: `60.0` Hz). |
| `--profile <NAME>` | String | Key/axis mapping profile (default: `"default"`). |
| `--config <FILE>` | String | Path to custom `config.json` (defaults to `%APPDATA%\OmniControl\config.json`). |
| `--duration <N>` | Float | Automatically terminates execution after $N$ seconds (useful for benchmarks/CI). |
| `-v, --verbose` | Flag | Enables verbose debug logging. |

---

## 🧪 Automated Test Suite

OmniControl includes an extensive automated test suite covering all safety rules, state transitions, Win32 struct alignment, rate limiters, mutex locks, and crash handlers. All tests execute without requiring physical camera, microphone, or elevated privileges:

```bash
python -m pytest -v
```

```text
tests/test_safety.py::test_input_struct_is_40_bytes_on_x64 PASSED                [  1%]
tests/test_safety.py::test_key_input_uses_scancodes_and_extended_flag PASSED     [  2%]
tests/test_safety.py::test_mouse_inputs PASSED                                   [  4%]
tests/test_safety.py::test_face_loss_releases_everything_on_first_tick PASSED     [  5%]
tests/test_safety.py::test_safe_idle_blocks_input_until_face_back_300ms PASSED   [  7%]
tests/test_safety.py::test_face_flicker_resets_regain_timer PASSED               [  8%]
tests/test_safety.py::test_hold_expires_and_releases PASSED                      [ 10%]
tests/test_safety.py::test_hold_is_capped_at_max_hold_ms PASSED                  [ 11%]
tests/test_safety.py::test_cancel_signals_release_latch_in_one_tick PASSED       [ 13%]
tests/test_safety.py::test_cancel_keeps_head_aim_alive PASSED                    [ 14%]
tests/test_safety.py::test_freeze_cancels_latch_then_locks PASSED                [ 16%]
tests/test_safety.py::test_window_switch_releases_everything PASSED              [ 17%]
tests/test_safety.py::test_gateway_releases_before_pressing PASSED               [ 19%]
tests/test_safety.py::test_axis_key_threshold_has_hysteresis PASSED              [ 20%]
tests/test_safety.py::test_mouse_axis_accumulates_subpixels PASSED               [ 22%]
tests/test_safety.py::test_watchdog_trips_when_loop_stalls PASSED                 [ 23%]
tests/test_safety.py::test_replay_face_loss_mid_hold_ends_with_zero_keys PASSED  [ 25%]
tests/test_app_hardening.py::test_is_admin_check PASSED                          [ 26%]
tests/test_app_hardening.py::test_ensure_admin_relaunches_on_uac_accept PASSED   [ 27%]
tests/test_app_hardening.py::test_check_foreground_elevation_detects_uipi PASSED [ 29%]
tests/test_app_hardening.py::test_single_instance_guard PASSED                   [ 30%]
tests/test_app_hardening.py::test_deny_list_forces_lock_and_cancels_latches PASSED [ 32%]
tests/test_app_hardening.py::test_rate_limiter_trips_and_releases_keys PASSED    [ 33%]
tests/test_app_hardening.py::test_session_lock_releases_keys PASSED              [ 35%]
tests/test_app_hardening.py::test_crash_hook_releases_keys_before_logging PASSED [ 36%]
...
============================= 68 passed in 0.50s ==============================
```

---

## ⚡ Performance Benchmarks

Measured on live 60 Hz execution loop:

| Metric | Target | Measured Result | Status |
|---|---|---|:---:|
| **Engine Tick Duration ($p_{50}$)** | $< 1.0\text{ ms}$ | **$0.034\text{ ms}$** | 🚀 Ultra-Fast |
| **Engine Tick Duration ($p_{99}$)** | $< 3.0\text{ ms}$ | **$0.645\text{ ms}$** | 🚀 Deterministic |
| **Event-to-Output Processing** | $< 16.7\text{ ms}$ | **$0.94\text{ ms}$** | 🚀 Sub-Frame |
| **Frame Overruns** | $0$ | **$0\text{ overruns}$** | 🛡️ Rock Solid |
| **Memory Allocation in Loop** | Zero runtime churn | **Ring buffers only** | 🛡️ GC-Safe |

---

## 🛡️ UIPI, Anti-Cheat & Windows Security Disclosure

To maintain engineering honesty and user safety, OmniControl documents the following hard operating system constraints:

1. **User Interface Privilege Isolation (UIPI)**:
   - A non-elevated application running in Windows cannot send synthetic `SendInput` messages to a higher-integrity (elevated/admin) process.
   - *Solution*: Launch OmniControl as Administrator when controlling games or utilities launched with administrative rights.
2. **UAC Secure Desktop & Logon Screen**:
   - `consent.exe` and `LogonUI.exe` run on isolated secure desktops. User-mode software cannot inject keystrokes into these windows.
   - *Behavior*: OmniControl automatically transitions into `CONVERSE_LOCK` and releases all keys when the workstation is locked.
3. **Kernel Anti-Cheat Software**:
   - Anti-cheat solutions with kernel drivers (BattlEye, Easy Anti-Cheat, Riot Vanguard, Ricochet) hook low-level input stacks and intercept synthetic input events regardless of administrative status.
   - *Policy*: **Use OmniControl strictly in single-player, story mode, or non-competitive titles.** Never attempt to bypass kernel security drivers.
4. **`uiAccess="true"` Path**:
   - Windows allows accessibility applications to bypass UIPI without full admin rights only if the executable is digitally signed by a trusted certificate authority and located in `C:\Program Files\`. OmniControl provides the manifest configuration for this optional advanced workflow.

---

## 👥 Unbound Team Roles & Component Ownership

OmniControl integrates cleanly with all Unbound modules via decoupled `importlib` interfaces:

| Owner | Module Responsibility | Handoff Boundary |
|---|---|---|
| **Binore** | **Systems, Brain & Gateway** | State machine, cancel rules, Win32 SendInput gateway, watchdog deadman, main loop, replay adapter, packaging, and tests. |
| **Antas** | **Vision & Gesture Extraction** | `adapters/webcam.py`, One-Euro filters, fastpath head-tracking, slowpath blendshape triggers, and gesture assistance. |
| **Dishti** | **Input Mapping & Voice** | `mapping.py`, game profile JSONs, `adapters/audio.py` (tongue-pop detector, speech recognition), and `adapters/switch.py`. |
| **Shaury** | **Dashboard & Calibration** | Telemetry HUD (`dashboard/server.py`), real-time HTML5 gauges, and user neutral-pose calibration. |

---

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details. Built with pride for accessibility and open-source gaming.
