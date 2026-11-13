"""copilot/arbiter.py - Shared-Autonomy Control Arbiter (CP-02, CP-10).

Integrates Human Intent, Lateral/Longitudinal Controllers, Safety Layer,
and Modulator into a unified ActuatorCommand and digital keystrokes dict.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple
from .types import ActuatorCommand, HumanIntent, VehicleState
from .lateral import LateralController
from .longitudinal import LongitudinalController
from .safety import SafetyLayer
from .modulator import ActuatorModulator


class CoPilotArbiter:
    """Master shared-autonomy controller orchestrating control laws and vetoes."""

    def __init__(self,
                 lateral: Optional[LateralController] = None,
                 longitudinal: Optional[LongitudinalController] = None,
                 safety: Optional[SafetyLayer] = None,
                 modulator: Optional[ActuatorModulator] = None):
        self.lateral = lateral or LateralController()
        self.longitudinal = longitudinal or LongitudinalController()
        self.safety = safety or SafetyLayer()
        self.modulator = modulator or ActuatorModulator()

        self.last_state_t = 0.0
        self.target_lane_index = 1
        self.e_bias = 0.0

    def step(self,
             state: Optional[VehicleState],
             intent: HumanIntent,
             now: float) -> Tuple[ActuatorCommand, Dict[str, bool]]:
        """Processes one 60 Hz control step. Returns (command, key_states)."""
        reasons: List[str] = []

        # 1. Stale telemetry check (CP-01, CP-08)
        if state is None or (now - state.t) > 0.150:
            reasons.append("TELEMETRY LOST: LIMP-HOME BRAKING")
            cmd = ActuatorCommand(
                steer=0.0,
                throttle=0.0,
                brake=0.5,
                handbrake_pulse_ms=0,
                nitro_pulse_ms=0,
                reasons=tuple(reasons),
            )
            keys = self.modulator.modulate(0.0, 0.0, 0.5)
            return (cmd, keys)

        # 2. Check game pause state
        if state.paused:
            reasons.append("GAME PAUSED")
            cmd = ActuatorCommand(0.0, 0.0, 0.0, 0, 0, tuple(reasons))
            self.modulator.reset()
            return (cmd, {"a": False, "d": False, "w": False, "s": False})

        # 3. Process human lane-step intent
        if intent.lane_step != 0:
            self.target_lane_index = max(0, min(state.lane_count - 1, state.lane_index + intent.lane_step))
            reasons.append(f"LANE CHANGE -> {self.target_lane_index}")

        # Continuous lateral bias integrator
        target_e_star = intent.lateral_bias * 1.5  # +/- 1.5m offset within lane

        # 4. Assist Level Arbitration (CP-02)
        assist = intent.assist_level.upper()
        if assist == "OFF":
            # Direct human control (Baseline)
            steer = intent.lateral_bias
            throttle = 1.0 if intent.cruise_on else 0.0
            brake = 0.0
            nitro_ms = 100 if intent.req_nitro else 0
            handbrake_ms = 100 if intent.req_drift else 0
            reasons.append("ASSIST: OFF (RAW MANUAL)")

        elif assist == "LANE":
            # Lateral stabilization + AEB; Human throttle
            steer = self.lateral.compute(state.e, state.psi_e, state.speed, state.kappa_ahead, target_e_star)
            throttle = 1.0 if intent.cruise_on else 0.0
            brake = 0.0
            allow_nitro, allow_drift, tac_reasons = self.safety.validate_tactical(state, intent)
            reasons.extend(tac_reasons)
            nitro_ms = 100 if allow_nitro else 0
            handbrake_ms = 100 if allow_drift else 0

        else: # "FULL"
            # Full lateral + longitudinal cruise + AEB
            steer = self.lateral.compute(state.e, state.psi_e, state.speed, state.kappa_ahead, target_e_star)
            current_gap = state.gaps[state.lane_index] if state.gaps else None
            throttle, brake = self.longitudinal.compute(
                state.speed, intent.v_set, state.kappa_ahead, intent.cruise_on, current_gap
            )
            allow_nitro, allow_drift, tac_reasons = self.safety.validate_tactical(state, intent)
            reasons.extend(tac_reasons)
            nitro_ms = 100 if allow_nitro else 0
            handbrake_ms = 100 if allow_drift else 0

        # 5. Safety Override Layer (AEB / Saturation)
        aeb_engaged, override_brake, safety_reasons = self.safety.evaluate(state, intent, steer)
        if aeb_engaged or override_brake > 0.0:
            brake = max(brake, override_brake)
            throttle = 0.0
            reasons.extend(safety_reasons)

        cmd = ActuatorCommand(
            steer=steer,
            throttle=throttle,
            brake=brake,
            handbrake_pulse_ms=handbrake_ms,
            nitro_pulse_ms=nitro_ms,
            reasons=tuple(reasons),
        )

        # 6. Discrete keystroke modulation
        keys = self.modulator.modulate(steer, throttle, brake)
        return (cmd, keys)

