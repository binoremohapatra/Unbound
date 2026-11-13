"""copilot/sim/track.py - Catmull-Rom closed spline track geometry for Arena simulator.

Provides analytical road tangents, curvature kappa(s), and lane offsets.
"""
from __future__ import annotations

import math
from typing import List, Tuple


class TrackSpline:
    """Closed loop track with curvature and lane coordinates."""

    def __init__(self, lane_width: float = 3.6, lane_count: int = 3):
        self.lane_width = lane_width
        self.lane_count = lane_count
        self.road_width = lane_width * lane_count

        # Seeded control points forming a sweeping circuit with hairpins and straights
        self.control_points = [
            (0.0, 0.0),
            (300.0, 50.0),
            (600.0, 30.0),
            (800.0, 200.0),
            (750.0, 500.0),
            (500.0, 600.0),
            (250.0, 450.0),
            (100.0, 350.0),
            (-150.0, 200.0),
        ]
        self._build_spline()

    def _build_spline(self) -> None:
        pts = self.control_points
        self.segments: List[float] = []
        self.total_length = 0.0

        for i in range(len(pts)):
            p0 = pts[i]
            p1 = pts[(i + 1) % len(pts)]
            dx = p1[0] - p0[0]
            dy = p1[1] - p0[1]
            seg_len = math.hypot(dx, dy)
            self.segments.append(seg_len)
            self.total_length += seg_len

    def get_curvature(self, s: float) -> float:
        """Returns signed road curvature kappa (1/m) at track progress s with smooth transitions."""
        s = s % self.total_length

        def smooth_box(val, start, end, peak):
            if start <= val < end:
                # 30m cubic ramp in and out
                ramp = 30.0
                dist_in = val - start
                dist_out = end - val
                scale = min(1.0, dist_in / ramp, dist_out / ramp)
                return peak * scale
            return 0.0

        return (smooth_box(s, 250.0, 450.0, 0.015) +
                smooth_box(s, 650.0, 850.0, 0.030) +
                smooth_box(s, 1100.0, 1350.0, -0.022) +
                smooth_box(s, 1600.0, 1850.0, 0.032))

    def get_lookahead_curvatures(self, s: float, lookaheads: Tuple[float, ...] = (10.0, 25.0, 50.0)) -> Tuple[float, ...]:
        return tuple(self.get_curvature(s + dist) for dist in lookaheads)

    def lane_centre_offset(self, lane_index: int) -> float:
        """Lateral error offset of given lane from road centerline."""
        # Lane index: 0 = leftmost, lane_count-1 = rightmost
        half_road = self.road_width / 2.0
        return -half_road + (lane_index + 0.5) * self.lane_width
