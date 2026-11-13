"""test_one_euro_parity.py — Verifies 1-Euro filter parity and mathematical invariants (T-05)."""
from __future__ import annotations

import math
import numpy as np
import pytest

from host.omniband.tracking.webcam_tracker import OneEuroFilter


def test_one_euro_dc_passthrough():
    """Constant signal at steady state must pass through with zero offset."""
    f = OneEuroFilter(min_cutoff=1.0, beta=0.0)
    val = 42.5
    t = 0.0
    for _ in range(50):
        out = f.filter(t, val)
        t += 0.016
    assert abs(out - val) < 1e-4, f"DC signal must pass through, got {out}"


def test_one_euro_step_response():
    """Step response must smoothly asymptotically converge without overshoot."""
    f = OneEuroFilter(min_cutoff=1.0, beta=0.01)
    f.filter(0.0, 0.0)
    t = 0.0
    prev = 0.0
    for _ in range(100):
        t += 0.01
        cur = f.filter(t, 100.0)
        assert cur >= prev, "Step response should be monotonic"
        prev = cur
    assert abs(cur - 100.0) < 0.1, f"Step response must converge to 100.0, got {cur}"


def test_one_euro_dynamic_cutoff_adaptation():
    """Higher speed must increase effective cutoff, reducing lag."""
    # Slow drift
    f_slow = OneEuroFilter(min_cutoff=0.5, beta=0.05)
    f_slow.filter(0.0, 0.0)
    slow_resp = f_slow.filter(0.01, 1.0)  # low velocity

    # Fast jerk
    f_fast = OneEuroFilter(min_cutoff=0.5, beta=0.05)
    f_fast.filter(0.0, 0.0)
    fast_resp = f_fast.filter(0.01, 100.0)  # high velocity

    # Fraction of step completed in first frame: fast should be much higher due to beta * dx
    frac_slow = slow_resp / 1.0
    frac_fast = fast_resp / 100.0
    assert frac_fast > frac_slow, (
        f"Beta term must increase cutoff on fast moves: fast frac={frac_fast}, slow frac={frac_slow}"
    )

