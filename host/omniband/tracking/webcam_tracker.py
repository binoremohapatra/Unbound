"""webcam_tracker.py - Real-time webcam optical face & jaw motion tracker.

Runs in a background thread:
1. Captures live frames from cv2.VideoCapture(0)
2. Computes real physical motion optical envelopes:
   - CH1: Left-ward head movement (left optical flow / center-of-mass shift)
   - CH2: Right-ward head movement
   - CH3: Upward / nod motion (pitch)
   - CH4: Jaw drop / mouth opening motion (vertical facial elongation & intensity delta)
3. Generates preview QImage frames with crosshair & facial tracking bounding box.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Tuple

import cv2
import numpy as np

log = logging.getLogger("omniband.optical.tracker")


class RealWebcamTracker:
    """Live webcam optical face & motion analyzer."""

    def __init__(
        self,
        camera_id: int = 0,
        on_envelopes: Optional[Callable[[list[float], list[bool]], None]] = None,
        on_frame: Optional[Callable[[np.ndarray], None]] = None,
        on_gesture: Optional[Callable[[int, bool, float], None]] = None,
    ) -> None:
        self.camera_id = camera_id
        self.on_envelopes = on_envelopes
        self.on_frame = on_frame
        self.on_gesture = on_gesture

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._prev_gray: Optional[np.ndarray] = None
        self._face_cx = 320.0
        self._face_cy = 240.0
        self._ch1_active = False
        self._ch2_active = False
        self._ch4_active = False

        # Auto-centering baseline calibration (first 15 frames)
        self._calib_frames = 0
        self._base_x = 0.0
        self._base_y = 0.0
        self._calibrated = False

        # Head mouse state
        self.head_mouse_enabled = False

    def recenter(self) -> None:
        """Trigger re-calibration of the resting neutral face center."""
        self._calib_frames = 0
        self._base_x = 0.0
        self._base_y = 0.0
        self._calibrated = False
        log.info("Face baseline reset for re-centering")

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="WebcamOpticalThread")
        self._thread.start()
        log.info("RealWebcamTracker started on camera_id=%d", self.camera_id)
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        log.info("RealWebcamTracker stopped")

    def _capture_loop(self) -> None:
        cap = cv2.VideoCapture(self.camera_id)
        if not cap.isOpened():
            log.warning("Could not open webcam index %d", self.camera_id)
            self._running = False
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        # Warmup sensor
        for _ in range(5):
            cap.read()

        while self._running:
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w = frame.shape[:2]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            ch1 = 0.0  # Steer Left
            ch2 = 0.0  # Steer Right
            ch3 = 0.0  # Nod / Up
            ch4 = 0.0  # Jaw Drop / Mouth Open

            if self._prev_gray is not None:
                delta = cv2.absdiff(self._prev_gray, gray)
                thresh = cv2.threshold(delta, 20, 255, cv2.THRESH_BINARY)[1]

                # Center region where face resides
                cx_start, cx_end = int(w * 0.25), int(w * 0.75)
                cy_start, cy_end = int(h * 0.15), int(h * 0.85)
                roi = thresh[cy_start:cy_end, cx_start:cx_end]

                # Optical flow / movement magnitude
                moments = cv2.moments(roi)
                total_area = (cx_end - cx_start) * (cy_end - cy_start)
                if moments["m00"] > 100:
                    mx = moments["m10"] / moments["m00"]
                    my = moments["m01"] / moments["m00"]

                    # Calibration over first 15 frames
                    if not self._calibrated:
                        self._base_x += mx
                        self._base_y += my
                        self._calib_frames += 1
                        if self._calib_frames >= 15:
                            self._base_x /= 15.0
                            self._base_y /= 15.0
                            self._calibrated = True
                            log.info("Face baseline calibrated to (%.1f, %.1f)", self._base_x, self._base_y)

                    center_x = self._base_x if self._calibrated else (cx_end - cx_start) / 2.0
                    center_y = self._base_y if self._calibrated else (cy_end - cy_start) / 2.0

                    norm_x = (mx - center_x) / ((cx_end - cx_start) / 2.0)
                    norm_y = (my - center_y) / ((cy_end - cy_start) / 2.0)
                    motion_intensity = min(1.0, float(moments["m00"]) / (total_area * 0.25))

                    # Strict ±18% neutral deadzone
                    DEADZONE = 0.18
                    if norm_x < -DEADZONE:
                        ch1 = min(1.0, motion_intensity * ((abs(norm_x) - DEADZONE) / (1.0 - DEADZONE)) * 2.5)
                    elif norm_x > DEADZONE:
                        ch2 = min(1.0, motion_intensity * ((norm_x - DEADZONE) / (1.0 - DEADZONE)) * 2.5)

                    # Lower third of face roi: jaw / mouth region
                    jaw_roi = roi[int(roi.shape[0] * 0.65):, :]
                    jaw_moments = cv2.moments(jaw_roi)
                    if jaw_moments["m00"] > 60:
                        jaw_intensity = min(1.0, float(jaw_moments["m00"]) / (jaw_roi.size * 0.35))
                        if jaw_intensity > 0.22:
                            ch4 = (jaw_intensity - 0.22) / 0.78

                    if norm_y < -DEADZONE:
                        ch3 = min(1.0, motion_intensity * ((abs(norm_y) - DEADZONE) / (1.0 - DEADZONE)) * 2.0)

                    # Webcam Head-Mouse feature: move Windows mouse cursor
                    if self.head_mouse_enabled and (abs(norm_x) > DEADZONE or abs(norm_y) > DEADZONE):
                        try:
                            import ctypes
                            dx = int((norm_x if abs(norm_x) > DEADZONE else 0.0) * 16)
                            dy = int((norm_y if abs(norm_y) > DEADZONE else 0.0) * 16)
                            pt = ctypes.wintypes.POINT()
                            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                            ctypes.windll.user32.SetCursorPos(pt.x + dx, pt.y + dy)
                        except Exception:
                            pass

            self._prev_gray = gray

            # Draw visual tracking HUD onto preview frame
            cv2.rectangle(frame_rgb, (int(w * 0.25), int(h * 0.15)), (int(w * 0.75), int(h * 0.85)), (240, 120, 66), 2)
            cv2.line(frame_rgb, (int(w * 0.5), int(h * 0.15)), (int(w * 0.5), int(h * 0.85)), (113, 109, 103), 1)
            cv2.line(frame_rgb, (int(w * 0.25), int(h * 0.65)), (int(w * 0.75), int(h * 0.65)), (240, 120, 66), 1)
            cv2.putText(frame_rgb, "REAL WEBCAM TRACKING ACTIVE", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (76, 175, 80), 2)
            cv2.putText(frame_rgb, f"CH1(L):{ch1:.2f} CH2(R):{ch2:.2f} CH4(JAW):{ch4:.2f}", (20, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (247, 245, 240), 1)

            if self.on_frame:
                self.on_frame(frame_rgb)

            if self.on_envelopes:
                active = [ch1 > 0.35, ch2 > 0.35, ch3 > 0.35, ch4 > 0.35]
                self.on_envelopes([ch1, ch2, ch3, ch4], active)

            if self.on_gesture:
                # Steer Left (CH1)
                is_ch1 = ch1 > 0.35
                if is_ch1 != self._ch1_active:
                    self._ch1_active = is_ch1
                    self.on_gesture(1, is_ch1, ch1 * 950.0)

                # Steer Right (CH2)
                is_ch2 = ch2 > 0.35
                if is_ch2 != self._ch2_active:
                    self._ch2_active = is_ch2
                    self.on_gesture(2, is_ch2, ch2 * 950.0)

                # Jaw Drop / Mouth Open (CH4)
                is_ch4 = ch4 > 0.35
                if is_ch4 != self._ch4_active:
                    self._ch4_active = is_ch4
                    self.on_gesture(4, is_ch4, ch4 * 950.0)

            time.sleep(0.02)  # ~50 FPS capture loop

        cap.release()
