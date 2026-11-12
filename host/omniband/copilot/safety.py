"""copilot/safety.py - Automatic Emergency Braking (AEB) and Transparent Vetoes (CP-08, CP-09).

Enforces Time-To-Collision (TTC) bounds, steering saturation watchdogs,
and validates tactical intent requests (Nitro, Drift) against road conditions.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple
from .types import HumanIntent, VehicleState


class SafetyLayer:
    """Evaluates safety invariants and veto conditions on human intents."""

    def __init__(self,
                 ttc_warn_s: float = 2.0,
                 ttc_brake_s: float = 1.2,
                 ttc_cancel_cruise_s: float = 0.6,
                 min_drift_speed: float = 12.0,
                 corner_nitro_thr: float = 0.025):
        self.ttc_warn_s = ttc_warn_s
        self.ttc_brake_s = ttc_brake_s
        self.ttc_cancel_cruise_s = ttc_cancel_cruise_s
        self.min_drift_speed = min_drift_speed
        self.corner_nitro_thr = corner_nitro_thr

        self.sat_ticks = 0
        self.aeb_active = False

    def evaluate(self,
                 state: VehicleState,
                 intent: HumanIntent,
                 raw_steer: float) -> Tuple[bool, float, List[str]]:
        """Returns (aeb_engaged, override_brake, reasons)."""
        reasons: List[str] = []
        override_brake = 0.0
        self.aeb_active = False

        # 1. Forward collision checks via TTC = distance / closing_speed
        current_lane = min(max(0, state.lane_index), len(state.gaps) - 1)
        lead_gap = state.gaps[current_lane] if state.gaps else None

        if lead_gap is not None:
            dist_m, closing_mps = lead_gap
            if closing_mps > 1e-2:
                ttc = dist_m / closing_mps
                if ttc <= self.ttc_brake_s:
                    self.aeb_active = True
                    override_brake = 1.0
                    reasons.append(f"AEB ENGAGED: TTC {ttc:.1f}s <= {self.ttc_brake_s}s")
                elif ttc <= self.ttc_warn_s:
                    reasons.append(f"COLLISION WARNING: TTC {ttc:.1f}s")

        # 2. Steering saturation watchdog (|u| == 1.0 for > 1.5s while |e| not shrinking)
        if abs(raw_steer) >= 0.98:
            self.sat_ticks += 1
            if self.sat_ticks > 90:  # 90 ticks @ 60Hz = 1.5s
                reasons.append("STEERING SATURATION DETECTED: AUTO-RECOVERY")
                override_brake = max(override_brake, 0.4)
        else:
            self.sat_ticks = max(0, self.sat_ticks - 2)

        # 3. Off-road recovery
        if state.offroad:
            override_brake = max(override_brake, 0.5)
            reasons.append("OFFROAD RECOVERY: SPEED LIMITED")

        return (self.aeb_active, override_brake, reasons)

    def validate_tactical(self,
                          state: VehicleState,
                          intent: HumanIntent) -> Tuple[bool, bool, List[str]]:
        """Applies transparent tactical vetoes on Nitro and Drift requests."""
        reasons: List[str] = []
        allow_nitro = False
        allow_drift = False

        # Nitro validation
        if intent.req_nitro:
            max_curve = max([abs(k) for k in state.kappa_ahead], default=0.0)
            if max_curve >= self.corner_nitro_thr:
                reasons.append("NITRO VETOED: CORNER AHEAD")
            else:
                allow_nitro = True
                reasons.append("NITRO EXECUTED")

        # Drift validation
        if intent.req_drift:
            if state.speed < self.min_drift_speed:
                reasons.append("DRIFT VETOED: SPEED LOW")
            else:
                allow_drift = True
                reasons.append("DRIFT EXECUTED")

        return (allow_nitro, allow_drift, reasons)

