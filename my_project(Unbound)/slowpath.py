"""Slow path: blendshape events -> discrete ``Intent(kind="action")``.

Schmitt trigger per gesture (fire at >= high, re-arm only at <= low) plus a
refractory period measured from the last fired action.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from core.events import Intent, SignalEvent

_NEG_INF = float("-inf")


@dataclass(frozen=True)
class GestureThreshold:
    high: float
    low: float
    debounce_ms: int = 150

    def __post_init__(self) -> None:
        if not (0.0 <= self.low < self.high <= 1.0):
            raise ValueError(f"need 0 <= low < high <= 1 (got {self.low}, {self.high})")
        if self.debounce_ms < 0:
            raise ValueError("debounce_ms must be >= 0")


class _Gesture:
    __slots__ = ("name", "high", "low", "debounce_s", "engaged", "last_fire_t")

    def __init__(self, name: str, th: GestureThreshold) -> None:
        self.name = name
        self.high = th.high
        self.low = th.low
        self.debounce_s = th.debounce_ms / 1000.0
        self.engaged = False
        self.last_fire_t = _NEG_INF


class SlowPath:
    """Stateful gesture detector. Not thread-safe; call from the main loop."""

    __slots__ = ("_gestures", "_min_conf")

    def __init__(self, thresholds: Mapping[str, GestureThreshold], min_conf: float = 0.5) -> None:
        self._gestures: dict[str, _Gesture] = {
            name: _Gesture(name, th) for name, th in thresholds.items()
        }
        self._min_conf = min_conf

    @classmethod
    def from_calibration(cls, cal: Mapping[str, Any], min_conf: float = 0.5) -> "SlowPath":
        th = {
            name: GestureThreshold(
                high=float(v["high"]),
                low=float(v["low"]),
                debounce_ms=int(v.get("debounce_ms", 150)),
            )
            for name, v in cal["thresholds"].items()
        }
        return cls(th, min_conf=min_conf)

    def on_signal(self, event: SignalEvent) -> Optional[Intent]:
        """Feed one raw blendshape event. Allocates only when an action fires."""
        g = self._gestures.get(event.name)
        if g is None or event.conf < self._min_conf:
            return None
        v = event.value
        if not math.isfinite(v):
            return None

        if g.engaged:
            if v <= g.low:               # hysteresis release: re-arm
                g.engaged = False
            return None

        if v < g.high:
            return None

        # Crossed the high threshold while armed. Latch either way, so one
        # physical gesture can never yield more than one action.
        g.engaged = True
        t = event.t
        if t < g.last_fire_t:            # clock went backwards (e.g. replay seek)
            g.last_fire_t = _NEG_INF
        if t - g.last_fire_t < g.debounce_s:
            return None                  # inside refractory window: consumed, not fired
        g.last_fire_t = t
        return Intent(kind="action", name=g.name)

    def is_engaged(self, name: str) -> bool:
        g = self._gestures.get(name)
        return g.engaged if g is not None else False

    def reset(self, name: Optional[str] = None) -> None:
        """Re-arm one gesture, or all of them (e.g. after face loss / pause)."""
        targets = self._gestures.values() if name is None else [self._gestures[name]]
        for g in targets:
            g.engaged = False
            g.last_fire_t = _NEG_INF
