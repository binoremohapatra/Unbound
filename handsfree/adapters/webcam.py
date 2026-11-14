"""Webcam adapter using MediaPipe FaceLandmarker: emits head pose and facial blendshape SignalEvents."""

import logging
from pathlib import Path
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

    def _find_model_path(self) -> Optional[Path]:
        candidates = [
            Path("face_landmarker.task"),
            Path(__file__).resolve().parent.parent.parent / "face_landmarker.task",
            Path(__file__).resolve().parent.parent / "face_landmarker.task",
        ]
        for c in candidates:
            if c.is_file():
                return c
        return None

    def _capture_loop(self) -> None:
        if self.on_heartbeat:
            self.on_heartbeat()

        if not HAS_CV2:
            logger.warning("OpenCV not installed. Running webcam adapter in mock mode.")
            self._run_mock_loop()
            return

        cap = cv2.VideoCapture(self.camera_id)
        if hasattr(cv2, "CAP_PROP_FRAME_WIDTH"):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        if hasattr(cv2, "CAP_PROP_FRAME_HEIGHT"):
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if hasattr(cv2, "CAP_PROP_FPS"):
            cap.set(cv2.CAP_PROP_FPS, self.fps)

        if not cap.isOpened():
            logger.error(f"Cannot open camera {self.camera_id}. Falling back to mock loop.")
            self._run_mock_loop()
            return

        # Attempt 1: Modern MediaPipe Tasks FaceLandmarker
        model_path = self._find_model_path()
        if HAS_MEDIAPIPE and hasattr(mp, "tasks") and model_path:
            try:
                base = mp.tasks.BaseOptions(model_asset_path=str(model_path))
                options = mp.tasks.vision.FaceLandmarkerOptions(
                    base_options=base,
                    output_face_blendshapes=True,
                    num_faces=1,
                    min_face_detection_confidence=0.5,
                    min_tracking_confidence=0.5,
                )
                landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)
                logger.info("Using MediaPipe Tasks FaceLandmarker for vision tracking.")
                self._run_mediapipe_tasks_loop(cap, landmarker)
                return
            except Exception as e:
                logger.warning(f"Could not initialize MediaPipe Tasks FaceLandmarker: {e}")

        # Attempt 2: Legacy mp.solutions.face_mesh
        if HAS_MEDIAPIPE and hasattr(mp, "solutions") and hasattr(mp.solutions, "face_mesh"):
            try:
                logger.info("Using legacy MediaPipe solutions.face_mesh.")
                self._run_legacy_solutions_loop(cap)
                return
            except Exception as e:
                logger.warning(f"Could not initialize legacy FaceMesh: {e}")

        # Attempt 3: Built-in OpenCV Face Tracking (Cascade Classifier)
        if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data") and hasattr(cv2.data, "haarcascades"):
            try:
                cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                face_cascade = cv2.CascadeClassifier(cascade_path)
                logger.info("Using built-in OpenCV face detection for head tracking.")
                self._run_opencv_cascade_loop(cap, face_cascade)
                return
            except Exception as e:
                logger.warning(f"Could not initialize OpenCV cascade classifier: {e}")

        # Attempt 4: Fallback to mock loop if no vision detectors available
        logger.info("No face tracking backend available on camera. Falling back to mock loop.")
        self._run_mock_loop()

    def _run_mediapipe_tasks_loop(self, cap, landmarker) -> None:
        while self.is_running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.01)
                continue

            now = time.time()
            if self.on_heartbeat:
                self.on_heartbeat()

            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
            detection_result = landmarker.detect(mp_image)

            if detection_result.face_landmarks:
                face_landmarks = detection_result.face_landmarks[0]
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

                # Extract blendshapes
                blendshape_map = {}
                if detection_result.face_blendshapes:
                    for b in detection_result.face_blendshapes[0]:
                        blendshape_map[b.category_name] = b.score

                # High-sensitivity Eye Aspect Ratio (EAR) ensembling
                ear_l = abs(face_landmarks[159].y - face_landmarks[145].y) / (abs(face_landmarks[33].x - face_landmarks[133].x) + 1e-6)
                ear_r = abs(face_landmarks[386].y - face_landmarks[374].y) / (abs(face_landmarks[263].x - face_landmarks[362].x) + 1e-6)
                geom_blink_l = max(0.0, min(1.0, (0.24 - ear_l) * 9.0))
                geom_blink_r = max(0.0, min(1.0, (0.24 - ear_r) * 9.0))

                # Combine neural blendshapes with geometric EAR for maximum detection accuracy
                bs_l = blendshape_map.get("eyeBlinkLeft", 0.0)
                bs_r = blendshape_map.get("eyeBlinkRight", 0.0)
                blendshape_map["eyeBlinkLeft"] = max(bs_l, geom_blink_l)
                blendshape_map["eyeBlinkRight"] = max(bs_r, geom_blink_r)

                if "jawOpen" not in blendshape_map:
                    upper_lip = face_landmarks[13].y
                    lower_lip = face_landmarks[14].y
                    jaw_open_raw = max(0.0, min(1.0, (lower_lip - upper_lip) * 15.0))
                    blendshape_map["jawOpen"] = jaw_open_raw

                if self.on_event:
                    self.on_event(
                        SignalEvent(
                            signal_type=SignalType.BLENDSHAPE,
                            name="blendshapes",
                            value=blendshape_map,
                            timestamp=now,
                        )
                    )

        cap.release()

    def _run_legacy_solutions_loop(self, cap) -> None:
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

                        # Eye Aspect Ratio for blink
                        ear_l = abs(face_landmarks[159].y - face_landmarks[145].y) / (abs(face_landmarks[33].x - face_landmarks[133].x) + 1e-6)
                        ear_r = abs(face_landmarks[386].y - face_landmarks[374].y) / (abs(face_landmarks[263].x - face_landmarks[362].x) + 1e-6)
                        blink_l = max(0.0, min(1.0, (0.22 - ear_l) * 9.0))
                        blink_r = max(0.0, min(1.0, (0.22 - ear_r) * 9.0))

                        self.on_event(
                            SignalEvent(
                                signal_type=SignalType.BLENDSHAPE,
                                name="blendshapes",
                                value={"jawOpen": jaw_open_raw, "eyeBlinkLeft": blink_l, "eyeBlinkRight": blink_r},
                                timestamp=now,
                            )
                        )

            cap.release()

    def _run_opencv_cascade_loop(self, cap, face_cascade) -> None:

        while self.is_running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.01)
                continue

            now = time.time()
            if self.on_heartbeat:
                self.on_heartbeat()

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5, minSize=(60, 60))

            if len(faces) > 0:
                (x, y, w, h) = faces[0]
                frame_h, frame_w = frame.shape[:2]
                face_center_x = x + w / 2.0
                face_center_y = y + h / 2.0

                # Compute approximate yaw and pitch based on displacement from frame center
                norm_x = (face_center_x - frame_w / 2.0) / (frame_w / 2.0)
                norm_y = (face_center_y - frame_h / 2.0) / (frame_h / 2.0)

                yaw = norm_x * 45.0
                pitch = norm_y * 35.0

                if self.on_event:
                    self.on_event(
                        SignalEvent(
                            signal_type=SignalType.HEAD_POSE,
                            name="pose",
                            value={"yaw": yaw, "pitch": pitch, "roll": 0.0},
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
