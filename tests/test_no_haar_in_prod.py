"""test_no_haar_in_prod.py — Verifies R02 & M-08: Haar cascades removed from production."""
from __future__ import annotations

from pathlib import Path
import pytest


def test_no_haar_in_webcam_tracker():
    """webcam_tracker.py must not import or instantiate CascadeClassifier."""
    root = Path(__file__).parent.parent
    tracker_path = root / "host" / "omniband" / "tracking" / "webcam_tracker.py"
    text = tracker_path.read_text(encoding="utf-8")

    assert "CascadeClassifier" not in text, (
        "CascadeClassifier found in webcam_tracker.py — must be archived in legacy/"
    )
    assert "haarcascade_" not in text, (
        "haarcascade XML path found in webcam_tracker.py — must be archived in legacy/"
    )


def test_legacy_haar_tracker_exists():
    """legacy/haar_tracker.py must contain the archived Haar cascade code."""
    root = Path(__file__).parent.parent
    legacy_path = root / "legacy" / "haar_tracker.py"
    assert legacy_path.is_file(), f"Expected legacy/haar_tracker.py at {legacy_path}"
    text = legacy_path.read_text(encoding="utf-8")
    assert "haarcascade" in text

