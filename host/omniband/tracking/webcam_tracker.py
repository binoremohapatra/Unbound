"""webcam_tracker.py - Google Project GameFace architecture with 468-point 3D FaceMesh & 1-Euro adaptive filter.

Features:
1. MediaPipe 468-Point 3D FaceMesh with Sci-Fi Biometric Wireframe Overlay:
   - Cyber-Teal (#00E5FF) tessellation mesh
   - Neon Green (#00E676) eye contours with EAR readout
   - Warm Copper (#F07842) lips & jaw contours with MAR readout
   - 3D Head Pose vector arrow from nose tip anchor
   - Telemetry HUD: FPS, EAR, MAR, filter mode, head-mouse state
   - Graceful Delaunay geometric mesh fallback if MediaPipe is unavailable
2. Casiez 2012 OneEuroFilter adaptive low-pass filter:
   - min_cutoff = 0.8 Hz (low-speed jitter elimination)
   - beta = 0.04 (high-speed lag reduction)
   - d_cutoff = 1.0 Hz (derivative cutoff)
3. Decoupled sub-pixel cursor targeting:
   - Publishes thread-safe target_pos for decoupled 120 Hz interpolation loop
   - Micro-deadzone of 0.008 to eliminate camera sensor vibration
   - Power-curve acceleration: gain * sign(d) * (|d| ** 1.35)
4. Biometric Gesture Decoders with Anti-Slip Cursor Freeze:
   - Double-blink (EAR < 0.21 twice within 500ms) -> Left Click + 350ms freeze
   - Quick jaw open (MAR > 0.35, 100-450ms) -> Left Click + 350ms freeze
   - Sustained jaw open (MAR > 0.35, > 700ms) -> Toggle Drag Latch + 350ms freeze
"""
from __future__ import annotations

import collections
import logging
import math
import sys
import threading
import time
from typing import Callable, Optional, Tuple, List

import cv2
import numpy as np

log = logging.getLogger("omniband.tracking.webcam")

# Try importing mediapipe face mesh
HAS_MEDIAPIPE = False
try:
    import mediapipe as mp
    if hasattr(mp, "solutions") and hasattr(mp.solutions, "face_mesh"):
        HAS_MEDIAPIPE = True
except Exception:
    HAS_MEDIAPIPE = False


class OneEuroFilter:
    """Casiez et al. 2012 1-Euro adaptive low-pass filter for jitter-free, zero-lag tracking."""

    def __init__(
        self,
        t0: float = 0.0,
        x0: float = 0.0,
        dx0: float = 0.0,
        min_cutoff: float = 0.8,
        beta: float = 0.04,
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

        # 1. Filter the derivative (speed estimate)
        dx = (x - self.x_prev) / dt
        alpha_d = self._alpha(dt, self.d_cutoff)
        edx = alpha_d * dx + (1.0 - alpha_d) * self.dx_prev

        # 2. Adaptive cutoff frequency: increases as velocity increases
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


# MediaPipe FaceMesh key indices
LEFT_EYE_INDICES = [33, 160, 158, 133, 153, 144]
RIGHT_EYE_INDICES = [362, 385, 387, 263, 373, 380]
LEFT_EYE_CONTOUR = [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246]
RIGHT_EYE_CONTOUR = [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398]
INNER_LIP_INDICES = [13, 14, 78, 308]
LIPS_CONTOUR = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95]
NOSE_TIP_IDX = 4
CHIN_IDX = 152
FOREHEAD_IDX = 10


def calculate_ear(pts: List[Tuple[float, float]]) -> float:
    """Calculate Eye Aspect Ratio (EAR) given 6 eye landmarks [p1..p6]."""
    if len(pts) < 6:
        return 0.30
    p1, p2, p3, p4, p5, p6 = pts[:6]
    # Vertical distances
    d_v1 = math.hypot(p2[0] - p6[0], p2[1] - p6[1])
    d_v2 = math.hypot(p3[0] - p5[0], p3[1] - p5[1])
    # Horizontal distance
    d_h = math.hypot(p1[0] - p4[0], p1[1] - p4[1]) + 1e-6
    return (d_v1 + d_v2) / (2.0 * d_h)


def calculate_mar(inner_pts: List[Tuple[float, float]]) -> float:
    """Calculate Mouth Aspect Ratio (MAR) given [p13, p14, p78, p308]."""
    if len(inner_pts) < 4:
        return 0.10
    p13, p14, p78, p308 = inner_pts[:4]
    d_v = math.hypot(p13[0] - p14[0], p13[1] - p14[1])
    d_h = math.hypot(p78[0] - p308[0], p78[1] - p308[1]) + 1e-6
    return d_v / d_h


class RealWebcamTracker:
    """Google Project GameFace style 468-point 3D FaceMesh optical tracker with 1-Euro adaptive smoothing."""

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
        self.on_envelopes = on_envelopes
        self.on_frame = on_frame
        self.on_gesture = on_gesture
        self.on_click = on_click
        self.on_drag_toggle = on_drag_toggle

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 1-Euro Filters for X and Y cursor axes
        self.filter_x = OneEuroFilter(min_cutoff=0.8, beta=0.04, d_cutoff=1.0)
        self.filter_y = OneEuroFilter(min_cutoff=0.8, beta=0.04, d_cutoff=1.0)

        # Control Parameters (tunable from UI)
        self.speed_multiplier: float = 1.2
        self.min_cutoff: float = 0.8
        self.head_mouse_enabled: bool = True

        # Anti-slip freeze timestamp
        self._freeze_until: float = 0.0

        # Resting center calibration (first 15 frames or on recenter)
        self._calib_frames: int = 0
        self._base_x: float = 0.5
        self._base_y: float = 0.5
        self._calibrated: bool = False

        # Decoupled Target Position (thread-safe for 120 Hz interpolation loop)
        self._target_x: float = 960.0
        self._target_y: float = 540.0
        self._target_lock = threading.Lock()

        # Biometric Telemetry
        self.fps: float = 0.0
        self.current_ear: float = 0.30
        self.current_mar: float = 0.10
        self.head_yaw: float = 0.0
        self.head_pitch: float = 0.0

        # Gesture Detection State
        self._blink_active = False
        self._last_blink_time: float = 0.0
        self._blink_count: int = 0
        self._jaw_open_start: float = 0.0
        self._jaw_latched_drag: bool = False

        # Channel states for Scope
        self._ch1_active = False
        self._ch2_active = False
        self._ch4_active = False

    @property
    def target_pos(self) -> Tuple[float, float]:
        """Thread-safe target coordinate for 120 Hz decoupled cursor loop."""
        with self._target_lock:
            return (self._target_x, self._target_y)

    @property
    def freeze_until(self) -> float:
        return self._freeze_until

    def set_pointer_speed(self, speed: float) -> None:
        self.speed_multiplier = max(0.2, min(5.0, float(speed)))

    def set_smoothing_cutoff(self, min_cutoff: float) -> None:
        self.min_cutoff = max(0.1, min(5.0, float(min_cutoff)))
        self.filter_x.min_cutoff = self.min_cutoff
        self.filter_y.min_cutoff = self.min_cutoff

    def trigger_blink_freeze(self, duration_s: float = 0.35) -> None:
        """Freeze cursor movement during clicks so head-twitch doesn't slip off target."""
        self._freeze_until = time.perf_counter() + duration_s

    def recenter(self) -> None:
        """Recalibrate neutral face resting point to current pose."""
        self._calib_frames = 0
        self._base_x = 0.0
        self._base_y = 0.0
        self._calibrated = False
        t = time.perf_counter()
        self.filter_x.reset(t, self._target_x)
        self.filter_y.reset(t, self._target_y)
        log.info("Webcam neutral face baseline reset for re-centering")

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
        log.info("RealWebcamTracker started on camera_id=%d (MediaPipe=%s)", self.camera_id, HAS_MEDIAPIPE)
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

        # Warm up
        for _ in range(5):
            cap.read()

        # Initialize screen dimensions for target coordinate mapping
        screen_w, screen_h = 1920, 1080
        if sys.platform == "win32":
            try:
                import ctypes
                user32 = ctypes.windll.user32
                screen_w = user32.GetSystemMetrics(0) or 1920
                screen_h = user32.GetSystemMetrics(1) or 1080
            except Exception:
                pass

        with self._target_lock:
            self._target_x = screen_w / 2.0
            self._target_y = screen_h / 2.0

        # Initialize MediaPipe FaceMesh if available
        face_mesh = None
        if HAS_MEDIAPIPE:
            try:
                face_mesh = mp.solutions.face_mesh.FaceMesh(
                    max_num_faces=1,
                    refine_landmarks=True,
                    min_detection_confidence=0.6,
                    min_tracking_confidence=0.6,
                )
            except Exception as e:
                log.warning("Failed to initialize MediaPipe FaceMesh: %s. Using Geometric Mesh.", e)
                face_mesh = None

        prev_time = time.perf_counter()
        prev_gray = None

        while self._running:
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            # Mirror frame for natural intuitive selfie-cam head control
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            now = time.perf_counter()
            dt = max(1e-4, now - prev_time)
            self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt) if self.fps > 0 else (1.0 / dt)
            prev_time = now

            norm_nose_x, norm_nose_y = 0.5, 0.5
            ear = 0.30
            mar = 0.10
            yaw, pitch = 0.0, 0.0

            landmarks_found = False

            if face_mesh is not None:
                results = face_mesh.process(frame_rgb)
                if results and results.multi_face_landmarks:
                    landmarks_found = True
                    fl = results.multi_face_landmarks[0].landmark

                    # 1. Nose Tip & Anchors
                    nose_pt = fl[NOSE_TIP_IDX]
                    chin_pt = fl[CHIN_IDX]
                    forehead_pt = fl[FOREHEAD_IDX]
                    norm_nose_x = nose_pt.x
                    norm_nose_y = nose_pt.y

                    # 2. Head Pose estimate (Yaw & Pitch)
                    yaw = (norm_nose_x - 0.5) * 60.0
                    pitch = (norm_nose_y - 0.45) * 60.0

                    # 3. Eye Landmarks & EAR
                    left_pts = [(fl[i].x * w, fl[i].y * h) for i in LEFT_EYE_INDICES]
                    right_pts = [(fl[i].x * w, fl[i].y * h) for i in RIGHT_EYE_INDICES]
                    ear_l = calculate_ear(left_pts)
                    ear_r = calculate_ear(right_pts)
                    ear = (ear_l + ear_r) / 2.0

                    # 4. Lip Landmarks & MAR
                    inner_lip_pts = [(fl[i].x * w, fl[i].y * h) for i in INNER_LIP_INDICES]
                    mar = calculate_mar(inner_lip_pts)

                    # 5. Draw 468-Point Sci-Fi Wireframe
                    # Tessellation connections in Cyber-Teal (#00E5FF)
                    try:
                        tess = mp.solutions.face_mesh.FACEMESH_TESSELATION
                        for p1_i, p2_i in tess:
                            pt1 = (int(fl[p1_i].x * w), int(fl[p1_i].y * h))
                            pt2 = (int(fl[p2_i].x * w), int(fl[p2_i].y * h))
                            cv2.line(frame_rgb, pt1, pt2, (0, 229, 255), 1, cv2.LINE_AA)
                    except Exception:
                        pass

                    # Eye contours in Neon Green (#00E676)
                    for contour in [LEFT_EYE_CONTOUR, RIGHT_EYE_CONTOUR]:
                        pts_arr = np.array([(int(fl[i].x * w), int(fl[i].y * h)) for i in contour], np.int32)
                        cv2.polylines(frame_rgb, [pts_arr], True, (0, 230, 118), 1, cv2.LINE_AA)

                    # Lips contour in Warm Copper (#F07842)
                    lip_arr = np.array([(int(fl[i].x * w), int(fl[i].y * h)) for i in LIPS_CONTOUR], np.int32)
                    cv2.polylines(frame_rgb, [lip_arr], True, (240, 120, 66), 2, cv2.LINE_AA)

                    # Nose Tip Pinpoint & 3D Pose Vector
                    nx, ny = int(norm_nose_x * w), int(norm_nose_y * h)
                    cv2.circle(frame_rgb, (nx, ny), 4, (255, 255, 255), -1, cv2.LINE_AA)
                    # Vector arrow projecting gaze/pose direction
                    target_vector = (int(nx + yaw * 1.8), int(ny + pitch * 1.8))
                    cv2.arrowedLine(frame_rgb, (nx, ny), target_vector, (0, 229, 255), 2, cv2.LINE_AA, tipLength=0.25)

            # Geometric Delaunay Landmark Fallback if MediaPipe is unavailable or no face detected
            if not landmarks_found:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                gray = cv2.GaussianBlur(gray, (21, 21), 0)

                cx_box, cy_box = int(w * 0.5), int(h * 0.45)
                norm_nose_x, norm_nose_y = 0.5, 0.45

                if prev_gray is not None:
                    delta = cv2.absdiff(prev_gray, gray)
                    thresh = cv2.threshold(delta, 20, 255, cv2.THRESH_BINARY)[1]
                    m = cv2.moments(thresh)
                    if m["m00"] > 80:
                        cx_box = int(m["m10"] / m["m00"])
                        cy_box = int(m["m01"] / m["m00"])
                        norm_nose_x = cx_box / float(w)
                        norm_nose_y = cy_box / float(h)

                prev_gray = gray
                yaw = (norm_nose_x - 0.5) * 60.0
                pitch = (norm_nose_y - 0.45) * 60.0

                # Render Delaunay Triangulation Sci-Fi Wireframe
                self._draw_geometric_face_mesh(frame_rgb, cx_box, cy_box, w, h)

            self.current_ear = ear
            self.current_mar = mar
            self.head_yaw = yaw
            self.head_pitch = pitch

            # Baseline resting face calibration (first 15 frames or after recenter)
            if not self._calibrated:
                self._base_x += norm_nose_x
                self._base_y += norm_nose_y
                self._calib_frames += 1
                if self._calib_frames >= 15:
                    self._base_x /= 15.0
                    self._base_y /= 15.0
                    self._calibrated = True
                    log.info("Neutral head baseline calibrated to (%.3f, %.3f)", self._base_x, self._base_y)

            # Normalization relative to neutral center
            bx = self._base_x if self._calibrated else 0.5
            by = self._base_y if self._calibrated else 0.45
            rel_x = norm_nose_x - bx
            rel_y = norm_nose_y - by

            # Micro-deadzone of 0.008 suppresses camera sensor noise
            DEADZONE = 0.008
            accel_x = 0.0
            if abs(rel_x) > DEADZONE:
                eff_x = (abs(rel_x) - DEADZONE) / (0.5 - DEADZONE)
                accel_x = self.speed_multiplier * math.copysign(min(2.5, eff_x ** 1.35), rel_x)

            accel_y = 0.0
            if abs(rel_y) > DEADZONE:
                eff_y = (abs(rel_y) - DEADZONE) / (0.5 - DEADZONE)
                accel_y = self.speed_multiplier * math.copysign(min(2.5, eff_y ** 1.35), rel_y)

            # Screen Target Calculation
            raw_tgt_x = (screen_w / 2.0) + (accel_x * (screen_w / 2.0) * 1.5)
            raw_tgt_y = (screen_h / 2.0) + (accel_y * (screen_h / 2.0) * 1.5)
            raw_tgt_x = max(0.0, min(float(screen_w - 1), raw_tgt_x))
            raw_tgt_y = max(0.0, min(float(screen_h - 1), raw_tgt_y))

            # 1-Euro Adaptive Low-Pass Filter
            filt_x = self.filter_x.filter(now, raw_tgt_x)
            filt_y = self.filter_y.filter(now, raw_tgt_y)

            # Publish thread-safe target coordinates for decoupled 120 Hz loop
            with self._target_lock:
                self._target_x = filt_x
                self._target_y = filt_y

            # Biometric Gesture Decoders
            # 1. EAR Double-Blink Left Click
            blink_threshold = 0.21
            is_blinking = ear < blink_threshold
            if is_blinking and not self._blink_active:
                # Blink onset
                self._blink_active = True
                if now - self._last_blink_time < 0.50:
                    # Double-Blink Detected! Trigger Left Click + 350ms cursor freeze
                    self.trigger_blink_freeze(0.35)
                    if self.on_click:
                        self.on_click()
                    self._last_blink_time = 0.0
                else:
                    self._last_blink_time = now
            elif not is_blinking:
                self._blink_active = False

            # 2. MAR Jaw Open Click & Drag Latch
            jaw_threshold = 0.35
            is_jaw_open = mar > jaw_threshold
            if is_jaw_open:
                if self._jaw_open_start == 0.0:
                    self._jaw_open_start = now
                    self._jaw_latched_drag = False
                else:
                    # Check sustained hold (>700ms) for drag latch toggle
                    hold_dur = now - self._jaw_open_start
                    if hold_dur > 0.70 and not self._jaw_latched_drag:
                        self._jaw_latched_drag = True
                        self.trigger_blink_freeze(0.35)
                        if self.on_drag_toggle:
                            self.on_drag_toggle()
            else:
                if self._jaw_open_start > 0.0:
                    # Released jaw
                    open_dur = now - self._jaw_open_start
                    self._jaw_open_start = 0.0
                    # Quick tap (100ms - 450ms) triggers Left Click
                    if 0.10 <= open_dur <= 0.45:
                        self.trigger_blink_freeze(0.35)
                        if self.on_click:
                            self.on_click()

            # Envelopes & Gestures for Oscilloscope & Key Sinks
            ch1 = min(1.0, max(0.0, -rel_x * 4.0))  # Left
            ch2 = min(1.0, max(0.0, rel_x * 4.0))   # Right
            ch3 = min(1.0, max(0.0, -rel_y * 4.0))  # Up/Nod
            ch4 = min(1.0, max(0.0, (mar - 0.15) * 3.5))  # Jaw open

            if self.on_envelopes:
                active = [ch1 > 0.35, ch2 > 0.35, ch3 > 0.35, ch4 > 0.35]
                self.on_envelopes([ch1, ch2, ch3, ch4], active)

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

            # Draw Telemetry HUD Overlay
            self._draw_hud_telemetry(frame_rgb, w, h, ear, mar, yaw, pitch)

            if self.on_frame:
                self.on_frame(frame_rgb)

            time.sleep(0.015)  # ~60 FPS capture rate

        cap.release()

    def _draw_geometric_face_mesh(self, frame_rgb: np.ndarray, cx: int, cy: int, w: int, h: int) -> None:
        """Render high-density 68-node biometric Delaunay wireframe when MediaPipe FaceMesh is in fallback."""
        rw = int(w * 0.18)
        rh = int(h * 0.24)

        # Generate 68 biometric landmark points around cx, cy
        pts = []
        # Jawline & chin (17 pts)
        for i in range(17):
            ang = math.pi * 0.1 + (i / 16.0) * math.pi * 0.8
            px = int(cx + rw * 1.1 * math.cos(ang))
            py = int(cy + rh * 1.1 * math.sin(ang))
            pts.append((px, py))

        # Eyebrows (10 pts)
        for i in range(5):
            pts.append((int(cx - rw * 0.8 + i * (rw * 0.6 / 4)), int(cy - rh * 0.6)))
        for i in range(5):
            pts.append((int(cx + rw * 0.2 + i * (rw * 0.6 / 4)), int(cy - rh * 0.6)))

        # Nose bridge & base (9 pts)
        for i in range(4):
            pts.append((cx, int(cy - rh * 0.4 + i * (rh * 0.4 / 3))))
        for i in range(5):
            pts.append((int(cx - rw * 0.25 + i * (rw * 0.5 / 4)), int(cy + rh * 0.1)))

        # Left and Right Eyes (12 pts)
        for i in range(6):
            ang = (i / 6.0) * 2 * math.pi
            pts.append((int(cx - rw * 0.5 + rw * 0.2 * math.cos(ang)), int(cy - rh * 0.25 + rh * 0.12 * math.sin(ang))))
        for i in range(6):
            ang = (i / 6.0) * 2 * math.pi
            pts.append((int(cx + rw * 0.5 + rw * 0.2 * math.cos(ang)), int(cy - rh * 0.25 + rh * 0.12 * math.sin(ang))))

        # Lips & Mouth (20 pts)
        for i in range(12):
            ang = (i / 12.0) * 2 * math.pi
            pts.append((int(cx + rw * 0.45 * math.cos(ang)), int(cy + rh * 0.45 + rh * 0.22 * math.sin(ang))))
        for i in range(8):
            ang = (i / 8.0) * 2 * math.pi
            pts.append((int(cx + rw * 0.28 * math.cos(ang)), int(cy + rh * 0.45 + rh * 0.14 * math.sin(ang))))

        # Compute Delaunay Triangulation
        rect = (0, 0, w, h)
        subdiv = cv2.Subdiv2D(rect)
        for p in pts:
            if 0 <= p[0] < w and 0 <= p[1] < h:
                subdiv.insert(p)

        triangle_list = subdiv.getTriangleList()
        for t in triangle_list:
            pt1 = (int(t[0]), int(t[1]))
            pt2 = (int(t[2]), int(t[3]))
            pt3 = (int(t[4]), int(t[5]))
            # Verify triangles are within bounding area
            if (
                0 <= pt1[0] < w and 0 <= pt1[1] < h
                and 0 <= pt2[0] < w and 0 <= pt2[1] < h
                and 0 <= pt3[0] < w and 0 <= pt3[1] < h
            ):
                cv2.line(frame_rgb, pt1, pt2, (0, 229, 255), 1, cv2.LINE_AA)
                cv2.line(frame_rgb, pt2, pt3, (0, 229, 255), 1, cv2.LINE_AA)
                cv2.line(frame_rgb, pt3, pt1, (0, 229, 255), 1, cv2.LINE_AA)

        # Highlight lips contour in Warm Copper
        cv2.ellipse(frame_rgb, (cx, int(cy + rh * 0.45)), (int(rw * 0.45), int(rh * 0.22)), 0, 0, 360, (240, 120, 66), 2, cv2.LINE_AA)
        # Highlight eyes in Neon Green
        cv2.circle(frame_rgb, (int(cx - rw * 0.5), int(cy - rh * 0.25)), int(rw * 0.18), (0, 230, 118), 1, cv2.LINE_AA)
        cv2.circle(frame_rgb, (int(cx + rw * 0.5), int(cy - rh * 0.25)), int(rw * 0.18), (0, 230, 118), 1, cv2.LINE_AA)
        # Nose Tip Pinpoint
        cv2.circle(frame_rgb, (cx, cy), 4, (255, 255, 255), -1, cv2.LINE_AA)

    def _draw_hud_telemetry(
        self,
        frame_rgb: np.ndarray,
        w: int,
        h: int,
        ear: float,
        mar: float,
        yaw: float,
        pitch: float,
    ) -> None:
        """Render tactical HUD overlays directly on preview frame."""
        # Top Left: FPS & Filter
        cv2.putText(
            frame_rgb,
            f"FPS: {self.fps:4.1f} | FILTER: 1-Euro Adaptive (fc={self.min_cutoff:.1f}Hz)",
            (16, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (0, 229, 255),
            1,
            cv2.LINE_AA,
        )

        # Top Right: Head Mouse State
        hm_str = "HEAD-MOUSE: ACTIVE (120Hz)" if self.head_mouse_enabled else "HEAD-MOUSE: PAUSED"
        hm_col = (0, 230, 118) if self.head_mouse_enabled else (113, 109, 103)
        cv2.putText(
            frame_rgb,
            hm_str,
            (w - 240, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.46,
            hm_col,
            1,
            cv2.LINE_AA,
        )

        # Bottom Left: Biometric EAR / MAR Readout
        ear_str = f"EAR: {ear:.2f}" + (" [BLINK]" if ear < 0.21 else "")
        mar_str = f"MAR: {mar:.2f}" + (" [JAW OPEN]" if mar > 0.35 else "")
        cv2.putText(
            frame_rgb,
            f"{ear_str} | {mar_str}",
            (16, h - 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (247, 245, 240),
            1,
            cv2.LINE_AA,
        )

        # Bottom Right: Head Pose (Pitch / Yaw)
        cv2.putText(
            frame_rgb,
            f"PITCH: {pitch:+4.1f}* | YAW: {yaw:+4.1f}*",
            (w - 220, h - 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (240, 120, 66),
            1,
            cv2.LINE_AA,
        )
