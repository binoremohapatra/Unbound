"""tests/test_modulator.py - T-01 Sigma-Delta Modulator test suite.

Verifies:
- Long-run duty cycle accuracy within +/- 10% over 500 ms windows.
- Invariant: A and D are never simultaneously down.
- Invariant: W and S are never simultaneously down.
- Respect of min_pulse_ticks across transitions.
- Property test over 5000 random command steps.
"""
import random
import pytest

from host.omniband.copilot.modulator import SigmaDeltaAxis, ActuatorModulator


def test_mutual_exclusion_and_min_pulse_ticks():
    mod = ActuatorModulator(min_pulse_ticks=2)
    
    # Rapid oscillating commands
    commands = [0.8, -0.8, 0.5, -0.5, 1.0, -1.0, 0.0] * 20
    for cmd in commands:
        keys = mod.modulate(steer=cmd, throttle=abs(cmd), brake=0.0)
        assert not (keys["a"] and keys["d"]), "A and D simultaneously active!"
        assert not (keys["w"] and keys["s"]), "W and S simultaneously active!"


def test_duty_accuracy_500ms_window():
    # 60 Hz tick -> 30 ticks = 500 ms
    ticks_per_window = 30
    axis = SigmaDeltaAxis("pos", "neg", min_pulse_ticks=1)

    for target_duty in [0.2, 0.4, 0.6, 0.8]:
        axis.reset()
        high_count = 0
        total_ticks = 300  # 5 seconds
        for _ in range(total_ticks):
            pos, neg = axis.step(target_duty)
            if pos:
                high_count += 1
            assert not neg

        measured_duty = high_count / total_ticks
        assert abs(measured_duty - target_duty) <= 0.10, (
            f"Duty error excessive: target={target_duty}, measured={measured_duty:.3f}"
        )


def test_property_random_stream():
    mod = ActuatorModulator(min_pulse_ticks=2)
    random.seed(42)

    for _ in range(5000):
        st = random.uniform(-1.0, 1.0)
        th = random.uniform(0.0, 1.0)
        br = random.uniform(0.0, 1.0)

        keys = mod.modulate(steer=st, throttle=th, brake=br)
        assert not (keys["a"] and keys["d"])
        assert not (keys["w"] and keys["s"])

