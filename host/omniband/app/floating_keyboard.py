"""floating_keyboard.py - Native Touch Keyboard Integration Bridge.

Architectural Refactor:
Custom QWidget floating keyboard stripped out and replaced with Microsoft
Windows official Native Touch Keyboard (TabTip.exe).
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Optional

from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from host.omniband.app.theme import BURNT_ORANGE, SURFACE_2
from host.omniband.app.touch_keyboard import show_windows_touch_keyboard, hide_windows_touch_keyboard


class FloatingKeyboardWindow(QWidget):
    """Bridge for Windows Native Touch Keyboard (TabTip.exe)."""

    sig_scroll = Signal(int)
    sig_toggle_drag = Signal()
    sig_zoom = Signal(int)
    sig_zoom_reset = Signal()
    sig_dismissed = Signal()

    def __init__(
        self,
        on_type_fn: Optional[Callable[[str], None]] = None,
        on_scroll_fn: Optional[Callable[[int], None]] = None,
        on_toggle_drag_fn: Optional[Callable[[], None]] = None,
        on_zoom_fn: Optional[Callable[[int], None]] = None,
        on_zoom_reset_fn: Optional[Callable[[], None]] = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.on_type_fn = on_type_fn
        self.on_scroll_fn = on_scroll_fn
        self.on_toggle_drag_fn = on_toggle_drag_fn
        self.on_zoom_fn = on_zoom_fn
        self.on_zoom_reset_fn = on_zoom_reset_fn
        self._last_external_hwnd: int = 0

        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.resize(320, 100)

        layout = QVBoxLayout(self)
        lbl = QLabel("⌨️ Windows Native Touch Keyboard (TabTip.exe) Active")
        lbl.setFont(QFont("Segoe UI", 9, QFont.Bold))
        lbl.setStyleSheet(f"color: {BURNT_ORANGE};")
        lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl)

        btn_launch = QPushButton("Open Windows Touch Keyboard")
        btn_launch.setFont(QFont("Segoe UI", 8))
        btn_launch.clicked.connect(show_windows_touch_keyboard)
        layout.addWidget(btn_launch)

        self.hide()

    def show(self) -> None:
        # Instead of rendering a custom QWidget on screen, invoke Microsoft Native Touch Keyboard!
        show_windows_touch_keyboard()

    def dismiss(self) -> None:
        hide_windows_touch_keyboard()
        if self.isVisible():
            self.hide()
        else:
            self.sig_dismissed.emit()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.sig_dismissed.emit()

    def set_last_external_hwnd(self, hwnd: int) -> None:
        self._last_external_hwnd = hwnd

    def get_last_external_hwnd(self) -> int:
        return self._last_external_hwnd
