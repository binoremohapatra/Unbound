"""copilot/longitudinal.py - Longitudinal cruise and braking control (CP-05).

Calculates speed setpoint bounded by cornering lateral acceleration limits and traffic gaps:
  v_star = min(v_set, sqrt(a_lat_max / max(|kappa|, 1e-4)), v_traffic)
Runs a PI controller for smooth throttle/brake modulation without simultaneous application.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple


class LongitudinalController:
    """PI controller for throttle and braking effort."""

    def __init__(self,
                 kp: float = 0.15,
                 ki: float = 0.02,
                 a_lat_max: float = 4.5,
                 a_brake_comfort: float = 3.5,
                 dt: float = 1.0 / 60.0):
        self.kp = kp
        self.ki = ki
        self.a_lat_max = a_lat_max
        self.a_brake_comfort = a_brake_comfort
        self.dt = dt
        self.integral_error = 0.0

    def compute(self,
                v_current: float,
                v_set: float,
                kappa_ahead: Tuple[float, ...],
                cruise_enabled: bool,
                traffic_gap: Optional[Tuple[float, float]] = None) -> Tuple[float, float]:
        """Computes (throttle, brake) in [0.0, 1.0]. Never simultaneous."""
        if not cruise_enabled:
            self.integral_error = 0.0
            return (0.0, 0.0)

        # 1. Cornering speed limit: v_corner = sqrt(a_lat_max / |kappa|)
        max_kappa = max([abs(k) for k in kappa_ahead], default=0.0)
        if max_kappa > 1e-4:
            v_corner = math.sqrt(self.a_lat_max / max_kappa)
        else:
            v_corner = v_set

        # 2. Traffic speed adaptation
        v_target = min(v_set, v_corner)
        if traffic_gap is not None:
            dist_m, closing_speed = traffic_gap
            if dist_m < 35.0:
                v_target = min(v_target, max(0.0, v_current - closing_speed - 2.0))

        # 3. PI tracking
        error = v_target - v_current
        self.integral_error = max(-20.0, min(20.0, self.integral_error + error * self.dt))

        demand = self.kp * error + self.ki * self.integral_error

        if demand > 0.05:
            throttle = min(1.0, demand)
            brake = 0.0
        elif demand < -0.05:
            throttle = 0.0
            brake = min(1.0, -demand)
        else:
            throttle = 0.0
            brake = 0.0

        return (throttle, brake)

    def reset(self) -> None:
        self.integral_error = 0.0

