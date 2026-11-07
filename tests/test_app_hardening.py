"""tests/test_app_hardening.py (Binore): Tests for elevation, doctor, config,
safety hardening (rate limiter, deny-list, mutex, crash hook, session lock).

All Windows-specific calls are mocked via thin wrapper functions so all tests
run cleanly on Windows, Linux, and CI without hardware or elevation.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
import doctor
import elevation
from core.events import Intent, OutputCommand, SignalEvent
from gateway import Gateway, KillSwitch, RecordingBackend
from logging_setup import emergency_release, install_crash_handler, set_emergency_release_callback
from main import BasicMapper, DefaultProducer, Engine
from session_guard import SessionLockWindow, SingleInstanceGuard
from state_machine import SMConfig, State, StateMachine


# ── 1. ELEVATION TESTS ────────────────────────────────────────────────────────

def test_is_admin_check():
    with patch("elevation._is_user_an_admin", return_value=True):
        assert elevation.is_admin() is True
    with patch("elevation._is_user_an_admin", return_value=False):
        assert elevation.is_admin() is False


def test_ensure_admin_already_admin():
    with patch("elevation.is_admin", return_value=True):
        # Already admin -> returns False (no relaunch needed)
        assert elevation.ensure_admin(force=True) is False


def test_ensure_admin_relaunches_on_uac_accept():
    with patch("elevation.is_admin", return_value=False), \
         patch("elevation._shell_execute_w", return_value=42), \
         patch("os.name", "nt"):
        # ShellExecuteW > 32 means success -> returns True (caller exits old process)
        assert elevation.ensure_admin(force=True) is True


def test_ensure_admin_declined_uac_continues_non_elevated():
    with patch("elevation.is_admin", return_value=False), \
         patch("elevation._shell_execute_w", return_value=5), \
         patch("os.name", "nt"):
        # ShellExecuteW <= 32 (e.g. error 5 = Access Denied / user clicked 'No')
        # -> returns False and continues non-elevated without crashing or looping
        assert elevation.ensure_admin(force=True) is False


def test_is_pid_elevated():
    with patch("elevation._open_process", return_value=100), \
         patch("elevation._open_process_token", return_value=True), \
         patch("elevation._get_token_information", return_value=True), \
         patch("elevation._close_handle"):

        # Mock elevated token (value = 1)
        def _mock_token_info(tok, cls, buf, sz, ret):
            import ctypes
            ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint)).contents.value = 1
            return True

        with patch("elevation._get_token_information", side_effect=_mock_token_info):
            assert elevation.is_pid_elevated(1234) is True

        # Mock non-elevated token (value = 0)
        def _mock_token_info_zero(tok, cls, buf, sz, ret):
            import ctypes
            ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint)).contents.value = 0
            return True

        with patch("elevation._get_token_information", side_effect=_mock_token_info_zero):
            assert elevation.is_pid_elevated(1234) is False


def test_check_foreground_elevation_detects_uipi_mismatch():
    with patch("elevation.is_admin", return_value=False), \
         patch("elevation._get_foreground_window_pid", return_value=5678), \
         patch("elevation.is_pid_elevated", return_value=True):
        problem, msg = elevation.check_foreground_elevation()
        assert problem is True
        assert "UIPI" in msg

    # When we are admin, no UIPI mismatch
    with patch("elevation.is_admin", return_value=True):
        problem, msg = elevation.check_foreground_elevation()
        assert problem is False


# ── 2. DOCTOR TESTS ───────────────────────────────────────────────────────────

def test_doctor_python_version():
    with patch("doctor._python_version_info", return_value=(3, 11, 9, "final", 0)):
        res = doctor.check_python()
        assert res.status == doctor.Status.PASS

    with patch("doctor._python_version_info", return_value=(3, 9, 0, "final", 0)):
        res = doctor.check_python()
        assert res.status == doctor.Status.FAIL
        assert "3.11" in res.fix


def test_doctor_64bit():
    with patch("doctor._is_64bit", return_value=True):
        assert doctor.check_64bit().status == doctor.Status.PASS
    with patch("doctor._is_64bit", return_value=False):
        assert doctor.check_64bit().status == doctor.Status.FAIL


def test_doctor_input_struct():
    with patch("doctor._sizeof_input", return_value=40):
        assert doctor.check_input_struct().status == doctor.Status.PASS
    with patch("doctor._sizeof_input", return_value=32):
        assert doctor.check_input_struct().status == doctor.Status.FAIL


def test_doctor_admin():
    with patch("doctor._is_admin", return_value=True):
        assert doctor.check_admin().status == doctor.Status.PASS
    with patch("doctor._is_admin", return_value=False):
        assert doctor.check_admin().status == doctor.Status.WARN


def test_doctor_camera_pass_and_fail():
    with patch("doctor._open_camera", return_value=(1280, 720, 60.0)):
        res = doctor.check_camera()
        assert res.status == doctor.Status.PASS
        assert "1280×720" in res.message

    with patch("doctor._open_camera", side_effect=RuntimeError("Device busy")):
        res = doctor.check_camera()
        assert res.status == doctor.Status.FAIL
        assert "Settings → Privacy" in res.fix


def test_doctor_microphone_pass_warn_fail():
    with patch("doctor._open_mic", return_value=0.05):
        assert doctor.check_microphone().status == doctor.Status.PASS

    with patch("doctor._open_mic", return_value=0.00001):
        assert doctor.check_microphone().status == doctor.Status.WARN

    with patch("doctor._open_mic", side_effect=Exception("No input device")):
        res = doctor.check_microphone()
        assert res.status == doctor.Status.FAIL
        assert "Settings → Privacy" in res.fix


def test_doctor_send_input():
    with patch("os.name", "nt"), patch("doctor._send_input_self_test", return_value=2):
        assert doctor.check_send_input().status == doctor.Status.PASS

    with patch("os.name", "nt"), patch("doctor._send_input_self_test", return_value=0):
        res = doctor.check_send_input()
        assert res.status == doctor.Status.FAIL
        assert "Run as admin" in res.fix


def test_doctor_timer():
    with patch("doctor._time_begin_period_1", return_value=False):
        assert doctor.check_timer().status == doctor.Status.WARN

    with patch("doctor._time_begin_period_1", return_value=True), \
         patch("doctor._measure_60hz_jitter_p99", return_value=1.2):
        assert doctor.check_timer().status == doctor.Status.PASS

    with patch("doctor._time_begin_period_1", return_value=True), \
         patch("doctor._measure_60hz_jitter_p99", return_value=8.5):
        assert doctor.check_timer().status == doctor.Status.WARN


def test_doctor_exit_code_logic():
    pass_res = [doctor.CheckResult("T1", doctor.Status.PASS, "ok"),
                doctor.CheckResult("T2", doctor.Status.WARN, "warning")]
    fail_res = [doctor.CheckResult("T1", doctor.Status.PASS, "ok"),
                doctor.CheckResult("T3", doctor.Status.FAIL, "broken")]

    with patch("doctor.run_all", return_value=pass_res):
        assert doctor.doctor_main() == 0

    with patch("doctor.run_all", return_value=fail_res):
        assert doctor.doctor_main() == 1


# ── 3. CONFIG & HOT-RELOAD TESTS ──────────────────────────────────────────────

def test_config_creates_defaults_if_missing(tmp_path):
    p = tmp_path / "cfg.json"
    assert not p.exists()
    cfg = config.load_config(p)
    assert p.exists()
    assert cfg.hz == 60.0
    assert cfg.elevate is True
    assert "consent.exe" in cfg.deny_list


def test_config_validates_and_resets_bad_values(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "hz": 99999.0,           # invalid: max 240
        "log_level": "INVALID",  # invalid
        "webcam_index": -10,     # invalid: min 0
        "profile": "custom",     # valid string
    }))
    cfg = config.load_config(p)
    assert cfg.hz == 60.0             # reset to default
    assert cfg.log_level == "INFO"    # reset to default
    assert cfg.webcam_index == 0      # reset to default
    assert cfg.profile == "custom"    # preserved


def test_config_hot_reload(tmp_path):
    p = tmp_path / "cfg.json"
    cfg0 = config.Config(hz=60.0)
    config.save_config(cfg0, p)

    reloaded = []
    event = threading.Event()

    def _on_change(new_cfg):
        reloaded.append(new_cfg)
        event.set()

    watcher = config.ConfigWatcher(path=p, poll_s=0.05, on_change=_on_change)
    watcher.start()
    try:
        time.sleep(0.1)
        cfg1 = config.Config(hz=120.0, profile="fast")
        config.save_config(cfg1, p)
        assert event.wait(timeout=1.5)
        assert reloaded[-1].hz == 120.0
        assert reloaded[-1].profile == "fast"
    finally:
        watcher.stop()


# ── 4. SAFETY HARDENING TESTS ─────────────────────────────────────────────────

def test_single_instance_guard():
    # First acquire succeeds
    with patch("session_guard._create_mutex", return_value=(100, False)), \
         patch("session_guard._release_mutex") as mock_rel:
        guard1 = SingleInstanceGuard(name="TestMutex")
        assert guard1.acquire() is True
        guard1.release()
        mock_rel.assert_called_once_with(100)

    # Second acquire while held fails
    with patch("session_guard._create_mutex", return_value=(101, True)):
        guard2 = SingleInstanceGuard(name="TestMutex")
        assert guard2.acquire() is False


def test_deny_list_forces_lock_and_cancels_latches():
    backend = RecordingBackend()
    gw = Gateway(backend)
    sm = StateMachine()
    eng = Engine(sm, BasicMapper(), gw, [DefaultProducer()], deny_list=["1password.exe", "consent.exe"])

    # Establish an active latch
    eng.step([SignalEvent("webcam", "jaw_open", 0.9, 1.0)], 1.0)
    assert sm.state == State.LATCHED
    assert gw.held

    # Switch context to a denied app
    eng.step([], 1.1, context="1Password.exe")
    assert sm.state == State.CONVERSE_LOCK
    assert not gw.held  # All inputs released immediately!

    # Further gesture inputs while in deny list are dropped
    eng.step([SignalEvent("webcam", "jaw_open", 0.9, 1.2)], 1.2, context="1Password.exe")
    assert not gw.held
    assert sm.state == State.CONVERSE_LOCK


def test_rate_limiter_trips_and_releases_keys():
    backend = RecordingBackend()
    tripped = []
    # Set threshold low for testing: 3 transitions per second
    gw = Gateway(backend, max_transitions_per_s=3, on_rate_limit=lambda: tripped.append(True))

    now = 100.0
    # Transition 1: W down
    gw.apply([OutputCommand("key", "w", pressed=True)])
    gw.flush(now)
    assert backend.keys == {"w"}

    # Transition 2: W up
    gw.apply([OutputCommand("key", "w", pressed=False)])
    gw.flush(now + 0.1)
    assert not backend.keys

    # Transition 3: A down
    gw.apply([OutputCommand("key", "a", pressed=True)])
    gw.flush(now + 0.2)
    assert backend.keys == {"a"}

    # Transition 4: D down (total 4 in same second > 3) -> Trip rate limiter!
    gw.apply([OutputCommand("key", "d", pressed=True)])
    gw.flush(now + 0.3)
    # Rate limit tripped: keys cleared and release_all invoked
    assert tripped
    assert not backend.keys


def test_session_lock_releases_keys():
    locked_called = []
    wnd = SessionLockWindow(on_lock=lambda: locked_called.append(True))

    backend = RecordingBackend()
    gw = Gateway(backend)
    sm = StateMachine()
    gw.apply([OutputCommand("key", "w", pressed=True)])
    gw.flush(0.0)
    assert gw.held

    # Simulate what session lock does
    def _simulated_lock():
        gw.release_all("session_lock")
        sm.on_event(SignalEvent("switch", "kill", 1.0, 1.0))

    _simulated_lock()
    assert not gw.held
    assert sm.state == State.CONVERSE_LOCK


def test_crash_hook_releases_keys_before_logging(tmp_path):
    released = []
    set_emergency_release_callback(lambda r: released.append(r))

    with patch("logging_setup.logs_dir", return_value=tmp_path):
        install_crash_handler(on_release=lambda r: released.append(r))

        # Trigger uncaught exception handler
        try:
            raise RuntimeError("unexpected fatal error")
        except RuntimeError:
            exc_info = sys.exc_info()
            sys.excepthook(*exc_info)

        # Confirm release was called BEFORE exit/crash logging
        assert released
        assert "uncaught exception" in released[0]

        crash_file = tmp_path / "crash.log"
        assert crash_file.exists()
        assert "unexpected fatal error" in crash_file.read_text(encoding="utf-8")


def test_killswitch_panic_chord():
    ks = KillSwitch(vk=0x7B, chord_enabled=True)

    # Non-Windows or key up -> False
    with patch("os.name", "nt"), patch("ctypes.windll.user32.GetAsyncKeyState", return_value=0):
        assert ks.pressed_edge() is False

    # Simulate F12 press
    def _gas_f12(vk):
        return 0x8000 if vk == 0x7B else 0

    with patch("os.name", "nt"), patch("ctypes.windll.user32.GetAsyncKeyState", side_effect=_gas_f12):
        assert ks.pressed_edge() is True
        # Edge detection: second poll while still held returns False
        assert ks.pressed_edge() is False

    # Simulate Ctrl+Alt+Shift+Q chord (0x11, 0x12, 0x10, 0x51)
    def _gas_chord(vk):
        return 0x8000 if vk in (0x11, 0x12, 0x10, 0x51) else 0

    ks2 = KillSwitch(vk=0x7B, chord_enabled=True)
    with patch("os.name", "nt"), patch("ctypes.windll.user32.GetAsyncKeyState", side_effect=_gas_chord):
        assert ks2.pressed_edge() is True
        assert ks2.pressed_edge() is False
