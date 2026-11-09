<div align="center">

# 🎮 OmniControl (Unbound)
### **High-Performance, Safety-Critical Hands-Free Game & Desktop Controller**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Platform Windows 11](https://img.shields.io/badge/platform-Windows%2011%20x64-0078D6.svg?style=for-the-badge&logo=windows&logoColor=white)](https://microsoft.com/windows)
[![Tests Passing](https://img.shields.io/badge/tests-passed-brightgreen.svg?style=for-the-badge&logo=pytest&logoColor=white)](#automated-test-suite)
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
- **Real-Time Telemetry HUD & Calibration**: Built-in FastAPI + WebSocket live visualizer for neutral pose tuning, blendshape activation bars, and virtual stick tracking.

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

## 📁 Repository Structure

```
Unbound/
├── main.py                  # OmniControl 60 Hz main loop & orchestrator
├── state_machine.py         # Deterministic derived-state finite state machine
├── gateway.py               # Pure Win32 SendInput hardware-accurate output gateway
├── watchdog.py              # Heartbeat deadman channels & latency logger
├── doctor.py                # System diagnostics, permissions, & camera/mic audit
├── tray.py                  # Windows notification area system tray controller
├── config.py                # Hot-reloading application configuration watcher
├── elevation.py             # UAC detection and admin elevation manager
├── single_instance.py       # Global named mutex single-instance enforcer
├── logsetup.py              # Ring-buffer logging and emergency crash hooks
├── version.py               # Semantic version declaration
├── adapters/
│   └── replay.py            # Stream recorder and deterministic replay source
├── core/
│   ├── clock.py             # Monotonic time provider
│   └── events.py            # SignalEvent, Intent, and OutputCommand models
├── handsfree/
│   ├── adapters/            # Webcam (MediaPipe), audio (pop/Vosk), switch
│   ├── core/                # Filters (One-Euro, EMA), calibration, fastpath, slowpath
│   ├── dashboard/           # FastAPI + WebSocket server & HTML5 HUD
│   └── profiles/            # Game mapping profiles (default, supertuxkart)
├── tests/                   # Automated pytest suites (210+ test cases)
└── requirements.txt         # Project dependencies
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

### 4. Telemetry HUD Dashboard (`handsfree/dashboard/`)
- Embedded FastAPI + WebSocket dashboard running on `http://127.0.0.1:8080`.
- Visualizes real-time head pose (yaw/pitch/roll), facial blendshape activation bars, analog stick vector canvas, active pressed virtual keys, and system states.

---

## 🩺 System Diagnostics (`--doctor`)

OmniControl features a built-in diagnostic subsystem that evaluates hardware, permissions, and latency characteristics:

```bash
python main.py --doctor
```

---

## 🚀 Quick Start & Installation

```bash
# Clone the repository
git clone https://github.com/shauryrana/Unbound.git
cd Unbound

# Install dependencies
pip install -r requirements.txt

# Run hardware and permission doctor
python main.py --doctor
```

### Running OmniControl

```bash
# Run with live camera in dry-run mode (safe testing)
python main.py --dry-run

# Run with local telemetry HUD dashboard enabled
python main.py --dashboard

# Replay deterministic session
python main.py --replay demo.jsonl --dry-run --speed 2

# Standalone handsfree controller mode
python main.py --handsfree
```

---

## 🕹️ CLI Options & Modes

```bash
python main.py [options]
```

| Flag | Type | Description |
|---|---|---|
| `--doctor` | Flag | Runs full hardware, permission, and timer diagnostics and exits. |
| `--dry-run` | Flag | Simulates input pipeline without sending real OS keystrokes or mouse movements. |
| `--dashboard` | Flag | Launches Shaury's real-time telemetry HUD server (`http://127.0.0.1:8080`). |
| `--safe` | Flag | Safe mode: starts pipeline with injection disabled for camera calibration. |
| `--elevate` | Flag | Requests UAC administrator elevation if not currently elevated. |
| `--replay <FILE>` | String | Replays deterministic recorded `.jsonl` landmark stream through the pipeline. |
| `--speed <N>` | Float | Speed multiplier for replay (e.g. `2.0` for 2x speed). |
| `--record <FILE>` | String | Records live sensor events into a replayable JSONL session file. |
| `--hz <N>` | Float | Target loop frequency (default: `60.0` Hz). |
| `--profile <NAME>` | String | Key/axis mapping profile (default: `"default"`). |
| `--handsfree` | Flag | Runs standalone handsfree controller entrypoint directly. |

---

## 🧪 Automated Test Suite

```bash
# Run full test suite with pytest
python -m pytest -v

# Run handsfree unit tests
python -m unittest discover -s handsfree/tests
```

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

## 🗺️ Mapping & Profile System (Dishti's Slice)

This section covers everything in Dishti's ownership:  
`core/mapping.py`, `core/profile.py`, `profiles/*.json`, `adapters/switch.py`, `adapters/audio.py`, `try_mapping.py`.

---

### Profile JSON Format

Profiles live in `profiles/` and control every aspect of the mapping engine.

```jsonc
{
  "game": "supertuxkart",
  "output": "gamepad",           // "gamepad" | "keyboard" — never both
  "layers": {
    "drive": {
      "auto_throttle": 0.8,     // axis value sent on layer entry (gamepad only)
      "stick": { "x_axis": "left_x", "y_axis": "left_y" },
      "actions": {
        "jaw_open": {
          "button": "A",         // "button" for gamepad, "key" for keyboard
          "mode": "tap",         // "tap" | "hold" | "toggle"
          "cooldown_ms": 400
        },
        "brow_raise": { "button": "B", "mode": "toggle", "cooldown_ms": 300 },
        "blink_long":  { "button": "LB", "mode": "hold",  "cooldown_ms": 200 }
      },
      "cancel": ["pop", "brow_hard"]   // intents that trigger cancel_all
    },
    "menu": { "actions": { "jaw_open": { "button": "A", "cooldown_ms": 300 } } }
  },
  "assist": { "hold_ms": 2000, "min_ms": 500, "max_ms": 3000 },
  "flags": { "talk_lock": true, "glasses_fallback": true, "assist": true }
}
```

**Accepted gateway names**

| Output | Accepted values |
|---|---|
| `button` (gamepad) | `A B X Y LB RB LT RT START BACK L3 R3 DPAD_UP DPAD_DOWN DPAD_LEFT DPAD_RIGHT` |
| `axis` (gamepad stick) | `left_x  left_y  right_x  right_y` |
| `key` (keyboard) | lowercase letters, digits `0-9`, `esc tab enter space backspace shift ctrl alt up down left right home end pgup pgdn insert delete f1`–`f10` |

---

### Loading a Profile in Code

```python
from core.profile import load_profile, DEFAULT_PROFILE

profile, errors = load_profile("profiles/supertuxkart.json")
if errors:
    print("Validation problems:", errors)   # falls back to DEFAULT_PROFILE
```

`load_profile` **never raises during play** — a bad file or schema error returns the
safe `DEFAULT_PROFILE` and a list of human-readable messages.

---

### Using the Mapper

```python
from core.events import Intent
from core.mapping import Mapper

mapper = Mapper(profile)
commands = mapper.start()          # emit auto_throttle axis if profile uses it

# Every 60 Hz tick in main.py:
commands = mapper.handle(intent, now=time.monotonic())   # Intent → [OutputCommand]
commands = mapper.tick(now)        # release expired holds / queued state changes
```

**Behaviour by action mode**

| mode | `action` intent | `hold`/`release` intent |
|---|---|---|
| `tap` | press + immediate release | — |
| `hold` | hold until a `release` intent | press on `hold`, release on `release` |
| `toggle` | flip on/off | — |

**Special intents**

| Intent kind | Mapper behaviour |
|---|---|
| `layer <name>` | `release_all`, clear held/toggled/cooldowns, switch layer, re-emit auto_throttle |
| `cancel_all` | same as layer switch (without changing layer) |
| `pause` | same + ignore all intents except `resume` |
| `resume` | resume normal operation |
| `stick x y` | gamepad → axis commands; keyboard → edge-detected direction keys past 0.5 threshold |

---

### `try_mapping.py` — Interactive Tester

No camera, no game. Type intents in a REPL and see exactly what
`OutputCommand`s the gateway would receive.

```bash
python try_mapping.py profiles/supertuxkart.json
```

```
✓  Loaded: profiles/supertuxkart.json  (output=gamepad)
  layers: ['drive', 'menu']
  actions in 'drive': ['jaw_open', 'brow_raise', 'blink_long']
  Type 'help' for commands, 'quit' to exit.

intent> jaw_open
  → OutputCommand(kind='button', name='A', pressed=True, value=0.0)
  → OutputCommand(kind='button', name='A', pressed=False, value=0.0)
intent> stick 0.7 -0.3
  → OutputCommand(kind='axis', name='left_x', pressed=None, value=0.7)
  → OutputCommand(kind='axis', name='left_y', pressed=None, value=-0.3)
intent> layer menu
  → OutputCommand(kind='release_all', name='', pressed=None, value=0.0)
intent> quit
```

Available commands: `<action_name>`, `hold <name>`, `release <name>`,
`stick <x> [y]`, `layer <name>`, `cancel`, `pause`, `resume`, `tick`,
`layers`, `actions`, `profile`, `help`, `quit`.

---

### Switch Adapter

The switch adapter turns a keyboard key (or real adaptive switch) into a
`SignalEvent(src="switch", …)` for the main polling loop.

```python
from adapters.switch import SwitchSource

# Hardware-free test: inject events directly
src = SwitchSource(monitored_keys=["space"])
src.inject("space", pressed=True, t=1.0)
events = src.poll(now=1.0)   # → [SignalEvent(src='switch', name='space', value=1.0, t=1.0)]

# Production: set env var OMNICONTROL_SWITCH=1 so create_source() succeeds
# from adapters.switch import create_source
# src = create_source(monitored_keys=["space"])
```

---

### Audio Pop Detector

Detects a short loud mouth-pop or tongue-click from raw 16-bit LE mono PCM.

```python
from adapters.audio import AudioSource, create_source

# Inject synthetic audio (tests / dry-run):
src = AudioSource(audio_fn=lambda: get_pcm_chunk())

# Real mic (requires sounddevice + a microphone):
# src = create_source()

events = src.poll(now=time.monotonic())
# → [SignalEvent(src='mic', name='pop', value=1.0, t=...)]
```

**Algorithm**: RMS > 0.08 AND peak/RMS > 3.5 (impulsive crest) → pop detected.  
**Refractory period**: 300 ms cooldown after each pop — one physical pop → one event.  
**Vosk voice commands**: off by default; enable with `AudioSource(enable_vosk=True)`.  
No threads. `main.py`'s 60 Hz loop calls `poll()` every tick.

---

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details. Built with pride for accessibility and open-source gaming.
