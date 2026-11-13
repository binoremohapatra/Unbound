"""test_ble_and_calibration.py - Unit tests for BLE client, cross-talk suppression, and auto-gain."""
import pytest
import time
from host.omniband.hardware.ble_client import parse_ble_packet
from host.omniband.hardware.hardware_calibration import HardwareCalibrator, HardwareCalibrationProfile


def test_parse_ble_packet_csv():
    # Valid compact CSV: S,ch1,ch2,ch3,ch4,seq
    line = "S,120,340,560,780,42\n"
    pkt = parse_ble_packet(line)
    assert pkt is not None
    assert pkt["kind"] == "somatic"
    assert pkt["channels"] == [120.0, 340.0, 560.0, 780.0]
    assert pkt["seq"] == 42


def test_parse_ble_packet_invalid():
    assert parse_ble_packet("") is None
    assert parse_ble_packet("S,1,2\n") is None
    assert parse_ble_packet("invalid,data,here\n") is None


def test_parse_ble_packet_ndjson():
    line = '{"k":"tele","mode":1,"seq":99}\n'
    pkt = parse_ble_packet(line)
    assert pkt is not None
    assert pkt["kind"] == "ndjson"
    assert pkt["obj"]["k"] == "tele"
    assert pkt["obj"]["seq"] == 99


def test_baseline_and_auto_gain_calculation():
    cal = HardwareCalibrator()
    # Mock resting baseline samples
    resting = [
        [50.0, 48.0, 52.0, 49.0],
        [51.0, 49.0, 50.0, 50.0],
        [49.0, 50.0, 51.0, 51.0],
        [50.0, 51.0, 49.0, 50.0],
    ]
    cal.compute_baseline(resting)

    for i in range(4):
        assert abs(cal.profile.mu[i] - 50.0) < 1.0
        assert cal.profile.sigma[i] > 0.0
        # Optimal threshold must be >= 0.42
        assert cal.profile.thresholds[i] >= 0.42

    # Mock flex on CH1 Left Forehead (Raise Eyebrow)
    flex_ch1 = [
        [500.0, 100.0, 60.0, 50.0],
        [850.0, 150.0, 70.0, 55.0],
        [600.0, 120.0, 65.0, 52.0],
    ]
    cal.register_flex_step(0, flex_ch1)

    # Check that gain was scaled so flex reaches ~0.85
    target_amp = 850.0 - cal.profile.mu[0]
    expected_gain = 0.85 / target_amp
    assert abs(cal.profile.gains[0] - expected_gain) < 1e-4

    # Check cross-talk leakage ratio from CH1 into CH2
    leak_ch2 = 150.0 - cal.profile.mu[1]
    expected_crosstalk = leak_ch2 / target_amp
    assert abs(cal.profile.crosstalk[0][1] - expected_crosstalk) < 1e-3


def test_crosstalk_dominance_gate_jaw_clench():
    cal = HardwareCalibrator()
    cal.profile.mu = [0.0, 0.0, 0.0, 0.0]
    cal.profile.gains = [1.0, 1.0, 1.0, 1.0]
    # Say Jaw Clench (CH4, index 3) leaks 30% into CH1 and 25% into CH2
    cal.profile.crosstalk[3][0] = 0.30
    cal.profile.crosstalk[3][1] = 0.25

    t0 = time.perf_counter()

    # Case 1: Strong Jaw Clench (CH4 = 0.80) with leakage on CH1 = 0.24, CH2 = 0.20
    raw_sample = [0.24, 0.20, 0.0, 0.80]
    filtered = cal.apply_filter(raw_sample, now=t0)

    # Cross-talk dominance gate must subtract the leakage!
    assert filtered[3] == 0.80
    assert filtered[0] <= 0.01  # CH1 suppressed
    assert filtered[1] <= 0.01  # CH2 suppressed

    # Case 2: Intentional CH1 steering flex (CH1 = 0.85, CH4 = 0.10 - no jaw clench)
    steering_sample = [0.85, 0.10, 0.0, 0.10]
    filtered_steering = cal.apply_filter(steering_sample, now=t0 + 0.1)
    assert abs(filtered_steering[0] - 0.85) < 1e-3
    assert filtered_steering[3] == 0.10
