# Assumptions Record

This document records engineering and architectural assumptions made during the execution of the surgical master fix prompt.

1. **Operating System & Python Environment**: Windows 11 64-bit AMD64, Python 3.11.x 64-bit.
2. **Frameworks & GUI**: PySide6 is used for the desktop interface.
3. **Model Runtime**: MediaPipe Tasks Python API (`mediapipe.tasks.python.vision.FaceLandmarker`) is the primary neural face landmarker (`face_landmarker.task`), running in VIDEO mode synchronously on the tracking thread.
4. **Detector Assist**: `cv2.FaceDetectorYN` using OpenCV Zoo's `face_detection_yunet_2023mar.onnx` serves as Stage S2 / S3 fallback when the primary landmarker drops detection across consecutive frames.
5. **Headphone Occlusion & Lighting**: White over-ear headphones occlude ears and crown; lighting is dim room conditions. No artificial landmarks or geometric fallback meshes will be synthesized; degraded states will clearly declare `tier="KEYPOINT5"` or `face_found=False`.
6. **Key Injection Boundaries**: `SendInput` / Win32 scancode keyboard injection is restricted by a sink-level process executable gate (`Cyberpunk2077.exe` or `Unbound.exe` with title `Cyber-Arena`). In desktop head-mouse mode, zero movement keys (WASD) may ever reach desktop apps (Notepad, browser, etc.).
