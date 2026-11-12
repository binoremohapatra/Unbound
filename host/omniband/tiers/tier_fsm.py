"""tiers/tier_fsm.py - 4-Tier Fallback State Machine (TR-01, TR-02, TR-03, TR-04, TR-05).

Deterministic pure function:
  step(state, inputs, now) -> (next_state, actions)

Tiers:
  T1_USB / T1_BT: Pure 4-channel piezo hardware
  T2_HYBRID: Hardware + Optical (Head turn = steering, Clench = action)
  T3_OPTICAL: Optical fallback (3.0s hardware link loss bridge)
  T4_SIM: Non-conflicting simulator mode (F7-F10)
  LIMP_HOME: Controlled stop when all human sources lost
  GRACE: 3.0s autonomous lane-keeping bridge during cable pull
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class TierInputs:
    link_ok: bool                 # hardware frame received within 150 ms
    cam_ok: bool                  # optical camera active and tracking face
    sim_enabled: bool             # simulator transport active
    hybrid_enabled: bool          # hybrid mode enabled in settings
    optical_fallback_enabled: bool# optical fallback enabled in settings
    forced_tier: Optional[str] = None # manual tier override if requested


@dataclass(frozen=True)
class TierState:
    active_tier: str              # "T1_USB" | "T1_BT" | "T2_HYBRID" | "T3_OPTICAL" | "T4_SIM" | "LIMP_HOME" | "GRACE"
    target_tier: str
    grace_start_t: float
    link_lost_t: float
    held_keys: Tuple[str, ...]
    settling_until_t: float


class TierFSM:
    """Deterministic 4-Tier Fallback State Machine with GRACE bridging."""

    GRACE_DURATION_S = 3.0
    SETTLE_DURATION_S = 0.500

    def __init__(self):
        self.state = TierState(
            active_tier="T4_SIM",
            target_tier="T4_SIM",
            grace_start_t=0.0,
            link_lost_t=0.0,
            held_keys=(),
            settling_until_t=0.0,
        )

    def step(self,
             inputs: TierInputs,
             now: float) -> Tuple[TierState, List[str]]:
        """Advances tier state machine. Returns (new_state, list_of_action_events)."""
        actions: List[str] = []
        cur_tier = self.state.active_tier
        grace_start = self.state.grace_start_t
        link_lost = self.state.link_lost_t
        settling_until = self.state.settling_until_t

        # 0. Check forced override
        if inputs.forced_tier:
            if cur_tier != inputs.forced_tier:
                actions.append("RELEASE_ALL")
                actions.append(f"TIER_SWITCH: {cur_tier} -> {inputs.forced_tier}")
            return (TierState(inputs.forced_tier, inputs.forced_tier, 0.0, 0.0, (), 0.0), actions)

        # 1. Update link loss timers
        if not inputs.link_ok and link_lost == 0.0:
            link_lost = now
        elif inputs.link_ok:
            link_lost = 0.0

        # 2. Determine ideal target tier based on availability table (TR-02)
        if inputs.link_ok:
            if inputs.cam_ok and inputs.hybrid_enabled:
                desired = "T2_HYBRID"
            else:
                desired = "T1_USB"
        else:
            # Hardware link lost
            if cur_tier in ("T1_USB", "T1_BT", "T2_HYBRID"):
                # Enter GRACE state immediately upon hardware silence > 150ms (TR-03)
                desired = "GRACE"
                grace_start = now
                actions.append("RELEASE_ALL")
                actions.append("ENTER_GRACE: 3.0s lane-keeping bridge")
            elif cur_tier == "GRACE":
                # Check if GRACE timer expired
                if now - grace_start >= self.GRACE_DURATION_S:
                    actions.append("RELEASE_ALL")
                    actions.append("GRACE_EXPIRED")
                    if inputs.cam_ok and inputs.optical_fallback_enabled:
                        desired = "T3_OPTICAL"
                    elif inputs.sim_enabled:
                        desired = "T4_SIM"
                    else:
                        desired = "LIMP_HOME"
                else:
                    desired = "GRACE"
            else:
                # Already past GRACE
                if inputs.cam_ok and inputs.optical_fallback_enabled:
                    desired = "T3_OPTICAL"
                elif inputs.sim_enabled:
                    desired = "T4_SIM"
                else:
                    desired = "LIMP_HOME"

        # 3. Handle Promotion / Transition
        if desired != cur_tier:
            if desired in ("T1_USB", "T2_HYBRID") and cur_tier in ("T3_OPTICAL", "T4_SIM", "LIMP_HOME"):
                # Promotion settling period (500 ms)
                settling_until = now + self.SETTLE_DURATION_S
                actions.append("PROMOTION_SETTLING")

            actions.append("RELEASE_ALL")
            actions.append(f"TIER_TRANSITION: {cur_tier} -> {desired}")
            cur_tier = desired

        new_state = TierState(
            active_tier=cur_tier,
            target_tier=desired,
            grace_start_t=grace_start,
            link_lost_t=link_lost,
            held_keys=(),
            settling_until_t=settling_until,
        )
        self.state = new_state
        return (new_state, actions)

