# OmniBand V2 — User Walkthrough & Operating Guide

## 1. Quick Start
Launch the application by running the built desktop binary or Python engine:
```powershell
# From repository root:
.\dist\Unbound\Unbound.exe

# Or via Python:
python host\omniband_engine.py
```

---

## 2. Head-Mouse Control & Calibration

### 2.1 Resting Center Alignment
1. Position your head comfortably facing the webcam.
2. Click **Recenter Face (Zero)** or press **F11** to toggle head-mouse mode.
3. The app establishes your neutral resting center:
   - Green circle: Deadzone (pointer stays stationary within deadzone).
   - Moving your head outside the circle glides the cursor smoothly in that direction.
   - Returning to center stops cursor motion cleanly.

### 2.2 Gestures
- **Left Click**: Double-blink or quick mouth tap (0.10s–0.45s).
- **Drag & Hold Lock**: Open and hold jaw open for > 0.70 seconds.
- **Anti-Slip Freeze**: A 350ms cursor freeze automatically activates on clicks and drags so cursor does not jitter during intentional facial gestures.

---

## 3. Desktop Key-Leak Protection (ProcessGate)
- In **Desktop**, **Browser**, **Chat**, or **Notepad** windows, head movements **never** type `a`, `d`, `w`, or `s`.
- Gaming keys are gated at the Win32 kernel level: scancodes are only transmitted when the active window is `Cyberpunk2077.exe` or `Unbound.exe` on the Cyber-Arena tab.
- Typing on desktop is accomplished via the **Floating Virtual Keyboard** or Windows OSK, which never steal focus from your target window.

---

## 4. Self-Test & Diagnostic Commands
The executable and Python CLI support 4 standalone verification probes:
```powershell
# 1. Probe runtime dependencies (Python, OpenCV, MediaPipe, PySide6)
.\dist\Unbound\Unbound.exe --selftest-env

# 2. Verify model files and SHA-256 integrity
.\dist\Unbound\Unbound.exe --selftest-resources

# 3. Test multi-stage tracker pipeline and negative controls
.\dist\Unbound\Unbound.exe --selftest-tracker

# 4. Test camera device capture and exposure
.\dist\Unbound\Unbound.exe --selftest-camera
```
All 4 probes return exit code 0 when healthy.

---

## 5. Double-Blink Selection & Calm Cursor Speed Calibrations

### Double-Blink & Selection Fixes
1. **Adaptive EAR Baseline**: Instead of rigid fixed thresholds, the tracker dynamically tracks open-eye baseline and triggers blinks at `min(0.24, max(0.21, baseline * 0.78))`.
2. **Human Blink Cadence**: Expanded the double-blink detection window from `0.50s` to `0.10s <= gap <= 0.85s` to comfortably capture natural human blink rates.
3. **Deliberate Long-Blink / Wink Selection**: Closing eyes deliberately for `>= 0.40s` triggers a left click immediately without needing repeated blinks.
4. **Dual Win32 Injection & Qt Thread Safety**: Biometric events are dispatched to the main Qt GUI thread via `sig_biometric_click` and inject both modern `SendInput` packets (with 25ms mouse down dwell) and legacy `mouse_event` fallback to select icons, buttons, and links across all Windows 11 apps.

### Calm Cursor Speed Calibrations
1. **Gain Reduction**: Lowered `gain_x` from `10.0` to `5.5` and `gain_y` from `12.0` to `6.0`.
2. **Speed Multiplier**: Reduced default base multiplier to `0.8x` (slider range `0.2x – 2.5x`).
3. **Fine-Aim Dwell**: Widened deadzone threshold to `excess < 0.025` with `0.35x` dampening for micro-targeting.

### Verification Status
- **Full Pytest Suite**: 709 passed, 0 failed, 5 skipped in 34.56s.
- **Frozen Binary Gate**: `dist/Unbound/Unbound.exe` verified with `--selftest-env`, `--selftest-resources`, `--selftest-tracker`, and `--selftest-camera` passing.
