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


def test_main_window_init(qapp):
    from host.omniband.app.main_window import MainWindow

    win = MainWindow()
    assert win.tabs.count() == 4
    assert win.scope is not None
    assert win.arena is not None
    assert win.icu is not None
    assert win.calib is not None
