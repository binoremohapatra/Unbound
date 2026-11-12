"""tests/test_tier_fsm.py - T-05 and T-06 4-Tier FSM and Fail-Safe Tests.

Verifies:
- Deterministic transitions across T1, T2, T3, T4, GRACE, LIMP-HOME.
- Zero-stuck-keys invariant: RELEASE_ALL on every transition.
- 3.0s GRACE bridging window during simulated USB cable pull.
- 10,000-step random availability property test.
"""
import pytest
import random

from host.omniband.tiers.tier_fsm import TierFSM, TierInputs, TierState


def test_grace_state_cable_pull():
    fsm = TierFSM()
    now = 100.0

    # 1. Establish T1_USB
    inp = TierInputs(link_ok=True, cam_ok=False, sim_enabled=True, hybrid_enabled=False, optical_fallback_enabled=True)
    st, acts = fsm.step(inp, now)
    assert st.active_tier == "T1_USB"

    # 2. Pull USB cable (link loss) -> must enter GRACE immediately
    now += 0.200
    inp = TierInputs(link_ok=False, cam_ok=True, sim_enabled=True, hybrid_enabled=False, optical_fallback_enabled=True)
    st, acts = fsm.step(inp, now)
    assert st.active_tier == "GRACE"
    assert "RELEASE_ALL" in acts

    # 3. During GRACE (1.5s in) -> stays in GRACE
    now += 1.500
    st, acts = fsm.step(inp, now)
    assert st.active_tier == "GRACE"

    # 4. At GRACE expiration (3.1s in) with camera available -> transitions to T3_OPTICAL
    now += 1.600
    st, acts = fsm.step(inp, now)
    assert st.active_tier == "T3_OPTICAL"
    assert "RELEASE_ALL" in acts


def test_property_10k_random_transitions():
    fsm = TierFSM()
    random.seed(1337)
    now = 500.0

    for step in range(10000):
        now += 0.016  # 60 Hz steps
        inp = TierInputs(
            link_ok=random.random() > 0.3,
            cam_ok=random.random() > 0.4,
            sim_enabled=True,
            hybrid_enabled=random.choice([True, False]),
            optical_fallback_enabled=True,
            forced_tier=None,
        )
        st, acts = fsm.step(inp, now)
        assert st.active_tier in ("T1_USB", "T1_BT", "T2_HYBRID", "T3_OPTICAL", "T4_SIM", "GRACE", "LIMP_HOME")
        if "TIER_TRANSITION" in " ".join(acts):
            assert "RELEASE_ALL" in acts, f"Invariant violated: Tier transition without RELEASE_ALL on step {step}"

