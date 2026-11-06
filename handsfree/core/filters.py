"""Signal filtering and stabilization: One-Euro, EMA, Hysteresis, and Debounce."""

import math
import time
from typing import Optional


class OneEuroFilter:
    """1€ Filter for low-latency, jitter-free signal smoothing."""

    def __init__(
        self,
        freq: float = 60.0,
        min_cutoff: float = 1.0,
        beta: float = 0.007,
        d_cutoff: float = 1.0,
    ):
        self.freq = freq
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.x_prev: Optional[float] = None
        self.dx_prev: float = 0.0
        self.t_prev: Optional[float] = None

    def _alpha(self, cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def filter(self, x: float, timestamp: Optional[float] = None) -> float:
        now = timestamp if timestamp is not None else time.time()

        if self.t_prev is None:
            self.x_prev = x
            self.dx_prev = 0.0
            self.t_prev = now
            return x

        dt = now - self.t_prev
        if dt <= 1e-6:
            dt = 1.0 / self.freq if self.freq > 0 else 0.016

        dx = (x - self.x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1.0 - a_d) * self.dx_prev

        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1.0 - a) * self.x_prev

        self.x_prev = x_hat
        self.dx_prev = dx_hat
        self.t_prev = now
        return x_hat

    def reset(self) -> None:
        self.x_prev = None
        self.dx_prev = 0.0
        self.t_prev = None


class EMAFilter:
    """Exponential Moving Average (EMA) low-pass filter."""

    def __init__(self, alpha: float = 0.3):
        if not (0.0 < alpha <= 1.0):
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = alpha
        self.value: Optional[float] = None

    def filter(self, x: float) -> float:
        if self.value is None:
            self.value = x
        else:
            self.value = self.alpha * x + (1.0 - self.alpha) * self.value
        return self.value

    def reset(self) -> None:
        self.value = None


class HysteresisFilter:
    """Dual-threshold Schmitt trigger to prevent fluttering around threshold boundaries."""

    def __init__(self, low_threshold: float, high_threshold: float, initial_state: bool = False):
        if low_threshold > high_threshold:
            raise ValueError("low_threshold must be <= high_threshold")
        self.low_threshold = low_threshold
        self.high_threshold = high_threshold
        self.state = initial_state

    def update(self, value: float) -> bool:
        if not self.state and value >= self.high_threshold:
            self.state = True
        elif self.state and value <= self.low_threshold:
            self.state = False
        return self.state


class DebounceFilter:
    """Debounce logic with configurable minimum hold time and refractory cooldown."""

    def __init__(self, hold_seconds: float = 0.05, cooldown_seconds: float = 0.15):
        self.hold_seconds = hold_seconds
        self.cooldown_seconds = cooldown_seconds
        self.state = False
        self._candidate_state = False
        self._candidate_time = 0.0
        self._last_trigger_time = 0.0

    def update(self, raw_state: bool, timestamp: Optional[float] = None) -> bool:
        now = timestamp if timestamp is not None else time.time()

        if not self.state and raw_state and (now - self._last_trigger_time < self.cooldown_seconds):
            return False

        if raw_state != self._candidate_state:
            self._candidate_state = raw_state
            self._candidate_time = now

        if (now - self._candidate_time) >= self.hold_seconds:
            if self._candidate_state != self.state:
                self.state = self._candidate_state
                if self.state:
                    self._last_trigger_time = now

        return self.state

