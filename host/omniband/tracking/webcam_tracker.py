"""webcam_tracker.py — Production Neural Face Tracking & Head-Mouse Engine.

Strictly follows:
  R01: Pure neural outputs only. Zero synthetic geometry.
  R02: Zero Haar cascades in production paths (archived in legacy/haar_tracker.py).
  M01: MediaPipe Tasks FaceLandmarker in RunningMode.VIDEO with verified model bytes.
  M02: cv2.FaceDetectorYN (YuNet 2023mar ONNX) detector assist for low-light & headphones.
  M03: Multi-stage pipeline: S1 (Full VIDEO) -> S2 (YuNet + IMAGE crop) -> S3 (YuNet 5-pt) -> S4 (NO_FACE).
  M04: Anatomical nose tip (#1), scale-normalized EAR & MAR, 3D solvePnP head pose.
  M05: TrackerResult frozen dataclass with full metadata.
  L01: Decoupled FrameSource thread with latest-frame-wins cell (CAP_PROP_BUFFERSIZE=1).
  L03: LAB CLAHE + adaptive gamma low-light enhancer (<4ms budget).
"""
from __future__ import annotations

import collections
from dataclasses import dataclass
import logging
import math
from pathlib import Path
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np

from host.omniband.tracking.paths import (
    load_verified_model_bytes,
    resource_path,
    ModelIntegrityError,
)
from host.omniband.tracking.optical_calibration import (
    OpticalCalibrator,
    OpticalCalibrationProfile,
)

log = logging.getLogger("omniband.tracking.webcam")

# ---------------------------------------------------------------------------
# MediaPipe Tasks & Optional Dependencies
# ---------------------------------------------------------------------------
HAS_MEDIAPIPE = False
try:
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision
    HAS_MEDIAPIPE = True
except Exception:
    HAS_MEDIAPIPE = False

HAS_SCIPY = False
try:
    import scipy.spatial
    HAS_SCIPY = True
except Exception:
    HAS_SCIPY = False


# ---------------------------------------------------------------------------
# Tracker Result Data Structure (M-05)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TrackerResult:
    """Immutable result packet from the neural tracking pipeline."""
    tier: str  # "FACEMESH478", "KEYPOINT5", "NO_FACE"
    face_found: bool
    nose_tip: Optional[Tuple[float, float]]  # Normalized (x, y) in [0.0, 1.0]
    landmarks: Optional[List[Tuple[float, float, float]]]  # 478 3D coords (x, y, z) or None
    ear: Optional[float]  # Scale-normalized Eye Aspect Ratio or None
    mar: Optional[float]  # Scale-normalized Mouth Aspect Ratio or None
    yaw: float  # Degrees (+ right, - left)
    pitch: float  # Degrees (+ down, - up)
    timestamp_ms: int
    left_ear: Optional[float] = None
    right_ear: Optional[float] = None
    eye_dist: Optional[float] = None
    frame_rgb: Optional[np.ndarray] = None


# ---------------------------------------------------------------------------
# Backward Compatibility: find_model_path
# ---------------------------------------------------------------------------
def find_model_path() -> Optional[str]:
    """Locate face_landmarker.task via canonical paths.py resource resolver."""
    try:
        p = resource_path("models/face_landmarker.task")
        if p.is_file():
            return str(p)
        p_root = resource_path("face_landmarker.task")
        if p_root.is_file():
            return str(p_root)
    except Exception:
        pass
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
NOSE_TIP_INDEX = 1
LEFT_EYE_INDICES  = [33, 160, 158, 133, 153, 144]
RIGHT_EYE_INDICES = [362, 385, 387, 263, 373, 380]
LEFT_EYE_CONTOUR  = [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246]
RIGHT_EYE_CONTOUR = [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398]
INNER_LIP_INDICES = [13, 14, 78, 308]
LIPS_CONTOUR      = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 308,
                     324, 318, 402, 317, 14, 87, 178, 88, 95]

# Standard 3D model points for solvePnP head pose estimation (in mm, +Y down, +X right)
HEAD_POSE_MODEL_3D = np.array([
    (0.0, 0.0, 0.0),          # Nose tip (#1)
    (0.0, 110.0, -65.0),      # Chin (#199)
    (-70.0, -50.0, -50.0),    # Left eye outer corner (#33)
    (70.0, -50.0, -50.0),     # Right eye outer corner (#263)
    (-50.0, 60.0, -40.0),     # Left mouth corner (#61)
    (50.0, 60.0, -40.0),      # Right mouth corner (#291)
], dtype=np.float64)

HEAD_POSE_LANDMARK_INDICES = [1, 199, 33, 263, 61, 291]


# ---------------------------------------------------------------------------
# Biometric Feature Math: EAR / MAR / Head Pose (M-04)
# ---------------------------------------------------------------------------
def calculate_ear(pts: List[Tuple[float, float]]) -> float:
    """Eye Aspect Ratio from 6 real landmark pixel coords. Returns 0.0 on insufficient pts."""
    if len(pts) < 6:
        return 0.0
    p1, p2, p3, p4, p5, p6 = pts[:6]
    d_v1 = math.hypot(p2[0] - p6[0], p2[1] - p6[1])
    d_v2 = math.hypot(p3[0] - p5[0], p3[1] - p5[1])
    d_h  = math.hypot(p1[0] - p4[0], p1[1] - p4[1]) + 1e-6
    return float((d_v1 + d_v2) / (2.0 * d_h))


def calculate_mar(inner_pts: List[Tuple[float, float]]) -> float:
    """Mouth Aspect Ratio from [p13, p14, p78, p308] real landmark coords."""
    if len(inner_pts) < 4:
        return 0.0
    p13, p14, p78, p308 = inner_pts[:4]
    d_v = math.hypot(p13[0] - p14[0], p13[1] - p14[1])
    d_h = math.hypot(p78[0] - p308[0], p78[1] - p308[1]) + 1e-6
    return float(d_v / d_h)


def estimate_head_pose(
    landmarks_px: List[Tuple[float, float]],
    frame_w: int,
    frame_h: int,
) -> Tuple[float, float]:
    """Estimate head yaw and pitch angles in degrees using solvePnP."""
    if len(landmarks_px) < 468:
        return 0.0, 0.0

    image_points = np.array([
        landmarks_px[idx] for idx in HEAD_POSE_LANDMARK_INDICES
    ], dtype=np.float64)

    # Approximate camera intrinsics
    focal_length = float(frame_w)
    center = (float(frame_w) / 2.0, float(frame_h) / 2.0)
    camera_matrix = np.array([
        [focal_length, 0.0, center[0]],
        [0.0, focal_length, center[1]],
        [0.0, 0.0, 1.0],
    ], dtype=np.float64)
    dist_coeffs = np.zeros((4, 1), dtype=np.float64)

    success, rot_vec, _ = cv2.solvePnP(
        HEAD_POSE_MODEL_3D,
        image_points,
        camera_matrix,
        dist_coeffs,
        flags=cv2.SOLVEPNP_ITERATIVE,
    )
    if not success:
        return 0.0, 0.0

    rot_mat, _ = cv2.Rodrigues(rot_vec)
    angles, _, _, _, _, _ = cv2.RQDecomp3x3(rot_mat)
    pitch_deg, yaw_deg, _ = angles

    return float(yaw_deg), float(pitch_deg)


# ---------------------------------------------------------------------------
# Low-Light LAB CLAHE + Gamma Enhancer (L-03)
# ---------------------------------------------------------------------------
def enhance_low_light(frame_bgr: np.ndarray) -> np.ndarray:
    """Enhance low-light frame using LAB color space + CLAHE + adaptive gamma (<4ms budget)."""
    lab = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    mean_l = float(np.mean(l_channel))

    if mean_l < 75.0:
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        l_enhanced = clahe.apply(l_channel)
        gamma = 1.35 if mean_l < 45.0 else 1.15
        inv_gamma = 1.0 / gamma
        table = np.array([((i / 255.0) ** inv_gamma) * 255.0 for i in range(256)], dtype=np.uint8)
        l_enhanced = cv2.LUT(l_enhanced, table)
        enhanced_lab = cv2.merge([l_enhanced, a_channel, b_channel])
        return cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)

    return frame_bgr


# ---------------------------------------------------------------------------
# Decoupled Camera FrameSource Thread (L-01, L-02)
# ---------------------------------------------------------------------------
class FrameSource:
    """Decoupled camera capture thread with latest-frame-wins cell (CAP_PROP_BUFFERSIZE=1)."""

    def __init__(self, camera_id: int = 0) -> None:
        self.camera_id = camera_id
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._new_frame_event = threading.Event()
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_seq: int = 0
        self.actual_fps: float = 0.0

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(
            target=self._run_capture,
            daemon=True,
            name="OmniBandFrameSourceThread",
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        self._new_frame_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def read_latest(self, timeout: float = 0.2) -> Optional[np.ndarray]:
        """Return the newest acquired frame or None on timeout."""
        if not self._new_frame_event.wait(timeout):
            return None
        self._new_frame_event.clear()
        with self._lock:
            return None if self._latest_frame is None else self._latest_frame.copy()

    def _run_capture(self) -> None:
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.camera_id, backend)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.camera_id)

        if not cap.isOpened():
            log.warning("FrameSource: could not open camera index %d", self.camera_id)
            self._running = False
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # Warmup: discard initial pitch-black frames during sensor auto-exposure ramp
        for _ in range(15):
            ret, frame = cap.read()
            if ret and frame is not None and np.mean(frame) > 20.0:
                break
            time.sleep(0.02)

        prev_t = time.perf_counter()

        while self._running:
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.005)
                continue

            now = time.perf_counter()
            dt = max(1e-4, now - prev_t)
            prev_t = now
            inst_fps = 1.0 / dt
            self.actual_fps = 0.9 * self.actual_fps + 0.1 * inst_fps if self.actual_fps > 0 else inst_fps

            with self._lock:
                self._latest_frame = frame
                self._frame_seq += 1
            self._new_frame_event.set()

        cap.release()
        log.info("FrameSource capture thread exited cleanly")


# ---------------------------------------------------------------------------
# Multi-Stage Neural Face Tracker (M-01, M-02, M-03, M-04, R01, R02)
# ---------------------------------------------------------------------------
class RealWebcamTracker:
    """Production Multi-Stage Face & Head-Mouse Tracker.

    Pipeline:
      S1: MediaPipe Tasks FaceLandmarker in RunningMode.VIDEO (Full frame).
      S2: Detector assist cv2.FaceDetectorYN (YuNet 2023mar) -> 1.6x expanded square ROI -> FaceLandmarker IMAGE mode.
      S3: YuNet 5-point keypoint fallback if landmarker fails on crop.
      S4: NO_FACE state -> Cursor frozen, HUD alignment guide.
    """

    DEADZONE: float = 0.012

    def __init__(
        self,
        camera_id: int = 0,
        on_envelopes: Optional[Callable[[list[float], list[bool]], None]] = None,
        on_frame: Optional[Callable[[np.ndarray], None]] = None,
        on_gesture: Optional[Callable[[int, bool, float], None]] = None,
        on_click: Optional[Callable[[], None]] = None,
        on_drag_toggle: Optional[Callable[[], None]] = None,
        on_scroll_clutch: Optional[Callable[[bool, float], None]] = None,
        on_zoom_clutch: Optional[Callable[[bool, float, float], None]] = None,
        on_latch_changed: Optional[Callable[[str], None]] = None,
        on_universal_exit: Optional[Callable[[], None]] = None,
        optical_calibrator: Optional[OpticalCalibrator] = None,
    ) -> None:
        self.camera_id = camera_id
        self.on_envelopes = on_envelopes
        self.on_frame = on_frame
        self.on_gesture = on_gesture
        self.on_click = on_click
        self.on_drag_toggle = on_drag_toggle
        self.on_scroll_clutch = on_scroll_clutch
        self.on_zoom_clutch = on_zoom_clutch
        self.on_latch_changed = on_latch_changed
        self.on_universal_exit = on_universal_exit
        self.calibrator = optical_calibrator or OpticalCalibrator()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._frame_source: Optional[FrameSource] = None

        # 1-Euro Filters for relative deltas
        self.filter_dx = OneEuroFilter(min_cutoff=0.6, beta=0.08, d_cutoff=1.0)
        self.filter_dy = OneEuroFilter(min_cutoff=0.6, beta=0.08, d_cutoff=1.0)

        # Sensitivity and control (calm, precise head-mouse tracking)
        self.speed_multiplier: float = 0.6
        self.gain_x: float = 4.5
        self.gain_y: float = 5.0
        self.min_cutoff: float = 0.6
        self.head_mouse_enabled: bool = True

        # Anti-slip freeze
        self._freeze_until: float = 0.0

        # Resting centre calibration
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

        # Telemetry
        self.fps: float = 0.0
        self.current_ear: Optional[float] = None
        self.current_mar: Optional[float] = None
        self.current_left_ear: Optional[float] = None
        self.current_right_ear: Optional[float] = None
        self.current_eye_dist: Optional[float] = None
        self.head_yaw: float = 0.0
        self.head_pitch: float = 0.0
        self.current_tier: str = "NO_FACE"
        self.latest_result: Optional[TrackerResult] = None

        # Asymmetric Wink-Latch State Machine (Opposite-Eye Latch: SCROLL_LOCKED / ZOOM_LOCKED)
        self.active_latch: str = "IDLE"  # "IDLE", "SCROLL_LOCKED", "ZOOM_LOCKED"
        self._left_wink_start: float = 0.0
        self._right_wink_start: float = 0.0
        self._latch_locked_time: float = 0.0
        self._pitch_anchor_y: float = 0.45
        self._filtered_pitch_y: float = 0.45
        self._scale_anchor_dist: float = 0.12
        self._last_zoom_step_time: float = 0.0

        # Auto-Baseline Rolling Tracker (Zero-Click Auto-Enable on Startup)
        self.auto_open_ear_L: float = 0.28
        self.auto_open_ear_R: float = 0.28
        self._auto_baseline_samples: int = 0
        self._auto_calibrated: bool = False

        # Gesture state & adaptive baseline
        self._baseline_ear: float = 0.28
        self._blink_active: bool = False
        self._blink_start_time: float = 0.0
        self._last_blink_time: float = 0.0
        self._long_blink_fired: bool = False
        self._jaw_open_start: float = 0.0
        self._jaw_latched_drag: bool = False
        self._last_gesture_banner: str = ""
        self._gesture_banner_expire: float = 0.0

        # Universal Triple-Blink Exit tracking & Look-Away guard
        self._blink_timestamps: list[float] = []
        self._bilateral_blink_start: float = 0.0
        self.is_looking_away: bool = False

        # Channel states
        self._ch1_active: bool = False
        self._ch2_active: bool = False
        self._ch4_active: bool = False

        # Mesh cache
        self._cached_triangles: Optional[np.ndarray] = None
        self._tri_cache_age: int = 0

        # Pipeline state tracking
        self._consecutive_misses: int = 0
        self._last_video_timestamp_ms: int = 0

    @property
    def active_clutch(self) -> str:
        """Backward-compatibility alias: maps active_latch to clutch names."""
        if self.active_latch == "SCROLL_LOCKED":
            return "SCROLL"
        elif self.active_latch == "ZOOM_LOCKED":
            return "ZOOM"
        return "NONE"

    @active_clutch.setter
    def active_clutch(self, val: str) -> None:
        if val == "SCROLL":
            self.active_latch = "SCROLL_LOCKED"
        elif val == "ZOOM":
            self.active_latch = "ZOOM_LOCKED"
        else:
            self.active_latch = "IDLE"

    @property
    def velocity_state(self) -> Tuple[float, float, bool]:
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
        self._freeze_until = time.perf_counter() + duration_s

    def unlock_all_to_idle(self) -> None:
        """Universal Escape: resets any active latch (SCROLL_LOCKED, ZOOM_LOCKED) back to IDLE immediately."""
        self.active_latch = "IDLE"
        self._left_wink_start = 0.0
        self._right_wink_start = 0.0
        self._blink_timestamps.clear()
        self._last_gesture_banner = "✅ UNLOCKED — FREE CURSOR"
        self._gesture_banner_expire = time.time() + 1.5
        if self.on_latch_changed:
            self.on_latch_changed("IDLE")
        if self.on_scroll_clutch:
            self.on_scroll_clutch(False, 0.0)
        if self.on_zoom_clutch:
            self.on_zoom_clutch(False, 0.0, 1.0)
        if self.on_universal_exit:
            try:
                self.on_universal_exit()
            except Exception:
                pass

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
        self._frame_source = FrameSource(self.camera_id)
        if not self._frame_source.start():
            self._running = False
            return False

        self._thread = threading.Thread(
            target=self._tracker_loop,
            daemon=True,
            name="OmniBandNeuralTrackerLoop",
        )
        self._thread.start()
        log.info("RealWebcamTracker started on camera_id=%d", self.camera_id)
        return True

    def stop(self) -> None:
        self._running = False
        if self._frame_source:
            self._frame_source.stop()
            self._frame_source = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        log.info("RealWebcamTracker stopped")

    # ------------------------------------------------------------------
    # Model Loading
    # ------------------------------------------------------------------
    def _init_models(self) -> Tuple[Optional[object], Optional[object], Optional[cv2.FaceDetectorYN]]:
        det_video = None
        det_image = None
        yunet = None

        if HAS_MEDIAPIPE:
            try:
                model_bytes = load_verified_model_bytes("face_landmarker.task")
                base_opts = mp_python.BaseOptions(model_asset_buffer=model_bytes)

                # S1 Video mode detector
                opts_v = vision.FaceLandmarkerOptions(
                    base_options=base_opts,
                    running_mode=vision.RunningMode.VIDEO,
                    num_faces=1,
                    min_face_detection_confidence=0.15,
                    min_face_presence_confidence=0.15,
                    output_face_blendshapes=True,
                )
                det_video = vision.FaceLandmarker.create_from_options(opts_v)

                # S2 Image mode detector for cropped ROI
                opts_i = vision.FaceLandmarkerOptions(
                    base_options=base_opts,
                    running_mode=vision.RunningMode.IMAGE,
                    num_faces=1,
                    min_face_detection_confidence=0.15,
                    min_face_presence_confidence=0.15,
                    output_face_blendshapes=True,
                )
                det_image = vision.FaceLandmarker.create_from_options(opts_i)
                log.info("MediaPipe VIDEO & IMAGE landmarker models loaded successfully")
            except Exception as e:
                log.warning("MediaPipe FaceLandmarker load failed: %s", e)

        # YuNet FaceDetectorYN
        try:
            yn_path = resource_path("models/face_detection_yunet_2023mar.onnx")
            if yn_path.is_file():
                yunet = cv2.FaceDetectorYN.create(str(yn_path), "", (640, 480), 0.6, 0.3)
                log.info("YuNet FaceDetectorYN loaded from %s", yn_path)
            else:
                log.warning("YuNet ONNX file not found at %s", yn_path)
        except Exception as e:
            log.warning("YuNet FaceDetectorYN load failed: %s", e)

        return det_video, det_image, yunet

    # ------------------------------------------------------------------
    # Main Processing Loop
    # ------------------------------------------------------------------
    def _tracker_loop(self) -> None:
        det_video, det_image, yunet = self._init_models()
        prev_time = time.perf_counter()

        while self._running:
            if self._frame_source is None:
                break
            raw_frame = self._frame_source.read_latest(timeout=0.2)
            if raw_frame is None:
                continue

            # Mirror: head right -> cursor right
            frame = cv2.flip(raw_frame, 1)
            h, w = frame.shape[:2]

            now = time.perf_counter()
            dt = max(1e-4, now - prev_time)
            inst_fps = 1.0 / dt
            self.fps = 0.9 * self.fps + 0.1 * inst_fps if self.fps > 0 else inst_fps
            prev_time = now

            # Strictly increasing integer ms for MediaPipe VIDEO mode (R07)
            t_ms = int(now * 1000)
            if t_ms <= self._last_video_timestamp_ms:
                t_ms = self._last_video_timestamp_ms + 1
            self._last_video_timestamp_ms = t_ms

            # Low-light enhancement (L-03)
            enhanced_frame = enhance_low_light(frame)
            frame_rgb = cv2.cvtColor(enhanced_frame, cv2.COLOR_BGR2RGB)

            # Pipeline execution
            res = self._execute_pipeline(
                frame_rgb, enhanced_frame, w, h, t_ms, det_video, det_image, yunet
            )
            self.latest_result = res
            self.current_tier = res.tier
            self.current_ear = res.ear
            self.current_mar = res.mar
            self.current_left_ear = res.left_ear
            self.current_right_ear = res.right_ear
            self.current_eye_dist = res.eye_dist
            self.head_yaw = res.yaw
            self.head_pitch = res.pitch

            # Velocity integration
            self._update_velocity(res, now, w, h)

            # Gesture decoding (Asymmetric Wink-Clutch + Blinks + Jaw)
            if res.left_ear is not None and res.right_ear is not None and res.mar is not None:
                self._process_gestures(res, now)
            elif res.ear is not None and res.mar is not None:
                self._process_legacy_gestures(res.ear, res.mar, now)

            # Envelopes
            self._emit_envelopes(res)

            # HUD rendering
            cx0 = self._base_x if self._calibrated else 0.5
            cy0 = self._base_y if self._calibrated else 0.45
            nose_px = (int(res.nose_tip[0] * w), int(res.nose_tip[1] * h)) if res.nose_tip else None
            self._draw_hud(
                frame_rgb, w, h, cx0, cy0, nose_px,
                self._in_deadzone, self._vx, self._vy, res.face_found
            )

            if self.on_frame:
                self.on_frame(frame_rgb)

        # Cleanup detectors
        if det_video is not None:
            try:
                det_video.close()
            except Exception:
                pass
        if det_image is not None:
            try:
                det_image.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Pipeline Dispatch: S1 -> S2 -> S3 -> S4
    # ------------------------------------------------------------------
    def _execute_pipeline(
        self,
        frame_rgb: np.ndarray,
        frame_bgr: np.ndarray,
        w: int,
        h: int,
        timestamp_ms: int,
        det_video: Optional[object],
        det_image: Optional[object],
        yunet: Optional[cv2.FaceDetectorYN],
    ) -> TrackerResult:
        # S1: MediaPipe VIDEO mode full frame
        if det_video is not None:
            try:
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
                out = det_video.detect_for_video(mp_img, timestamp_ms)
                if out and out.face_landmarks and len(out.face_landmarks) > 0:
                    self._consecutive_misses = 0
                    return self._build_result_from_landmarks(
                        out.face_landmarks[0], out.face_blendshapes, w, h, timestamp_ms, "FACEMESH478"
                    )
            except Exception as e:
                log.debug("Stage S1 error: %s", e)

        self._consecutive_misses += 1

        # S2 / S3: YuNet Assist
        if yunet is not None:
            try:
                yunet.setInputSize((w, h))
                _, faces = yunet.detect(frame_bgr)
                if faces is not None and len(faces) > 0:
                    faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
                    best_face = faces[0]
                    fx, fy, fw, fh = int(best_face[0]), int(best_face[1]), int(best_face[2]), int(best_face[3])

                    # S2: 1.6x expanded square ROI -> FaceLandmarker IMAGE mode
                    if det_image is not None:
                        cx = fx + fw // 2
                        cy = fy + fh // 2
                        roi_size = int(max(fw, fh) * 1.6)
                        rx1 = max(0, cx - roi_size // 2)
                        ry1 = max(0, cy - roi_size // 2)
                        rx2 = min(w, rx1 + roi_size)
                        ry2 = min(h, ry1 + roi_size)
                        rw = rx2 - rx1
                        rh = ry2 - ry1

                        if rw > 40 and rh > 40:
                            crop_rgb = frame_rgb[ry1:ry2, rx1:rx2]
                            crop_mp = mp.Image(image_format=mp.ImageFormat.SRGB, data=crop_rgb)
                            crop_out = det_image.detect(crop_mp)
                            if crop_out and crop_out.face_landmarks and len(crop_out.face_landmarks) > 0:
                                mapped_landmarks = []
                                for lm in crop_out.face_landmarks[0]:
                                    mx = (rx1 + lm.x * rw) / float(w)
                                    my = (ry1 + lm.y * rh) / float(h)
                                    mz = lm.z * (float(rw) / float(w))
                                    mapped_landmarks.append((mx, my, mz))

                                self._consecutive_misses = 0
                                return self._build_result_from_mapped_landmarks(
                                    mapped_landmarks, crop_out.face_blendshapes, w, h, timestamp_ms, "FACEMESH478"
                                )

                    # S3: YuNet 5-point keypoint fallback
                    nx_norm = float(best_face[8]) / float(w)
                    ny_norm = float(best_face[9]) / float(h)
                    yaw = (nx_norm - 0.5) * 60.0
                    pitch = (ny_norm - 0.45) * 60.0

                    return TrackerResult(
                        tier="KEYPOINT5",
                        face_found=True,
                        nose_tip=(nx_norm, ny_norm),
                        landmarks=None,
                        ear=None,
                        mar=None,
                        yaw=yaw,
                        pitch=pitch,
                        timestamp_ms=timestamp_ms,
                    )
            except Exception as e:
                log.debug("Stage S2/S3 YuNet error: %s", e)

        # S4: NO_FACE
        return TrackerResult(
            tier="NO_FACE",
            face_found=False,
            nose_tip=None,
            landmarks=None,
            ear=None,
            mar=None,
            yaw=0.0,
            pitch=0.0,
            timestamp_ms=timestamp_ms,
        )

    # ------------------------------------------------------------------
    # Landmark Processing Helpers
    # ------------------------------------------------------------------
    def _build_result_from_landmarks(
        self,
        raw_landmarks: list,
        blendshapes: list,
        w: int,
        h: int,
        timestamp_ms: int,
        tier: str,
    ) -> TrackerResult:
        landmarks_3d = [(float(lm.x), float(lm.y), float(lm.z)) for lm in raw_landmarks]
        return self._build_result_from_mapped_landmarks(
            landmarks_3d, blendshapes, w, h, timestamp_ms, tier
        )

    def _build_result_from_mapped_landmarks(
        self,
        landmarks_3d: List[Tuple[float, float, float]],
        blendshapes: list,
        w: int,
        h: int,
        timestamp_ms: int,
        tier: str,
    ) -> TrackerResult:
        nose_norm = (landmarks_3d[NOSE_TIP_INDEX][0], landmarks_3d[NOSE_TIP_INDEX][1])
        landmarks_px = [(p[0] * w, p[1] * h) for p in landmarks_3d]
        yaw, pitch = estimate_head_pose(landmarks_px, w, h)

        ear: Optional[float] = None
        mar: Optional[float] = None
        left_ear: Optional[float] = None
        right_ear: Optional[float] = None
        eye_dist: Optional[float] = None

        left_eye_pts = [landmarks_px[i] for i in LEFT_EYE_INDICES]
        right_eye_pts = [landmarks_px[i] for i in RIGHT_EYE_INDICES]
        left_ear = calculate_ear(left_eye_pts)
        right_ear = calculate_ear(right_eye_pts)
        ear = (left_ear + right_ear) / 2.0

        p_le = landmarks_3d[33]
        p_re = landmarks_3d[263]
        eye_dist = math.hypot(p_le[0] - p_re[0], p_le[1] - p_re[1])

        if blendshapes and len(blendshapes) > 0:
            bs = {b.category_name: b.score for b in blendshapes[0]}
            blink_score = (bs.get("eyeBlinkLeft", 0.0) + bs.get("eyeBlinkRight", 0.0)) / 2.0
            # Blendshape refine EAR if valid
            blink_l = bs.get("eyeBlinkLeft", 0.0)
            blink_r = bs.get("eyeBlinkRight", 0.0)
            left_ear = max(0.05, 0.35 * (1.0 - blink_l))
            right_ear = max(0.05, 0.35 * (1.0 - blink_r))
            ear = (left_ear + right_ear) / 2.0
            mar = bs.get("jawOpen", 0.0)
        else:
            inner_lip_pts = [landmarks_px[i] for i in INNER_LIP_INDICES]
            mar = calculate_mar(inner_lip_pts)

        return TrackerResult(
            tier=tier,
            face_found=True,
            nose_tip=nose_norm,
            landmarks=landmarks_3d,
            ear=ear,
            mar=mar,
            yaw=yaw,
            pitch=pitch,
            timestamp_ms=timestamp_ms,
            left_ear=left_ear,
            right_ear=right_ear,
            eye_dist=eye_dist,
        )

    # ------------------------------------------------------------------
    # Velocity Integration & Auto-Recenter
    # ------------------------------------------------------------------
    def _update_velocity(self, res: TrackerResult, now: float, w: int, h: int) -> None:
        if res.face_found and not self._face_first_seen:
            self._face_first_seen = True
            self.recenter()

        if not self._calibrated and res.face_found and res.nose_tip is not None:
            self._base_x += res.nose_tip[0]
            self._base_y += res.nose_tip[1]
            self._calib_frames += 1
            if self._calib_frames >= 15:
                self._base_x /= 15.0
                self._base_y /= 15.0
                self._calibrated = True

        cx0 = self._base_x if self._calibrated else 0.5
        cy0 = self._base_y if self._calibrated else 0.45

        vx, vy = 0.0, 0.0
        in_deadzone = True

        if res.face_found and res.nose_tip is not None:
            raw_dx = res.nose_tip[0] - cx0
            raw_dy = res.nose_tip[1] - cy0

            filt_dx = self.filter_dx.filter(now, raw_dx)
            filt_dy = self.filter_dy.filter(now, raw_dy)

            offset_mag = math.hypot(filt_dx, filt_dy)
            in_deadzone = offset_mag < self.DEADZONE

            if not in_deadzone and offset_mag > 0.0:
                excess = offset_mag - self.DEADZONE
                ux = filt_dx / offset_mag
                uy = filt_dy / offset_mag
                base_speed = self.speed_multiplier * (excess ** 1.35) * 1600.0
                vx = ux * base_speed * self.gain_x
                vy = uy * base_speed * self.gain_y

                # Precision Micro-Braking Near Targets (Anti-Jitter for Small Buttons):
                # When head velocity drops below 0.004 / frame, dynamically increase damping (min_cutoff = 0.35)
                if excess < 0.004:
                    self.filter_dx.min_cutoff = 0.35
                    self.filter_dy.min_cutoff = 0.35
                    vx *= 0.20
                    vy *= 0.20
                elif excess < 0.025:
                    self.filter_dx.min_cutoff = self.min_cutoff
                    self.filter_dy.min_cutoff = self.min_cutoff
                    vx *= 0.35
                    vy *= 0.35
                else:
                    self.filter_dx.min_cutoff = self.min_cutoff
                    self.filter_dy.min_cutoff = self.min_cutoff
        else:
            self.filter_dx.reset(now, 0.0)
            self.filter_dy.reset(now, 0.0)

        # Look-Away Auto-Pause Guard (Neck Fatigue & Conversation Guard)
        # If user turns head sharply away (|yaw| > 22.0 or |nose_x - 0.5| > 0.38 or face leaves frame)
        is_look_away = False
        if not res.face_found or res.nose_tip is None:
            is_look_away = True
        elif abs(res.yaw) > 22.0 or abs(res.nose_tip[0] - 0.5) > 0.38:
            is_look_away = True

        self.is_looking_away = is_look_away
        if is_look_away:
            vx = 0.0
            vy = 0.0
            self._last_gesture_banner = "⏸️ PAUSED (LOOKING AWAY)"
            self._gesture_banner_expire = now + 0.4
        elif now < self._freeze_until or self.active_latch != "IDLE":
            vx = 0.0
            vy = 0.0

        with self._state_lock:
            self._vx = vx
            self._vy = vy
            self._in_deadzone = in_deadzone

    # ------------------------------------------------------------------
    # Gesture Decoders (Opposite-Eye Latch Lock + Auto-Baseline Rolling Tracker)
    # ------------------------------------------------------------------
    def _process_gestures(self, res: TrackerResult, now: float) -> None:
        left_ear = res.left_ear if res.left_ear is not None else 0.28
        right_ear = res.right_ear if res.right_ear is not None else 0.28
        mar = res.mar if res.mar is not None else 0.0
        ear = res.ear if res.ear is not None else (left_ear + right_ear) / 2.0
        ny = res.nose_tip[1] if res.nose_tip is not None else 0.45
        eye_dist = res.eye_dist if res.eye_dist is not None else 0.12

        # ----------------------------------------------------------------
        # 1. AUTO-BASELINE ROLLING TRACKER (Zero-Click Auto-Enable on Launch)
        # ----------------------------------------------------------------
        # Track live open-eye baseline whenever eyes are open and symmetric
        if left_ear > 0.20 and right_ear > 0.20 and abs(left_ear - right_ear) < 0.045:
            if not self._auto_calibrated:
                self._auto_baseline_samples += 1
                # Fast convergence over first 20 frames
                alpha_init = 0.25 if self._auto_baseline_samples < 20 else 0.05
                self.auto_open_ear_L = (1.0 - alpha_init) * self.auto_open_ear_L + alpha_init * left_ear
                self.auto_open_ear_R = (1.0 - alpha_init) * self.auto_open_ear_R + alpha_init * right_ear
                if self._auto_baseline_samples >= 20:
                    self._auto_calibrated = True
                    log.info("Auto-baseline optical calibration ready: L=%.2f, R=%.2f",
                             self.auto_open_ear_L, self.auto_open_ear_R)
            else:
                # Slow continuous EMA tracking
                self.auto_open_ear_L = 0.98 * self.auto_open_ear_L + 0.02 * left_ear
                self.auto_open_ear_R = 0.98 * self.auto_open_ear_R + 0.02 * right_ear

        # Derive dynamic relative wink thresholds
        # Left Wink Tap: left_ear < auto_open_ear_L * 0.68 while right_ear > auto_open_ear_R * 0.78
        l_wink_thresh = min(0.24, max(0.14, self.auto_open_ear_L * 0.68))
        r_open_floor = max(0.16, min(0.28, self.auto_open_ear_R * 0.78))

        # Right Wink Tap: right_ear < auto_open_ear_R * 0.68 while left_ear > auto_open_ear_L * 0.78
        r_wink_thresh = min(0.24, max(0.14, self.auto_open_ear_R * 0.68))
        l_open_floor = max(0.16, min(0.28, self.auto_open_ear_L * 0.78))

        # Bilateral Blink: both eyes closing simultaneously
        is_bilateral_blink = (left_ear < l_wink_thresh) and (right_ear < r_wink_thresh)

        # Track bilateral blink duration (70 ms - 350 ms) & rolling timestamps for Triple-Blink Universal Exit
        if is_bilateral_blink:
            if self._bilateral_blink_start == 0.0:
                self._bilateral_blink_start = now
        else:
            if self._bilateral_blink_start > 0.0:
                dur = now - self._bilateral_blink_start
                self._bilateral_blink_start = 0.0
                if 0.06 <= dur <= 0.40:
                    self._blink_timestamps.append(now)
                    self._blink_timestamps = [t for t in self._blink_timestamps if now - t <= 1.20]
                    # Check Universal Triple-Blink Exit (3 blinks within 1.20s)
                    if len(self._blink_timestamps) >= 3:
                        self._blink_timestamps.clear()
                        self._last_blink_time = 0.0
                        self._blink_active = False
                        self.unlock_all_to_idle()
                        return

        # Asymmetric Left Wink condition
        is_left_wink = (
            left_ear < l_wink_thresh
            and right_ear >= r_open_floor
            and (right_ear - left_ear) >= 0.04
            and not is_bilateral_blink
        )

        # Asymmetric Right Wink condition
        is_right_wink = (
            right_ear < r_wink_thresh
            and left_ear >= l_open_floor
            and (left_ear - right_ear) >= 0.04
            and not is_bilateral_blink
        )

        # Smooth filtered pitch
        self._filtered_pitch_y = 0.80 * self._filtered_pitch_y + 0.20 * ny

        # Track wink tap durations (140 ms to 600 ms)
        if is_left_wink:
            if self._left_wink_start == 0.0:
                self._left_wink_start = now
        else:
            self._left_wink_start = 0.0

        if is_right_wink:
            if self._right_wink_start == 0.0:
                self._right_wink_start = now
        else:
            self._right_wink_start = 0.0

        # ====================================================================
        # 2. OPPOSITE-EYE LATCH LOCK STATE MACHINE
        # ====================================================================
        min_wink_s = 0.14  # 140 ms minimum sustained wink tap

        # -------------------------------------------------------------
        # STATE: IDLE (Normal Cursor Movement)
        # -------------------------------------------------------------
        if self.active_latch == "IDLE":
            # Left-Wink Tap -> LATCH INTO SCROLL_LOCKED
            if is_left_wink and (now - self._left_wink_start >= min_wink_s):
                self.active_latch = "SCROLL_LOCKED"
                self._latch_locked_time = now
                self._pitch_anchor_y = ny
                self._filtered_pitch_y = ny
                self._left_wink_start = 0.0
                self._last_gesture_banner = "🔵 SCROLL LOCKED | 👁️x3 Triple-Blink to Exit"
                self._gesture_banner_expire = now + 2.0
                if self.on_latch_changed:
                    self.on_latch_changed("SCROLL_LOCKED")
                if self.on_scroll_clutch:
                    self.on_scroll_clutch(True, 0.0)
                return

            # Right-Wink Tap -> LATCH INTO ZOOM_LOCKED
            elif is_right_wink and (now - self._right_wink_start >= min_wink_s):
                self.active_latch = "ZOOM_LOCKED"
                self._latch_locked_time = now
                self._pitch_anchor_y = ny
                self._filtered_pitch_y = ny
                self._scale_anchor_dist = eye_dist
                self._right_wink_start = 0.0
                self._last_gesture_banner = "🟣 ZOOM LOCKED | 👁️x3 Triple-Blink to Exit"
                self._gesture_banner_expire = now + 2.0
                if self.on_latch_changed:
                    self.on_latch_changed("ZOOM_LOCKED")
                if self.on_zoom_clutch:
                    self.on_zoom_clutch(True, 0.0, 1.0)
                return

        # -------------------------------------------------------------
        # STATE: SCROLL_LOCKED (Both eyes comfortably open! Continuous 60 Hz)
        # -------------------------------------------------------------
        elif self.active_latch == "SCROLL_LOCKED":
            time_since_lock = now - self._latch_locked_time
            # OPPOSITE-EYE UNLOCK: Right-Wink Tap (>= 140ms) unlocks back to IDLE
            # Requires 400ms cooldown after entry to prevent instant double-tap exit
            if time_since_lock >= 0.40 and is_right_wink and (now - self._right_wink_start >= min_wink_s):
                self.active_latch = "IDLE"
                self._right_wink_start = 0.0
                self._last_gesture_banner = "SCROLL UNLOCKED (CURSOR FREE)"
                self._gesture_banner_expire = now + 1.2
                if self.on_latch_changed:
                    self.on_latch_changed("IDLE")
                if self.on_scroll_clutch:
                    self.on_scroll_clutch(False, 0.0)
                return

            # Continuous 60 Hz Smooth Scroll while locked
            # delta_pitch = pitch_anchor_y - filtered_ny (Head UP -> delta_pitch > 0 -> Scroll UP)
            delta_pitch = self._pitch_anchor_y - self._filtered_pitch_y
            if self.on_scroll_clutch:
                self.on_scroll_clutch(True, delta_pitch)
            return

        # -------------------------------------------------------------
        # STATE: ZOOM_LOCKED (Both eyes open! Precision Zoom Steps)
        # -------------------------------------------------------------
        elif self.active_latch == "ZOOM_LOCKED":
            time_since_lock = now - self._latch_locked_time
            # OPPOSITE-EYE UNLOCK: Left-Wink Tap (>= 140ms) unlocks back to IDLE
            if time_since_lock >= 0.40 and is_left_wink and (now - self._left_wink_start >= min_wink_s):
                self.active_latch = "IDLE"
                self._left_wink_start = 0.0
                self._last_gesture_banner = "ZOOM UNLOCKED (CURSOR FREE)"
                self._gesture_banner_expire = now + 1.2
                if self.on_latch_changed:
                    self.on_latch_changed("IDLE")
                if self.on_zoom_clutch:
                    self.on_zoom_clutch(False, 0.0, 1.0)
                return

            delta_pitch = self._pitch_anchor_y - self._filtered_pitch_y
            scale_ratio = eye_dist / max(1e-4, self._scale_anchor_dist)
            if self.on_zoom_clutch:
                self.on_zoom_clutch(True, delta_pitch, scale_ratio)
            return

        # ====================================================================
        # 3. NORMAL BILATERAL BLINK & JAW GESTURES (Only when IDLE)
        # ====================================================================
        self._process_legacy_gestures(ear, mar, now)

    def _process_legacy_gestures(self, ear: float, mar: float, now: float) -> None:
        # Update running baseline for open-eye EAR
        if ear > 0.22:
            self._baseline_ear = 0.95 * self._baseline_ear + 0.05 * ear

        # Adaptive blink threshold
        blink_threshold = min(0.24, max(0.21, self._baseline_ear * 0.78))
        is_blinking = ear < blink_threshold

        if is_blinking and not self._blink_active:
            self._blink_active = True
            self._blink_start_time = now

            time_since_first = now - self._last_blink_time
            if 0.10 <= time_since_first <= 0.85:
                self.trigger_blink_freeze(0.35)
                self._last_gesture_banner = "DOUBLE-BLINK -> LEFT CLICK"
                self._gesture_banner_expire = now + 1.2
                self._last_blink_time = 0.0
                if self.on_click:
                    self.on_click()
            else:
                self._last_blink_time = now
                self._last_gesture_banner = "BLINK [1/2] - BLINK AGAIN TO CLICK"
                self._gesture_banner_expire = now + 0.85

        elif is_blinking and self._blink_active:
            closed_dur = now - self._blink_start_time
            if closed_dur >= 0.40 and not self._long_blink_fired:
                self._long_blink_fired = True
                self.trigger_blink_freeze(0.35)
                self._last_gesture_banner = "LONG-BLINK -> LEFT CLICK"
                self._gesture_banner_expire = now + 1.2
                self._last_blink_time = 0.0
                if self.on_click:
                    self.on_click()

        elif not is_blinking:
            self._blink_active = False
            self._long_blink_fired = False

        # Jaw tap -> Click / Jaw hold -> Drag Latch
        jaw_threshold = self.calibrator.profile.jaw_click_thresh
        is_jaw_open = mar > jaw_threshold
        if is_jaw_open:
            if self._jaw_open_start == 0.0:
                self._jaw_open_start = now
                self._jaw_latched_drag = False
            else:
                hold_dur = now - self._jaw_open_start
                if hold_dur > 0.65 and not self._jaw_latched_drag:
                    self._jaw_latched_drag = True
                    self.trigger_blink_freeze(0.35)
                    self._last_gesture_banner = "JAW HOLD -> DRAG LATCH"
                    self._gesture_banner_expire = now + 1.2
                    if self.on_drag_toggle:
                        self.on_drag_toggle()
        else:
            if self._jaw_open_start > 0.0:
                open_dur = now - self._jaw_open_start
                self._jaw_open_start = 0.0
                if 0.10 <= open_dur <= 0.45:
                    self.trigger_blink_freeze(0.35)
                    self._last_gesture_banner = "JAW TAP -> LEFT CLICK"
                    self._gesture_banner_expire = now + 1.2
                    if self.on_click:
                        self.on_click()

    # ------------------------------------------------------------------
    # Channel Envelopes
    # ------------------------------------------------------------------
    def _emit_envelopes(self, res: TrackerResult) -> None:
        cx0 = self._base_x if self._calibrated else 0.5
        cy0 = self._base_y if self._calibrated else 0.45

        if res.face_found and res.nose_tip is not None:
            raw_dx = res.nose_tip[0] - cx0
            raw_dy = res.nose_tip[1] - cy0
            mar_val = res.mar if res.mar is not None else 0.0
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

    # ------------------------------------------------------------------
    # Drawing & HUD Helpers
    # ------------------------------------------------------------------
    def _draw_facemesh_wireframe(
        self,
        frame_rgb: np.ndarray,
        lm: list,
        w: int,
        h: int,
    ) -> None:
        """Draw real wireframe mesh on detected face landmarks."""
        pts = np.array([(int(p.x * w), int(p.y * h)) for p in lm[:468]], dtype=np.int32)

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

        for contour in [LEFT_EYE_CONTOUR, RIGHT_EYE_CONTOUR]:
            c_pts = np.array([pts[i] for i in contour], dtype=np.int32)
            cv2.polylines(frame_rgb, [c_pts], True, (0, 230, 118), 1, cv2.LINE_AA)

        lip_pts = np.array([pts[i] for i in LIPS_CONTOUR], dtype=np.int32)
        cv2.polylines(frame_rgb, [lip_pts], True, (240, 120, 66), 2, cv2.LINE_AA)

    def _draw_cascade_overlay(
        self,
        frame_rgb: np.ndarray,
        fx: int,
        fy: int,
        fw: int,
        fh: int,
        eye_cx: int,
        eye_cy: int,
        nx_px: int,
        ny_px: int,
        w: int,
        h: int,
    ) -> None:
        """Backward compatibility drawer for verified face regions."""
        cv2.rectangle(frame_rgb, (fx, fy), (fx + fw, fy + fh), (233, 229, 222), 1, cv2.LINE_AA)
        cv2.circle(frame_rgb, (eye_cx, eye_cy), 6, (0, 230, 118), 2, cv2.LINE_AA)
        cv2.circle(frame_rgb, (nx_px, ny_px), 5, (240, 120, 66), -1, cv2.LINE_AA)

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
        """Render deadzone ring, vector line, alignment guide, and HUD text."""
        center_px = (int(cx0 * w), int(cy0 * h))
        radius_px = max(6, int(self.DEADZONE * min(w, h)))

        if face_found and nose_px is not None:
            ring_color = (0, 230, 118) if in_deadzone else (240, 120, 66)
            cv2.circle(frame_rgb, center_px, radius_px, ring_color, 2, cv2.LINE_AA)
            cv2.circle(frame_rgb, center_px, 3, ring_color, -1)
            cv2.line(frame_rgb, center_px, nose_px, ring_color, 2, cv2.LINE_AA)

            status = "CURSOR: STILL (DEADZONE)" if in_deadzone else f"CURSOR: GLIDING  vx={vx:+.0f} vy={vy:+.0f}"
            cv2.putText(frame_rgb, status, (16, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.50, ring_color, 2, cv2.LINE_AA)

            ear_str = f"{self.current_ear:.2f}" if self.current_ear is not None else "--"
            l_ear_str = f"{self.current_left_ear:.2f}" if self.current_left_ear is not None else "--"
            r_ear_str = f"{self.current_right_ear:.2f}" if self.current_right_ear is not None else "--"
            mar_str = f"{self.current_mar:.2f}" if self.current_mar is not None else "--"
            latch_str = self.active_latch
            cv2.putText(
                frame_rgb,
                f"FPS: {self.fps:4.1f} | L:{l_ear_str} R:{r_ear_str} | MAR:{mar_str} | LATCH: {latch_str}",
                (16, h - 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (247, 245, 240), 1, cv2.LINE_AA,
            )

        # Active gesture feedback banner pill (top center)
        now_t = time.perf_counter()
        if now_t < self._gesture_banner_expire and self._last_gesture_banner:
            banner_text = self._last_gesture_banner
            (tw, th), _ = cv2.getTextSize(banner_text, cv2.FONT_HERSHEY_SIMPLEX, 0.50, 2)
            bx1 = (w - tw) // 2 - 10
            by1 = 12
            bx2 = bx1 + tw + 20
            by2 = by1 + th + 14
            is_click = "CLICK" in banner_text
            b_col = (0, 230, 118) if is_click else (240, 120, 66)
            cv2.rectangle(frame_rgb, (bx1, by1), (bx2, by2), (37, 37, 37), -1)
            cv2.rectangle(frame_rgb, (bx1, by1), (bx2, by2), b_col, 2, cv2.LINE_AA)
            cv2.putText(frame_rgb, banner_text, (bx1 + 10, by1 + th + 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.50, b_col, 2, cv2.LINE_AA)

        if not (face_found and nose_px is not None):
            pad_x, pad_y = w // 6, h // 6
            overlay = frame_rgb.copy()
            cv2.rectangle(overlay, (pad_x, pad_y), (w - pad_x, h - pad_y), (240, 120, 66), 2, cv2.LINE_AA)
            tick = 20
            for (cx, cy) in [(pad_x, pad_y), (w - pad_x, pad_y), (pad_x, h - pad_y), (w - pad_x, h - pad_y)]:
                dx = 1 if cx == pad_x else -1
                dy = 1 if cy == pad_y else -1
                cv2.line(overlay, (cx, cy), (cx + dx * tick, cy), (240, 120, 66), 2)
                cv2.line(overlay, (cx, cy), (cx, cy + dy * tick), (240, 120, 66), 2)
            cv2.addWeighted(overlay, 0.7, frame_rgb, 0.3, 0, frame_rgb)

            cv2.putText(
                frame_rgb,
                "EAR: -- | MAR: -- | \u26a0 ALIGN FULL FACE IN FRAME",
                (16, h - 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (240, 120, 66), 1, cv2.LINE_AA,
            )
            cv2.putText(
                frame_rgb,
                f"FPS: {self.fps:4.1f} | Searching for face...",
                (16, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (247, 245, 240), 1, cv2.LINE_AA,
            )


# Alias for backward compatibility
WebcamTracker = RealWebcamTracker
