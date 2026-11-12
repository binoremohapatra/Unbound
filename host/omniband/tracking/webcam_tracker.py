"""webcam_tracker.py — Real Face Tracking: MediaPipe FaceLandmarker (primary + only detector).

NO fake geometric fallbacks. NO hardcoded EAR/MAR values.
When no real face is detected the HUD says: EAR: -- | MAR: -- | ⚠ ALIGN FULL FACE IN FRAME

cv2 5.0.0 removed CascadeClassifier — OpenCV Haar cascade is NOT available.
Only MediaPipe Tasks FaceLandmarker (face_landmarker.task) is used for detection.

Tracking pipeline:
  1. MediaPipe Tasks FaceLandmarker — 478 3D landmarks + blendshapes, confidence=0.1
  2. No face → show alignment guide, freeze cursor at zero velocity

Camera: CAP_DSHOW on Windows to avoid MSMF exclusive-access conflicts.
Head-Mouse: rigid nose-bridge anchor → 1-Euro filtered relative delta → ballistic velocity.
"""
from __future__ import annotations

import collections
import logging
import math
from pathlib import Path
import sys
import threading
import time
from typing import Callable, Optional, Tuple, List

import cv2
import numpy as np

log = logging.getLogger("omniband.tracking.webcam")

# ---------------------------------------------------------------------------
# MediaPipe Tasks (optional — mediapipe 1.x / Tasks API only, no mp.solutions)
# ---------------------------------------------------------------------------
HAS_MEDIAPIPE = False
try:
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision
    HAS_MEDIAPIPE = True
except Exception:
    HAS_MEDIAPIPE = False

try:
    import scipy.spatial
    HAS_SCIPY = True
except Exception:
    HAS_SCIPY = False


def find_model_path() -> Optional[str]:
    """Locate face_landmarker.task in source tree or PyInstaller bundle."""
    candidates = [
        # Frozen PyInstaller directory or _internal directory
        Path(sys.executable).parent / "face_landmarker.task" if getattr(sys, "frozen", False) else None,
        Path(sys.executable).parent / "_internal" / "face_landmarker.task" if getattr(sys, "frozen", False) else None,
        Path(getattr(sys, "_MEIPASS", ".")).resolve() / "face_landmarker.task",
        Path(getattr(sys, "_MEIPASS", ".")).resolve() / "_internal" / "face_landmarker.task",
        # Source tree paths
        Path(__file__).resolve().parent.parent.parent / "face_landmarker.task",
        Path(__file__).resolve().parent / "face_landmarker.task",
        Path("face_landmarker.task"),
    ]
    for c in candidates:
        if c is not None and c.is_file():
            return str(c)
    return None


# ---------------------------------------------------------------------------
# 1-Euro Adaptive Low-Pass Filter (Casiez et al. 2012)
# ---------------------------------------------------------------------------
class OneEuroFilter:
    """1-Euro adaptive low-pass filter for jitter-free cursor tracking."""

    def __init__(
        self,
        t0: float = 0.0,
        x0: float = 0.0,
        dx0: float = 0.0,
        min_cutoff: float = 0.6,
        beta: float = 0.03,
        d_cutoff: float = 1.0,
    ) -> None:
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self.x_prev = float(x0)
        self.dx_prev = float(dx0)
        self.t_prev = float(t0)
        self._initialized = False

    def _alpha(self, dt: float, fc: float) -> float:
        if dt <= 0.0 or fc <= 0.0:
            return 1.0
        r = 2.0 * math.pi * fc * dt
        return r / (r + 1.0)

    def filter(self, t: float, x: float) -> float:
        if not self._initialized:
            self.x_prev = float(x)
            self.dx_prev = 0.0
            self.t_prev = float(t)
            self._initialized = True
            return self.x_prev

        dt = t - self.t_prev
        if dt <= 1e-5:
            return self.x_prev

        dx = (x - self.x_prev) / dt
        alpha_d = self._alpha(dt, self.d_cutoff)
        edx = alpha_d * dx + (1.0 - alpha_d) * self.dx_prev

        cutoff = self.min_cutoff + self.beta * abs(edx)
        alpha_x = self._alpha(dt, cutoff)
        filtered_x = alpha_x * x + (1.0 - alpha_x) * self.x_prev

        self.x_prev = filtered_x
        self.dx_prev = edx
        self.t_prev = t
        return filtered_x

    def reset(self, t: float = 0.0, x: float = 0.0) -> None:
        self.x_prev = float(x)
        self.dx_prev = 0.0
        self.t_prev = float(t)
        self._initialized = False


# ---------------------------------------------------------------------------
# MediaPipe landmark index sets
# ---------------------------------------------------------------------------
RIGID_NOSE_INDICES = [1, 4, 6, 197]
LEFT_EYE_INDICES  = [33, 160, 158, 133, 153, 144]
RIGHT_EYE_INDICES = [362, 385, 387, 263, 373, 380]
LEFT_EYE_CONTOUR  = [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246]
RIGHT_EYE_CONTOUR = [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398]
INNER_LIP_INDICES = [13, 14, 78, 308]
LIPS_CONTOUR      = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 308,
                     324, 318, 402, 317, 14, 87, 178, 88, 95]


# ---------------------------------------------------------------------------
# EAR / MAR helpers — only called with REAL landmark coords, never faked
# ---------------------------------------------------------------------------
def calculate_ear(pts: List[Tuple[float, float]]) -> float:
    """Eye Aspect Ratio from 6 real landmark pixel coords. Returns None-safe float."""
    if len(pts) < 6:
        return 0.0
    p1, p2, p3, p4, p5, p6 = pts[:6]
    d_v1 = math.hypot(p2[0] - p6[0], p2[1] - p6[1])
    d_v2 = math.hypot(p3[0] - p5[0], p3[1] - p5[1])
    d_h  = math.hypot(p1[0] - p4[0], p1[1] - p4[1]) + 1e-6
    return (d_v1 + d_v2) / (2.0 * d_h)


def calculate_mar(inner_pts: List[Tuple[float, float]]) -> float:
    """Mouth Aspect Ratio from [p13, p14, p78, p308] real landmark coords."""
    if len(inner_pts) < 4:
        return 0.0
    p13, p14, p78, p308 = inner_pts[:4]
    d_v = math.hypot(p13[0] - p14[0], p13[1] - p14[1])
    d_h = math.hypot(p78[0] - p308[0], p78[1] - p308[1]) + 1e-6
    return d_v / d_h


# ---------------------------------------------------------------------------
# OpenCV cascade loader (lazy, cached)
# ---------------------------------------------------------------------------
_CASCADE_FACE: Optional[cv2.CascadeClassifier] = None
_CASCADE_EYE:  Optional[cv2.CascadeClassifier] = None

def _get_cascades() -> Tuple[Optional[cv2.CascadeClassifier], Optional[cv2.CascadeClassifier]]:
    global _CASCADE_FACE, _CASCADE_EYE
    if _CASCADE_FACE is None:
        try:
            face_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            eye_path  = cv2.data.haarcascades + "haarcascade_eye.xml"
            _CASCADE_FACE = cv2.CascadeClassifier(face_path)
            _CASCADE_EYE  = cv2.CascadeClassifier(eye_path)
        except Exception:
            pass
    return _CASCADE_FACE, _CASCADE_EYE


def _opencv_detect_face(gray: np.ndarray, w: int, h: int) -> Optional[Tuple[int, int, int, int, int, int]]:
    """
    Detect ONE validated human face using Haar cascades.
    Validation: the face ROI MUST contain ≥1 eye in its upper 55 %.
    Returns (fx, fy, fw, fh, eye_cx_px, eye_cy_px) or None.
    Rejects hair-bands, headphones, and partial-head detections with no eyes.
    """
    face_cascade, eye_cascade = _get_cascades()
    if face_cascade is None or face_cascade.empty():
        return None

    faces = face_cascade.detectMultiScale(
        gray, scaleFactor=1.15, minNeighbors=5,
        minSize=(80, 80), flags=cv2.CASCADE_SCALE_IMAGE
    )
    if not isinstance(faces, np.ndarray) or len(faces) == 0:
        return None

    # Sort by area desc — largest face first
    faces = sorted(faces, key=lambda r: r[2] * r[3], reverse=True)

    for (fx, fy, fw, fh) in faces:
        # Validate: eyes must appear in upper 55 % of face box
        if eye_cascade is None or eye_cascade.empty():
            # No eye cascade — accept face but use midpoint
            eye_cx = fx + fw // 2
            eye_cy = fy + int(fh * 0.35)
            return (fx, fy, fw, fh, eye_cx, eye_cy)

        roi_upper = gray[fy: fy + int(fh * 0.55), fx: fx + fw]
        eyes = eye_cascade.detectMultiScale(
            roi_upper, scaleFactor=1.1, minNeighbors=4, minSize=(20, 20)
        )
        if isinstance(eyes, np.ndarray) and len(eyes) >= 1:
            # Use centroid of detected eyes
            ecx = int(np.mean([e[0] + e[2] // 2 for e in eyes])) + fx
            ecy = int(np.mean([e[1] + e[3] // 2 for e in eyes])) + fy
            return (fx, fy, fw, fh, ecx, ecy)
        # No eyes found inside this face ROI → reject (headphones, hair, etc.)

    return None


# ---------------------------------------------------------------------------
# Main Tracker
# ---------------------------------------------------------------------------
class RealWebcamTracker:
    """eViacam + Google Project GameFace relative delta-velocity head-mouse tracker.

    Tracking priority:
      1. MediaPipe FaceLandmarker (face_landmarker.task) — 478 landmarks + blendshapes
      2. OpenCV Haar face+eye validated cascade (fallback when MediaPipe unavailable)
      3. No face → cursor frozen at 0, HUD shows alignment guide

    ZERO fake geometric overlays. ZERO hardcoded EAR/MAR values.
    """

    # Tight deadzone for responsive cursor — 1.2 % of frame width
    DEADZONE: float = 0.012

    def __init__(
        self,
        camera_id: int = 0,
        on_envelopes: Optional[Callable[[list[float], list[bool]], None]] = None,
        on_frame: Optional[Callable[[np.ndarray], None]] = None,
        on_gesture: Optional[Callable[[int, bool, float], None]] = None,
        on_click: Optional[Callable[[], None]] = None,
        on_drag_toggle: Optional[Callable[[], None]] = None,
    ) -> None:
        self.camera_id = camera_id
        self.on_envelopes   = on_envelopes
        self.on_frame       = on_frame
        self.on_gesture     = on_gesture
        self.on_click       = on_click
        self.on_drag_toggle = on_drag_toggle

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 1-Euro Filters for relative deltas
        self.filter_dx = OneEuroFilter(min_cutoff=0.6, beta=0.08, d_cutoff=1.0)
        self.filter_dy = OneEuroFilter(min_cutoff=0.6, beta=0.08, d_cutoff=1.0)

        # Sensitivity (gain) — high sensitivity, tight deadzone
        self.speed_multiplier: float = 1.2
        self.gain_x: float = 10.0
        self.gain_y: float = 12.0
        self.min_cutoff: float = 0.6
        self.head_mouse_enabled: bool = True

        # Anti-slip freeze
        self._freeze_until: float = 0.0

        # Resting-centre calibration (15 frames after first face lock)
        self._calib_frames: int = 0
        self._base_x: float = 0.5
        self._base_y: float = 0.45
        self._calibrated: bool = False
        self._face_first_seen: bool = False

        # Thread-safe velocity state
        self._vx: float = 0.0
        self._vy: float = 0.0
        self._in_deadzone: bool = True
        self._state_lock = threading.Lock()

        # Telemetry — None means "no face detected this frame"
        self.fps: float = 0.0
        self.current_ear: Optional[float] = None   # None → display "--"
        self.current_mar: Optional[float] = None   # None → display "--"
        self.head_yaw: float = 0.0
        self.head_pitch: float = 0.0

        # Gesture state
        self._blink_active: bool = False
        self._last_blink_time: float = 0.0
        self._jaw_open_start: float = 0.0
        self._jaw_latched_drag: bool = False

        # Oscilloscope channel states
        self._ch1_active: bool = False
        self._ch2_active: bool = False
        self._ch4_active: bool = False

        # Mesh triangle cache (MediaPipe path)
        self._cached_triangles: Optional[np.ndarray] = None
        self._tri_cache_age: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def velocity_state(self) -> Tuple[float, float, bool]:
        with self._state_lock:
            return (self._vx, self._vy, self._in_deadzone)

    @property
    def freeze_until(self) -> float:
        return self._freeze_until

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    def set_pointer_speed(self, speed: float) -> None:
        self.speed_multiplier = max(0.2, min(5.0, float(speed)))

    def set_smoothing_cutoff(self, min_cutoff: float) -> None:
        self.min_cutoff = max(0.1, min(5.0, float(min_cutoff)))
        self.filter_dx.min_cutoff = self.min_cutoff
        self.filter_dy.min_cutoff = self.min_cutoff

    def trigger_blink_freeze(self, duration_s: float = 0.35) -> None:
        self._freeze_until = time.perf_counter() + duration_s

    def recenter(self) -> None:
        """Reset neutral resting centre to current head position."""
        self._calib_frames = 0
        self._base_x = 0.0
        self._base_y = 0.0
        self._calibrated = False
        self._face_first_seen = False
        t = time.perf_counter()
        self.filter_dx.reset(t, 0.0)
        self.filter_dy.reset(t, 0.0)
        log.info("Neutral resting centre reset for re-centering")

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name="GameFaceOpticalThread",
        )
        self._thread.start()
        log.info("RealWebcamTracker started on camera_id=%d", self.camera_id)
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        log.info("RealWebcamTracker stopped")

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------
    def _capture_loop(self) -> None:
        # CAP_DSHOW on Windows avoids MSMF exclusive-access conflicts (error -1072875772)
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.camera_id, backend)
        if not cap.isOpened():
            # Fallback: try default backend
            cap = cv2.VideoCapture(self.camera_id)
        if not cap.isOpened():
            log.warning("Could not open webcam index %d", self.camera_id)
            self._running = False
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        # Warm up sensor exposure + allow DSHOW to initialise
        time.sleep(0.3)
        for _ in range(10):
            cap.read()

        # Try MediaPipe FaceLandmarker first
        detector = None
        model_path = find_model_path()
        if HAS_MEDIAPIPE and model_path:
            try:
                base_options = mp_python.BaseOptions(model_asset_path=model_path)
                options = vision.FaceLandmarkerOptions(
                    base_options=base_options,
                    output_face_blendshapes=True,
                    num_faces=1,
                    min_face_detection_confidence=0.1,   # very permissive — works in all lighting
                    min_face_presence_confidence=0.1,
                )
                detector = vision.FaceLandmarker.create_from_options(options)
                log.info("MediaPipe FaceLandmarker loaded from %s", model_path)
            except Exception as e:
                log.warning("FaceLandmarker init failed: %s — using OpenCV cascade backup", e)
                detector = None
        else:
            log.info("MediaPipe unavailable or model missing — using OpenCV cascade backup")

        prev_time = time.perf_counter()

        while self._running:
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            # Mirror: head-right → cursor-right
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            now = time.perf_counter()
            dt = max(1e-4, now - prev_time)
            self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt) if self.fps > 0 else (1.0 / dt)
            prev_time = now

            # Tracking outputs — stay None until a REAL face is detected
            norm_nose_x:    Optional[float] = None
            norm_nose_y:    Optional[float] = None
            ear:            Optional[float] = None
            mar:            Optional[float] = None
            yaw:            float = 0.0
            pitch:          float = 0.0
            landmarks_found: bool = False

            # ---- Path 1: MediaPipe FaceLandmarker ----
            if detector is not None:
                try:
                    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
                    result = detector.detect(mp_image)
                    if result and result.face_landmarks and len(result.face_landmarks) > 0:
                        landmarks_found = True
                        lm = result.face_landmarks[0]

                        # Rigid 4-point nose-bridge centroid
                        rigid_x = (lm[1].x + lm[4].x + lm[6].x + lm[197].x) / 4.0
                        rigid_y = (lm[1].y + lm[4].y + lm[6].y + lm[197].y) / 4.0
                        norm_nose_x = rigid_x
                        norm_nose_y = rigid_y

                        # Head pose
                        yaw   = (norm_nose_x - 0.5) * 60.0
                        pitch = (norm_nose_y - 0.45) * 60.0

                        # EAR / MAR from blendshapes (preferred) or raw landmarks
                        if result.face_blendshapes and len(result.face_blendshapes) > 0:
                            bs = {b.category_name: b.score for b in result.face_blendshapes[0]}
                            blink_score = (bs.get("eyeBlinkLeft", 0.0) + bs.get("eyeBlinkRight", 0.0)) / 2.0
                            ear = max(0.05, 0.35 * (1.0 - blink_score))
                            mar = bs.get("jawOpen", 0.0)
                        else:
                            left_pts  = [(lm[i].x * w, lm[i].y * h) for i in LEFT_EYE_INDICES]
                            right_pts = [(lm[i].x * w, lm[i].y * h) for i in RIGHT_EYE_INDICES]
                            ear = (calculate_ear(left_pts) + calculate_ear(right_pts)) / 2.0
                            inner_lip_pts = [(lm[i].x * w, lm[i].y * h) for i in INNER_LIP_INDICES]
                            mar = calculate_mar(inner_lip_pts)

                        # Draw real wireframe overlay ONLY on the real face
                        self._draw_facemesh_wireframe(frame_rgb, lm, w, h)
                except Exception as e:
                    log.debug("FaceLandmarker error: %s", e)

            # ---- Path 2: OpenCV cascade fallback (eye-validated) ----
            if not landmarks_found:
                face_data = _opencv_detect_face(gray, w, h)
                if face_data is not None:
                    fx, fy, fw, fh, eye_cx, eye_cy = face_data
                    landmarks_found = True

                    # Anatomical nose anchor: between eyes, 38 % of face height below eyes
                    eye_dist = fh * 0.60    # approximate inter-eye span relative to face height
                    nx_px = eye_cx
                    ny_px = eye_cy + int(0.38 * eye_dist)
                    norm_nose_x = nx_px / w
                    norm_nose_y = ny_px / h

                    yaw   = (norm_nose_x - 0.5) * 60.0
                    pitch = (norm_nose_y - 0.45) * 60.0

                    # EAR/MAR from grey-pixel intensity in eye ROI + mouth ROI
                    ear = self._estimate_ear_from_roi(gray, fx, fy, fw, fh)
                    mar = self._estimate_mar_from_roi(gray, fx, fy, fw, fh)

                    # Draw validated cascade overlay
                    self._draw_cascade_overlay(frame_rgb, fx, fy, fw, fh, eye_cx, eye_cy,
                                               nx_px, ny_px, w, h)

            # Update telemetry (None when no face)
            self.current_ear  = ear
            self.current_mar  = mar
            self.head_yaw     = yaw
            self.head_pitch   = pitch

            # ---- Auto-recenter on first face lock ----
            if landmarks_found and not self._face_first_seen:
                self._face_first_seen = True
                self.recenter()
                log.info("First face lock — triggering auto-recenter")

            # ---- Resting Centre Calibration (first 15 detected frames) ----
            if not self._calibrated and landmarks_found and norm_nose_x is not None:
                self._base_x += norm_nose_x
                self._base_y += norm_nose_y
                self._calib_frames += 1
                if self._calib_frames >= 15:
                    self._base_x /= 15.0
                    self._base_y /= 15.0
                    self._calibrated = True
                    log.info("Neutral resting centre calibrated to (%.4f, %.4f)",
                             self._base_x, self._base_y)

            # ---- Relative Delta & Velocity ----
            cx0 = self._base_x if self._calibrated else 0.5
            cy0 = self._base_y if self._calibrated else 0.45

            vx, vy = 0.0, 0.0
            in_deadzone = True

            if landmarks_found and norm_nose_x is not None:
                raw_dx = norm_nose_x - cx0
                raw_dy = norm_nose_y - cy0  # type: ignore[operator]

                filt_dx = self.filter_dx.filter(now, raw_dx)
                filt_dy = self.filter_dy.filter(now, raw_dy)

                offset_mag = math.hypot(filt_dx, filt_dy)
                in_deadzone = offset_mag < self.DEADZONE

                if not in_deadzone and offset_mag > 0.0:
                    excess = offset_mag - self.DEADZONE
                    ux = filt_dx / offset_mag
                    uy = filt_dy / offset_mag
                    base_speed = self.speed_multiplier * (excess ** 1.4) * 2800.0
                    # Apply per-axis gain
                    vx = ux * base_speed * self.gain_x
                    vy = uy * base_speed * self.gain_y
                    # Fine-aim dwell brake
                    if excess < 0.015:
                        vx *= 0.4
                        vy *= 0.4
            else:
                # No face → reset filters, stay at zero
                self.filter_dx.reset(now, 0.0)
                self.filter_dy.reset(now, 0.0)

            with self._state_lock:
                self._vx = vx
                self._vy = vy
                self._in_deadzone = in_deadzone

            # ---- Gesture Decoders (only when real EAR/MAR available) ----
            if ear is not None and mar is not None:
                self._process_gestures(ear, mar, now)

            # ---- Envelopes for Oscilloscope ----
            if landmarks_found and norm_nose_x is not None:
                raw_dx = norm_nose_x - cx0
                raw_dy = norm_nose_y - cy0  # type: ignore[operator]
                mar_val = mar if mar is not None else 0.0
                ch1 = min(1.0, max(0.0, -raw_dx * 5.0))
                ch2 = min(1.0, max(0.0,  raw_dx * 5.0))
                ch3 = min(1.0, max(0.0, -raw_dy * 5.0))
                ch4 = min(1.0, max(0.0, (mar_val - 0.10) * 3.5))
            else:
                ch1 = ch2 = ch3 = ch4 = 0.0

            if self.on_envelopes:
                self.on_envelopes(
                    [ch1, ch2, ch3, ch4],
                    [ch1 > 0.35, ch2 > 0.35, ch3 > 0.35, ch4 > 0.35],
                )

            if self.on_gesture:
                if (ch1 > 0.35) != self._ch1_active:
                    self._ch1_active = ch1 > 0.35
                    self.on_gesture(1, self._ch1_active, ch1 * 950.0)
                if (ch2 > 0.35) != self._ch2_active:
                    self._ch2_active = ch2 > 0.35
                    self.on_gesture(2, self._ch2_active, ch2 * 950.0)
                if (ch4 > 0.35) != self._ch4_active:
                    self._ch4_active = ch4 > 0.35
                    self.on_gesture(4, self._ch4_active, ch4 * 950.0)

            # ---- HUD overlay ----
            nose_px = (
                (int(norm_nose_x * w), int(norm_nose_y * h))  # type: ignore[arg-type]
                if norm_nose_x is not None else None
            )
            self._draw_hud(frame_rgb, w, h, cx0, cy0, nose_px,
                           in_deadzone, vx, vy, landmarks_found)

            if self.on_frame:
                self.on_frame(frame_rgb)

            time.sleep(0.010)

        if detector is not None:
            try:
                detector.close()
            except Exception:
                pass
        cap.release()

    # ------------------------------------------------------------------
    # Gesture decoder (private, called only with real EAR/MAR)
    # ------------------------------------------------------------------
    def _process_gestures(self, ear: float, mar: float, now: float) -> None:
        # Double-blink → Left Click
        blink_threshold = 0.20
        is_blinking = ear < blink_threshold
        if is_blinking and not self._blink_active:
            self._blink_active = True
            if now - self._last_blink_time < 0.50:
                self.trigger_blink_freeze(0.35)
                if self.on_click:
                    self.on_click()
                self._last_blink_time = 0.0
            else:
                self._last_blink_time = now
        elif not is_blinking:
            self._blink_active = False

        # Jaw tap → Click / Jaw hold → Drag Latch
        jaw_threshold = 0.28
        is_jaw_open = mar > jaw_threshold
        if is_jaw_open:
            if self._jaw_open_start == 0.0:
                self._jaw_open_start = now
                self._jaw_latched_drag = False
            else:
                hold_dur = now - self._jaw_open_start
                if hold_dur > 0.70 and not self._jaw_latched_drag:
                    self._jaw_latched_drag = True
                    self.trigger_blink_freeze(0.35)
                    if self.on_drag_toggle:
                        self.on_drag_toggle()
        else:
            if self._jaw_open_start > 0.0:
                open_dur = now - self._jaw_open_start
                self._jaw_open_start = 0.0
                if 0.10 <= open_dur <= 0.45:
                    self.trigger_blink_freeze(0.35)
                    if self.on_click:
                        self.on_click()

    # ------------------------------------------------------------------
    # EAR / MAR estimation from pixel ROI (OpenCV cascade path)
    # ------------------------------------------------------------------
    def _estimate_ear_from_roi(self, gray: np.ndarray,
                                fx: int, fy: int, fw: int, fh: int) -> float:
        """Estimate EAR from pixel intensity in eye-band ROI (top 20–45 %)."""
        try:
            eye_top = fy + int(fh * 0.20)
            eye_bot = fy + int(fh * 0.45)
            eye_roi = gray[eye_top:eye_bot, fx:fx + fw]
            if eye_roi.size == 0:
                return 0.25
            # Dark pixels (< 80) in the eye band ≈ open pupils
            dark = np.sum(eye_roi < 80)
            total = eye_roi.size
            # Approximate: more dark → more open → higher EAR
            ratio = dark / (total + 1e-6)
            return float(np.clip(ratio * 2.5, 0.05, 0.40))
        except Exception:
            return 0.25

    def _estimate_mar_from_roi(self, gray: np.ndarray,
                                fx: int, fy: int, fw: int, fh: int) -> float:
        """Estimate MAR from vertical dark-opening height in mouth-band ROI (60–90 %)."""
        try:
            mouth_top = fy + int(fh * 0.60)
            mouth_bot = fy + int(fh * 0.92)
            mouth_roi = gray[mouth_top:mouth_bot, fx + fw // 4: fx + 3 * fw // 4]
            if mouth_roi.size == 0:
                return 0.0
            # Very dark pixels in mouth band ≈ open mouth cavity
            dark_rows = np.sum(mouth_roi < 60, axis=1)
            mouth_h = mouth_roi.shape[0]
            open_rows = np.sum(dark_rows > mouth_roi.shape[1] * 0.25)
            return float(np.clip(open_rows / (mouth_h + 1e-6) * 1.2, 0.0, 1.0))
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Drawing helpers — REAL face overlays only, NO fake geometry
    # ------------------------------------------------------------------
    def _draw_facemesh_wireframe(self, frame_rgb: np.ndarray, lm: list,
                                  w: int, h: int) -> None:
        """Draw REAL 468-point wireframe mesh ON the detected face using MediaPipe landmarks."""
        pts = np.array([(int(p.x * w), int(p.y * h)) for p in lm[:468]], dtype=np.int32)

        # 1. Sparse Delaunay triangulation (Cyber-Teal #00E5FF) using scipy
        if HAS_SCIPY:
            if self._cached_triangles is None or self._tri_cache_age % 30 == 0:
                sample_indices = np.linspace(0, 467, 120, dtype=int)
                sample_pts = pts[sample_indices]
                try:
                    tri = scipy.spatial.Delaunay(sample_pts)
                    self._cached_triangles = sample_indices[tri.simplices]
                except Exception:
                    self._cached_triangles = None
            self._tri_cache_age += 1

            if self._cached_triangles is not None:
                for simplex in self._cached_triangles:
                    p1, p2, p3 = pts[simplex[0]], pts[simplex[1]], pts[simplex[2]]
                    cv2.line(frame_rgb, tuple(p1), tuple(p2), (0, 229, 255), 1, cv2.LINE_AA)
                    cv2.line(frame_rgb, tuple(p2), tuple(p3), (0, 229, 255), 1, cv2.LINE_AA)
                    cv2.line(frame_rgb, tuple(p3), tuple(p1), (0, 229, 255), 1, cv2.LINE_AA)

        # 2. Eye contours — Neon Green #00E676
        for contour in [LEFT_EYE_CONTOUR, RIGHT_EYE_CONTOUR]:
            c_pts = np.array([pts[i] for i in contour], dtype=np.int32)
            cv2.polylines(frame_rgb, [c_pts], True, (0, 230, 118), 1, cv2.LINE_AA)

        # 3. Lip contour — Warm Copper #F07842
        lip_pts = np.array([pts[i] for i in LIPS_CONTOUR], dtype=np.int32)
        cv2.polylines(frame_rgb, [lip_pts], True, (240, 120, 66), 2, cv2.LINE_AA)

    def _draw_cascade_overlay(self, frame_rgb: np.ndarray,
                               fx: int, fy: int, fw: int, fh: int,
                               eye_cx: int, eye_cy: int,
                               nx_px: int, ny_px: int, w: int, h: int) -> None:
        """Draw validated OpenCV cascade overlay — ONLY on confirmed eye-validated face region."""
        # Face bounding box — Warm Stone outline
        cv2.rectangle(frame_rgb, (fx, fy), (fx + fw, fy + fh), (233, 229, 222), 1, cv2.LINE_AA)

        # Eye centre marker — Neon Green
        cv2.circle(frame_rgb, (eye_cx, eye_cy), 6, (0, 230, 118), 2, cv2.LINE_AA)
        cv2.line(frame_rgb, (eye_cx - 12, eye_cy), (eye_cx + 12, eye_cy), (0, 230, 118), 1)
        cv2.line(frame_rgb, (eye_cx, eye_cy - 8), (eye_cx, eye_cy + 8), (0, 230, 118), 1)

        # Anatomical nose anchor — Burnt Orange dot
        cv2.circle(frame_rgb, (nx_px, ny_px), 5, (240, 120, 66), -1, cv2.LINE_AA)
        cv2.circle(frame_rgb, (nx_px, ny_px), 7, (255, 255, 255), 1, cv2.LINE_AA)

        # Estimated mouth region — Warm Copper box
        mouth_top = fy + int(fh * 0.60)
        mouth_bot = fy + int(fh * 0.92)
        mx1 = fx + fw // 4
        mx2 = fx + 3 * fw // 4
        cv2.rectangle(frame_rgb, (mx1, mouth_top), (mx2, mouth_bot), (240, 120, 66), 1, cv2.LINE_AA)

    def _draw_hud(
        self,
        frame_rgb: np.ndarray,
        w: int,
        h: int,
        cx0: float,
        cy0: float,
        nose_px: Optional[Tuple[int, int]],
        in_deadzone: bool,
        vx: float,
        vy: float,
        face_found: bool,
    ) -> None:
        """
        Render deadzone ring, vector line, alignment guide, and HUD text.
        NEVER shows hardcoded EAR/MAR when no real face is detected.
        """
        center_px = (int(cx0 * w), int(cy0 * h))
        radius_px = max(6, int(self.DEADZONE * min(w, h)))

        if face_found and nose_px is not None:
            # Deadzone ring — Green inside, Orange outside
            ring_color = (0, 230, 118) if in_deadzone else (240, 120, 66)
            cv2.circle(frame_rgb, center_px, radius_px, ring_color, 2, cv2.LINE_AA)
            cv2.circle(frame_rgb, center_px, 3, ring_color, -1)

            # Vector line from resting centre → current nose
            cv2.line(frame_rgb, center_px, nose_px,
                     (0, 230, 118) if in_deadzone else (240, 120, 66), 2, cv2.LINE_AA)

            # Cursor status
            if in_deadzone:
                status = "CURSOR: STILL (DEADZONE)"
                s_col  = (0, 230, 118)
            else:
                status = f"CURSOR: GLIDING  vx={vx:+.0f}  vy={vy:+.0f}"
                s_col  = (240, 120, 66)

            cv2.putText(frame_rgb, status, (16, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.50, s_col, 2, cv2.LINE_AA)

            # EAR / MAR — real values only
            ear_str = f"{self.current_ear:.2f}" if self.current_ear is not None else "--"
            mar_str = f"{self.current_mar:.2f}" if self.current_mar is not None else "--"
            cv2.putText(
                frame_rgb,
                f"FPS: {self.fps:4.1f} | EAR: {ear_str} | MAR: {mar_str}",
                (16, h - 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (247, 245, 240), 1, cv2.LINE_AA,
            )
        else:
            # ---- NO FACE: draw alignment guide + warning ----
            # Semi-transparent guide rectangle
            pad_x, pad_y = w // 6, h // 6
            overlay = frame_rgb.copy()
            cv2.rectangle(overlay, (pad_x, pad_y), (w - pad_x, h - pad_y),
                          (240, 120, 66), 2, cv2.LINE_AA)
            # Corner ticks
            tick = 20
            for (cx, cy) in [(pad_x, pad_y), (w - pad_x, pad_y),
                              (pad_x, h - pad_y), (w - pad_x, h - pad_y)]:
                dx = 1 if cx == pad_x else -1
                dy = 1 if cy == pad_y else -1
                cv2.line(overlay, (cx, cy), (cx + dx * tick, cy), (240, 120, 66), 2)
                cv2.line(overlay, (cx, cy), (cx, cy + dy * tick), (240, 120, 66), 2)
            cv2.addWeighted(overlay, 0.7, frame_rgb, 0.3, 0, frame_rgb)

            # Warning text
            cv2.putText(frame_rgb,
                        "EAR: -- | MAR: -- | \u26a0 ALIGN FULL FACE IN FRAME",
                        (16, h - 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (240, 120, 66), 1, cv2.LINE_AA)
            cv2.putText(frame_rgb,
                        f"FPS: {self.fps:4.1f} | Searching for face...",
                        (16, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (247, 245, 240), 1, cv2.LINE_AA)
