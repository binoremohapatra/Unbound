"""copilot/modulator.py - First-order Sigma-Delta Modulator.

Translates continuous analog actuator demands into discrete digital keystrokes
at 60 Hz engine tick. Enforces mutual exclusion (A/D, W/S) and minimum pulse/gap
duration. Long-run duty cycle matches continuous input without fixed-period PWM artifacts.
"""
from __future__ import annotations

from typing import Tuple


class SigmaDeltaAxis:
    """Single-axis first-order Sigma-Delta modulator."""

    def __init__(self, key_pos: str, key_neg: str, min_pulse_ticks: int = 2):
        self.key_pos = key_pos
        self.key_neg = key_neg
        self.min_pulse_ticks = max(1, min_pulse_ticks)
        self.acc = 0.0
        self.current_state = 0   # +1: pos, -1: neg, 0: idle
        self.ticks_in_state = 0

    def step(self, u: float) -> Tuple[bool, bool]:
        """Processes continuous command u in [-1.0, 1.0]. Returns (pos_down, neg_down)."""
        u = max(-1.0, min(1.0, u))
        
        # Determine candidate direction
        candidate = 0
        if abs(u) > 1e-4:
            self.acc += abs(u)
            if self.acc >= 1.0:
                self.acc -= 1.0
                candidate = 1 if u > 0 else -1
        else:
            self.acc = 0.0

        # Enforce minimum pulse and gap ticks
        if candidate != self.current_state:
            if self.ticks_in_state < self.min_pulse_ticks:
                # Must hold current state to respect min pulse/gap
                self.ticks_in_state += 1
                return (self.current_state == 1, self.current_state == -1)
            else:
                self.current_state = candidate
                self.ticks_in_state = 1
        else:
            self.ticks_in_state += 1

        pos_down = (self.current_state == 1)
        neg_down = (self.current_state == -1)

        # Invariant: mutual exclusion
        assert not (pos_down and neg_down), f"Mutual exclusion violated: {self.key_pos} and {self.key_neg}"
        return (pos_down, neg_down)

    def reset(self) -> None:
        self.acc = 0.0
        self.current_state = 0
        self.ticks_in_state = 0


class ActuatorModulator:
    """Co-Pilot modulator for Steering (A/D) and Throttle/Brake (W/S)."""

    def __init__(self, min_pulse_ticks: int = 2):
        # Steer: -1=A (left), +1=D (right)
        self.steer_mod = SigmaDeltaAxis("d", "a", min_pulse_ticks=min_pulse_ticks)
        # Longitudinal: +1=W (throttle), -1=S (brake)
        self.long_mod = SigmaDeltaAxis("w", "s", min_pulse_ticks=min_pulse_ticks)

    def modulate(self, steer: float, throttle: float, brake: float) -> dict[str, bool]:
        """Returns dict of key -> pressed_state."""
        d_down, a_down = self.steer_mod.step(steer)
        # Compute combined longitudinal effort [-1.0, 1.0]
        long_effort = max(0.0, min(1.0, throttle)) - max(0.0, min(1.0, brake))
        w_down, s_down = self.long_mod.step(long_effort)

        return {
            "a": a_down,
            "d": d_down,
            "w": w_down,
            "s": s_down,
        }

    def reset(self) -> None:
        self.steer_mod.reset()
        self.long_mod.reset()

