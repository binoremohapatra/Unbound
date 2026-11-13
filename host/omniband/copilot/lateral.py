"""copilot/lateral.py - Lateral path tracking and steering control (CP-04).

Implements lookahead curvature feedforward plus proportional-derivative error feedback:
  kappa_cmd = kappa_ff(s + L) + K_e * (e - e_star) + K_psi * psi_e
with dynamic lookahead L = L0 + k_v * v.
"""
from __future__ import annotations

import math
from typing import Tuple


class LateralController:
    """Computes normalized steering effort [-1.0, 1.0] from VehicleState and HumanIntent."""

    def __init__(self,
                 k_e: float = 0.65,
                 k_psi: float = 2.2,
                 l0: float = 6.0,
                 k_v: float = 0.40,
                 wheelbase: float = 2.7,
                 max_steer_rad: float = 0.45):
        self.k_e = k_e
        self.k_psi = k_psi
        self.l0 = l0
        self.k_v = k_v
        self.wheelbase = wheelbase
        self.max_steer_rad = max_steer_rad

    def compute(self,
                e: float,
                psi_e: float,
                speed: float,
                kappa_ahead: Tuple[float, ...],
                e_star: float = 0.0) -> float:
        """Computes normalized steering demand in [-1.0, 1.0] (+right)."""
        # Dynamic lookahead
        L = self.l0 + self.k_v * max(0.0, speed)

        # Curvature feedforward: pick primary lookahead sample
        kappa_ff = kappa_ahead[0] if kappa_ahead else 0.0

        # Feedback law: to correct positive lateral error (car is right of path),
        # vehicle must steer LEFT (negative curvature).
        # To correct positive heading error (pointing right), vehicle must turn LEFT.
        lateral_error = e - e_star
        kappa_cmd = kappa_ff - self.k_e * lateral_error - self.k_psi * psi_e

        # Convert requested curvature into front road wheel angle: delta = atan(L * kappa)
        delta_req = math.atan(self.wheelbase * kappa_cmd)

        # Normalize to steering actuator [-1.0, 1.0]
        u = delta_req / self.max_steer_rad
        return max(-1.0, min(1.0, u))
