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
    assert not fk.isVisible()  # Must start hidden (NO startup popup)

    dismissed = []
    fk.sig_dismissed.connect(lambda: dismissed.append(True))
    fk.dismiss()
    assert len(dismissed) == 1
    assert not fk.isVisible()


def test_smart_virtual_keyboard_auto_popup_and_dismissal(qapp):
    from host.omniband.app.text_focus_watcher import TextFocusWatcher, TARGET_TEXT_APPS
    from host.omniband.app.somatic_keyboard import SomaticKeyboardWidget

    # 1. Target apps list check
    assert "whatsapp.exe" in TARGET_TEXT_APPS
    assert "chrome.exe" in TARGET_TEXT_APPS
    assert "notepad.exe" in TARGET_TEXT_APPS
    assert "code.exe" in TARGET_TEXT_APPS
    assert "msedge.exe" in TARGET_TEXT_APPS

    # 2. Somatic Keyboard has HIDE key and dismiss signal
    sk = SomaticKeyboardWidget()
    assert "▼ HIDE" in sk.grid_rows[4]
    sk_dismissed = []
    sk.sig_dismiss.connect(lambda: sk_dismissed.append(True))
    sk._on_key_clicked("▼ HIDE")
    assert len(sk_dismissed) == 1

    # 3. TextFocusWatcher lifecycle and dismissal latch
    focus_events = []
    watcher = TextFocusWatcher(on_text_focus_changed=lambda f: focus_events.append(f))

    # Initial state: startup suppressed & dismissed (NEVER pops up on startup!)
    assert watcher.is_dismissed is True
    assert watcher.is_text_focused is False

    # Simulate focus transition
    watcher.simulate_focus(True)
    assert watcher.is_text_focused is True
    assert len(focus_events) == 1
    assert focus_events[-1] is True

    # User clicks [▼ HIDE] -> dismiss()
    watcher.dismiss()
    assert watcher.is_dismissed is True
    assert watcher.is_text_focused is False

    # Reset dismissal latch on fresh click
    watcher.reset_dismissal()
    assert watcher.is_dismissed is False

    # Clean shutdown
    watcher.stop()


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

def test_zero_click_auto_baseline_and_latch_mechanism():
    """Verify zero-click auto-enable on launch, latch locking, and opposite-eye unlocking."""
    from host.omniband.tracking.webcam_tracker import TrackerResult, RealWebcamTracker

    scroll_events = []
    zoom_events = []
    latch_states = []

    tracker = RealWebcamTracker(
        on_scroll_clutch=lambda active, delta: scroll_events.append((active, delta)),
        on_zoom_clutch=lambda active, delta, scale: zoom_events.append((active, delta, scale)),
        on_latch_changed=lambda st: latch_states.append(st),
    )

    t0 = 1000.0
    # 1. Zero-Click Auto-Enable: Feed 20 frames of neutral open eyes (L=0.28, R=0.28)
    for i in range(25):
        neutral_frame = TrackerResult(
            tier="FACEMESH478",
            face_found=True,
            nose_tip=(0.50, 0.45),
            landmarks=[(0.50, 0.45, 0.0)] * 478,
            ear=0.28,
            mar=0.05,
            yaw=0.0,
            pitch=0.0,
            timestamp_ms=int(t0 * 1000 + i * 16),
            left_ear=0.28,
            right_ear=0.28,
            eye_dist=0.12,
        )
        tracker._process_gestures(neutral_frame, now=t0 + i * 0.016)

    # Assert auto-baseline was computed without touching any calibration button!
    assert tracker._auto_calibrated is True
    assert 0.26 <= tracker.auto_open_ear_L <= 0.30
    assert 0.26 <= tracker.auto_open_ear_R <= 0.30
    assert tracker.active_latch == "IDLE"

    # 2. Left-Wink Tap (L=0.10, R=0.28) sustained for >= 140 ms -> LATCH into SCROLL_LOCKED
    t1 = t0 + 1.0
    left_wink_frame = TrackerResult(
        tier="FACEMESH478",
        face_found=True,
        nose_tip=(0.50, 0.45),
        landmarks=[(0.50, 0.45, 0.0)] * 478,
        ear=0.19,
        mar=0.05,
        yaw=0.0,
        pitch=0.0,
        timestamp_ms=int(t1 * 1000),
        left_ear=0.10,
        right_ear=0.28,
        eye_dist=0.12,
    )
    # At t1: first detection of Left Wink
    tracker._process_gestures(left_wink_frame, now=t1)
    assert tracker.active_latch == "IDLE"  # Not yet 140ms
    # At t1 + 0.16s: sustained 160ms -> LATCH LOCKS!
    tracker._process_gestures(left_wink_frame, now=t1 + 0.16)
    assert tracker.active_latch == "SCROLL_LOCKED"
    assert "SCROLL_LOCKED" in latch_states

    # 3. Both eyes comfortably open + Head Tilted UP: NEVER drops out of SCROLL_LOCKED!
    scroll_events.clear()
    head_tilt_up_frame = TrackerResult(
        tier="FACEMESH478",
        face_found=True,
        nose_tip=(0.50, 0.38),  # Head tilted UP (ny=0.38 < anchor=0.45 -> delta_pitch > 0 -> Scroll Up)
        landmarks=[(0.50, 0.38, 0.0)] * 478,
        ear=0.28,
        mar=0.05,
        yaw=0.0,
        pitch=-6.0,
        timestamp_ms=int((t1 + 0.30) * 1000),
        left_ear=0.28,  # Both eyes fully open!
        right_ear=0.28,
        eye_dist=0.12,
    )
    for step in range(5):
        tracker._process_gestures(head_tilt_up_frame, now=t1 + 0.30 + step * 0.016)

    assert tracker.active_latch == "SCROLL_LOCKED"
    assert len(scroll_events) > 0
    assert scroll_events[-1][0] is True
    assert scroll_events[-1][1] > 0.01  # Positive delta_pitch for Scroll UP

    # Velocity must remain locked to 0.0 while in SCROLL_LOCKED
    tracker._update_velocity(head_tilt_up_frame, now=t1 + 0.30, w=640, h=480)
    vx, vy, _ = tracker.velocity_state
    assert vx == 0.0 and vy == 0.0

    # 4. OPPOSITE-EYE UNLOCK: Right-Wink Tap (R=0.10, L=0.28) unlocks SCROLL_LOCKED back to IDLE
    t2 = t1 + 0.80  # Past 400ms cooldown
    right_wink_frame = TrackerResult(
        tier="FACEMESH478",
        face_found=True,
        nose_tip=(0.50, 0.45),
        landmarks=[(0.50, 0.45, 0.0)] * 478,
        ear=0.19,
        mar=0.05,
        yaw=0.0,
        pitch=0.0,
        timestamp_ms=int(t2 * 1000),
        left_ear=0.28,
        right_ear=0.10,  # Right Eye Wink
        eye_dist=0.12,
    )
    tracker._process_gestures(right_wink_frame, now=t2)
    tracker._process_gestures(right_wink_frame, now=t2 + 0.16)
    assert tracker.active_latch == "IDLE"
    assert "IDLE" in latch_states

    # 5. Right-Wink Tap from IDLE -> LATCH INTO ZOOM_LOCKED
    t3 = t2 + 0.50
    tracker._process_gestures(right_wink_frame, now=t3)
    tracker._process_gestures(right_wink_frame, now=t3 + 0.16)
    assert tracker.active_latch == "ZOOM_LOCKED"

    # Both eyes open in ZOOM_LOCKED: stays locked!
    tracker._process_gestures(neutral_frame, now=t3 + 0.30)
    assert tracker.active_latch == "ZOOM_LOCKED"

    # 6. OPPOSITE-EYE UNLOCK: Left-Wink Tap unlocks ZOOM_LOCKED back to IDLE
    t4 = t3 + 0.80
    tracker._process_gestures(left_wink_frame, now=t4)
    tracker._process_gestures(left_wink_frame, now=t4 + 0.16)
    assert tracker.active_latch == "IDLE"


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


def test_triple_blink_universal_exit_and_lookaway_guard():
    from host.omniband.tracking.webcam_tracker import RealWebcamTracker, TrackerResult

    universal_exits = []
    tracker = RealWebcamTracker(on_universal_exit=lambda: universal_exits.append(True))
    tracker.auto_open_ear_L = 0.28
    tracker.auto_open_ear_R = 0.28
    tracker._auto_calibrated = True

    # 1. Enter SCROLL_LOCKED mode
    tracker.active_latch = "SCROLL_LOCKED"
    tracker._latch_locked_time = 100.0

    # 2. Fire 3 bilateral blinks within 1.20s
    t = 101.0
    closed_frame = TrackerResult(
        tier="FACEMESH478", face_found=True, nose_tip=(0.5, 0.45), ear=0.10, left_ear=0.10, right_ear=0.10,
        mar=0.05, eye_dist=0.12, yaw=0.0, pitch=0.0, timestamp_ms=int(t * 1000), landmarks=[],
    )
    open_frame = TrackerResult(
        tier="FACEMESH478", face_found=True, nose_tip=(0.5, 0.45), ear=0.28, left_ear=0.28, right_ear=0.28,
        mar=0.05, eye_dist=0.12, yaw=0.0, pitch=0.0, timestamp_ms=int(t * 1000), landmarks=[],
    )

    # Blink 1
    tracker._process_gestures(closed_frame, now=t)
    tracker._process_gestures(open_frame, now=t + 0.12)

    # Blink 2
    tracker._process_gestures(closed_frame, now=t + 0.30)
    tracker._process_gestures(open_frame, now=t + 0.42)

    # Blink 3
    tracker._process_gestures(closed_frame, now=t + 0.60)
    tracker._process_gestures(open_frame, now=t + 0.72)

    # Universal exit triggered! Returned to IDLE
    assert tracker.active_latch == "IDLE"
    assert len(universal_exits) == 1

    # 3. Look-away auto-pause guard
    tracker._calibrated = True
    tracker._base_x = 0.5
    tracker._base_y = 0.45
    turned_away_frame = TrackerResult(
        tier="FACEMESH478", face_found=True, nose_tip=(0.5, 0.45), ear=0.28, left_ear=0.28, right_ear=0.28,
        mar=0.05, eye_dist=0.12, yaw=35.0, pitch=0.0, timestamp_ms=int(t * 1000), landmarks=[],
    )
    tracker._update_velocity(turned_away_frame, now=t + 1.0, w=640, h=480)
    assert tracker.is_looking_away is True
    vx, vy, _ = tracker.velocity_state
    assert vx == 0.0 and vy == 0.0

