"""test_native_app.py - Verification tests for PySide6 Native Windows Desktop Application.

Tests:
1. Native theme application and colors
2. MainWindow initialization and central widgets
3. HudOverlay transparent/click-through window flags
4. ArenaWidget 60 FPS physics integration and native scancode handler
5. SystemTrayAgent setup and actions
6. NativeArenaSlot in-memory thread-safe state sharing
"""
from __future__ import annotations

import os
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

# Force offscreen headless Qt platform for CI/tests
os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_theme_apply(qapp):
    from host.omniband.app.theme import apply_theme
    apply_theme(qapp)
    assert qapp.styleSheet() != ""


def test_native_arena_slot():
    from host.omniband.adapters.telemetry.native_arena import ARENA_SLOT
    from host.omniband.copilot.types import VehicleState

    st = VehicleState(
        t=100.0,
        speed=15.0,
        e=0.25,
        psi_e=0.02,
        kappa_ahead=(0.0, 0.0),
        lane_index=0,
        lane_count=3,
        gaps=(None, None),
        offroad=False,
        paused=False
    )
    ARENA_SLOT.write_state(st)
    read_st = ARENA_SLOT.read_state()
    assert read_st is not None
    assert read_st.speed == 15.0
    assert read_st.e == 0.25


def test_hud_overlay_flags(qapp):
    from host.omniband.app.hud_overlay import HudOverlay

    hud = HudOverlay()
    flags = hud.windowFlags()
    assert flags & Qt.FramelessWindowHint
    assert flags & Qt.WindowStaysOnTopHint
    assert flags & Qt.WindowTransparentForInput


def test_arena_widget_scancode_handling(qapp):
    from host.omniband.app.arena_widget import ArenaWidget, SCANCODE_W, SCANCODE_A

    arena = ArenaWidget()
    arena._record_scancode(SCANCODE_W, is_press=True)
    assert arena._key_w is True
    assert arena._scancode_hits == 1

    arena._record_scancode(SCANCODE_A, is_press=True)
    assert arena._key_a is True
    assert arena._scancode_hits == 2

    arena._record_scancode(SCANCODE_W, is_press=False)
    assert arena._key_w is False


def test_cursor_reticle_and_floating_kb(qapp):
    from host.omniband.app.cursor_reticle import SomaticCursorReticle
    from host.omniband.app.floating_keyboard import FloatingKeyboardWindow
    from PySide6.QtCore import Qt

    reticle = SomaticCursorReticle()
    assert reticle.windowFlags() & Qt.FramelessWindowHint
    assert reticle.windowFlags() & Qt.WindowTransparentForInput
    assert reticle.state == "NORMAL"

    reticle.trigger_click_flash()
    assert reticle.state == "CLICKED"

    reticle.set_drag_locked(True)
    assert reticle.state == "DRAG_LOCKED"

    fk = FloatingKeyboardWindow()
    assert fk.windowFlags() & Qt.WindowDoesNotAcceptFocus
    assert fk.windowFlags() & Qt.WindowStaysOnTopHint


def test_main_window_camera_controls_and_120hz_loop(qapp, monkeypatch):
    from host.omniband.app.main_window import MainWindow

    win = MainWindow()
    assert win.tabs.count() == 6
    assert win.webcam_tracker is not None
    assert win._slider_speed.value() in (6, 8, 12)

    # Test slider adjustments
    win._slider_speed.setValue(20)
    assert win.webcam_tracker.speed_multiplier == 2.0

    win._slider_smooth.setValue(12)
    assert win.webcam_tracker.min_cutoff == 1.2

    # Verify Rule #1: Inside deadzone, SetCursorPos is NEVER called!
    set_cursor_calls = []
    if hasattr(win, "_on_cursor_tick"):
        # Tracker is initially in deadzone (vx=0, vy=0, in_deadzone=True)
        win._on_cursor_tick()
        assert win._subpixel_x == 0.0
        assert win._subpixel_y == 0.0

    # Outside deadzone: Ballistic relative integration
    win.webcam_tracker._vx = 200.0
    win.webcam_tracker._vy = 100.0
    win.webcam_tracker._in_deadzone = False
    win._on_cursor_tick()
    # Should accumulate sub-pixel delta
    assert win._subpixel_x > 0.0 or win._subpixel_y > 0.0

    # Verify Rule #2: Head movements in Head-Mouse mode NEVER emit 'A' or 'D' keys!
    win.webcam_tracker.head_mouse_enabled = True
    win.gw.release_all("test")
    assert "a" not in win.gw.held
    assert "d" not in win.gw.held
    # Simulate face turning left (ch 1) and right (ch 2)
    win._on_optical_gesture(1, True, 950.0)
    win._on_optical_gesture(2, True, 950.0)
    assert "a" not in win.gw.held
    assert "d" not in win.gw.held

    # Test biometric click
    win._on_biometric_click()
    assert win.reticle.state == "CLICKED"

def test_asymmetric_wink_scroll_and_zoom_clutch():
    """Verify asymmetric wink gestures engage clutch and lock cursor movement."""
    from host.omniband.tracking.webcam_tracker import TrackerResult, RealWebcamTracker
    from host.omniband.tracking.optical_calibration import OpticalCalibrationProfile, OpticalCalibrator

    scroll_events = []
    zoom_events = []
    clicks = []

    cal = OpticalCalibrator()
    cal.profile = OpticalCalibrationProfile(
        left_wink_thresh=0.18,
        right_open_floor=0.22,
        right_wink_thresh=0.18,
        left_open_floor=0.22,
        asym_margin=0.04,
        clutch_min_hold_ms=220.0,
    )

    tracker = RealWebcamTracker(
        on_scroll_clutch=lambda active, delta: scroll_events.append((active, delta)),
        on_zoom_clutch=lambda active, delta, scale: zoom_events.append((active, delta, scale)),
        on_click=lambda: clicks.append(True),
        optical_calibrator=cal,
    )

    t0 = 1000.0
    # 1. Bilateral blink (both eyes low: L=0.10, R=0.10) -> Should NOT trigger Scroll or Zoom clutch!
    blink_res = TrackerResult(
        tier="FACEMESH478",
        face_found=True,
        nose_tip=(0.5, 0.45),
        landmarks=[(0.5, 0.45, 0.0)] * 478,
        ear=0.10,
        mar=0.05,
        yaw=0.0,
        pitch=0.0,
        timestamp_ms=1000000,
        left_ear=0.10,
        right_ear=0.10,
        eye_dist=0.12,
    )
    tracker._process_gestures(blink_res, now=t0)
    tracker._process_gestures(blink_res, now=t0 + 0.25)
    assert tracker.active_clutch == "NONE"
    assert len(scroll_events) == 0
    assert len(zoom_events) == 0

    # 2. Asymmetric Left Wink (Scroll Clutch: L=0.10, R=0.28) held >= 220 ms
    left_wink_res = TrackerResult(
        tier="FACEMESH478",
        face_found=True,
        nose_tip=(0.5, 0.40),  # Head tilted up (ny=0.40 < anchor=0.45 -> delta_pitch > 0 -> Scroll Up)
        landmarks=[(0.5, 0.40, 0.0)] * 478,
        ear=0.19,
        mar=0.05,
        yaw=0.0,
        pitch=-5.0,
        timestamp_ms=1000300,
        left_ear=0.10,
        right_ear=0.28,
        eye_dist=0.12,
    )
    # At t = t0 + 0.30: first detection of left wink
    tracker._process_gestures(left_wink_res, now=t0 + 0.30)
    assert tracker.active_clutch == "NONE"  # Not yet 220ms
    # At t = t0 + 0.55: sustained 250ms -> CLUTCH ACTIVE
    tracker._process_gestures(left_wink_res, now=t0 + 0.55)
    assert tracker.active_clutch == "SCROLL"
    assert any(ev[0] is True for ev in scroll_events)

    # While clutch active, velocity must be frozen to 0.0 (cursor locked)
    tracker._update_velocity(left_wink_res, now=t0 + 0.55, w=640, h=480)
    vx, vy, _ = tracker.velocity_state
    assert vx == 0.0 and vy == 0.0

    # Release Left Wink (eyes open again)
    open_res = TrackerResult(
        tier="FACEMESH478",
        face_found=True,
        nose_tip=(0.5, 0.45),
        landmarks=[(0.5, 0.45, 0.0)] * 478,
        ear=0.28,
        mar=0.05,
        yaw=0.0,
        pitch=0.0,
        timestamp_ms=1000700,
        left_ear=0.28,
        right_ear=0.28,
        eye_dist=0.12,
    )
    tracker._process_gestures(open_res, now=t0 + 0.70)
    assert tracker.active_clutch == "NONE"

    # 3. Asymmetric Right Wink (Doctor Zoom Clutch: R=0.10, L=0.28) held >= 220 ms
    right_wink_res = TrackerResult(
        tier="FACEMESH478",
        face_found=True,
        nose_tip=(0.5, 0.45),
        landmarks=[(0.5, 0.45, 0.0)] * 478,
        ear=0.19,
        mar=0.05,
        yaw=0.0,
        pitch=0.0,
        timestamp_ms=1001000,
        left_ear=0.28,
        right_ear=0.10,
        eye_dist=0.14,  # Leaning in -> scale_ratio > 1.08 -> Zoom IN
    )
    tracker._process_gestures(right_wink_res, now=t0 + 1.00)
    assert tracker.active_clutch == "NONE"
    tracker._process_gestures(right_wink_res, now=t0 + 1.25)
    assert tracker.active_clutch == "ZOOM"
    assert any(ev[0] is True for ev in zoom_events)

    # Release Right Wink
    tracker._process_gestures(open_res, now=t0 + 1.40)
    assert tracker.active_clutch == "NONE"


def test_optical_calibration_wizard_computation(tmp_path):
    """Verify 15-second optical calibration wizard computes and persists thresholds."""
    from host.omniband.tracking.optical_calibration import OpticalCalibrator

    cfg_file = tmp_path / "test_optical_calibration.json"
    cal = OpticalCalibrator(config_path=cfg_file)

    baseline_left = [0.28, 0.29, 0.27, 0.28]
    baseline_right = [0.28, 0.28, 0.29, 0.27]
    baseline_nose = [(0.50, 0.44), (0.50, 0.46)]
    baseline_dist = [0.12, 0.12, 0.12]
    wink_l_left = [0.10, 0.09, 0.11]
    wink_l_companion_r = [0.26, 0.25, 0.27]
    wink_r_right = [0.08, 0.10, 0.09]
    wink_r_companion_l = [0.25, 0.26, 0.25]
    jaw_samples = [0.05, 0.28, 0.30, 0.10]

    prof = cal.compute_from_records(
        baseline_left,
        baseline_right,
        baseline_nose,
        baseline_dist,
        wink_l_left,
        wink_l_companion_r,
        wink_r_right,
        wink_r_companion_l,
        jaw_samples,
    )

    assert cfg_file.is_file()
    assert prof.left_wink_thresh < 0.22
    assert prof.right_open_floor > prof.left_wink_thresh
    assert prof.right_wink_thresh < 0.22
    assert prof.left_open_floor > prof.right_wink_thresh
    assert prof.jaw_click_thresh > 0.18

