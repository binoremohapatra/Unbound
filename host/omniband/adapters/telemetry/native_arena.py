"""native_arena.py - Thread-safe in-memory VehicleState slot.

ArenaWidget writes here at 60 Hz; CoPilotArbiter reads here with zero HTTP/socket
serialization. Lock-free for reads when HAVE_ATOMIC, mutex fallback otherwise.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from host.omniband.copilot.types import VehicleState, HumanIntent


class NativeArenaSlot:
    """Singleton in-process shared state between ArenaWidget and Co-Pilot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Optional[VehicleState] = None
        self._intent: Optional[HumanIntent] = None
        self._ts: float = 0.0

    # ── write side (ArenaWidget, 60 Hz) ────────────────────────────────────
    def write_state(self, state: VehicleState) -> None:
        with self._lock:
            self._state = state
            self._ts = time.perf_counter()

    def write_intent(self, intent: HumanIntent) -> None:
        with self._lock:
            self._intent = intent

    # ── read side (Co-Pilot arbiter, engine tick) ───────────────────────────
    def read_state(self) -> Optional[VehicleState]:
        with self._lock:
            return self._state

    def read_intent(self) -> Optional[HumanIntent]:
        with self._lock:
            return self._intent

    def age_s(self) -> float:
        """Seconds since last write_state call."""
        with self._lock:
            if self._ts == 0.0:
                return float("inf")
            return time.perf_counter() - self._ts

    def is_stale(self, max_age_s: float = 0.5) -> bool:
        return self.age_s() > max_age_s


# Module-level singleton — import and use directly.
ARENA_SLOT = NativeArenaSlot()

