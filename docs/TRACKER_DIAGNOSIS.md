# Phase 0: Tracker Diagnosis Report (Empirical & Verbatim)

Generated at: 2026-11-13T01:56:00+05:30
Environment: Windows 11 (AMD64 64-bit), Python 3.11.9

---

## 1. D-01: Environment & Package Audit

### Command Execution:
```powershell
python --version
python -c "import struct; print(struct.calcsize('P')*8)"
pip list | Select-String -Pattern "mediapipe|opencv|numpy|PySide6|pyinstaller|onnxruntime|sounddevice"
```

### Verbatim Output:
```text
Python 3.11.9
64

mediapipe                                1.2.3
numpy                                    1.26.4
onnxruntime                              1.30.0
onnxruntime-gpu                          1.31.0
opencv-contrib-python                    4.10.0.84
pyinstaller                              6.22.3
pyinstaller-hooks-contrib                2026.10
PySide6                                  6.12.0
PySide6_Addons                           6.12.0
PySide6_Essentials                       6.12.0
PySide6_Pdf                              6.12.0.140
PySide6_WebEngine                        6.12.0.140
sounddevice                              0.5.6
```

### Multiple OpenCV Distributions Finding & Remediation:
- **Pre-Fix Finding**: System previously had BOTH `opencv-python 5.0.0.93` AND `opencv-python-headless 4.10.0.84` installed. `cv2 5.0.0` had removed `cv2.CascadeClassifier`, broke module paths, and conflicted with `mediapipe`'s native wheel dependencies.
- **Action Taken**: Completely uninstalled `opencv-python` and `opencv-python-headless`. Pinned exactly ONE clean OpenCV distribution: `opencv-contrib-python==4.10.0.84` in `requirements.txt`. Pinned `sounddevice>=0.4.6`.

---

## 2. D-02: API Probe (`tools/probe_env.py`)

### Command Execution:
```powershell
python tools/probe_env.py
```

### Verbatim Output:
```json
{
  "python_version": "3.11.9 (tags/v3.11.9:de54cf5, Apr  2 2024, 10:12:12) [MSC v.1938 64 bit (AMD64)]",
  "pointer_size_bits": 64,
  "mediapipe": {
    "installed": true,
    "version": "1.2.3",
    "has_solutions": false,
    "tasks_available": true,
    "FaceLandmarker": true,
    "FaceLandmarkerOptions": true,
    "RunningMode": true,
    "BaseOptions": true
  },
  "opencv": {
    "installed": true,
    "version": "4.10.0",
    "has_FaceDetectorYN": true,
    "haarcascades_dir": "C:\\Users\\mr baddy\\AppData\\Local\\Programs\\Python\\Python311\\Lib\\site-packages\\cv2\\data\\",
    "haarcascades_exists": true,
    "xml_files_count": 17
  },
  "numpy": {
    "version": "1.26.4"
  },
  "PySide6": {
    "version": "6.12.0"
  },
  "PyInstaller": {
    "version": "6.22.3"
  }
}
```

### Key API Conclusions:
1. `mediapipe.solutions`: **FALSE** (`mp.solutions` does NOT exist in MediaPipe 1.2.3).
2. MediaPipe Tasks API: **TRUE** (`FaceLandmarker`, `FaceLandmarkerOptions`, `RunningMode`, `BaseOptions` all present and functional).
3. `cv2.FaceDetectorYN`: **TRUE** (supported in OpenCV 4.10.0; YuNet detector assist can be fully utilized).

---

## 3. D-03: Frozen Binary Probe (`dist/Unbound`)

### Inventory of Model/Data Files in `dist/Unbound`:
```text
D:\unbound\unbound\dist\Unbound\face_landmarker.task           3,758,596 bytes
D:\unbound\unbound\dist\Unbound\_internal\face_landmarker.task 3,758,596 bytes
```

### Missing Files Identified:
- `models/face_detection_yunet_2023mar.onnx`: Not yet present in repo or `dist/Unbound`. Required for M-02 detector assist.
- `models/MANIFEST.json`: Required for M-06 SHA256 integrity verification.

---

## 4. D-04: Camera Probe (`tools/camera_probe.py`)

### Command Execution:
```powershell
python tools/camera_probe.py --seconds 5
```

### Verbatim Output:
```text
=== Camera Probe Report (Index 0, 5.0s duration) ===

--- Backend: CAP_DSHOW ---
  Opened: True (in 0.348s)
  Negotiated: 640x480 @ 0.0 fps (YUY2)
  Exposure: -1.0 (can_set=False), AutoExp=-1.0, Gain=16.0, Brightness=128.0
  Measured FPS: 4.11 (22 frames in 5.354s)
  Intervals: p50=56.01ms, p95=1001.21ms
  Mean Luma: 17.39 / 255 (by sec: {0: 2.84, 1: 0.0, 2: 5.68, 3: 0.0, 4: 20.23, 5: 47.6})

--- Backend: CAP_MSMF ---
  Opened: True (in 0.215s)
  Negotiated: 640x480 @ 30.0 fps (    )
  Exposure: -1.0 (can_set=False), AutoExp=-1.0, Gain=16.0, Brightness=128.0
  Measured FPS: 2.07 (11 frames in 5.304s)
  Intervals: p50=63.11ms, p95=2207.65ms
  Mean Luma: 17.84 / 255 (by sec: {0: 0.0, 4: 18.04, 5: 33.86})
```

### Critical Empirical Findings:
1. **Initial Black Frames**: During seconds 0 to 3, `Mean Luma` is `0.0` to `5.68` (sensor exposure warmup ramp-up). Single-shot capture checks fail if run without warming up.
2. **Dim Room Ambient Luma**: Ambient luma settles around `47.6 / 255` (~18% of dynamic range). This necessitates **L-03 CLAHE + Gamma enhancement** on model input copies.
3. **Backend Choice**: `CAP_DSHOW` delivers twice the throughput of `CAP_MSMF` (4.11 fps warmup vs 2.07 fps) and p50 frame intervals of 56 ms (~18 fps active).

---

## 5. D-05: Root-Cause Synthesis Table

| Cause Area | Finding | Empirical Evidence | Impact & Required Fix |
|:---|:---|:---|:---|
| **(a) Legacy `mp.solutions`** | Missing in `mediapipe 1.2.3` | `has_solutions: false` in `tools/probe_env.py` | Must use Tasks API `FaceLandmarker` exclusively (M-01). |
| **(b) Tasks API availability** | Present and verified | `tasks_available: true`, `FaceLandmarker: true` in probe | Use `FaceLandmarkerOptions(running_mode=RunningMode.VIDEO)`. |
| **(c) Models in `dist/`** | `face_landmarker.task` present; YuNet missing | File inventory: only `face_landmarker.task` exists | Create `models/MANIFEST.json`, download `face_detection_yunet_2023mar.onnx` via `tools/fetch_models.py` (M-02, M-06). |
| **(d) Haar Cascades in PyInstaller** | Obsolete & broken | `cv2 5.0.0` had removed `CascadeClassifier`; XML paths unbundled | Move all Haar code to `legacy/` (R02, M-08). |
| **(e) Camera Luma & FPS in Dim Room** | Initial luma=0, settling at 47.6/255 | `camera_probe.py`: sec 0 luma=2.84, sec 5 luma=47.6 | Implement L-01 latest-frame queue and L-03 CLAHE+Gamma enhancer. |
| **(f) Bottom Telemetry Crash** | Unhandled `None` formatting crash | `main_window.py` line 335: `current_ear:.2f` crashed on `None` | Guard telemetry format strings to display `"--"` when `None`. |


## 6. K-01: Key Leak Path Identification & Diagnosis

### Exact Reproduction Path:
1. When head turns left/right (`ch1`/`ch2` envelope), `RealWebcamTracker.on_gesture(ch, active, amp)` fires from `webcam_tracker.py`.
2. In `host/omniband/app/main_window.py` line 504: `_on_optical_gesture(ch, down, amp)` is invoked.
3. If `self.webcam_tracker.head_mouse_enabled` is False (or during mode toggles, physical key simulation, or when `tabs.currentIndex() != 1` and `currentIndex() != 3`), `cmds.append(OutputCommand(kind="key", name="a", pressed=down))` is queued.
4. `self.gw.apply(cmds)` -> `self.gw.flush()` -> `Gateway._sync_locked()` calls `self.backend.key("a", True)`.
5. `gateway.py` lines 170-174 (`SendInputBackend.key()`):
   ```python
   def key(self, name: str, down: bool) -> None:
       if name in SCANCODES:
           self._q.append(build_key_input(name, down))
   ```
6. `SendInputBackend.commit()` issues raw Win32 `SendInput()` scancodes directly to the active Windows foreground window (e.g. Notepad, browser, chat).
7. **Systemic Defect**: There was ZERO process-level validation or sink-level executable gate inside `SendInputBackend` before calling `SendInput`. Any key event that bypassed higher-level GUI checks immediately leaked into the user's active window.
8. **Required Resolution (K-02 & K-03)**:
   - Structural separation: Head-mouse (desktop) profile contains ZERO movement-key actions.
   - Sink-level gate inside `SendInputBackend` / `Gateway`: verifies foreground window process via `QueryFullProcessImageNameW`. Only allow-listed processes (`Cyberpunk2077.exe` or `Unbound.exe` with title `Cyber-Arena`) receive gated keys (`{w, a, s, d, space, lshift, lctrl}`). All other windows drop the key, log `LEAK_BLOCKED`, and increment the leak block counter.
