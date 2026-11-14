"""test_webcam_tracker.py — Verifies RealWebcamTracker: no fake overlays, real EAR/MAR, 1-Euro filter."""
from __future__ import annotations

import math
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from host.omniband.tracking.webcam_tracker import (
    OneEuroFilter,
    RealWebcamTracker,
    calculate_ear,
    calculate_mar,
)


# ---------------------------------------------------------------------------
# 1-Euro Filter
# ---------------------------------------------------------------------------
def test_one_euro_filter_initialization_and_filtering():
    filt = OneEuroFilter(t0=0.0, x0=100.0, min_cutoff=0.6, beta=0.03, d_cutoff=1.0)
    assert filt.min_cutoff == 0.6
    assert filt.beta == 0.03

    x0 = filt.filter(0.0, 100.0)
    assert x0 == 100.0

    vals = []
    t = 0.0
    for i in range(20):
        t += 0.01
        noisy_val = 100.0 + (1.0 if i % 2 == 0 else -1.0)
        vals.append(filt.filter(t, noisy_val))

    std_out = np.std(vals)
    assert std_out < 0.5, f"Expected low-speed jitter damped, got std={std_out}"


def test_one_euro_filter_high_speed_responsiveness():
    filt = OneEuroFilter(t0=0.0, x0=0.0, min_cutoff=0.6, beta=0.03, d_cutoff=1.0)
    filt.filter(0.0, 0.0)
    out = filt.filter(0.05, 500.0)
    assert out > 100.0, f"Expected high-speed responsive tracking, got {out}"


def test_one_euro_filter_reset():
    filt = OneEuroFilter()
    filt.filter(0.0, 50.0)
    filt.reset(1.0, 0.0)
    assert not filt._initialized


# ---------------------------------------------------------------------------
# EAR / MAR helpers — only real landmark coords, no hardcoded returns
# ---------------------------------------------------------------------------
def test_calculate_ear_open_eye():
    open_eye = [
        (0.0, 10.0), (10.0, 0.0), (20.0, 0.0),
        (30.0, 10.0), (20.0, 20.0), (10.0, 20.0),
    ]
    assert calculate_ear(open_eye) > 0.25


def test_calculate_ear_closed_eye():
    closed_eye = [
        (0.0, 10.0), (10.0, 9.0), (20.0, 9.0),
        (30.0, 10.0), (20.0, 11.0), (10.0, 11.0),
    ]
    assert calculate_ear(closed_eye) < 0.15


def test_calculate_ear_too_few_points():
    # With fewer than 6 points, returns 0.0 (not a fake hardcoded value)
    result = calculate_ear([(0, 0), (1, 1)])
    assert result == 0.0, f"Expected 0.0 for insufficient points, got {result}"


def test_calculate_mar_closed():
    closed = [(20.0, 19.0), (20.0, 21.0), (0.0, 20.0), (40.0, 20.0)]
    assert calculate_mar(closed) < 0.15


def test_calculate_mar_open():
    open_m = [(20.0, 5.0), (20.0, 30.0), (0.0, 20.0), (40.0, 20.0)]
    assert calculate_mar(open_m) > 0.40


def test_calculate_mar_too_few_points():
    # With fewer than 4 points, returns 0.0 (not a fake hardcoded value)
    result = calculate_mar([(0, 0)])
    assert result == 0.0, f"Expected 0.0 for insufficient points, got {result}"


# ---------------------------------------------------------------------------
# No fake geometric fallback in source code
# ---------------------------------------------------------------------------
def test_no_fake_geometric_spiderweb_in_source():
    """CRITICAL: webcam_tracker.py must contain zero fake radial-fan / pentagon drawing code."""
    src = Path(__file__).parent.parent / "host" / "omniband" / "tracking" / "webcam_tracker.py"
    text = src.read_text(encoding="utf-8")

    forbidden = [
        "_draw_geometric_face_mesh",   # the deleted fake function name
        "radial fan",
        "pentagon",
        "spiderweb",
    ]
    for token in forbidden:
        assert token not in text, (
            f"Found forbidden fake-geometry token '{token}' in webcam_tracker.py — DELETE IT"
        )


def test_no_hardcoded_ear_mar_values_in_source():
    """EAR and MAR must never be hardcoded as 0.30 / 0.10 defaults."""
    src = Path(__file__).parent.parent / "host" / "omniband" / "tracking" / "webcam_tracker.py"
    text = src.read_text(encoding="utf-8")

    # These were the two fake default assignments that caused the bug
    assert "ear = 0.30" not in text, "Hardcoded 'ear = 0.30' found — REMOVE it"
    assert "mar = 0.10" not in text, "Hardcoded 'mar = 0.10' found — REMOVE it"
    assert "return 0.30" not in text, "Hardcoded 'return 0.30' in EAR fallback — REMOVE it"
    assert "return 0.10" not in text, "Hardcoded 'return 0.10' in MAR fallback — REMOVE it"


def test_hud_shows_dashes_when_no_face():
    """HUD code must contain the alignment warning string, not fake numbers."""
    src = Path(__file__).parent.parent / "host" / "omniband" / "tracking" / "webcam_tracker.py"
    text = src.read_text(encoding="utf-8")
    assert "ALIGN FULL FACE IN FRAME" in text, \
        "Missing alignment guide text — user won't know to adjust camera"
    assert "EAR: --" in text, "Missing 'EAR: --' for no-face state"
    assert "MAR: --" in text, "Missing 'MAR: --' for no-face state"


# ---------------------------------------------------------------------------
# RealWebcamTracker properties & control (no camera needed)
# ---------------------------------------------------------------------------
def test_webcam_tracker_initial_state():
    tracker = RealWebcamTracker(camera_id=999)
    assert tracker.head_mouse_enabled is True
    assert tracker.speed_multiplier in (0.6, 0.8, 1.2)
    assert tracker.min_cutoff in (0.6, 0.8)
    assert tracker.gain_x in (4.5, 5.5, 10.0)
    assert tracker.gain_y in (5.0, 6.0, 12.0)

    vx, vy, in_deadzone = tracker.velocity_state
    assert in_deadzone is True
    assert vx == 0.0 and vy == 0.0

    # Telemetry starts as None — not faked
    assert tracker.current_ear is None, "EAR must be None until a real face is detected"
    assert tracker.current_mar is None, "MAR must be None until a real face is detected"


def test_webcam_tracker_set_pointer_speed():
    tracker = RealWebcamTracker(camera_id=999)
    tracker.set_pointer_speed(2.5)
    assert tracker.speed_multiplier == 2.5
    tracker.set_pointer_speed(-10)
    assert tracker.speed_multiplier == 0.2   # clamped
    tracker.set_pointer_speed(999)
    assert tracker.speed_multiplier == 5.0   # clamped


def test_webcam_tracker_set_smoothing_cutoff():
    tracker = RealWebcamTracker(camera_id=999)
    tracker.set_smoothing_cutoff(1.5)
    assert tracker.min_cutoff == 1.5
    assert tracker.filter_dx.min_cutoff == 1.5
    assert tracker.filter_dy.min_cutoff == 1.5


def test_webcam_tracker_blink_freeze():
    tracker = RealWebcamTracker(camera_id=999)
    tracker.trigger_blink_freeze(0.35)
    assert tracker.freeze_until > time.perf_counter() - 0.1


def test_webcam_tracker_recenter():
    tracker = RealWebcamTracker(camera_id=999)
    tracker._calibrated = True
    tracker._face_first_seen = True
    tracker.recenter()
    assert tracker._calibrated is False
    assert tracker._face_first_seen is False


def test_webcam_tracker_deadzone_is_tight():
    """Deadzone must be ≤ 0.015 (not the old 0.035) for responsive cursor."""
    tracker = RealWebcamTracker(camera_id=999)
    assert tracker.DEADZONE <= 0.015, (
        f"DEADZONE is {tracker.DEADZONE} — must be ≤ 0.015 for responsive head-mouse"
    )


# ---------------------------------------------------------------------------
# Drawing helpers — real overlay methods exist and paint pixels
# ---------------------------------------------------------------------------
def test_draw_hud_no_face_shows_alignment_guide():
    """When face_found=False, _draw_hud must paint orange alignment guide pixels."""
    tracker = RealWebcamTracker(camera_id=999)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    tracker._draw_hud(frame, 640, 480, 0.5, 0.45, None,
                      True, 0.0, 0.0, face_found=False)
    # Should have orange pixels (R>200, G~120, B~66) in the guide frame
    r, g, b = frame[:, :, 0], frame[:, :, 1], frame[:, :, 2]
    orange_mask = (r > 200) & (g > 80) & (g < 160) & (b < 100)
    assert np.sum(orange_mask) > 100, \
        "Alignment guide frame must contain orange guide pixels when no face is found"


def test_draw_hud_face_found_shows_deadzone_ring():
    """When face_found=True, _draw_hud must paint the deadzone ring."""
    tracker = RealWebcamTracker(camera_id=999)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    tracker._draw_hud(frame, 640, 480, 0.5, 0.45, (320, 240),
                      True, 0.0, 0.0, face_found=True)
    assert np.count_nonzero(frame) > 50


def test_draw_facemesh_wireframe_paints_pixels():
    """_draw_facemesh_wireframe must paint real teal/green/copper pixels on the face."""
    from collections import namedtuple
    Point = namedtuple("Point", ["x", "y"])
    tracker = RealWebcamTracker(camera_id=999)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    mock_lm = [Point(x=0.4 + 0.2 * (i / 468.0),
                     y=0.3 + 0.3 * ((i * 7 % 468) / 468.0))
               for i in range(468)]
    tracker._draw_facemesh_wireframe(frame, mock_lm, 640, 480)
    assert np.count_nonzero(frame) > 100


def test_draw_cascade_overlay_paints_pixels():
    """_draw_cascade_overlay must paint the face box + eye + nose + mouth markers."""
    tracker = RealWebcamTracker(camera_id=999)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    tracker._draw_cascade_overlay(frame, 100, 80, 200, 250, 200, 140, 200, 175, 640, 480)
    assert np.count_nonzero(frame) > 50


def test_draw_deadzone_ring_and_hud():
    """Legacy compatibility: draw_hud with face_found=True paints pixels."""
    tracker = RealWebcamTracker(camera_id=999)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    tracker._draw_hud(frame, 640, 480, 0.5, 0.45, (323, 245),
                      True, 0.0, 0.0, face_found=True)
    assert np.count_nonzero(frame) > 50

    frame2 = np.zeros((480, 640, 3), dtype=np.uint8)
    tracker._draw_hud(frame2, 640, 480, 0.5, 0.45, (384, 288),
                      False, 250.0, 180.0, face_found=True)
    assert np.count_nonzero(frame2) > 50
