"""Unit tests for signal filters (One-Euro, EMA, Hysteresis, Debounce)."""

import time
import unittest
from handsfree.core.filters import OneEuroFilter, EMAFilter, HysteresisFilter, DebounceFilter


class TestFilters(unittest.TestCase):

    def test_one_euro_filter(self):
        f = OneEuroFilter(freq=60.0, min_cutoff=1.0, beta=0.01)
        # First sample returns identical value
        v0 = f.filter(0.0, timestamp=1.0)
        self.assertAlmostEqual(v0, 0.0)

        # Smooth gradual progression
        v1 = f.filter(10.0, timestamp=1.016)
        self.assertTrue(0.0 < v1 < 10.0)

    def test_ema_filter(self):
        f = EMAFilter(alpha=0.5)
        self.assertEqual(f.filter(10.0), 10.0)
        self.assertEqual(f.filter(20.0), 15.0)

    def test_hysteresis_filter(self):
        h = HysteresisFilter(low_threshold=0.2, high_threshold=0.7)
        # Below high threshold, remains False
        self.assertFalse(h.update(0.5))
        # Hits or exceeds high threshold, switches to True
        self.assertTrue(h.update(0.75))
        # Drops below high but above low threshold, stays True
        self.assertTrue(h.update(0.4))
        # Drops below low threshold, switches to False
        self.assertFalse(h.update(0.15))

    def test_debounce_filter(self):
        d = DebounceFilter(hold_seconds=0.05, cooldown_seconds=0.1)
        t = 1000.0
        # Instant flick does not trigger
        self.assertFalse(d.update(True, timestamp=t))
        self.assertFalse(d.update(True, timestamp=t + 0.02))
        # Held past hold_seconds triggers True
        self.assertTrue(d.update(True, timestamp=t + 0.06))


if __name__ == "__main__":
    unittest.main()

