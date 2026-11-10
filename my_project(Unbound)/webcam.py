"""handsfree/adapters/webcam.py -- webcam capture + MediaPipe FaceLandmarker.

Owner: Antas.

Turns a webcam stream into *unconditioned* ``SignalEvent``s (src="webcam"):

    face_present                      1.0 / 0.0 every processed frame
    head_yaw / head_pitch / head_roll degrees (sign-corrected, see below)
    jaw_open, brow_raise, ...         blendshape scores 0.0..1.0

Every event of a frame carries ``t`` = the monotonic time taken the instant
``cap.grab()`` returned, i.e. *capture* time, never inference-completion time.

Threads
-------
    capture thread   cap.grab() -> stamp t -> cap.retrieve() into a spare buffer
                     -> publish into a depth-1 mailbox (3 buffers, newest wins)
    feeder thread    take newest frame -> BGR->RGB -> landmarker.detect_async()
    MediaPipe thread result callback: matrix -> Euler, blendshapes -> events

The capture thread never waits on inference, so the driver buffer is drained
at camera rate and stale frames are overwritten, never queued. MediaPipe may
additionally drop frames in LIVE_STREAM mode; capture times are carried
through by timestamp so surviving results still map to the right frame.

Consuming events (never blocks frame ingestion)
-----------------------------------------------
    with WebcamAdapter(WebcamConfig()) as cam:
        buf: list[SignalEvent] = []
        while running:                       # your 60 Hz loop
            cam.poll_into(buf)               # or: for ev in cam.poll(): ...
            ...
            buf.clear()

    cam.add_listener(fn)   # push style, runs on the MediaPipe thread: keep fast
    for ev in cam.events(): ...   # blocking generator

Do not call ``stop()`` from inside a listener (it would join its own thread).

Setup: the FaceLandmarker model (``face_landmarker.task``) is not bundled.
Place it at ``<repo>/models/face_landmarker.task``, set
``UNBOUND_FACE_LANDMARKER_MODEL``, or pass ``WebcamConfig(model_path=...)``.

Sign conventions (verify once on your camera with the demo, see ``_cli``):
    head_yaw   > 0 : you turn your head to YOUR right
    head_pitch > 0 : you look up
    head_roll  > 0 : you tilt your head toward YOUR right shoulder
These follow from the geometry of MediaPipe's camera space, but if your setup
disagrees, flip ``yaw_sign`` / ``pitch_sign`` / ``roll_sign`` in WebcamConfig.

Requires Python 3.10+, ``opencv-python``, ``mediapipe`` (imported lazily in
``start()``, so this module imports fine without them).
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Iterator, List, Optional, Tuple, Union

from core.events import SignalEvent

__all__ = [
    "WebcamAdapter",
    "WebcamConfig",
    "WebcamStats",
    "WebcamError",
    "DEFAULT_BLENDSHAPE_MAP",
    "head_pose_degrees",
    "resolve_model_path",
]

log = logging.getLogger(__name__)

PathLike = Union[str, "os.PathLike[str]"]

_IS_WINDOWS: bool = sys.platform.startswith("win")
_RAD2DEG: float = 180.0 / math.pi
_isfinite = math.isfinite
_atan2 = math.atan2
_hypot = math.hypot

_MODEL_ENV = "UNBOUND_FACE_LANDMARKER_MODEL"
_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/latest/face_landmarker.task"
)
_RING = 8  # capture-time lookup slots (frames in flight are << 8)

# SignalEvent name -> MediaPipe blendshape names, averaged. "Left"/"Right" are
# MediaPipe's labels; check which physical side moves with the demo and swap
# the entries here if they are reversed on your setup.
DEFAULT_BLENDSHAPE_MAP: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("jaw_open", ("jawOpen",)),
    ("brow_raise", ("browInnerUp", "browOuterUpLeft", "browOuterUpRight")),
    ("eye_blink_left", ("eyeBlinkLeft",)),
    ("eye_blink_right", ("eyeBlinkRight",)),
    ("mouth_smile", ("mouthSmileLeft", "mouthSmileRight")),
)


class WebcamError(RuntimeError):
    """Camera or landmarker could not be opened."""


# --------------------------------------------------------------------------- #
# Config / stats
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WebcamConfig:
    device_index: int = 0
    width: int = 640
    height: int = 480
    fps: float = 60.0
    fourcc: Optional[str] = "MJPG"          # MJPG keeps USB bandwidth low at high fps
    extra_props: Tuple[Tuple[int, float], ...] = ()   # e.g. ((cv2.CAP_PROP_AUTO_EXPOSURE, 0.25),)
    model_path: Optional[PathLike] = None
    min_face_detection_confidence: float = 0.5
    min_face_presence_confidence: float = 0.5
    min_tracking_confidence: float = 0.5
    yaw_sign: int = -1                      # MediaPipe +yaw = image right; flip so your-right > 0
    pitch_sign: int = 1
    roll_sign: int = 1
    blendshape_map: Tuple[Tuple[str, Tuple[str, ...]], ...] = DEFAULT_BLENDSHAPE_MAP
    queue_size: int = 512                   # events; oldest dropped on overflow
    camera_lost_after_s: float = 1.0        # continuous grab failures before reopen
    reopen_interval_s: float = 1.0

    def __post_init__(self) -> None:
        for name in ("yaw_sign", "pitch_sign", "roll_sign"):
            if getattr(self, name) not in (-1, 1):
                raise ValueError(f"{name} must be +1 or -1")
        if self.width <= 0 or self.height <= 0 or self.fps <= 0:
            raise ValueError("width, height and fps must be positive")
        if self.queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        if self.camera_lost_after_s <= 0 or self.reopen_interval_s <= 0:
            raise ValueError("camera_lost_after_s and reopen_interval_s must be > 0")


@dataclass(frozen=True)
class WebcamStats:
    running: bool
    camera_ok: bool
    frames_captured: int
    frames_dropped: int          # overwritten in the mailbox before inference
    frames_fed: int              # sent to detect_async
    results: int                 # landmarker callbacks received
    no_face_results: int
    capture_fps: float
    latency_ms_last: float       # capture time -> events emitted
    latency_ms_avg: float        # exponential moving average
    latency_ms_max: float
    reconnects: int
    events_dropped: int          # queue overflow (consumer too slow)
    listener_errors: int
    queue_depth: int


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def head_pose_degrees(matrix: Any) -> Tuple[float, float, float]:
    """(yaw, pitch, roll) in degrees from a 4x4 facial transformation matrix.

    Angles are in MediaPipe's own frame, before ``WebcamConfig`` sign factors:
    yaw = azimuth of the face-forward vector (positive toward camera +X),
    pitch = its elevation (positive up), roll = rotation about the forward
    axis (YXZ order). Only ratios of rotation entries are used, so the uniform
    scale baked into the matrix cancels out. Accepts a numpy array or nested
    sequences.
    """
    try:
        g = matrix.item
        r02, r10, r11 = g(0, 2), g(1, 0), g(1, 1)
        r12, r22 = g(1, 2), g(2, 2)
    except AttributeError:
        r02, r10, r11 = float(matrix[0][2]), float(matrix[1][0]), float(matrix[1][1])
        r12, r22 = float(matrix[1][2]), float(matrix[2][2])
    yaw = _atan2(r02, r22)
    pitch = _atan2(r12, _hypot(r02, r22))
    roll = _atan2(r10, r11)
    return yaw * _RAD2DEG, pitch * _RAD2DEG, roll * _RAD2DEG


def resolve_model_path(model_path: Optional[PathLike] = None) -> Path:
    """Find face_landmarker.task: explicit path, env var, <repo>/models, cwd."""
    candidates: List[Path] = []
    if model_path is not None:
        candidates.append(Path(model_path))
    env = os.environ.get(_MODEL_ENV)
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parents[2] / "models" / "face_landmarker.task")
    candidates.append(Path("face_landmarker.task"))
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "face_landmarker.task not found. Searched: "
        + ", ".join(str(c) for c in candidates)
        + f". Download the official model from {_MODEL_URL} and place it in "
        f"<repo>/models/, or set {_MODEL_ENV}."
    )


def _default_clock() -> Callable[[], float]:
    """core.clock's monotonic function if present, else time.monotonic."""
    try:
        from core import clock as mod  # type: ignore[attr-defined]
    except ImportError:
        return time.monotonic
    for name in ("monotonic", "clock"):
        cand = getattr(mod, name, None)
        if callable(cand):
            return cand
        inner = getattr(cand, "monotonic", None)
        if callable(inner):
            return inner
    return time.monotonic


# --------------------------------------------------------------------------- #
# Depth-1 frame mailbox
# --------------------------------------------------------------------------- #
class _FrameMailbox:
    """Latest-frame handoff over three buffers; the producer never blocks.

    The producer writes into a buffer that is neither published nor held by
    the consumer (always one of three exists) and publishes it, replacing any
    unconsumed frame. The consumer takes the newest frame, so anything it was
    too slow for is simply overwritten.
    """

    __slots__ = (
        "_cond", "_published", "_held", "_pub_t", "_pub_seq", "_last_taken",
        "_closed", "taken_idx", "taken_t", "taken_seq",
    )

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._published = -1
        self._held = -1
        self._pub_t = 0.0
        self._pub_seq = 0
        self._last_taken = 0
        self._closed = False
        self.taken_idx = -1     # valid after take() returned True (consumer only)
        self.taken_t = 0.0
        self.taken_seq = 0

    def acquire_write(self) -> int:
        with self._cond:
            for i in (0, 1, 2):
                if i != self._published and i != self._held:
                    return i
        raise AssertionError("unreachable: three buffers, two exclusions")

    def publish(self, idx: int, t: float) -> None:
        with self._cond:
            self._published = idx
            self._pub_t = t
            self._pub_seq += 1
            self._cond.notify()

    def take(self, timeout: float) -> bool:
        with self._cond:
            if self._pub_seq == self._last_taken and not self._closed:
                self._cond.wait(timeout)
            if self._pub_seq == self._last_taken:
                return False
            self._last_taken = self._pub_seq
            self._held = self._published
            self.taken_idx = self._published
            self.taken_t = self._pub_t
            self.taken_seq = self._pub_seq
            return True

    def release(self) -> None:
        with self._cond:
            self._held = -1

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        return self._closed


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class WebcamAdapter:
    """Webcam -> MediaPipe FaceLandmarker -> unconditioned SignalEvents."""

    def __init__(
        self,
        config: Optional[WebcamConfig] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._cfg = config or WebcamConfig()
        self._clock: Callable[[], float] = clock or _default_clock()

        self._lifecycle = threading.Lock()
        self._running = False
        self._stop = threading.Event()
        self._data_ready = threading.Event()
        self._first_result = threading.Event()

        self._queue: Deque[SignalEvent] = deque(maxlen=self._cfg.queue_size)
        self._listeners: Tuple[Callable[[SignalEvent], None], ...] = ()
        self._listeners_lock = threading.Lock()

        self._cv2: Any = None
        self._mp: Any = None
        self._landmarker: Any = None
        self._cap: Any = None
        self._threads: List[threading.Thread] = []

        self._mailbox = _FrameMailbox()
        self._bufs: List[Any] = [None, None, None]
        self._origin = 0.0
        self._ring_ts: List[int] = [-1] * _RING
        self._ring_t: List[float] = [0.0] * _RING
        self._ring_n = 0

        self._plan: Tuple[Tuple[str, Tuple[int, ...], float], ...] = ()
        self._plan_len = -1
        self._ysign = float(self._cfg.yaw_sign)
        self._psign = float(self._cfg.pitch_sign)
        self._rsign = float(self._cfg.roll_sign)

        # Counters: each is written by exactly one thread.
        self._frames_captured = 0
        self._frames_dropped = 0
        self._frames_fed = 0
        self._results = 0
        self._no_face = 0
        self._reconnects = 0
        self._events_dropped = 0
        self._listener_errors = 0
        self._camera_ok = False
        self._dt_ema = 0.0
        self._lat_last = 0.0
        self._lat_ema = 0.0
        self._lat_max = 0.0
        self._cb_errors = 0

    # ---- lifecycle -------------------------------------------------------- #
    def __enter__(self) -> "WebcamAdapter":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> "WebcamAdapter":
        """Open camera + landmarker and spawn threads. Raises on failure."""
        with self._lifecycle:
            if self._running:
                raise RuntimeError("WebcamAdapter already running")
            self._load_backends()
            model = resolve_model_path(self._cfg.model_path)
            try:
                self._landmarker = self._create_landmarker(model)
                self._cap = self._open_capture()
            except BaseException:
                self._release_resources()
                raise

            self._stop.clear()
            self._first_result.clear()
            self._data_ready.clear()
            self._queue.clear()
            self._mailbox = _FrameMailbox()
            self._bufs = [None, None, None]
            self._ring_ts = [-1] * _RING
            self._ring_t = [0.0] * _RING
            self._ring_n = 0
            self._origin = self._clock()
            self._camera_ok = True
            self._running = True

            self._threads = [
                threading.Thread(target=self._capture_loop, name="unbound-webcam-capture", daemon=True),
                threading.Thread(target=self._feed_loop, name="unbound-webcam-feed", daemon=True),
            ]
            for th in self._threads:
                th.start()
        log.info("webcam adapter started")
        return self

    def stop(self, timeout: float = 3.0) -> None:
        """Stop threads and release camera + landmarker. Idempotent."""
        with self._lifecycle:
            if not self._running and not self._threads:
                return
            self._running = False
            self._stop.set()
            self._mailbox.close()
            for th in self._threads:
                th.join(timeout)
                if th.is_alive():
                    log.warning("thread %s did not exit within %.1fs", th.name, timeout)
            self._threads = []
            self._release_resources()
            self._camera_ok = False
            self._data_ready.set()  # wake events() generators so they can finish
        log.info("webcam adapter stopped")

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """Block until the first landmarker result arrives (camera + model OK)."""
        return self._first_result.wait(timeout)

    # ---- consuming events ------------------------------------------------- #
    def poll_into(self, out: List[SignalEvent], max_events: int = 0) -> int:
        """Move queued events into ``out`` (0 = all). Never blocks."""
        pop = self._queue.popleft
        n = 0
        while max_events <= 0 or n < max_events:
            try:
                ev = pop()
            except IndexError:
                break
            out.append(ev)
            n += 1
        return n

    def poll(self, max_events: int = 0) -> List[SignalEvent]:
        out: List[SignalEvent] = []
        self.poll_into(out, max_events)
        return out

    def events(self, timeout: float = 0.1) -> Iterator[SignalEvent]:
        """Blocking generator; ends after stop() once the queue is drained."""
        q = self._queue
        while self._running or q:
            try:
                yield q.popleft()
            except IndexError:
                self._data_ready.clear()
                if q:
                    continue
                self._data_ready.wait(timeout)

    def add_listener(self, fn: Callable[[SignalEvent], None]) -> None:
        """Push-style delivery on the MediaPipe thread. Keep ``fn`` fast."""
        with self._listeners_lock:
            self._listeners = self._listeners + (fn,)

    def remove_listener(self, fn: Callable[[SignalEvent], None]) -> None:
        with self._listeners_lock:
            self._listeners = tuple(x for x in self._listeners if x is not fn)

    def stats(self, reset_max: bool = False) -> WebcamStats:
        s = WebcamStats(
            running=self._running,
            camera_ok=self._camera_ok,
            frames_captured=self._frames_captured,
            frames_dropped=self._frames_dropped,
            frames_fed=self._frames_fed,
            results=self._results,
            no_face_results=self._no_face,
            capture_fps=(1.0 / self._dt_ema) if self._dt_ema > 0.0 else 0.0,
            latency_ms_last=self._lat_last,
            latency_ms_avg=self._lat_ema,
            latency_ms_max=self._lat_max,
            reconnects=self._reconnects,
            events_dropped=self._events_dropped,
            listener_errors=self._listener_errors,
            queue_depth=len(self._queue),
        )
        if reset_max:
            self._lat_max = 0.0
        return s

    # ---- setup helpers ---------------------------------------------------- #
    def _load_backends(self) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise WebcamError("opencv-python is required: pip install opencv-python") from exc
        try:
            import mediapipe as mp
        except ImportError as exc:
            raise WebcamError("mediapipe is required: pip install mediapipe") from exc
        self._cv2, self._mp = cv2, mp

    def _create_landmarker(self, model: Path) -> Any:
        mp = self._mp
        vision = mp.tasks.vision
        cfg = self._cfg
        # Load as bytes: avoids MediaPipe's path handling with non-ASCII Windows paths.
        base = mp.tasks.BaseOptions(model_asset_buffer=model.read_bytes())
        options = vision.FaceLandmarkerOptions(
            base_options=base,
            running_mode=vision.RunningMode.LIVE_STREAM,
            num_faces=1,
            min_face_detection_confidence=cfg.min_face_detection_confidence,
            min_face_presence_confidence=cfg.min_face_presence_confidence,
            min_tracking_confidence=cfg.min_tracking_confidence,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
            result_callback=self._on_result,
        )
        try:
            return vision.FaceLandmarker.create_from_options(options)
        except Exception as exc:  # noqa: BLE001
            raise WebcamError(f"could not create FaceLandmarker: {exc}") from exc

    def _open_capture(self) -> Any:
        cv2, cfg = self._cv2, self._cfg
        backend = cv2.CAP_DSHOW if _IS_WINDOWS else cv2.CAP_ANY
        cap = cv2.VideoCapture(cfg.device_index, backend)
        if not cap.isOpened():
            cap.release()
            raise WebcamError(f"could not open camera index {cfg.device_index}")
        if cfg.fourcc:                          # DSHOW wants FOURCC before size/fps
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*cfg.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        cap.set(cv2.CAP_PROP_FPS, cfg.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)     # honored by some backends only
        for prop, value in cfg.extra_props:
            cap.set(prop, value)
        log.info(
            "camera %d opened: %sx%s @ %s fps",
            cfg.device_index,
            cap.get(cv2.CAP_PROP_FRAME_WIDTH),
            cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
            cap.get(cv2.CAP_PROP_FPS),
        )
        return cap

    def _release_resources(self) -> None:
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                log.exception("camera release failed")
        lm, self._landmarker = self._landmarker, None
        if lm is not None:
            try:
                lm.close()
            except Exception:  # noqa: BLE001
                log.exception("landmarker close failed")

    # ---- capture thread --------------------------------------------------- #
    def _capture_loop(self) -> None:
        clock, stop, cfg, mb = self._clock, self._stop, self._cfg, self._mailbox
        cap = self._cap
        fail_since = 0.0          # 0.0 = no failure in progress
        last_t = 0.0
        try:
            while not stop.is_set():
                if cap is None:                      # camera lost: try to reopen
                    if stop.wait(cfg.reopen_interval_s):
                        break
                    try:
                        cap = self._open_capture()
                    except WebcamError as exc:
                        log.debug("reopen failed: %s", exc)
                        continue
                    self._cap = cap
                    self._reconnects += 1
                    self._camera_ok = True
                    fail_since = 0.0
                    last_t = 0.0
                    log.info("camera reconnected")
                    continue

                ok = cap.grab()
                if ok:
                    t = clock()                      # capture instant
                    idx = mb.acquire_write()
                    buf = self._bufs[idx]
                    ok, img = cap.retrieve() if buf is None else cap.retrieve(buf)
                    ok = bool(ok) and img is not None
                if not ok:
                    now = clock()
                    if fail_since == 0.0:
                        fail_since = now
                    elif now - fail_since >= cfg.camera_lost_after_s:
                        log.warning("camera stopped delivering frames; reopening")
                        self._camera_ok = False
                        self._emit(self._event("face_present", 0.0, now))
                        try:
                            cap.release()
                        except Exception:  # noqa: BLE001
                            pass
                        cap = None
                        self._cap = None
                        continue
                    stop.wait(0.002)
                    continue

                fail_since = 0.0
                self._bufs[idx] = img                # same array unless cv2 reallocated
                mb.publish(idx, t)
                self._frames_captured += 1
                if last_t:
                    dt = t - last_t
                    self._dt_ema = dt if self._dt_ema == 0.0 else self._dt_ema + 0.1 * (dt - self._dt_ema)
                last_t = t
        except Exception:  # noqa: BLE001
            log.exception("capture thread crashed")
        finally:
            self._camera_ok = False
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass
            mb.close()

    # ---- feeder thread ---------------------------------------------------- #
    def _feed_loop(self) -> None:
        mb, stop = self._mailbox, self._stop
        cv2, mp = self._cv2, self._mp
        landmarker = self._landmarker
        to_rgb = cv2.COLOR_BGR2RGB
        srgb = mp.ImageFormat.SRGB
        make_image = mp.Image
        rgb_bufs: List[Any] = [None, None, None]   # rotated so an in-flight frame is never overwritten
        k = 0
        last_seq = 0
        last_ts = -1
        origin = self._origin
        errors = 0
        while not stop.is_set():
            if not mb.take(0.1):
                if mb.closed:
                    break
                continue
            idx, t, seq = mb.taken_idx, mb.taken_t, mb.taken_seq
            if seq > last_seq + 1:
                self._frames_dropped += seq - last_seq - 1
            last_seq = seq

            rgb = cv2.cvtColor(self._bufs[idx], to_rgb, rgb_bufs[k])   # dst=None first time
            rgb_bufs[k] = rgb
            k = (k + 1) % 3
            mb.release()                                               # BGR buffer free again

            ts = int((t - origin) * 1000.0)
            if ts <= last_ts:
                ts = last_ts + 1                                       # must be strictly increasing
            last_ts = ts
            n = self._ring_n
            slot = n & (_RING - 1)
            self._ring_t[slot] = t                                     # t first, then ts (lookup key)
            self._ring_ts[slot] = ts
            self._ring_n = n + 1
            try:
                landmarker.detect_async(make_image(image_format=srgb, data=rgb), ts)
                self._frames_fed += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if errors == 1 or errors % 100 == 0:
                    log.warning("detect_async failed (%d so far): %s", errors, exc)

    # ---- MediaPipe result callback ---------------------------------------- #
    def _on_result(self, result: Any, output_image: Any, timestamp_ms: int) -> None:
        try:
            t = self._capture_time(timestamp_ms)
            self._results += 1
            self._first_result.set()
            matrices = result.facial_transformation_matrixes
            if matrices is None or len(matrices) == 0:
                self._no_face += 1
                self._emit(self._event("face_present", 0.0, t))
                self._note_latency(t)
                return

            self._emit(self._event("face_present", 1.0, t))
            yaw, pitch, roll = head_pose_degrees(matrices[0])
            if _isfinite(yaw) and _isfinite(pitch) and _isfinite(roll):
                self._emit(self._event("head_yaw", yaw * self._ysign, t))
                self._emit(self._event("head_pitch", pitch * self._psign, t))
                self._emit(self._event("head_roll", roll * self._rsign, t))

            shapes = result.face_blendshapes
            if shapes is not None and len(shapes) > 0:
                cats = shapes[0]
                if len(cats) != self._plan_len:
                    self._build_plan(cats)
                for name, idxs, inv in self._plan:
                    acc = 0.0
                    for i in idxs:
                        acc += cats[i].score
                    v = acc * inv
                    if v < 0.0:
                        v = 0.0
                    elif v > 1.0:
                        v = 1.0
                    self._emit(self._event(name, v, t))
            self._note_latency(t)
        except Exception:  # noqa: BLE001 - never let a bad frame kill the dispatcher
            self._cb_errors += 1
            if self._cb_errors == 1 or self._cb_errors % 100 == 0:
                log.exception("result callback error (%d so far)", self._cb_errors)

    def _build_plan(self, cats: Any) -> None:
        index = {c.category_name: i for i, c in enumerate(cats)}
        plan: List[Tuple[str, Tuple[int, ...], float]] = []
        for out_name, sources in self._cfg.blendshape_map:
            idxs: List[int] = []
            for src in sources:
                i = index.get(src)
                if i is None:
                    log.warning("blendshape %r not in model output; %s disabled", src, out_name)
                    idxs = []
                    break
                idxs.append(i)
            if idxs:
                plan.append((out_name, tuple(idxs), 1.0 / len(idxs)))
        self._plan = tuple(plan)
        self._plan_len = len(cats)

    def _capture_time(self, ts_ms: int) -> float:
        ts, ring = self._ring_ts, self._ring_t
        for i in range(_RING):
            if ts[i] == ts_ms:
                return ring[i]
        return self._clock()    # unknown timestamp: best available fallback

    def _note_latency(self, t_capture: float) -> None:
        lat = (self._clock() - t_capture) * 1000.0
        self._lat_last = lat
        self._lat_ema = lat if self._lat_ema == 0.0 else self._lat_ema + 0.1 * (lat - self._lat_ema)
        if lat > self._lat_max:
            self._lat_max = lat

    # ---- emission ---------------------------------------------------------- #
    @staticmethod
    def _event(name: str, value: float, t: float) -> SignalEvent:
        return SignalEvent(src="webcam", name=name, value=value, t=t, conf=1.0)

    def _emit(self, ev: SignalEvent) -> None:
        q = self._queue
        if len(q) >= self._cfg.queue_size:
            self._events_dropped += 1       # deque drops the oldest on append
        q.append(ev)
        self._data_ready.set()
        for fn in self._listeners:          # tuple snapshot: add/remove are copy-on-write
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - a bad listener must not stop tracking
                self._listener_errors += 1
                if self._listener_errors == 1 or self._listener_errors % 100 == 0:
                    log.exception("listener error (%d so far)", self._listener_errors)


# --------------------------------------------------------------------------- #
# Live demo:  python -m handsfree.adapters.webcam [--camera 0] [--seconds 30]
# --------------------------------------------------------------------------- #
def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Print live webcam signals (sign/latency check).")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--model", default=None, help="path to face_landmarker.task")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--print-hz", type=float, default=5.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = WebcamConfig(device_index=args.camera, width=args.width, height=args.height,
                       fps=args.fps, model_path=args.model)
    print("Turn your head RIGHT: yaw should go positive. Look UP: pitch positive. "
          "Tilt toward your RIGHT shoulder: roll positive. Ctrl+C to quit.")
    latest: dict[str, float] = {}
    buf: List[SignalEvent] = []
    try:
        with WebcamAdapter(cfg) as cam:
            end = time.monotonic() + args.seconds
            next_print = 0.0
            while time.monotonic() < end:
                cam.poll_into(buf)
                for ev in buf:
                    latest[ev.name] = ev.value
                buf.clear()
                now = time.monotonic()
                if now >= next_print:
                    next_print = now + 1.0 / args.print_hz
                    s = cam.stats()
                    g = latest.get
                    print(
                        f"face={g('face_present', 0):.0f} "
                        f"yaw={g('head_yaw', 0):+6.1f} pitch={g('head_pitch', 0):+6.1f} "
                        f"roll={g('head_roll', 0):+6.1f} | jaw={g('jaw_open', 0):.2f} "
                        f"brow={g('brow_raise', 0):.2f} blinkL={g('eye_blink_left', 0):.2f} "
                        f"blinkR={g('eye_blink_right', 0):.2f} | cam={s.capture_fps:4.1f}fps "
                        f"lat={s.latency_ms_avg:4.1f}ms (max {s.latency_ms_max:4.1f}) "
                        f"dropped={s.frames_dropped}"
                    )
                time.sleep(1.0 / 60.0)
    except KeyboardInterrupt:
        pass
    except (WebcamError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
