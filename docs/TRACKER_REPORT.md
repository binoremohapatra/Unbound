# OmniBand V2 — Neural Face Tracking & Key-Leak Gate Report

## 1. Executive Summary
This document records the empirical diagnosis, architectural overhaul, and verification results for the OmniBand V2 head-mouse tracking pipeline and key injection safety gate.

Prior to this fix, the application suffered from two critical defects:
1. **Face Tracking Failure & Fake Geometric Overlays**: Under low-light room conditions and with white headphones on, earlier iterations fell back to OpenCV Haar cascades or fake geometric meshes (pentagon/spiderweb radial fans) anchored to hair and headphones.
2. **Key-Leak in Desktop Apps**: Head movement in desktop apps (such as Notepad, browsers, and chats) was emitting `a` and `d` scancodes intended exclusively for racing games.

Both issues have been completely solved:
- **Zero Fake Overlays**: Every fake geometric drawing function and Haar cascade was permanently eliminated from production paths.
- **Multi-Stage Neural Pipeline (M-01, M-02, M-03)**: MediaPipe Tasks `FaceLandmarker` in `RunningMode.VIDEO` paired with OpenCV `cv2.FaceDetectorYN` (YuNet 2023mar ONNX) and LAB CLAHE + adaptive gamma enhancement.
- **ProcessGate Protection (K-03)**: A Win32 sink-level executable gate (`QueryFullProcessImageNameW`) blocks all movement scancodes (`W`, `A`, `S`, `D`, etc.) unless the foreground process is explicitly verified as `Cyberpunk2077.exe` or `Unbound.exe` on the Cyber-Arena tab. All keys are immediately released on focus transition.

---

## 2. Empirical Verification Results

### 2.1 Live Camera Human Room Verification (`tools/live_verify.py`)
Tested directly in user room environment with low ambient lighting and white over-ear headphones:
```
=== OMNIBAND V2 LIVE CAMERA TRACKER VERIFICATION (3.0s) ===
Warming up camera sensor...
Starting live tracking measurement for 3.0 seconds. Please face camera...

--- MEASUREMENT SUMMARY ---
Total Frames Acquired: 41
FPS: Mean=14.1, Median=12.5, Min=9.6, Max=25.3
Tier Breakdown:
  FACEMESH478 :   41 frames (100.0%)
  KEYPOINT5   :    0 frames (  0.0%)
  NO_FACE     :    0 frames (  0.0%)

Overall Face Presence: 100.0%
Mean EAR: 0.279 (min=0.138, max=0.339)
Mean MAR: 0.001 (min=0.000, max=0.004)
Mean Yaw: +6.3 deg, Mean Pitch: -30.8 deg

VERDICT: [PASS] Face tracking confirmed stable.
```

### 2.2 Key-Leak ProcessGate Repro Test (`tests/test_key_leak_repro.py`)
Tested 6 critical desktop scenarios against `ProcessGate`:
1. `Notepad.exe` foreground -> Key injection strictly blocked (`PASS`).
2. `Cyberpunk2077.exe` foreground -> Key injection allowed (`PASS`).
3. Spoofed window title inside `chrome.exe` -> Blocked by executable identity (`PASS`).
4. `Unbound.exe` with `Cyber-Arena` active -> Allowed (`PASS`).
5. Window focus switch mid-hold -> Immediate hardware `release_all()` (`PASS`).
6. Unreadable / protected process -> Fail-safe blocked (`PASS`).

### 2.3 Negative Controls (`tests/test_negative_controls.py`)
R01 compliance verified against non-face inputs:
- Pure black frame (zeros) -> `face_found=False`, `tier="NO_FACE"`.
- Pure white frame (255s) -> `face_found=False`, `tier="NO_FACE"`.
- Uniform random noise -> `face_found=False`, `tier="NO_FACE"`.
- Linear intensity gradient -> `face_found=False`, `tier="NO_FACE"`.
- Geometric circles and rectangles -> `face_found=False`, `tier="NO_FACE"`.

### 2.4 Model Integrity Manifest (`models/MANIFEST.json`)
- `face_landmarker.task`: SHA-256 `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff` (3,758,596 bytes).
- `face_detection_yunet_2023mar.onnx`: SHA-256 `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` (232,589 bytes).

---

## 3. Architecture Summary
- **Capture Thread**: Decoupled `FrameSource` with `CAP_PROP_BUFFERSIZE=1`, skipping sensor warm-up black frames.
- **Detector**: MediaPipe Tasks `FaceLandmarker` + OpenCV `cv2.FaceDetectorYN` assist.
- **Head-Mouse**: 1-Euro filtered relative delta with 1.2% deadzone and fine-aim brake.
- **Reticle HUD**: 56x56 px tactical overlay (`WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOPMOST`).
- **Floating OSK**: `WM_MOUSEACTIVATE -> MA_NOACTIVATE` preventing focus theft.
- **Build Verification**: `tools/verify_frozen_tracker.py` running `--selftest-*` CLI suite against frozen binary.

