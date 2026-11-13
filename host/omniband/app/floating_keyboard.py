"""floating_keyboard.py - Sleek, Bottom-Docked Floating Always-On-Top Keyboard Window.

Applies Qt.WindowDoesNotAcceptFocus | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint
Plus Win32 WS_EX_NOACTIVATE | WS_EX_TOPMOST so looking at keys, clicking, or dwelling
types directly into Notepad, Chrome, VS Code, or WhatsApp WITHOUT stealing focus!

Includes:
- Full QWERTY keyboard with live word predictions
- Drag / Hold Lock toggle button
- Scroll Up & Scroll Down hover pads
- 1-click 'Open Windows Native OSK' launcher
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
from typing import Callable, Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QApplication
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor

from host.omniband.app.theme import (
    SURFACE_0, SURFACE_1, SURFACE_2, SURFACE_3, BURNT_ORANGE,
    STATUS_OK, STATUS_ERR, INDUSTRIAL_IVORY, PURE_WHITE
)
from host.omniband.app.somatic_keyboard import SomaticKeyboardWidget

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008


class FloatingKeyboardWindow(QWidget):
    """Floating non-activating always-on-top virtual keyboard."""

    sig_scroll = Signal(int)  # +1 for up, -1 for down
    sig_toggle_drag = Signal()

    def __init__(
        self,
        on_type_fn: Optional[Callable[[str], None]] = None,
        on_scroll_fn: Optional[Callable[[int], None]] = None,
        on_toggle_drag_fn: Optional[Callable[[], None]] = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.on_type_fn = on_type_fn
        self.on_scroll_fn = on_scroll_fn
        self.on_toggle_drag_fn = on_toggle_drag_fn

        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.resize(920, 310)

        # Position docked above the Taskbar work area
        self._dock_above_taskbar()

        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Left / Center: Somatic Keyboard
        kb_container = QWidget()
        kb_layout = QVBoxLayout(kb_container)
        kb_layout.setContentsMargins(0, 0, 0, 0)
        kb_layout.setSpacing(4)

        # Top Bar inside floating window
        top_bar = QHBoxLayout()
        lbl_title = QLabel("⌨️ SOMATIC FLOATING KEYBOARD (NO-FOCUS STEAL)")
        lbl_title.setFont(QFont("Segoe UI", 9, QFont.Bold))
        lbl_title.setStyleSheet(f"color: {BURNT_ORANGE};")

        btn_osk = QPushButton("Open Windows OSK")
        btn_osk.setFont(QFont("Segoe UI", 8))
        btn_osk.clicked.connect(self._launch_windows_osk)

        btn_close = QPushButton("✕ Hide")
        btn_close.setFont(QFont("Segoe UI", 8, QFont.Bold))
        btn_close.clicked.connect(self.hide)

        top_bar.addWidget(lbl_title)
        top_bar.addStretch()
        top_bar.addWidget(btn_osk)
        top_bar.addWidget(btn_close)
        kb_layout.addLayout(top_bar)

        self.somatic_kb = SomaticKeyboardWidget(on_type_fn=self.on_type_fn)
        kb_layout.addWidget(self.somatic_kb)
        root.addWidget(kb_container, stretch=5)

        # Right Side: Action Dock (Drag Lock + Scroll Pads)
        action_dock = QVBoxLayout()
        action_dock.setSpacing(6)

        self.btn_drag = QPushButton("🔒 DRAG\nHOLD")
        self.btn_drag.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.btn_drag.setMinimumWidth(80)
        self.btn_drag.setMinimumHeight(60)
        self.btn_drag.setStyleSheet(f"background: {SURFACE_2}; color: {BURNT_ORANGE}; border: 1px solid #555; border-radius: 4px;")
        self.btn_drag.clicked.connect(self._on_drag_clicked)

        self.btn_scroll_up = QPushButton("▲ SCROLL\nUP")
        self.btn_scroll_up.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.btn_scroll_up.setMinimumWidth(80)
        self.btn_scroll_up.setMinimumHeight(70)
        self.btn_scroll_up.setStyleSheet(f"background: {SURFACE_2}; color: {INDUSTRIAL_IVORY}; border: 1px solid #555; border-radius: 4px;")
        self.btn_scroll_up.clicked.connect(lambda: self._on_scroll(120))

        self.btn_scroll_down = QPushButton("▼ SCROLL\nDOWN")
        self.btn_scroll_down.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.btn_scroll_down.setMinimumWidth(80)
        self.btn_scroll_down.setMinimumHeight(70)
        self.btn_scroll_down.setStyleSheet(f"background: {SURFACE_2}; color: {INDUSTRIAL_IVORY}; border: 1px solid #555; border-radius: 4px;")
        self.btn_scroll_down.clicked.connect(lambda: self._on_scroll(-120))

        action_dock.addWidget(self.btn_drag)
        action_dock.addWidget(self.btn_scroll_up)
        action_dock.addWidget(self.btn_scroll_down)
        root.addLayout(action_dock, stretch=1)

        self.setStyleSheet(f"background-color: rgba(24, 26, 27, 0.95); border: 2px solid {BURNT_ORANGE}; border-radius: 8px;")

    def _dock_above_taskbar(self) -> None:
        """Position window docked cleanly ABOVE the Taskbar work area.

        Uses primaryScreen().availableGeometry() (not full geometry)
        so it never covers the bottom 48-60px Windows Taskbar, Start button, or tray!
        """
        screen = QApplication.primaryScreen()
        kb_w, kb_h = self.width(), self.height()
        if screen:
            avail = screen.availableGeometry()
            dock_x = avail.x() + (avail.width() - kb_w) // 2
            # 12px padding above taskbar
            dock_y = avail.y() + avail.height() - kb_h - 12
            self.move(dock_x, dock_y)
        elif sys.platform == "win32":
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            self.move((sw - kb_w) // 2, sh - kb_h - 60)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._dock_above_taskbar()
        self._apply_win32_noactivate()

    def _apply_win32_noactivate(self) -> None:
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
        except Exception:
            pass

    def nativeEvent(self, event_type, message):  # noqa: N802
        if sys.platform == "win32" and event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0021:  # WM_MOUSEACTIVATE
                # Return MA_NOACTIVATE (3) so clicking never steals focus from target app
                return True, 3
        return super().nativeEvent(event_type, message)

    def _on_drag_clicked(self) -> None:
        if self.on_toggle_drag_fn:
            self.on_toggle_drag_fn()
        self.sig_toggle_drag.emit()

    def _on_scroll(self, delta: int) -> None:
        if self.on_scroll_fn:
            self.on_scroll_fn(delta)
        self.sig_scroll.emit(delta)

    def _launch_windows_osk(self) -> None:
        try:
            subprocess.Popen("osk.exe", shell=True)
        except Exception:
            pass

