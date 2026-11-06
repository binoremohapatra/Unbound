"""Webcam adapter using MediaPipe: emits head pose and facial blendshape SignalEvents."""

import logging
import threading
import time
from typing import Callable, Optional, Tuple
from handsfree.core.events import SignalEvent, SignalType

logger = logging.getLogger("handsfree.adapter.webcam")

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    import mediapipe as mp
    HAS_MEDIAPIPE = True
except ImportError:
    HAS_MEDIAPIPE = False


class WebcamAdapter:
    """Captures camera frames and runs MediaPipe Face Landmarker to extract pose & blendshapes."""

    def __init__(
        self,
        camera_id: int = 0,
        width: int = 640,
        height: int = 480,
        fps: int = 60,
        on_event: Optional[Callable[[SignalEvent], None]] = None,
        on_heartbeat: Optional[Callable[[], None]] = None,
    ):
        self.camera_id = camera_id
        self.width = width
        self.height = height
        self.fps = fps
        self.on_event = on_event
        self.on_heartbeat = on_heartbeat
        self.is_running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.is_running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="WebcamThread")
        self._thread.start()
        logger.info(f"Webcam adapter started on camera_id={self.camera_id}")

    def stop(self) -> None:
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        logger.info("Webcam adapter stopped.")

    def _estimate_head_pose(self, landmarks, frame_w: int, frame_h: int) -> Tuple[float, float, float]:
        nose = landmarks[1]
        chin = landmarks[152]
        left_eye = landmarks[33]
        right_eye = landmarks[263]

        eye_center_x = (left_eye.x + right_eye.x) / 2.0
        eye_center_y = (left_eye.y + right_eye.y) / 2.0
        eye_dist = abs(right_eye.x - left_eye.x)

        yaw = ((nose.x - eye_center_x) / (eye_dist + 1e-6)) * 90.0
        upper_len = nose.y - eye_center_y
        lower_len = chin.y - nose.y
        pitch = ((upper_len - lower_len) / (upper_len + lower_len + 1e-6)) * 60.0
        dy = (right_eye.y - left_eye.y) * frame_h
        dx = (right_eye.x - left_eye.x) * frame_w
        roll = (dy / (dx + 1e-6)) * 57.3

        return yaw, pitch, roll

    def _capture_loop(self) -> None:
        if not HAS_CV2 or not HAS_MEDIAPIPE:
            logger.warning("OpenCV or MediaPipe not installed. Running webcam adapter in mock mode.")
            self._run_mock_loop()
            return

        cap = cv2.VideoCapture(self.camera_id)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)

        if not cap.isOpened():
            logger.error(f"Cannot open camera {self.camera_id}. Falling back to mock loop.")
            self._run_mock_loop()
            return

        mp_face_mesh = mp.solutions.face_mesh
        with mp_face_mesh.FaceMesh(
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        ) as face_mesh:
            while self.is_running:
                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.01)
                    continue

                now = time.time()
                if self.on_heartbeat:
                    self.on_heartbeat()

                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                results = face_mesh.process(rgb_frame)

                if results.multi_face_landmarks:
                    face_landmarks = results.multi_face_landmarks[0].landmark
                    h, w, _ = frame.shape
                    yaw, pitch, roll = self._estimate_head_pose(face_landmarks, w, h)

                    if self.on_event:
                        self.on_event(
                            SignalEvent(
                                signal_type=SignalType.HEAD_POSE,
                                name="pose",
                                value={"yaw": yaw, "pitch": pitch, "roll": roll},
                                timestamp=now,
                            )
                        )

                        upper_lip = face_landmarks[13].y
                        lower_lip = face_landmarks[14].y
                        jaw_open_raw = max(0.0, min(1.0, (lower_lip - upper_lip) * 15.0))

                        self.on_event(
                            SignalEvent(
                                signal_type=SignalType.BLENDSHAPE,
                                name="blendshapes",
                                value={"jawOpen": jaw_open_raw},
                                timestamp=now,
                            )
                        )

            cap.release()

    def _run_mock_loop(self) -> None:
        while self.is_running:
            time.sleep(1.0 / self.fps)
            now = time.time()
            if self.on_heartbeat:
                self.on_heartbeat()
            if self.on_event:
                self.on_event(
                    SignalEvent(
                        signal_type=SignalType.HEAD_POSE,
                        name="pose",
                        value={"yaw": 0.0, "pitch": 0.0, "roll": 0.0},
                        timestamp=now,
                    )
                )

