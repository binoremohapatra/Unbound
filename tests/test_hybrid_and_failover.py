"""test_hybrid_and_failover.py - Tests for Hybrid Mode, Piezo Visual Telemetry Only, and 5-Blink Failover."""
import time
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from host.omniband.app.main_window import MainWindow
from host.omniband.app.nfs_overlay import NFSMiniOverlay
from host.omniband.tracking.webcam_tracker import RealWebcamTracker
from host.omniband.wifi_transport import UnifiedGestureEngine, OmniBandWifiClient


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def mock_tracker_start(monkeypatch):
    """Prevent opening real camera capture threads during headless testing."""
    monkeypatch.setattr(RealWebcamTracker, "start", lambda self: None)


def test_unified_gesture_engine_piezo_clicks_disabled_by_default_in_wifi():
    """Verify that WiFi somatic client initializes UnifiedGestureEngine with piezo clicks disabled."""
    actions = []
    client = OmniBandWifiClient(on_gesture_action=lambda act, val: actions.append((act, val)))
    assert client.gesture_engine.enable_piezo_clicks is False

    t = 100.0
    # Feed massive jaw clenches
    client.gesture_engine.feed_somatic([1.0, 1.0, 0.0, 0.0], now=t)
    client.gesture_engine.feed_somatic([0.0, 0.0, 0.0, 0.0], now=t + 0.1)
    client.gesture_engine.poll(now=t + 0.5)

    # Must NOT emit any click/mouse_down/action
    assert len(actions) == 0


def test_main_window_piezo_visual_telemetry_only_no_gateway_clicks(qapp):
    """Verify somatic frames update oscilloscope and status pills, but NEVER fire OS clicks."""
    win = MainWindow()
    qapp.processEvents()

    # Clear proof label
    win._lbl_proof_last.setText("LAST: None")

    # Feed high somatic frames (Bite/Jaw spike)
    high_vals = [0.95, 0.85, 0.10, 0.90]
    active_flags = [True, True, False, True]
    win._on_ble_frame_event(high_vals, active_flags, seq=1)
    qapp.processEvents()

    # Status pills updated
    assert "0.95" in win._pill_ch1.text()
    assert "0.85" in win._pill_ch2.text()
    assert "CLENCH" in win._pill_ch4.text()

    # Proof label must NOT be a jaw click
    assert "Jaw -> Left Click" not in win._lbl_proof_last.text()
    assert "Jaw Clench -> LEFT CLICK" not in win._lbl_proof_last.text()

    win.stop()
    win.close()


def test_5_blink_failover_to_nose_tracking(qapp):
    """Verify 5 blinks within 3.0s triggers failover to nose tracking and updates badge."""
    win = MainWindow()
    qapp.processEvents()

    assert win.hardware_mpu_disabled is False
    t0 = 1000.0

    # 4 blinks -> not yet failover
    for i in range(4):
        win._on_webcam_blink_event(t0 + i * 0.4)
    assert win.hardware_mpu_disabled is False

    # 5th blink within 3.0s -> FAILOVER TRIGGERED!
    win._on_webcam_blink_event(t0 + 4 * 0.4)
    qapp.processEvents()

    assert win.hardware_mpu_disabled is True
    assert win.webcam_tracker.head_mouse_enabled is True
    assert "FALLBACK ACTIVE: NOSE + BLINK MODE" in win._lbl_tier.text()
    assert "NOSE FALLBACK ACTIVE" in win._btn_fallback_toggle.text()

    win.stop()
    win.close()


def test_5_blink_window_expiration(qapp):
    """Verify blinks older than 3.0s do not count towards the 5-blink failover."""
    win = MainWindow()
    qapp.processEvents()

    t0 = 2000.0
    win._on_webcam_blink_event(t0)
    win._on_webcam_blink_event(t0 + 0.5)
    win._on_webcam_blink_event(t0 + 1.0)
    # 4th and 5th blink arrive after 3.5s gap
    win._on_webcam_blink_event(t0 + 4.5)
    win._on_webcam_blink_event(t0 + 5.0)

    # Only 2 blinks within rolling 3.0s -> failover should NOT fire
    assert win.hardware_mpu_disabled is False

    win.stop()
    win.close()


def test_1_click_toggle_mpu_and_nose_fallback(qapp):
    """Verify 1-click toggle switches between Hybrid MPU mode and Nose Fallback."""
    win = MainWindow()
    qapp.processEvents()

    assert win.hardware_mpu_disabled is False

    # 1-Click toggle -> to Nose Fallback
    win._btn_fallback_toggle.click()
    qapp.processEvents()
    assert win.hardware_mpu_disabled is True
    assert win.webcam_tracker.head_mouse_enabled is True
    assert "FALLBACK ACTIVE: NOSE + BLINK MODE" in win._lbl_tier.text()

    # 1-Click toggle again -> back to Hybrid MPU mode
    win._btn_fallback_toggle.click()
    qapp.processEvents()
    assert win.hardware_mpu_disabled is False
    assert "HYBRID" in win._btn_fallback_toggle.text() or "MPU" in win._btn_fallback_toggle.text()

    win.stop()
    win.close()


def test_mpu_motion_ignored_when_fallback_active(qapp):
    """Verify Wi-Fi MPU deltas are discarded when failover / fallback is active."""
    win = MainWindow()
    qapp.processEvents()

    win._set_hardware_mpu_disabled(True, reason="Test")
    win._last_mpu_motion_time = 0.0

    # Feed MPU motion
    win._on_imu_motion_event(10.0, -15.0)
    assert win._last_mpu_motion_time == 0.0  # Motion dropped!

    # Restore Hybrid MPU
    win._set_hardware_mpu_disabled(False, reason="Test")
    win._on_imu_motion_event(10.0, -15.0)
    assert win._last_mpu_motion_time > 0.0  # Motion processed!

    win.stop()
    win.close()


def test_nfs_mini_overlay_toggle_button(qapp):
    """Verify toggle button on NFS Mini Overlay switches fallback state."""
    toggled = False

    def on_toggle():
        nonlocal toggled
        toggled = True

    overlay = NFSMiniOverlay(on_toggle_fallback=on_toggle)
    assert overlay.btn_fallback.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert "MPU" in overlay.btn_fallback.text()

    overlay.btn_fallback.click()
    assert toggled is True

    # Test visual state updates
    overlay.set_fallback_mode(True)
    assert "NOSE" in overlay.btn_fallback.text()
    assert "D500F9" in overlay.btn_fallback.styleSheet()

    overlay.set_fallback_mode(False)
    assert "MPU" in overlay.btn_fallback.text()
    assert "00FF66" in overlay.btn_fallback.styleSheet()
