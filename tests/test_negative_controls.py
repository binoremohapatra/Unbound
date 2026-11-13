"""test_negative_controls.py — Verifies R01: Negative controls must produce face_found=False.

Feeds:
  1. Pure black frame (zeros)
  2. Pure white frame (255s)
  3. Uniform random noise
  4. Linear intensity gradient
  5. Geometric shapes with high contrast but no face (e.g. circles and lines)

Asserts:
  - result.face_found is False
  - result.tier == "NO_FACE"
  - result.nose_tip is None
  - result.landmarks is None
  - result.ear is None
  - result.mar is None
"""
from __future__ import annotations

import numpy as np
import pytest

from host.omniband.tracking.webcam_tracker import RealWebcamTracker, TrackerResult


@pytest.fixture(scope="module")
def tracker_instance():
    tracker = RealWebcamTracker(camera_id=999)
    det_video, det_image, yunet = tracker._init_models()
    return tracker, det_video, det_image, yunet


def _run_frame(tracker_fixture, frame_bgr: np.ndarray, timestamp_ms: int) -> TrackerResult:
    tracker, det_video, det_image, yunet = tracker_fixture
    import cv2
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    h, w = frame_bgr.shape[:2]
    return tracker._execute_pipeline(
        frame_rgb, frame_bgr, w, h, timestamp_ms, det_video, det_image, yunet
    )


def test_negative_control_pure_black(tracker_instance):
    """Pure black frame must yield NO_FACE and face_found=False."""
    black = np.zeros((480, 640, 3), dtype=np.uint8)
    res = _run_frame(tracker_instance, black, 1000)
    assert not res.face_found, "Pure black frame must not report a face"
    assert res.tier == "NO_FACE"
    assert res.nose_tip is None
    assert res.landmarks is None
    assert res.ear is None
    assert res.mar is None


def test_negative_control_pure_white(tracker_instance):
    """Pure white frame must yield NO_FACE and face_found=False."""
    white = np.full((480, 640, 3), 255, dtype=np.uint8)
    res = _run_frame(tracker_instance, white, 2000)
    assert not res.face_found, "Pure white frame must not report a face"
    assert res.tier == "NO_FACE"
    assert res.nose_tip is None


def test_negative_control_random_noise(tracker_instance):
    """Uniform random noise must yield NO_FACE and face_found=False."""
    rng = np.random.default_rng(42)
    noise = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
    res = _run_frame(tracker_instance, noise, 3000)
    assert not res.face_found, "Random noise must not report a face"
    assert res.tier == "NO_FACE"
    assert res.nose_tip is None


def test_negative_control_gradient(tracker_instance):
    """Linear horizontal/vertical gradients must yield NO_FACE."""
    grad_x = np.tile(np.linspace(0, 255, 640, dtype=np.uint8), (480, 1))
    grad = np.stack([grad_x, grad_x, grad_x], axis=-1)
    res = _run_frame(tracker_instance, grad, 4000)
    assert not res.face_found, "Linear gradient must not report a face"
    assert res.tier == "NO_FACE"
    assert res.nose_tip is None


def test_negative_control_geometric_shapes(tracker_instance):
    """Synthetic geometric circles/rectangles (non-face) must yield NO_FACE."""
    import cv2
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (200, 200), 80, (255, 255, 255), -1)
    cv2.circle(img, (440, 200), 80, (255, 255, 255), -1)
    cv2.rectangle(img, (250, 320), (390, 360), (200, 200, 200), -1)
    res = _run_frame(tracker_instance, img, 5000)
    assert not res.face_found, "Geometric non-face shapes must not report a face"
    assert res.tier == "NO_FACE"

