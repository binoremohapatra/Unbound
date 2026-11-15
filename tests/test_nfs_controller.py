"""test_nfs_controller.py - Unit tests for DirectInput Racing Engine, Proportional PWM Steering, and Mini Overlay."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from gateway import Gateway, RecordingBackend, SCANCODES, NFS_ACTION_MAP
from host.omniband.nfs_controller import NFSRacingController


def test_directinput_scancodes_and_action_map():
    """Verify that all required DirectX DirectInput scancodes are correctly defined."""
    # Throttle
    assert SCANCODES["w"] == (0x11, False)
    assert SCANCODES["up"] == (0x48, True)
    assert NFS_ACTION_MAP["throttle"] == ["w", "up"]

    # Brake
    assert SCANCODES["s"] == (0x1F, False)
    assert SCANCODES["down"] == (0x50, True)
    assert NFS_ACTION_MAP["brake"] == ["s", "down"]

    # Steer Left & Right
    assert SCANCODES["a"] == (0x1E, False)
    assert SCANCODES["left"] == (0x4B, True)
    assert SCANCODES["d"] == (0x20, False)
    assert SCANCODES["right"] == (0x4D, True)

    # Handbrake, NOS, Speedbreaker
    assert SCANCODES["space"] == (0x39, False)
    assert SCANCODES["x"] == (0x2D, False)
    assert SCANCODES["lshift"] == (0x2A, False)
    assert SCANCODES["lalt"] == (0x38, False)
    assert SCANCODES["g"] == (0x22, False)


def test_gateway_stateful_nfs_action_and_release():
    """Verify set_nfs_action applies and releases dual keys without redundant events."""
    backend = RecordingBackend()
    gw = Gateway(backend)

    # 1. Throttle ON -> W and UP pressed
    gw.set_nfs_action("throttle", True)
    assert "w" in backend.keys
    assert "up" in backend.keys
    assert len(backend.keys) == 2

    # Redundant call -> no extra events sent
    log_len_before = len(backend.log)
    gw.set_nfs_action("throttle", True)
    assert len(backend.log) == log_len_before

    # 2. Steer Left ON -> A and LEFT pressed
    gw.set_nfs_action("steer_left", True)
    assert "a" in backend.keys
    assert "left" in backend.keys

    # 3. Release Steer Left -> A and LEFT released
    gw.set_nfs_action("steer_left", False)
    assert "a" not in backend.keys
    assert "left" not in backend.keys
    assert "w" in backend.keys  # Throttle still held

    # 4. Release all keys
    gw.release_all_keys()
    assert len(backend.keys) == 0


def test_auto_gas_and_head_tilt_brake():
    """Verify Auto-Gas is held by default and tilting head back triggers Brake/Reverse."""
    backend = RecordingBackend()
    gw = Gateway(backend)
    ctrl = NFSRacingController(gw)

    ctrl.enable()
    # Auto-gas engaged immediately
    assert "w" in backend.keys
    assert "up" in backend.keys
    assert "s" not in backend.keys

    t0 = 1000.0
    # Head neutral (pitch = 0.0)
    ctrl.feed_imu(dx=0.0, dy=0.0, pitch=0.0, roll=0.0, now=t0)
    assert "w" in backend.keys
    assert "s" not in backend.keys

    # Head tilted backward (pitch = +16.0° > 10.0°) -> releases gas, applies brake
    ctrl.feed_imu(dx=0.0, dy=0.0, pitch=16.0, roll=0.0, now=t0 + 0.1)
    assert "w" not in backend.keys
    assert "up" not in backend.keys
    assert "s" in backend.keys
    assert "down" in backend.keys

    # Head returns to neutral (pitch = +2.0° <= 10.0°) -> re-engages gas, releases brake
    ctrl.feed_imu(dx=0.0, dy=0.0, pitch=2.0, roll=0.0, now=t0 + 0.2)
    assert "w" in backend.keys
    assert "up" in backend.keys
    assert "s" not in backend.keys

    ctrl.disable()
    assert len(backend.keys) == 0


def test_proportional_pwm_steering():
    """Verify deadzone, PWM pulsing for moderate tilt, and continuous hold for full tilt."""
    backend = RecordingBackend()
    gw = Gateway(backend)
    ctrl = NFSRacingController(gw)
    ctrl.piezo_triggers_enabled = True
    ctrl.enable()

    t = 2000.0
    # 1. Neutral deadzone: roll = 0.0° (-10°..+10°)
    ctrl.feed_imu(dx=0.0, dy=0.0, pitch=0.0, roll=0.0, now=t)
    assert "a" not in backend.keys
    assert "d" not in backend.keys

    # 2. Moderate left tilt: roll = -16.0° (-22°..-10°) -> PWM left (45ms ON / 35ms OFF)
    # Phase < 45ms (e.g. t = 2000.010 -> 10ms into 80ms cycle) -> ON
    ctrl.feed_imu(dx=0.0, dy=0.0, pitch=0.0, roll=-16.0, now=2000.010)
    assert "a" in backend.keys
    assert "left" in backend.keys

    # Phase >= 45ms (e.g. t = 2000.050 -> 50ms into 80ms cycle) -> OFF
    ctrl.tick(now=2000.050)
    assert "a" not in backend.keys
    assert "left" not in backend.keys

    # 3. Full left tilt: roll = -28.0° (< -22°) -> continuous hold
    ctrl.feed_imu(dx=0.0, dy=0.0, pitch=0.0, roll=-28.0, now=2000.050)
    assert "a" in backend.keys
    assert "left" in backend.keys

    # 4. Moderate right tilt: roll = +15.0° (10°..22°) -> PWM right
    ctrl.feed_imu(dx=0.0, dy=0.0, pitch=0.0, roll=15.0, now=2000.010)
    assert "d" in backend.keys
    assert "right" in backend.keys
    assert "a" not in backend.keys

    # 5. Full right tilt: roll = +30.0° (> 22°) -> continuous hold
    ctrl.feed_imu(dx=0.0, dy=0.0, pitch=0.0, roll=30.0, now=2000.060)
    assert "d" in backend.keys
    assert "right" in backend.keys

    ctrl.disable()
    assert len(backend.keys) == 0


def test_piezo_muscle_triggers_nos_drift_speedbreaker():
    """Verify Jaw Hold (>350ms) triggers NOS, Quick Jaw Clench (<350ms) triggers Handbrake, Forehead triggers Speedbreaker."""
    backend = RecordingBackend()
    gw = Gateway(backend)
    ctrl = NFSRacingController(gw)
    ctrl.piezo_triggers_enabled = True
    ctrl.enable()

    t = 3000.0

    # 1. Single Jaw Clench (<350ms, e.g. 100ms) on CH1 -> Handbrake (SPACE for 180ms)
    ctrl.feed_somatic([0.8, 0.1, 0.1, 0.0], now=t)         # Clench starts
    assert "space" not in backend.keys
    assert "x" not in backend.keys

    ctrl.feed_somatic([0.0, 0.1, 0.1, 0.0], now=t + 0.100) # Clench released quickly (<350ms)
    assert "space" in backend.keys                          # Handbrake drift pulse fires
    assert "x" not in backend.keys

    # After 180ms pulse expires
    ctrl.tick(now=t + 0.300)
    assert "space" not in backend.keys                      # Handbrake released

    # 2. Jaw Hold (>350ms) on CH2 -> Hold NOS (X + LSHIFT + LALT)
    t2 = 4000.0
    ctrl.feed_somatic([0.1, 0.8, 0.1, 0.0], now=t2)         # Clench starts
    ctrl.feed_somatic([0.1, 0.8, 0.1, 0.0], now=t2 + 0.380) # Held >350ms
    assert "x" in backend.keys
    assert "lshift" in backend.keys
    assert "lalt" in backend.keys

    # Release jaw -> NOS released
    ctrl.feed_somatic([0.1, 0.0, 0.1, 0.0], now=t2 + 0.600)
    assert "x" not in backend.keys
    assert "lshift" not in backend.keys
    assert "lalt" not in backend.keys

    # 3. Forehead Eyebrow Raise (CH3 spike) -> Speedbreaker (G for 120ms)
    t3 = 5000.0
    ctrl.feed_somatic([0.1, 0.1, 0.8, 0.0], now=t3)
    assert "g" in backend.keys

    # After 120ms
    ctrl.tick(now=t3 + 0.150)
    assert "g" not in backend.keys

    ctrl.disable()
    assert len(backend.keys) == 0


def test_gyro_zeroing():
    """Verify zero_gyro recalibrates relative pitch and roll baseline."""
    backend = RecordingBackend()
    gw = Gateway(backend)
    ctrl = NFSRacingController(gw)
    ctrl.piezo_triggers_enabled = True
    ctrl.enable()

    t = 6000.0
    # Head resting at +15.0° pitch and +20.0° roll
    ctrl.feed_imu(dx=0.0, dy=0.0, pitch=15.0, roll=20.0, now=t)
    assert ctrl.brake_active is True  # Absolute pitch 15 > 10

    # User re-centers gyro (e.g. clicking [ 🎯 Zero ])
    ctrl.zero_gyro()
    ctrl.feed_imu(dx=0.0, dy=0.0, pitch=15.0, roll=20.0, now=t + 0.02)
    assert ctrl.gas_active is True    # Relative pitch 0 <= 10 -> back to auto-gas!
    assert ctrl.steer_state == "center" # Relative roll 0 -> deadzone!

    ctrl.disable()


def test_nfs_mini_overlay_properties():
    """Verify NFSMiniOverlay window flags, size, and telemetry update methods."""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from host.omniband.app.nfs_overlay import NFSMiniOverlay

    app = QApplication.instance() or QApplication([])

    zero_called = False
    exit_called = False

    def on_zero():
        nonlocal zero_called
        zero_called = True

    def on_exit():
        nonlocal exit_called
        exit_called = True

    overlay = NFSMiniOverlay(on_zero=on_zero, on_exit=on_exit)

    # Size & flags
    assert overlay.width() in (280, 310)
    assert overlay.height() == 75
    flags = overlay.windowFlags()
    assert bool(flags & Qt.WindowType.FramelessWindowHint)
    assert bool(flags & Qt.WindowType.WindowStaysOnTopHint)
    assert bool(flags & Qt.WindowType.Tool)
    assert bool(flags & Qt.WindowType.WindowDoesNotAcceptFocus)

    # Focus policy of buttons
    assert overlay.btn_zero.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert overlay.btn_exit.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert overlay.btn_fallback.focusPolicy() == Qt.FocusPolicy.NoFocus

    # Telemetry updates
    overlay.update_telemetry(gas=True, brake=False, steer_left=True, steer_right=False, nos=True, drift=False)
    assert "00FF66" in overlay.pill_gas.styleSheet()
    assert "00FF66" in overlay.pill_left.styleSheet()
    assert "00E5FF" in overlay.pill_nos.styleSheet()

    # Callbacks
    overlay.btn_zero.click()
    assert zero_called is True

    overlay.btn_exit.click()
    assert exit_called is True

    overlay.close()
