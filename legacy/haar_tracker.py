"""legacy/haar_tracker.py — Archived Haar cascade tracker (R02, M-08).

NOTE: Haar cascades (haarcascade_frontalface_default.xml, haarcascade_eye.xml)
have been completely removed from production tracking paths.
They are archived here for historical reference only and MUST NOT be used in production.
"""
from __future__ import annotations

import cv2
import numpy as np


class LegacyHaarTracker:
    """Historical reference only — DO NOT USE IN PRODUCTION."""
    def __init__(self):
        self._face_cascade = None
        self._eye_cascade = None

    def detect(self, gray: np.ndarray):
        return None

