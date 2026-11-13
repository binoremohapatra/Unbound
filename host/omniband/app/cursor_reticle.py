"""cursor_reticle.py - High-Visibility 60 FPS Somatic Cursor Reticle Overlay.

Frameless, click-through, always-on-top HUD crosshair ring tracking real Windows cursor coordinates.
Uses:
  Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput
Plus Win32 extended styles:
  WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_NOACTIVATE

States:
  - NORMAL: Burnt Orange tactical ring (#F07842)
  - CLICKED: Expands and flashes bright Emerald Green (#00E676) for 200ms
  - DRAG_LOCKED: Solid Crimson (#E0303D) with pulsing inner lock and 'HOLD' tag
  - SCROLLING: Vertical chevrons
  - DWELL: Radial progress arc (0..1.0)
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import time

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000


class SomaticCursorReticle(QWidget):
    """56x56 px tactical crosshair overlay tracking the real OS mouse cursor."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(56, 56)

        self.state = "NORMAL"  # NORMAL, CLICKED, DRAG_LOCKED, SCROLLING
        self.dwell_progress = 0.0  # 0.0 to 1.0
        self._click_expire = 0.0

        # 120 Hz Cursor tracking timer (8ms)
        self._timer = QTimer(self)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._sync_position)
        self._timer.start()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_win32_styles()

    def _apply_win32_styles(self) -> None:
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                style | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_NOACTIVATE
            )
        except Exception:
            pass

    def trigger_click_flash(self) -> None:
        self.state = "CLICKED"
        self._click_expire = time.perf_counter() + 0.20  # 200 ms flash
        self.update()

    def set_drag_locked(self, locked: bool) -> None:
        self.state = "DRAG_LOCKED" if locked else "NORMAL"
        self.update()

    def set_scrolling(self, scrolling: bool) -> None:
        if self.state != "DRAG_LOCKED":
            self.state = "SCROLLING" if scrolling else "NORMAL"
            self.update()

    def set_dwell_progress(self, progress: float) -> None:
        self.dwell_progress = max(0.0, min(1.0, progress))
        self.update()

    def _sync_position(self) -> None:
        if sys.platform == "win32":
            pt = wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            # Center the 56x56 reticle around the cursor hotspot
            self.move(pt.x - 28, pt.y - 28)

        # Check click expiration
        if self.state == "CLICKED" and time.perf_counter() >= self._click_expire:
            self.state = "NORMAL"

        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0

        # State color selection
        if self.state == "CLICKED":
            color = QColor("#00E676")  # Emerald Green
        elif self.state == "DRAG_LOCKED":
            color = QColor("#E0303D")  # Crimson
        elif self.state == "SCROLLING":
            color = QColor("#FFD600")  # Amber Yellow
        else:
            color = QColor("#F07842")  # Burnt Orange

        # Outer Shadow Halo for high contrast on light backgrounds
        p.setPen(QPen(QColor(0, 0, 0, 180), 3))
        p.drawEllipse(int(cx - 20), int(cy - 20), 40, 40)

        # Tactical Segmented Ring
        p.setPen(QPen(color, 2.5))
        p.drawArc(int(cx - 20), int(cy - 20), 40, 40, 30 * 16, 60 * 16)
        p.drawArc(int(cx - 20), int(cy - 20), 40, 40, 120 * 16, 60 * 16)
        p.drawArc(int(cx - 20), int(cy - 20), 40, 40, 210 * 16, 60 * 16)
        p.drawArc(int(cx - 20), int(cy - 20), 40, 40, 300 * 16, 60 * 16)

        # Center pinpoint crosshair
        p.setPen(QPen(color, 1.5))
        p.drawLine(int(cx - 5), int(cy), int(cx + 5), int(cy))
        p.drawLine(int(cx), int(cy - 5), int(cx), int(cy + 5))

        # Center pinpoint dot
        p.setBrush(QBrush(QColor(255, 255, 255)))
        p.setPen(Qt.NoPen)
        p.drawEllipse(int(cx - 2), int(cy - 2), 4, 4)

        # Dwell progress radial arc
        if self.dwell_progress > 0.0:
            p.setPen(QPen(QColor("#00E676"), 3))
            span = int(self.dwell_progress * 360 * 16)
            p.drawArc(int(cx - 24), int(cy - 24), 48, 48, 90 * 16, -span)

        # State specific badge/icons
        if self.state == "DRAG_LOCKED":
            p.setBrush(QBrush(color))
            p.drawRect(int(cx - 3), int(cy - 3), 6, 6)
            p.setFont(QFont("Cascadia Code", 7, QFont.Bold))
            p.setPen(QPen(color))
            p.drawText(int(cx - 12), int(cy + 24), "HOLD")
        elif self.state == "SCROLLING":
            p.setFont(QFont("Segoe UI", 9, QFont.Bold))
            p.setPen(QPen(color))
            p.drawText(int(cx - 4), int(cy - 16), "▲")
            p.drawText(int(cx - 4), int(cy + 24), "▼")

        p.end()

