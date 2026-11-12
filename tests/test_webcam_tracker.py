"""test_webcam_tracker.py - Verification tests for GameFace architecture, 1-Euro filter & biometric gestures."""
from __future__ import annotations

import math
import numpy as np
import pytest

from host.omniband.tracking.webcam_tracker import (
    OneEuroFilter,
    RealWebcamTracker,
    calculate_ear,
    calculate_mar,
)


def test_one_euro_filter_initialization_and_filtering():
    filt = OneEuroFilter(t0=0.0, x0=100.0, min_cutoff=0.8, beta=0.04, d_cutoff=1.0)
    assert filt.min_cutoff == 0.8
    assert filt.beta == 0.04

    # First filter call
    x0 = filt.filter(0.0, 100.0)
    assert x0 == 100.0

    # Low-speed jitter input: should be heavily smoothed
    vals = []
    t = 0.0
    for i in range(20):
        t += 0.01
        noisy_val = 100.0 + (1.0 if i % 2 == 0 else -1.0)
        smoothed = filt.filter(t, noisy_val)
        vals.append(smoothed)

    # Standard deviation of smoothed output should be much smaller than input jitter (1.0)
    std_out = np.std(vals)
    assert std_out < 0.5, f"Expected low-speed jitter to be damped, got std={std_out}"


def test_one_euro_filter_high_speed_responsiveness():
    filt = OneEuroFilter(t0=0.0, x0=0.0, min_cutoff=0.8, beta=0.04, d_cutoff=1.0)
    filt.filter(0.0, 0.0)

    # Fast jump: large derivative should increase cutoff and follow quickly
    t = 0.05
    jump_val = 500.0
    out = filt.filter(t, jump_val)
    # With high speed, output should respond significantly
    assert out > 100.0, f"Expected high-speed responsive tracking, got {out}"


def test_calculate_ear():
    # Eyes open: vertical distance ~ 10, horizontal distance ~ 30 -> EAR ~ 0.33
    open_eye = [
        (0.0, 10.0),    # p1 (left corner)
        (10.0, 0.0),    # p2 (top left)
        (20.0, 0.0),    # p3 (top right)
        (30.0, 10.0),   # p4 (right corner)
        (20.0, 20.0),   # p5 (bottom right)
        (10.0, 20.0),   # p6 (bottom left)
    ]
    ear_open = calculate_ear(open_eye)
    assert ear_open > 0.25

    # Eyes closed: vertical distance ~ 2, horizontal distance ~ 30 -> EAR ~ 0.06
    closed_eye = [
        (0.0, 10.0),
        (10.0, 9.0),
        (20.0, 9.0),
        (30.0, 10.0),
        (20.0, 11.0),
        (10.0, 11.0),
    ]
    ear_closed = calculate_ear(closed_eye)
    assert ear_closed < 0.15


def test_calculate_mar():
    # Closed lips: vertical ~ 2, horizontal ~ 40 -> MAR ~ 0.05
    closed_mouth = [
        (20.0, 19.0),   # p13
        (20.0, 21.0),   # p14
        (0.0, 20.0),    # p78
        (40.0, 20.0),   # p308
    ]
    mar_closed = calculate_mar(closed_mouth)
    assert mar_closed < 0.15

    # Open jaw: vertical ~ 25, horizontal ~ 40 -> MAR ~ 0.625
    open_mouth = [
        (20.0, 5.0),
        (20.0, 30.0),
        (0.0, 20.0),
        (40.0, 20.0),
    ]
    mar_open = calculate_mar(open_mouth)
    assert mar_open > 0.40


def test_webcam_tracker_properties_and_methods():
    tracker = RealWebcamTracker(camera_id=999)  # Non-existent cam, just testing methods
    assert tracker.head_mouse_enabled is True
    assert tracker.speed_multiplier == 1.2
    assert tracker.min_cutoff == 0.8

    tracker.set_pointer_speed(2.5)
    assert tracker.speed_multiplier == 2.5

    tracker.set_smoothing_cutoff(1.5)
    assert tracker.min_cutoff == 1.5
    assert tracker.filter_x.min_cutoff == 1.5

    tracker.trigger_blink_freeze(0.35)
    assert tracker.freeze_until > 0.0

    tracker.recenter()
    assert tracker._calibrated is False


def test_geometric_face_mesh_renderer():
    tracker = RealWebcamTracker(camera_id=999)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    tracker._draw_geometric_face_mesh(frame, 320, 240, 640, 480)
    # The wireframe lines must have colored non-zero pixels
    assert np.count_nonzero(frame) > 100
