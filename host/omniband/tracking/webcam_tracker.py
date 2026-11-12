"""webcam_tracker.py - eViacam + Google Project GameFace Relative Delta-Velocity Engine.

Key Architectural Principles:
1. eViacam Relative Velocity / Joystick Tracking (NOT Absolute Mapping):
   - Absolute mapping causes wild pixel jitter and violently fights the physical mouse.
   - Delta-velocity tracking integrates smooth relative displacement only outside a deadzone.
2. Rule #1 of Head Mice — NEVER Call SetCursorPos Inside the Deadzone:
   - When head offset is inside the neutral deadzone (deadzone = 0.035), velocity is (0.0, 0.0)
     and SetCursorPos is NEVER called, giving the cursor 0.0 px jitter and letting the user
     use their physical mouse freely.
3. Rigid 4-Point Nose Bridge Anchor (#1, #4, #6, #197):
   - Rigid landmarks that do not deform when blinking or speaking.
   - Paired with Lucas-Kanade Pyramidal Optical Flow (cv2.calcOpticalFlowPyrLK) as a sub-pixel
     optical flow tracking engine (eViacam algorithm) and Delaunay 3D wireframe mesh.
4. Magnetic Dwell Brake:
   - Dynamic 4x damping when slowing down inside the fine-aiming envelope.
5. Visual Deadzone Ring on Preview Feed:
   - Green deadzone ring at calibrated resting center.
   - Glowing vector line from center to nose tip.
   - HUD indicator: 'CURSOR: STILL (DEADZONE — 0 JITTER)' vs 'CURSOR: GLIDING'.
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

HAS_MEDIAPIPE = False
try:
    import mediapipe as mp
    if hasattr(mp, "solutions") and hasattr(mp.solutions, "face_mesh"):
        HAS_MEDIAPIPE = True
except Exception:
    HAS_MEDIAPIPE = False


class OneEuroFilter:
    """Casiez et al. 2012 1-Euro adaptive low-pass filter for jitter-free tracking."""

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

        # 1. Filter derivative
        dx = (x - self.x_prev) / dt
        alpha_d = self._alpha(dt, self.d_cutoff)
        edx = alpha_d * dx + (1.0 - alpha_d) * self.dx_prev

        # 2. Adaptive cutoff
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


# MediaPipe Key Landmark Indices
# Rigid nose bridge rig (does not deform during blinks or jaw movements)
RIGID_NOSE_INDICES = [1, 4, 6, 197]
LEFT_EYE_INDICES = [33, 160, 158, 133, 153, 144]
RIGHT_EYE_INDICES = [362, 385, 387, 263, 373, 380]
LEFT_EYE_CONTOUR = [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246]
RIGHT_EYE_CONTOUR = [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398]
INNER_LIP_INDICES = [13, 14, 78, 308]
LIPS_CONTOUR = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95]


def calculate_ear(pts: List[Tuple[float, float]]) -> float:
    """Calculate Eye Aspect Ratio (EAR) given 6 eye landmarks."""
    if len(pts) < 6:
        return 0.30
    p1, p2, p3, p4, p5, p6 = pts[:6]
    d_v1 = math.hypot(p2[0] - p6[0], p2[1] - p6[1])
    d_v2 = math.hypot(p3[0] - p5[0], p3[1] - p5[1])
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
    """eViacam / Project GameFace delta-velocity optical tracker."""

    DEADZONE: float = 0.035  # Neutral deadzone radius in normalized coordinates

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

        # 1-Euro Adaptive Filters for Delta X and Delta Y
        self.filter_dx = OneEuroFilter(min_cutoff=0.6, beta=0.03, d_cutoff=1.0)
        self.filter_dy = OneEuroFilter(min_cutoff=0.6, beta=0.03, d_cutoff=1.0)

        # Control Parameters
        self.speed_multiplier: float = 1.2
        self.min_cutoff: float = 0.6
        self.head_mouse_enabled: bool = True

        # Anti-slip freeze timestamp
        self._freeze_until: float = 0.0

        # Resting center calibration (first 15 frames or after recenter)
        self._calib_frames: int = 0
        self._base_x: float = 0.5
        self._base_y: float = 0.45
        self._calibrated: bool = False

        # Thread-safe Delta Velocity State (vx, vy in px/sec, in_deadzone)
        self._vx: float = 0.0
        self._vy: float = 0.0
        self._in_deadzone: bool = True
        self._state_lock = threading.Lock()

        # Telemetry
        self.fps: float = 0.0
        self.current_ear: float = 0.30
        self.current_mar: float = 0.10
        self.head_yaw: float = 0.0
        self.head_pitch: float = 0.0

        # Gesture Detection State
        self._blink_active: bool = False
        self._last_blink_time: float = 0.0
        self._jaw_open_start: float = 0.0
        self._jaw_latched_drag: bool = False

        # Channel states for Scope
        self._ch1_active: bool = False
        self._ch2_active: bool = False
        self._ch4_active: bool = False

        # Lucas-Kanade Optical Flow state (eViacam algorithm fallback)
        self._lk_prev_gray: Optional[np.ndarray] = None
        self._lk_points: Optional[np.ndarray] = None
        self._lk_frames: int = 0

    @property
    def velocity_state(self) -> Tuple[float, float, bool]:
        """Thread-safe relative velocity vector (vx, vy) and deadzone flag."""
        with self._state_lock:
            return (self._vx, self._vy, self._in_deadzone)

    @property
    def freeze_until(self) -> float:
        return self._freeze_until

    def set_pointer_speed(self, speed: float) -> None:
        self.speed_multiplier = max(0.2, min(5.0, float(speed)))

    def set_smoothing_cutoff(self, min_cutoff: float) -> None:
        self.min_cutoff = max(0.1, min(5.0, float(min_cutoff)))
        self.filter_dx.min_cutoff = self.min_cutoff
        self.filter_dy.min_cutoff = self.min_cutoff

    def trigger_blink_freeze(self, duration_s: float = 0.35) -> None:
        """Freeze cursor movement during clicks so involuntary twitch doesn't slip."""
        self._freeze_until = time.perf_counter() + duration_s

    def recenter(self) -> None:
        """Recalibrate neutral resting center to the current head position."""
        self._calib_frames = 0
        self._base_x = 0.0
        self._base_y = 0.0
        self._calibrated = False
        t = time.perf_counter()
        self.filter_dx.reset(t, 0.0)
        self.filter_dy.reset(t, 0.0)
        self._lk_points = None
        log.info("Neutral resting center reset for re-centering")

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
                log.warning("Could not initialize MediaPipe FaceMesh: %s. Using Lucas-Kanade Optical Flow.", e)
                face_mesh = None

        prev_time = time.perf_counter()

        while self._running:
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            # Mirror frame for natural intuitive selfie-cam head control
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            now = time.perf_counter()
            dt = max(1e-4, now - prev_time)
            self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt) if self.fps > 0 else (1.0 / dt)
            prev_time = now

            norm_nose_x, norm_nose_y = 0.5, 0.45
            ear = 0.30
            mar = 0.10
            yaw, pitch = 0.0, 0.0
            landmarks_found = False

            if face_mesh is not None:
                results = face_mesh.process(frame_rgb)
                if results and results.multi_face_landmarks:
                    landmarks_found = True
                    fl = results.multi_face_landmarks[0].landmark

                    # 1. Rigid 4-point Nose Bridge Centroid (#1, #4, #6, #197)
                    # These points do NOT deform during blinks or mouth movements!
                    rigid_pts = [(fl[i].x, fl[i].y) for i in RIGID_NOSE_INDICES]
                    norm_nose_x = sum(p[0] for p in rigid_pts) / float(len(rigid_pts))
                    norm_nose_y = sum(p[1] for p in rigid_pts) / float(len(rigid_pts))

                    # 2. Head Pose estimate
                    yaw = (norm_nose_x - 0.5) * 60.0
                    pitch = (norm_nose_y - 0.45) * 60.0

                    # 3. EAR Eye Blinks
                    left_pts = [(fl[i].x * w, fl[i].y * h) for i in LEFT_EYE_INDICES]
                    right_pts = [(fl[i].x * w, fl[i].y * h) for i in RIGHT_EYE_INDICES]
                    ear_l = calculate_ear(left_pts)
                    ear_r = calculate_ear(right_pts)
                    ear = (ear_l + ear_r) / 2.0

                    # 4. MAR Jaw Movements
                    inner_lip_pts = [(fl[i].x * w, fl[i].y * h) for i in INNER_LIP_INDICES]
                    mar = calculate_mar(inner_lip_pts)

                    # 5. Draw 468-Point Sci-Fi Wireframe
                    # Cyan Mesh Tessellation (#00E5FF)
                    try:
                        tess = mp.solutions.face_mesh.FACEMESH_TESSELATION
                        for p1_i, p2_i in tess:
                            pt1 = (int(fl[p1_i].x * w), int(fl[p1_i].y * h))
                            pt2 = (int(fl[p2_i].x * w), int(fl[p2_i].y * h))
                            cv2.line(frame_rgb, pt1, pt2, (0, 229, 255), 1, cv2.LINE_AA)
                    except Exception:
                        pass

                    # Neon Green Eye Rings (#00E676)
                    for contour in [LEFT_EYE_CONTOUR, RIGHT_EYE_CONTOUR]:
                        pts_arr = np.array([(int(fl[i].x * w), int(fl[i].y * h)) for i in contour], np.int32)
                        cv2.polylines(frame_rgb, [pts_arr], True, (0, 230, 118), 1, cv2.LINE_AA)

                    # Warm Copper Mouth Contour (#F07842)
                    lip_arr = np.array([(int(fl[i].x * w), int(fl[i].y * h)) for i in LIPS_CONTOUR], np.int32)
                    cv2.polylines(frame_rgb, [lip_arr], True, (240, 120, 66), 2, cv2.LINE_AA)

            # Lucas-Kanade Pyramidal Optical Flow Fallback (eViacam algorithm)
            if not landmarks_found:
                norm_nose_x, norm_nose_y = self._track_lucas_kanade(gray, w, h)
                yaw = (norm_nose_x - 0.5) * 60.0
                pitch = (norm_nose_y - 0.45) * 60.0
                # Draw Delaunay 3D Wireframe fallback
                self._draw_geometric_face_mesh(frame_rgb, int(norm_nose_x * w), int(norm_nose_y * h), w, h)

            self.current_ear = ear
            self.current_mar = mar
            self.head_yaw = yaw
            self.head_pitch = pitch

            # Resting Center Baseline Calibration (first 15 frames or after recenter)
            if not self._calibrated:
                self._base_x += norm_nose_x
                self._base_y += norm_nose_y
                self._calib_frames += 1
                if self._calib_frames >= 15:
                    self._base_x /= 15.0
                    self._base_y /= 15.0
                    self._calibrated = True
                    log.info("Neutral resting center calibrated to (%.4f, %.4f)", self._base_x, self._base_y)

            # Relative Delta Calculation
            cx0 = self._base_x if self._calibrated else 0.5
            cy0 = self._base_y if self._calibrated else 0.45
            raw_dx = norm_nose_x - cx0
            raw_dy = norm_nose_y - cy0

            # 1-Euro Adaptive Low-Pass Filter on Delts
            filt_dx = self.filter_dx.filter(now, raw_dx)
            filt_dy = self.filter_dy.filter(now, raw_dy)

            # RULE #1 OF HEAD MICE: Neutral Deadzone Check
            offset_mag = math.hypot(filt_dx, filt_dy)
            in_deadzone = offset_mag < self.DEADZONE

            vx, vy = 0.0, 0.0
            if not in_deadzone and offset_mag > 0.0:
                # Outside deadzone: Ballistic relative velocity
                excess = offset_mag - self.DEADZONE
                ux = filt_dx / offset_mag
                uy = filt_dy / offset_mag

                # Ballistic power curve
                speed = self.speed_multiplier * (excess ** 1.6) * 3200.0

                # Magnetic Dwell Brake: when slowing down to aim (<0.02 excess), apply 4x damping
                if excess < 0.02:
                    speed *= 0.35

                vx = ux * speed
                vy = uy * speed

            # Update thread-safe velocity state
            with self._state_lock:
                self._vx = vx
                self._vy = vy
                self._in_deadzone = in_deadzone

            # Biometric Gesture Decoders
            # 1. EAR Double-Blink Left Click
            blink_threshold = 0.21
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

            # 2. MAR Jaw Open Click & Drag Latch
            jaw_threshold = 0.35
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

            # Envelopes for Scope
            ch1 = min(1.0, max(0.0, -filt_dx * 5.0))
            ch2 = min(1.0, max(0.0, filt_dx * 5.0))
            ch3 = min(1.0, max(0.0, -filt_dy * 5.0))
            ch4 = min(1.0, max(0.0, (mar - 0.15) * 3.5))

            if self.on_envelopes:
                self.on_envelopes([ch1, ch2, ch3, ch4], [ch1 > 0.35, ch2 > 0.35, ch3 > 0.35, ch4 > 0.35])

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

            # Draw Visual Deadzone Ring & Vector Line on Preview Frame
            self._draw_deadzone_ring_and_hud(frame_rgb, w, h, cx0, cy0, norm_nose_x, norm_nose_y, in_deadzone, vx, vy)

            if self.on_frame:
                self.on_frame(frame_rgb)

            time.sleep(0.012)

        cap.release()

    def _track_lucas_kanade(self, gray: np.ndarray, w: int, h: int) -> Tuple[float, float]:
        """Lucas-Kanade Pyramidal Optical Flow sub-pixel tracker (eViacam algorithm)."""
        # Central nose ROI where tracking features are placed
        cx, cy = int(w * 0.5), int(h * 0.45)
        roi_w, roi_h = int(w * 0.20), int(h * 0.25)
        x0, y0 = max(0, cx - roi_w // 2), max(0, cy - roi_h // 2)

        need_reseed = (
            self._lk_points is None
            or len(self._lk_points) < 8
            or self._lk_frames % 45 == 0
            or self._lk_prev_gray is None
        )

        if need_reseed:
            roi = gray[y0:y0 + roi_h, x0:x0 + roi_w]
            pts = cv2.goodFeaturesToTrack(
                roi,
                maxCorners=25,
                qualityLevel=0.03,
                minDistance=8,
                blockSize=7,
            )
            if pts is not None and len(pts) > 0:
                pts[:, :, 0] += x0
                pts[:, :, 1] += y0
                self._lk_points = pts
            else:
                # 5x5 uniform grid fallback
                grid = []
                for gy in np.linspace(y0 + 10, y0 + roi_h - 10, 5):
                    for gx in np.linspace(x0 + 10, x0 + roi_w - 10, 5):
                        grid.append([[gx, gy]])
                self._lk_points = np.array(grid, dtype=np.float32)

            self._lk_prev_gray = gray
            self._lk_frames = 0
            return (cx / float(w), cy / float(h))

        # Calculate Lucas-Kanade optical flow vectors
        p1, st, err = cv2.calcOpticalFlowPyrLK(
            self._lk_prev_gray,
            gray,
            self._lk_points,
            None,
            winSize=(21, 21),
            maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
        )

        good_pts = p1[st == 1]
        self._lk_prev_gray = gray
        self._lk_frames += 1

        if len(good_pts) > 4:
            self._lk_points = good_pts.reshape(-1, 1, 2)
            mean_x = float(np.mean(good_pts[:, 0]))
            mean_y = float(np.mean(good_pts[:, 1]))
            return (mean_x / float(w), mean_y / float(h))

        self._lk_points = None
        return (0.5, 0.45)

    def _draw_deadzone_ring_and_hud(
        self,
        frame_rgb: np.ndarray,
        w: int,
        h: int,
        cx0: float,
        cy0: float,
        nx: float,
        ny: float,
        in_deadzone: bool,
        vx: float,
        vy: float,
    ) -> None:
        """Render the Green Neutral Deadzone Circle & Glowing Vector Line."""
        # Resting Center Point
        center_px = (int(cx0 * w), int(cy0 * h))
        nose_px = (int(nx * w), int(ny * h))

        # Radius in pixels corresponding to DEADZONE
        radius_px = int(self.DEADZONE * min(w, h))

        # 1. Neutral Deadzone Ring (Green)
        cv2.circle(frame_rgb, center_px, radius_px, (0, 230, 118), 2, cv2.LINE_AA)
        cv2.circle(frame_rgb, center_px, 2, (0, 230, 118), -1, cv2.LINE_AA)

        # 2. Glowing Copper Vector Line from resting center to current nose point
        line_color = (0, 230, 118) if in_deadzone else (240, 120, 66)
        cv2.line(frame_rgb, center_px, nose_px, line_color, 2, cv2.LINE_AA)

        # 3. Nose Point Dot
        dot_color = (0, 230, 118) if in_deadzone else (240, 120, 66)
        cv2.circle(frame_rgb, nose_px, 5, dot_color, -1, cv2.LINE_AA)
        cv2.circle(frame_rgb, nose_px, 6, (255, 255, 255), 1, cv2.LINE_AA)

        # 4. HUD Telemetry Overlay
        # Status Badge
        if in_deadzone:
            status_text = "CURSOR: STILL (DEADZONE — 0 JITTER)"
            status_color = (0, 230, 118)  # Green
        else:
            status_text = f"CURSOR: GLIDING (vx={vx:+.0f}, vy={vy:+.0f})"
            status_color = (240, 120, 66)  # Warm Copper

        cv2.putText(
            frame_rgb,
            status_text,
            (16, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.50,
            status_color,
            2,
            cv2.LINE_AA,
        )

        # Telemetry info
        cv2.putText(
            frame_rgb,
            f"FPS: {self.fps:4.1f} | EAR: {self.current_ear:.2f} | MAR: {self.current_mar:.2f}",
            (16, h - 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (247, 245, 240),
            1,
            cv2.LINE_AA,
        )

    def _draw_geometric_face_mesh(self, frame_rgb: np.ndarray, cx: int, cy: int, w: int, h: int) -> None:
        """Render high-density biometric Delaunay wireframe mesh fallback."""
        rw = int(w * 0.16)
        rh = int(h * 0.22)
        pts = []
        for i in range(16):
            ang = math.pi * 0.1 + (i / 15.0) * math.pi * 0.8
            pts.append((int(cx + rw * 1.1 * math.cos(ang)), int(cy + rh * 1.1 * math.sin(ang))))

        for i in range(5):
            pts.append((int(cx - rw * 0.7 + i * (rw * 0.6 / 4)), int(cy - rh * 0.55)))
            pts.append((int(cx + rw * 0.15 + i * (rw * 0.6 / 4)), int(cy - rh * 0.55)))

        for i in range(4):
            pts.append((cx, int(cy - rh * 0.35 + i * (rh * 0.4 / 3))))

        rect = (0, 0, w, h)
        subdiv = cv2.Subdiv2D(rect)
        for p in pts:
            if 0 <= p[0] < w and 0 <= p[1] < h:
                subdiv.insert(p)

        triangles = subdiv.getTriangleList()
        for t in triangles:
            p1, p2, p3 = (int(t[0]), int(t[1])), (int(t[2]), int(t[3])), (int(t[4]), int(t[5]))
            if 0 <= p1[0] < w and 0 <= p1[1] < h and 0 <= p2[0] < w and 0 <= p2[1] < h and 0 <= p3[0] < w and 0 <= p3[1] < h:
                cv2.line(frame_rgb, p1, p2, (0, 229, 255), 1, cv2.LINE_AA)
                cv2.line(frame_rgb, p2, p3, (0, 229, 255), 1, cv2.LINE_AA)
                cv2.line(frame_rgb, p3, p1, (0, 229, 255), 1, cv2.LINE_AA)
