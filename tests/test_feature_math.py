"""test_feature_math.py — Unit tests for biometric feature math (T-04).

Covers:
  - NOSE_TIP_INDEX == 1
  - RIGID_NOSE_INDICES == [1, 4, 6, 197]
  - calculate_ear symmetry and scaling
  - calculate_mar symmetry and scaling
  - estimate_head_pose via solvePnP
"""
from __future__ import annotations

import math
import numpy as np
import pytest

from host.omniband.tracking.webcam_tracker import (
    NOSE_TIP_INDEX,
    RIGID_NOSE_INDICES,
    HEAD_POSE_LANDMARK_INDICES,
    calculate_ear,
    calculate_mar,
    estimate_head_pose,
)


def test_landmark_indices():
    assert NOSE_TIP_INDEX == 1
    assert RIGID_NOSE_INDICES == [1, 4, 6, 197]
    assert HEAD_POSE_LANDMARK_INDICES == [1, 199, 33, 263, 61, 291]


def test_calculate_ear_scaling():
    # Eye: width 20, vertical open 6 -> EAR = (6 + 6) / (2 * 20) = 0.30
    pts_1x = [
        (10.0, 10.0), (15.0, 7.0), (25.0, 7.0),
        (30.0, 10.0), (25.0, 13.0), (15.0, 13.0),
    ]
    ear_1x = calculate_ear(pts_1x)
    assert abs(ear_1x - 0.30) < 0.01

    # Scaled 2x (user moves closer to camera) -> EAR must remain invariant!
    pts_2x = [(p[0] * 2.0, p[1] * 2.0) for p in pts_1x]
    ear_2x = calculate_ear(pts_2x)
    assert abs(ear_2x - ear_1x) < 1e-4, "EAR must be scale-invariant"


def test_calculate_mar_scaling():
    # Mouth: width 40, vertical open 12 -> MAR = 12 / 40 = 0.30
    # indices: [p13, p14, p78, p308]
    pts_1x = [
        (20.0, 14.0), (20.0, 26.0), (0.0, 20.0), (40.0, 20.0),
    ]
    mar_1x = calculate_mar(pts_1x)
    assert abs(mar_1x - 0.30) < 0.01

    # Scaled 2x -> MAR must remain invariant!
    pts_2x = [(p[0] * 2.0, p[1] * 2.0) for p in pts_1x]
    mar_2x = calculate_mar(pts_2x)
    assert abs(mar_2x - mar_1x) < 1e-4, "MAR must be scale-invariant"


def test_estimate_head_pose_frontal():
    # Create synthetic frontal face landmarks (478 points)
    w, h = 640, 480
    landmarks_px = [(w / 2.0, h / 2.0)] * 478

    # Set key points corresponding to standard frontal face (+Y down)
    landmarks_px[1]   = (320.0, 240.0)  # Nose tip
    landmarks_px[199] = (320.0, 350.0)  # Chin
    landmarks_px[33]  = (250.0, 190.0)  # Left eye outer
    landmarks_px[263] = (390.0, 190.0)  # Right eye outer
    landmarks_px[61]  = (270.0, 300.0)  # Left mouth
    landmarks_px[291] = (370.0, 300.0)  # Right mouth

    yaw, pitch = estimate_head_pose(landmarks_px, w, h)
    # Frontal face should have yaw and pitch near 0 degrees (within +/- 15 deg)
    assert abs(yaw) < 15.0, f"Expected near-zero yaw, got {yaw}"
    assert abs(pitch) < 15.0, f"Expected near-zero pitch, got {pitch}"
