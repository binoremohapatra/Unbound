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
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QRegion

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


def enforce_click_through_hwnd(hwnd: int) -> None:
    """Force Win32 extended click-through styles so OS hit-tests never trap on overlay."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        target = (
            style
            | WS_EX_LAYERED
            | WS_EX_TRANSPARENT
            | WS_EX_TOPMOST
            | WS_EX_TOOLWINDOW
            | WS_EX_NOACTIVATE
        )
        if (style & (WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE)) != (
            WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE
        ):
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, target)
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020
            SWP_NOACTIVATE = 0x0010
            user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE
            )
    except Exception:
        pass


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

        # Apply hollow 6x6 px center hole so cursor pixel does not belong to this window
        self._update_hollow_mask()
        self._style_check_counter = 0

        self.state = "NORMAL"  # NORMAL, CLICKED, DRAG_LOCKED, SCROLLING
        self.dwell_progress = 0.0  # 0.0 to 1.0
        self._click_expire = 0.0

        # 120 Hz Cursor tracking timer (8ms)
        self._timer = QTimer(self)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._sync_position)
        self._timer.start()

    def _update_hollow_mask(self) -> None:
        """Punch a 6x6 pixel hole in the exact center where the cursor hotspot sits.

        Result: The exact pixel under (cursor_x, cursor_y) physically does NOT belong
        to any Unbound.exe window at the OS window manager level. WindowFromPoint
        is guaranteed to hit Shell_TrayWnd (Taskbar), StartMenuExperienceHost,
        or the target desktop app!
        """
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        full_region = QRegion(0, 0, w, h)
        cx, cy = w // 2, h // 2
        center_hole = QRegion(cx - 3, cy - 3, 6, 6)
        self.setMask(full_region.subtracted(center_hole))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._update_hollow_mask()
        self._apply_win32_styles()

    def _apply_win32_styles(self) -> None:
        if sys.platform != "win32":
            return
        enforce_click_through_hwnd(int(self.winId()))

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
            self._style_check_counter += 1
            if self._style_check_counter >= 15:
                self._style_check_counter = 0
                enforce_click_through_hwnd(int(self.winId()))

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

