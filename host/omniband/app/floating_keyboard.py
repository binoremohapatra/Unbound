"""floating_keyboard.py - Sleek, Bottom-Docked Floating Always-On-Top Keyboard Window.

Applies Qt.WindowDoesNotAcceptFocus | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint
Plus Win32 WS_EX_NOACTIVATE | WS_EX_TOPMOST so looking at keys, clicking, or dwelling
types directly into Notepad, Chrome, VS Code, or WhatsApp WITHOUT stealing focus!

Includes:
- Full QWERTY keyboard with live word predictions
- Drag / Hold Lock toggle button
- Scroll Up & Scroll Down hover pads
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
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
WS_EX_TOOLWINDOW = 0x00000080


class FloatingKeyboardWindow(QWidget):
    """Floating non-activating always-on-top virtual keyboard."""

    sig_scroll = Signal(int)  # +1 for up, -1 for down
    sig_toggle_drag = Signal()
    sig_zoom = Signal(int)  # +1 for in, -1 for out
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
        self.resize(940, 340)

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

        btn_close = QPushButton("▼ HIDE")
        btn_close.setFocusPolicy(Qt.NoFocus)
        btn_close.setFont(QFont("Segoe UI", 8, QFont.Bold))
        btn_close.setStyleSheet(f"background: {SURFACE_2}; color: {BURNT_ORANGE}; border: 1px solid #555; border-radius: 4px; padding: 2px 8px;")
        btn_close.clicked.connect(self.dismiss)

        top_bar.addWidget(lbl_title)
        top_bar.addStretch()
        top_bar.addWidget(btn_close)
        kb_layout.addLayout(top_bar)

        # Quick Daily Action Strip: [ 📋 COPY ], [ 📌 PASTE ], [ ↩️ UNDO ], [ ⬅️ BROWSER BACK ], [ 🔄 SWITCH APP (Alt+Tab) ], [ 🎙️ BOLO / VOICE TYPE (Win+H) ]
        action_strip = QHBoxLayout()
        action_strip.setSpacing(4)

        actions = [
            ("📋 COPY", self._act_copy, False),
            ("📌 PASTE", self._act_paste, False),
            ("↩️ UNDO", self._act_undo, False),
            ("⬅️ BROWSER BACK", self._act_back, False),
            ("🔄 SWITCH APP", self._act_switch_app, False),
            ("🎙️ BOLO / VOICE TYPE (Win+H)", self._act_voice_type, True),
        ]
        for label, fn, is_voice in actions:
            btn = QPushButton(label)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setFont(QFont("Segoe UI", 8, QFont.Bold))
            if is_voice:
                btn.setStyleSheet("""
                    QPushButton {
                        background: #004D40;
                        color: #00E5FF;
                        border: 1px solid #00E5FF;
                        border-radius: 4px;
                        padding: 3px 8px;
                        font-weight: bold;
                    }
                    QPushButton:hover {
                        background: #00695C;
                        border-color: #18FFFF;
                    }
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {SURFACE_1};
                        color: {INDUSTRIAL_IVORY};
                        border: 1px solid #555;
                        border-radius: 4px;
                        padding: 3px 6px;
                    }}
                    QPushButton:hover {{
                        background: {SURFACE_3};
                        border-color: {BURNT_ORANGE};
                    }}
                """)
            btn.clicked.connect(fn)
            action_strip.addWidget(btn)

        kb_layout.addLayout(action_strip)

        self.somatic_kb = SomaticKeyboardWidget(on_type_fn=self.on_type_fn, on_dismiss_fn=self.dismiss)
        self.somatic_kb.sig_dismiss.connect(self.dismiss)
        kb_layout.addWidget(self.somatic_kb)
        root.addWidget(kb_container, stretch=5)

        # Right Side: Action Dock (Drag Lock + Scroll Pads)
        action_dock = QVBoxLayout()
        action_dock.setSpacing(6)

        self.btn_drag = QPushButton("🔒 DRAG\nHOLD")
        self.btn_drag.setFocusPolicy(Qt.NoFocus)
        self.btn_drag.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.btn_drag.setMinimumWidth(80)
        self.btn_drag.setMinimumHeight(60)
        self.btn_drag.setStyleSheet(f"background: {SURFACE_2}; color: {BURNT_ORANGE}; border: 1px solid #555; border-radius: 4px;")
        self.btn_drag.clicked.connect(self._on_drag_clicked)

        self.btn_scroll_up = QPushButton("▲ SCROLL\nUP")
        self.btn_scroll_up.setFocusPolicy(Qt.NoFocus)
        self.btn_scroll_up.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.btn_scroll_up.setMinimumWidth(80)
        self.btn_scroll_up.setMinimumHeight(70)
        self.btn_scroll_up.setStyleSheet(f"background: {SURFACE_2}; color: {INDUSTRIAL_IVORY}; border: 1px solid #555; border-radius: 4px;")
        self.btn_scroll_up.clicked.connect(lambda: self._on_scroll(120))

        self.btn_scroll_down = QPushButton("▼ SCROLL\nDOWN")
        self.btn_scroll_down.setFocusPolicy(Qt.NoFocus)
        self.btn_scroll_down.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.btn_scroll_down.setMinimumWidth(80)
        self.btn_scroll_down.setMinimumHeight(70)
        self.btn_scroll_down.setStyleSheet(f"background: {SURFACE_2}; color: {INDUSTRIAL_IVORY}; border: 1px solid #555; border-radius: 4px;")
        self.btn_scroll_down.clicked.connect(lambda: self._on_scroll(-120))

        action_dock.addWidget(self.btn_drag)
        action_dock.addWidget(self.btn_scroll_up)
        action_dock.addWidget(self.btn_scroll_down)

        # Doctor Precision Zoom Action Buttons
        btn_zoom_in = QPushButton("🔍 ZOOM +")
        btn_zoom_in.setFocusPolicy(Qt.NoFocus)
        btn_zoom_in.setFont(QFont("Segoe UI", 8, QFont.Bold))
        btn_zoom_in.setStyleSheet(f"background: {SURFACE_1}; color: #D500F9; border: 1px solid #555; border-radius: 4px; padding: 4px;")
        btn_zoom_in.clicked.connect(lambda: self._on_zoom_step(1))

        btn_zoom_out = QPushButton("🔍 ZOOM -")
        btn_zoom_out.setFocusPolicy(Qt.NoFocus)
        btn_zoom_out.setFont(QFont("Segoe UI", 8, QFont.Bold))
        btn_zoom_out.setStyleSheet(f"background: {SURFACE_1}; color: #D500F9; border: 1px solid #555; border-radius: 4px; padding: 4px;")
        btn_zoom_out.clicked.connect(lambda: self._on_zoom_step(-1))

        btn_zoom_reset = QPushButton("🔍 100% RESET")
        btn_zoom_reset.setFocusPolicy(Qt.NoFocus)
        btn_zoom_reset.setFont(QFont("Segoe UI", 7, QFont.Bold))
        btn_zoom_reset.setStyleSheet(f"background: {SURFACE_1}; color: {INDUSTRIAL_IVORY}; border: 1px solid #555; border-radius: 4px; padding: 3px;")
        btn_zoom_reset.clicked.connect(self._on_zoom_reset)

        action_dock.addWidget(btn_zoom_in)
        action_dock.addWidget(btn_zoom_out)
        action_dock.addWidget(btn_zoom_reset)
        root.addLayout(action_dock, stretch=1)

        self.setStyleSheet(f"background-color: rgba(24, 26, 27, 0.95); border: 2px solid {BURNT_ORANGE}; border-radius: 8px;")
        self.hide()  # Explicitly hidden on startup

    def dismiss(self) -> None:
        """Explicit dismissal by user - hides keyboard and emits sig_dismissed."""
        self.hide()
        self.sig_dismissed.emit()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.sig_dismissed.emit()

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

    def set_last_external_hwnd(self, hwnd: int) -> None:
        """Store the external window handle that held text focus."""
        if hwnd and hwnd != int(self.winId()):
            self._last_external_hwnd = hwnd

    def get_last_external_hwnd(self) -> int:
        return self._last_external_hwnd

    def _restore_external_focus_if_needed(self) -> None:
        """Ensure external target window receives focus before hotkeys / typing."""
        if sys.platform != "win32" or not self._last_external_hwnd:
            return
        try:
            u32 = ctypes.windll.user32
            fg = u32.GetForegroundWindow()
            if fg == int(self.winId()) or fg == 0:
                u32.SetForegroundWindow(self._last_external_hwnd)
        except Exception:
            pass

    def _apply_win32_noactivate(self) -> None:
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW)
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

    def _on_zoom_step(self, direction: int) -> None:
        if self.on_zoom_fn:
            self.on_zoom_fn(direction)
        self.sig_zoom.emit(direction)

    def _on_zoom_reset(self) -> None:
        if self.on_zoom_reset_fn:
            self.on_zoom_reset_fn()
        self.sig_zoom_reset.emit()

    # Quick Daily Action Handlers (Zero focus steal via Win32 keybd_event)
    def _send_win32_hotkey(self, *vks: int) -> None:
        if sys.platform != "win32":
            return
        self._restore_external_focus_if_needed()
        try:
            import time
            user32 = ctypes.windll.user32
            KEYEVENTF_KEYUP = 0x0002
            for vk in vks:
                user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.04)
            for vk in reversed(vks):
                user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            pass

    def _act_copy(self) -> None:
        self._send_win32_hotkey(0x11, 0x43)  # Ctrl+C

    def _act_paste(self) -> None:
        self._send_win32_hotkey(0x11, 0x56)  # Ctrl+V

    def _act_undo(self) -> None:
        self._send_win32_hotkey(0x11, 0x5A)  # Ctrl+Z

    def _act_back(self) -> None:
        self._send_win32_hotkey(0x12, 0x25)  # Alt+Left

    def _act_switch_app(self) -> None:
        self._send_win32_hotkey(0x12, 0x09)  # Alt+Tab

    def _act_voice_type(self) -> None:
        self._send_win32_hotkey(0x5B, 0x48)  # Win+H

