"""Tests for handsfree/adapters/webcam.py using fake cv2 / mediapipe backends.

No camera, model file or GPU needed. unittest-style so both ``pytest`` and
``python -m unittest`` collect them. Timing-based tests use generous bounds.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.events import SignalEvent
from handsfree.adapters import webcam as wc


# --------------------------------------------------------------------------- #
# Math helpers: build matrices from known angles
# --------------------------------------------------------------------------- #
def _mm(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def matrix4(yaw, pitch_up, roll, scale=1.0):
    """4x4 (nested lists) = scale * Ry(yaw) Rx(-pitch_up) Rz(roll), translation (1,2,3)."""
    y, x, z = math.radians(yaw), math.radians(-pitch_up), math.radians(roll)
    cy, sy, cx, sx, cz, sz = math.cos(y), math.sin(y), math.cos(x), math.sin(x), math.cos(z), math.sin(z)
    ry = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    rx = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    rz = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]
    r = _mm(_mm(ry, rx), rz)
    m = [[scale * r[i][j] for j in range(3)] + [float(i + 1)] for i in range(3)]
    m.append([0.0, 0.0, 0.0, 1.0])
    return m


_NAMES = ["_neutral", "browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft",
          "browOuterUpRight", "eyeBlinkLeft", "eyeBlinkRight", "jawOpen",
          "mouthSmileLeft", "mouthSmileRight", "noseSneerLeft"]


def make_result(tag=7, matrix=None, with_face=True):
    if not with_face:
        return SimpleNamespace(facial_transformation_matrixes=[], face_blendshapes=[],
                               face_landmarks=[])
    scores = {"browInnerUp": 0.6, "browOuterUpLeft": 0.3, "browOuterUpRight": 0.3,
              "eyeBlinkLeft": 0.9, "eyeBlinkRight": 0.1, "jawOpen": (tag % 1000) / 1000.0,
              "mouthSmileLeft": 0.2, "mouthSmileRight": 0.4}
    cats = [SimpleNamespace(category_name=n, score=scores.get(n, 0.0)) for n in _NAMES]
    return SimpleNamespace(
        facial_transformation_matrixes=[matrix if matrix is not None else matrix4(10, 5, -3, 1.4)],
        face_blendshapes=[cats], face_landmarks=[])


# --------------------------------------------------------------------------- #
# Fake cv2 + mediapipe
# --------------------------------------------------------------------------- #
class FakeFrame:
    shape = (480, 640, 3)

    def __init__(self, tag):
        self.tag = tag


class FakeCapture:
    def __init__(self, env, script):
        self.env, self.script = env, script
        self.n = 0
        self.current = 0
        self.released = False

    def isOpened(self):
        return not self.script.get("open_fail", False)

    def set(self, prop, value):
        self.env.props.append((prop, value))
        return True

    def get(self, prop):
        return 0.0

    def grab(self):
        if self.released:
            return False
        if self.n >= self.script.get("frames", 10 ** 9):
            time.sleep(0.002)
            return False
        time.sleep(self.script.get("period", 0.004))
        tag = self.env.next_tag
        self.env.next_tag += 1
        self.current = tag
        self.n += 1
        self.env.grab_times[tag] = time.monotonic()   # just before grab() returns
        return True

    def retrieve(self, buf=None):
        return True, FakeFrame(self.current)

    def release(self):
        self.released = True


class FakeLandmarker:
    """Drops frames while busy, like MediaPipe's LIVE_STREAM flow limiter."""

    def __init__(self, env, options):
        self.env, self.options = env, options
        self.busy = False
        self.lock = threading.Lock()
        self.closed = False
        self.timestamps = []
        self.dropped = 0

    def detect_async(self, image, ts):
        if self.closed:
            raise RuntimeError("landmarker closed")
        if self.timestamps and ts <= self.timestamps[-1]:
            raise ValueError("timestamp not strictly increasing")
        self.timestamps.append(ts)
        with self.lock:
            if self.busy:
                self.dropped += 1
                return
            self.busy = True
        threading.Thread(target=self._run, args=(image.tag, ts), daemon=True).start()

    def _run(self, tag, ts):
        try:
            time.sleep(self.env.landmarker_delay)
            if not self.closed:
                self.options.result_callback(make_result(tag), None, ts)
        finally:
            self.busy = False

    def close(self):
        self.closed = True


class Env:
    def __init__(self):
        self.scripts = [{}]
        self.opened = []
        self.next_tag = 1
        self.grab_times = {}
        self.props = []
        self.backends = []
        self.cvt_delay = 0.0
        self.landmarker_delay = 0.0
        self.landmarkers = []


def make_modules(env):
    cv2 = types.ModuleType("cv2")
    cv2.CAP_DSHOW, cv2.CAP_ANY = 700, 0
    cv2.CAP_PROP_FOURCC, cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT = 6, 3, 4
    cv2.CAP_PROP_FPS, cv2.CAP_PROP_BUFFERSIZE, cv2.COLOR_BGR2RGB = 5, 38, 4
    cv2.VideoWriter_fourcc = lambda *a: 0

    def video_capture(index, backend):
        env.backends.append(backend)
        script = env.scripts[min(len(env.opened), len(env.scripts) - 1)]
        cap = FakeCapture(env, script)
        env.opened.append(cap)
        return cap

    def cvt_color(src, code, dst=None):
        if env.cvt_delay:
            time.sleep(env.cvt_delay)
        return FakeFrame(src.tag)

    cv2.VideoCapture, cv2.cvtColor = video_capture, cvt_color

    mp = types.ModuleType("mediapipe")

    class FakeImage:
        def __init__(self, image_format, data):
            self.tag = data.tag

    class Options:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class FaceLandmarker:
        @staticmethod
        def create_from_options(options):
            lm = FakeLandmarker(env, options)
            env.landmarkers.append(lm)
            return lm

    mp.Image = FakeImage
    mp.ImageFormat = SimpleNamespace(SRGB=1)
    mp.tasks = SimpleNamespace(
        BaseOptions=Options,
        vision=SimpleNamespace(FaceLandmarker=FaceLandmarker, FaceLandmarkerOptions=Options,
                               RunningMode=SimpleNamespace(LIVE_STREAM="LIVE_STREAM")))
    return cv2, mp


class _Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        cv2, mp = make_modules(self.env)
        patcher = mock.patch.dict(sys.modules, {"cv2": cv2, "mediapipe": mp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.model = Path(self.tmp.name) / "face_landmarker.task"
        self.model.write_bytes(b"fake-model")
        self.cams = []

    def cam(self, **cfg):
        cfg.setdefault("model_path", self.model)
        cam = wc.WebcamAdapter(wc.WebcamConfig(**cfg), clock=time.monotonic)
        self.cams.append(cam)
        self.addCleanup(cam.stop)
        return cam

    def wait_until(self, pred, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.005)
        return pred()


# --------------------------------------------------------------------------- #
# Pure functions
# --------------------------------------------------------------------------- #
class TestHeadPose(unittest.TestCase):
    def test_roundtrip_nested_lists_and_scale(self):
        for yaw in (-40, 0, 25):
            for pitch in (-30, 0, 20):
                for roll in (-35, 0, 15):
                    got = wc.head_pose_degrees(matrix4(yaw, pitch, roll, scale=1.7))
                    for g, want in zip(got, (yaw, pitch, roll)):
                        self.assertAlmostEqual(g, want, places=6)

    def test_numpy_matrix_with_item(self):
        np = __import__("numpy")
        m = np.array(matrix4(-20, 10, 5, scale=1.2), dtype=np.float32)
        got = wc.head_pose_degrees(m)
        for g, want in zip(got, (-20, 10, 5)):
            self.assertAlmostEqual(g, want, places=3)

    def test_identity_is_zero(self):
        self.assertEqual(wc.head_pose_degrees(matrix4(0, 0, 0)), (0.0, 0.0, 0.0))


class TestConfig(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(ValueError):
            wc.WebcamConfig(yaw_sign=2)
        with self.assertRaises(ValueError):
            wc.WebcamConfig(width=0)
        with self.assertRaises(ValueError):
            wc.WebcamConfig(queue_size=0)

    def test_resolve_model_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.task"
            p.write_bytes(b"x")
            self.assertEqual(wc.resolve_model_path(p), p)
        with mock.patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(FileNotFoundError) as cm:
                wc.resolve_model_path("nope.task")
            self.assertIn("storage.googleapis.com", str(cm.exception))


# --------------------------------------------------------------------------- #
# Result callback (no threads)
# --------------------------------------------------------------------------- #
class TestOnResult(unittest.TestCase):
    def make(self, **cfg):
        return wc.WebcamAdapter(wc.WebcamConfig(**cfg), clock=lambda: 5.0)

    def test_events_values_and_signs(self):
        cam = self.make()
        cam._on_result(make_result(tag=7), None, 123)
        evs = {e.name: e for e in cam.poll()}
        self.assertEqual(set(evs), {"face_present", "head_yaw", "head_pitch", "head_roll", "jaw_open",
                                    "brow_raise", "eye_blink_left", "eye_blink_right", "mouth_smile"})
        self.assertEqual(evs["face_present"].value, 1.0)
        self.assertAlmostEqual(evs["head_yaw"].value, -10.0, places=5)   # default yaw_sign=-1
        self.assertAlmostEqual(evs["head_pitch"].value, 5.0, places=5)
        self.assertAlmostEqual(evs["head_roll"].value, -3.0, places=5)
        self.assertAlmostEqual(evs["jaw_open"].value, 0.007, places=6)
        self.assertAlmostEqual(evs["brow_raise"].value, 0.4, places=6)    # mean of three
        self.assertAlmostEqual(evs["eye_blink_left"].value, 0.9, places=6)
        self.assertAlmostEqual(evs["mouth_smile"].value, 0.3, places=6)
        for e in evs.values():
            self.assertIsInstance(e, SignalEvent)
            self.assertEqual((e.src, e.conf), ("webcam", 1.0))

    def test_sign_flip(self):
        cam = self.make(yaw_sign=1, pitch_sign=-1, roll_sign=-1)
        cam._on_result(make_result(), None, 1)
        evs = {e.name: e.value for e in cam.poll()}
        self.assertAlmostEqual(evs["head_yaw"], 10.0, places=5)
        self.assertAlmostEqual(evs["head_pitch"], -5.0, places=5)
        self.assertAlmostEqual(evs["head_roll"], 3.0, places=5)

    def test_no_face(self):
        cam = self.make()
        cam._on_result(make_result(with_face=False), None, 1)
        evs = cam.poll()
        self.assertEqual([(e.name, e.value) for e in evs], [("face_present", 0.0)])
        self.assertEqual(cam.stats().no_face_results, 1)

    def test_queue_overflow_is_bounded(self):
        cam = self.make(queue_size=10)
        for i in range(5):
            cam._on_result(make_result(), None, i)
        s = cam.stats()
        self.assertLessEqual(s.queue_depth, 10)
        self.assertGreater(s.events_dropped, 0)

    def test_listener_errors_isolated(self):
        cam = self.make()
        got = []

        def bad(ev):
            raise RuntimeError("boom")

        cam.add_listener(bad)
        cam.add_listener(got.append)
        cam._on_result(make_result(), None, 1)
        self.assertEqual(len(got), 9)
        self.assertEqual(len(cam.poll()), 9)
        self.assertEqual(cam.stats().listener_errors, 9)
        cam.remove_listener(bad)
        cam._on_result(make_result(), None, 2)
        self.assertEqual(cam.stats().listener_errors, 9)

    def test_missing_blendshape_disables_only_that_output(self):
        cam = self.make(blendshape_map=(("jaw_open", ("jawOpen",)), ("ghost", ("doesNotExist",))))
        cam._on_result(make_result(), None, 1)
        names = {e.name for e in cam.poll()}
        self.assertIn("jaw_open", names)
        self.assertNotIn("ghost", names)

    def test_malformed_matrix_does_not_raise(self):
        cam = self.make()
        bad = make_result(matrix=[[float("nan")] * 4] * 4)
        cam._on_result(bad, None, 1)
        names = {e.name for e in cam.poll()}
        self.assertNotIn("head_yaw", names)       # non-finite pose skipped
        self.assertIn("jaw_open", names)          # blendshapes still delivered


# --------------------------------------------------------------------------- #
# Threaded pipeline with fakes
# --------------------------------------------------------------------------- #
class TestPipeline(_Base):
    def test_lifecycle_and_context_manager(self):
        with self.cam() as cam:
            self.assertTrue(cam.is_running)
            self.assertTrue(cam.wait_ready(3.0))
            with self.assertRaises(RuntimeError):
                cam.start()
            lm = self.env.landmarkers[0]
            self.assertEqual(lm.options.running_mode, "LIVE_STREAM")
            self.assertTrue(lm.options.output_face_blendshapes)
            self.assertTrue(lm.options.output_facial_transformation_matrixes)
        self.assertFalse(cam.is_running)
        self.assertTrue(self.env.opened[0].released)
        self.assertTrue(lm.closed)
        names = {t.name for t in threading.enumerate()}
        self.assertFalse({"unbound-webcam-capture", "unbound-webcam-feed"} & names)
        cam.stop()   # idempotent

    def test_backend_selection(self):
        with mock.patch.object(wc, "_IS_WINDOWS", True):
            with self.cam():
                pass
        self.assertEqual(self.env.backends[-1], 700)   # CAP_DSHOW
        with mock.patch.object(wc, "_IS_WINDOWS", False):
            with self.cam():
                pass
        self.assertEqual(self.env.backends[-1], 0)     # CAP_ANY

    def test_capture_time_survives_async_inference(self):
        self.env.landmarker_delay = 0.010
        cam = self.cam()
        seen = []
        cam.add_listener(seen.append)
        with cam:
            self.assertTrue(self.wait_until(lambda: len(seen) > 150))
        checked = 0
        by_t = {}
        for ev in list(seen):
            by_t.setdefault(ev.t, []).append(ev)
            if ev.name == "jaw_open":
                tag = round(ev.value * 1000)
                lag = ev.t - self.env.grab_times[tag]
                self.assertGreaterEqual(lag, 0.0)
                self.assertLess(lag, 0.03, "t must be grab time, not inference time")
                checked += 1
        self.assertGreater(checked, 10)
        for group in by_t.values():              # one t per frame, shared by its events
            self.assertEqual(len({e.name for e in group}), len(group))
        ts = self.env.landmarkers[0].timestamps
        self.assertTrue(all(b > a for a, b in zip(ts, ts[1:])), "detect_async ts strictly increasing")
        self.assertEqual(cam.stats().listener_errors, 0)

    def test_depth_one_buffer_drops_instead_of_queueing(self):
        self.env.scripts = [{"period": 0.003}]
        self.env.cvt_delay = 0.03                # slow consumer, fast camera
        cam = self.cam()
        with cam:
            self.assertTrue(self.wait_until(lambda: cam.stats().results >= 8, timeout=5.0))
            time.sleep(0.3)
            s = cam.stats()
        self.assertGreater(s.frames_dropped, 20)
        self.assertGreater(s.frames_captured, s.frames_fed)
        self.assertLess(s.latency_ms_max, 200.0, "latency must not grow with backlog")

    def test_camera_loss_and_reconnect(self):
        self.env.scripts = [{"frames": 5, "period": 0.003}, {"period": 0.003}]
        cam = self.cam(camera_lost_after_s=0.05, reopen_interval_s=0.02)
        seen = []
        cam.add_listener(seen.append)
        with cam:
            self.assertTrue(self.wait_until(lambda: cam.stats().reconnects == 1, timeout=4.0))
            self.assertTrue(self.wait_until(lambda: cam.stats().camera_ok))
            n = len(seen)
            self.assertTrue(self.wait_until(lambda: len(seen) > n + 20))
        self.assertEqual(len(self.env.opened), 2)
        self.assertTrue(self.env.opened[0].released)
        evs = list(seen)
        lost = next(i for i, e in enumerate(evs) if e.name == "face_present" and e.value == 0.0)
        self.assertTrue(any(e.name == "face_present" and e.value == 1.0 for e in evs[lost:]))

    def test_open_failure_raises_and_cleans_up(self):
        self.env.scripts = [{"open_fail": True}]
        cam = self.cam()
        with self.assertRaises(wc.WebcamError):
            cam.start()
        self.assertFalse(cam.is_running)
        self.assertTrue(self.env.landmarkers[0].closed)

    def test_events_generator_finishes_after_stop(self):
        cam = self.cam()
        cam.start()
        gen = cam.events(timeout=0.02)
        first = [next(gen) for _ in range(3)]
        self.assertEqual(len(first), 3)
        cam.stop()
        self.assertIsInstance(list(gen), list)   # terminates

    def test_poll_never_blocks_and_respects_max(self):
        cam = self.cam()
        self.assertEqual(cam.poll(), [])
        cam._on_result(make_result(), None, 1)
        self.assertEqual(len(cam.poll(max_events=4)), 4)
        buf = []
        self.assertEqual(cam.poll_into(buf), 5)
        self.assertEqual(len(buf), 5)


if __name__ == "__main__":
    unittest.main()
