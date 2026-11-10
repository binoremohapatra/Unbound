"""Fast path: conditioned head pose -> continuous ``Intent(kind="stick")``.

Dataflow: SignalConditioner (filter + neutral offset) -> FastPath.on_signal()
-> FastPath.tick(now) from the 60 Hz loop -> Intent | None.

Intents are emitted ONLY. Cancellation, expiry and key release belong to
state_machine.py / gateway.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from core.events import Intent, SignalEvent

_YAW = "head_yaw"
_PITCH = "head_pitch"
_NEG_INF = float("-inf")


@dataclass(frozen=True)
class FastPathConfig:
    deadzone_yaw: float = 2.5
    deadzone_pitch: float = 2.5
    range_yaw: float = 25.0      # degrees (from neutral) for full deflection
    range_pitch: float = 20.0
    invert_x: bool = False
    invert_y: bool = False
    expo: float = 1.0            # 1.0 = linear; >1 softens small deflections
    min_conf: float = 0.5        # ignore events below this confidence
    stale_s: float = 0.25        # axis treated as centered if no fresh sample
    epsilon: float = 0.002       # suppress emissions smaller than this change
    heartbeat_s: float = 0.0     # >0: re-emit unchanged non-zero stick this often
    name: str = "look_stick"

    def __post_init__(self) -> None:
        for dz, rng, axis in (
            (self.deadzone_yaw, self.range_yaw, "yaw"),
            (self.deadzone_pitch, self.range_pitch, "pitch"),
        ):
            if dz < 0.0 or rng <= dz:
                raise ValueError(f"{axis}: need 0 <= deadzone < range (got {dz}, {rng})")
        if self.expo <= 0.0:
            raise ValueError("expo must be > 0")

    @classmethod
    def from_calibration(cls, cal: Mapping[str, Any], **overrides: Any) -> "FastPathConfig":
        dz = cal["deadzone"]
        rg = cal["range"]
        kwargs: dict[str, Any] = {
            "deadzone_yaw": float(dz["yaw"]),
            "deadzone_pitch": float(dz["pitch"]),
            "range_yaw": float(rg["yaw"]),
            "range_pitch": float(rg["pitch"]),
        }
        kwargs.update(overrides)
        return cls(**kwargs)


def _shape(v: float, dz: float, inv_span: float, expo: float) -> float:
    """Deadzone-rescaled, clamped, optionally curved axis mapping.

    Output is continuous: 0 at the deadzone edge, 1.0 at ``range``.
    """
    a = abs(v)
    if a <= dz:
        return 0.0
    m = (a - dz) * inv_span
    if m > 1.0:
        m = 1.0
    if expo != 1.0:
        m = m ** expo
    return m if v > 0.0 else -m


class FastPath:
    """Stateful head-pose -> stick translator. Not thread-safe; call from the main loop."""

    __slots__ = (
        "_cfg", "_inv_x", "_inv_y", "_sx", "_sy", "_yaw", "_pitch",
        "_t_yaw", "_t_pitch", "_last_x", "_last_y", "_last_emit_t",
    )

    def __init__(self, config: FastPathConfig) -> None:
        self._cfg = config
        self._inv_x = 1.0 / (config.range_yaw - config.deadzone_yaw)
        self._inv_y = 1.0 / (config.range_pitch - config.deadzone_pitch)
        self._sx = -1.0 if config.invert_x else 1.0
        self._sy = -1.0 if config.invert_y else 1.0
        self._yaw = 0.0
        self._pitch = 0.0
        self._t_yaw = _NEG_INF
        self._t_pitch = _NEG_INF
        self._last_x = 0.0
        self._last_y = 0.0
        self._last_emit_t = _NEG_INF

    def on_signal(self, event: SignalEvent) -> None:
        """Cache the latest conditioned yaw/pitch. Allocation-free."""
        if event.conf < self._cfg.min_conf or not math.isfinite(event.value):
            return
        name = event.name
        if name == _YAW:
            self._yaw = event.value
            self._t_yaw = event.t
        elif name == _PITCH:
            self._pitch = event.value
            self._t_pitch = event.t

    def tick(self, now: float) -> Optional[Intent]:
        """Call once per loop iteration with the monotonic time. Returns an
        Intent only when the stick changed (or on heartbeat)."""
        c = self._cfg
        stale = c.stale_s
        x = (
            self._sx * _shape(self._yaw, c.deadzone_yaw, self._inv_x, c.expo)
            if now - self._t_yaw <= stale else 0.0
        )
        y = (
            self._sy * _shape(self._pitch, c.deadzone_pitch, self._inv_y, c.expo)
            if now - self._t_pitch <= stale else 0.0
        )

        eps = c.epsilon
        centered = x == 0.0 and y == 0.0
        was_off_center = self._last_x != 0.0 or self._last_y != 0.0
        changed = (
            abs(x - self._last_x) > eps
            or abs(y - self._last_y) > eps
            or (centered and was_off_center)  # always deliver the return-to-center
        )
        if not changed:
            if not (
                c.heartbeat_s > 0.0
                and not centered
                and now - self._last_emit_t >= c.heartbeat_s
            ):
                return None

        self._last_x = x
        self._last_y = y
        self._last_emit_t = now
        return Intent(kind="stick", name=c.name, x=x, y=y)

    def reset(self) -> None:
        self._yaw = self._pitch = 0.0
        self._t_yaw = self._t_pitch = _NEG_INF
        self._last_x = self._last_y = 0.0
        self._last_emit_t = _NEG_INF
