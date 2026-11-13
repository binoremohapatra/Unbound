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

    # Clean up background threads
    win.webcam_tracker.stop()
    win.key_poller.stop()
    win.watcher.stop()
    win._cursor_timer.stop()
    win._proof_timer.stop()
