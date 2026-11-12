"""tiers/chaos.py - Chaos Engineering Injection Panel (TR-08).

Injects deterministic link drops, Bluetooth drops, camera occlusions,
and telemetry stalls for live stage demonstrations and property testing.
"""
from __future__ import annotations

import time
from typing import Optional


class ChaosEngine:
    """Manages synthetic fault injections."""

    def __init__(self):
        self.link_drop_until = 0.0
        self.cam_cover_until = 0.0
        self.telemetry_stall_until = 0.0

    def inject_link_drop(self, duration_s: float = 3.5) -> None:
        """Simulates USB cable pull."""
        self.link_drop_until = time.perf_counter() + duration_s

    def inject_camera_cover(self, duration_s: float = 3.0) -> None:
        """Simulates camera occlusion / lighting loss."""
        self.cam_cover_until = time.perf_counter() + duration_s

    def inject_telemetry_stall(self, duration_s: float = 2.0) -> None:
        """Simulates game pause or telemetry network hiccup."""
        self.telemetry_stall_until = time.perf_counter() + duration_s

    def filter_inputs(self, link_ok: bool, cam_ok: bool, now: float) -> tuple[bool, bool]:
        """Applies active chaos overrides to live inputs."""
        effective_link = link_ok and (now >= self.link_drop_until)
        effective_cam = cam_ok and (now >= self.cam_cover_until)
        return (effective_link, effective_cam)

    def is_telemetry_stalled(self, now: float) -> bool:
        return now < self.telemetry_stall_until

