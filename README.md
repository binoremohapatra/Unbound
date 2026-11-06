# Unbound: Handsfree Multimodal Controller

**Unbound** is an open-source, hands-free assistive controller for gaming and desktop accessibility. It translates head pose (yaw/pitch/roll), facial blendshapes, mouth acoustics, and adaptive switches into low-latency virtual gamepad (Xbox 360) and keyboard/mouse outputs.

---

## Architecture Overview

```
handsfree/
  adapters/
    webcam.py        # MediaPipe head pose & blendshape event generator
    audio.py         # Pop detector, Vosk speech-to-intent, speech activity flag
    switch.py        # Adaptive keyboard/hardware switch listener
    replay.py        # Landmark stream recording playback
  core/
    events.py        # SignalEvent and Intent dataclasses
    calibration.py   # Neutral pose, ranges, and user threshold manager
    filters.py       # One-Euro, EMA, hysteresis, and debounce filters
    fastpath.py      # Low-latency head pose to virtual stick vector (< 10ms)
    slowpath.py      # Facial blendshapes to discrete gesture events
    state_machine.py # State machine, layers, latches, allowed intents
    mapping.py       # Profile JSON to low-level output commands
    gateway.py       # vgamepad / pydirectinput dispatch & release_all()
    watchdog.py      # Heartbeat monitor & emergency input release
  profiles/
    default.json     # Standard virtual gamepad mapping
    supertuxkart.json# SuperTuxKart control mapping
  dashboard/
    server.py        # FastAPI + WebSocket telemetry server
    static/index.html# Live HTML5 HUD with virtual stick & gauge telemetry
  tests/
    recordings/      # Recorded landmark streams (.jsonl)
    test_safety.py   # Watchdog and emergency release test suite
    test_state_machine.py
    test_filters.py
    test_fastpath.py
  main.py            # Central engine wiring threads, flags & safe modes
```

---

## Features

- **Fastpath Steering**: 1€ filtered head tracking with customizable response curves and deadzones.
- **Slowpath Gestures**: Blendshape activations with Schmitt trigger hysteresis and debouncing.
- **Speech Activity Inhibition**: Automatic suppression of mouth-gesture triggers while speaking.
- **Safety Watchdog**: Auto-releases all pressed inputs and resets state if camera/adapter heartbeats stall.
- **Real-time Telemetry HUD**: Embedded FastAPI + WebSocket dashboard showing live stick coordinates and sensor meters.
- **Deterministic Replay**: Record and replay landmark streams without needing physical hardware connected.

---

## Installation

```bash
git clone https://github.com/shauryrana/Unbound.git
cd Unbound
pip install -r requirements.txt
```

---

## Quickstart

Run with default virtual gamepad profile and local HUD dashboard:
```bash
python main.py
```

Run in dry-run mode (no virtual gamepad driver required):
```bash
python main.py --dry-run
```

Run deterministic replay simulation:
```bash
python main.py --replay handsfree/tests/recordings/sample_session.jsonl --dry-run
```

Access the live HUD dashboard in your browser:
```
http://127.0.0.1:8080
```

---

## Testing

Run all unit tests:
```bash
python -m unittest discover -s handsfree/tests
```

