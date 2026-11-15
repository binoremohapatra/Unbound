"""nfs_overlay.py - Compact Always-On-Top Mini Overlay for Need for Speed: Most Wanted.

Characteristics:
- Frameless, 280x75 px, 80% opacity, docked at top-right corner of the screen.
- Windows WS_EX_NOACTIVATE & Qt.WindowType.WindowDoesNotAcceptFocus enabled:
  Clicking micro-buttons or dragging NEVER steals keyboard focus from NFS Most Wanted.
- Visual Pills:
  [ ◀ L ] [ ▲ GAS ] [ ▼ BRK ] [ R ▶ ] [ 🔥 NOS ] [ 🛞 DRIFT ]
- Micro-Buttons:
  [ 🎯 Zero ] (re-centers gyro in 1 click)
  [ ✖ Exit ] (releases all keys and restores main HUD)
"""
from __future__ import annotations

import ctypes
import sys
from typing import Callable, Optional

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class NFSMiniOverlay(QWidget):
    """Floating 280x75 px semi-transparent HUD overlay for NFS Most Wanted."""

    def __init__(
        self,
        on_zero: Optional[Callable[[], None]] = None,
        on_exit: Optional[Callable[[], None]] = None,
        on_toggle_fallback: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.on_zero = on_zero
        self.on_exit = on_exit
        self.on_toggle_fallback = on_toggle_fallback

        # Window flags: Tool, StaysOnTop, Frameless, NoFocus
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowOpacity(0.85)
        self.setFixedSize(310, 75)

        self._drag_pos: Optional[QPoint] = None

        self._init_ui()
        self._dock_top_right()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # ── Top Bar: Title + Micro Buttons ────────────────────────────────────
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(2, 0, 2, 0)
        top_bar.setSpacing(4)

        lbl_title = QLabel("🏎️ NFS RACING")
        lbl_title.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        lbl_title.setStyleSheet("color: #FF6600; background: transparent;")
        top_bar.addWidget(lbl_title)
        top_bar.addStretch()

        self.btn_fallback = QPushButton("🔄 MPU")
        self.btn_fallback.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_fallback.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        self.btn_fallback.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_fallback.setStyleSheet("""
            QPushButton {
                background: #0d2818;
                color: #00FF66;
                border: 1px solid #00FF66;
                border-radius: 3px;
                padding: 1px 5px;
            }
            QPushButton:hover {
                background: #143d24;
            }
        """)
        self.btn_fallback.clicked.connect(self._handle_toggle_fallback)
        top_bar.addWidget(self.btn_fallback)

        self.btn_zero = QPushButton("🎯 Zero")
        self.btn_zero.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_zero.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        self.btn_zero.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_zero.setStyleSheet("""
            QPushButton {
                background: #1e1e28;
                color: #00E5FF;
                border: 1px solid #00E5FF;
                border-radius: 3px;
                padding: 1px 6px;
            }
            QPushButton:hover {
                background: #003344;
            }
        """)
        self.btn_zero.clicked.connect(self._handle_zero)
        top_bar.addWidget(self.btn_zero)

        self.btn_exit = QPushButton("✖ Exit")
        self.btn_exit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_exit.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        self.btn_exit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_exit.setStyleSheet("""
            QPushButton {
                background: #2a1111;
                color: #FF4444;
                border: 1px solid #FF4444;
                border-radius: 3px;
                padding: 1px 6px;
            }
            QPushButton:hover {
                background: #441111;
            }
        """)
        self.btn_exit.clicked.connect(self._handle_exit)
        top_bar.addWidget(self.btn_exit)

        layout.addLayout(top_bar)

        # ── Bottom Bar: Compact Pill Telemetry Indicators ─────────────────────
        pills_row = QHBoxLayout()
        pills_row.setContentsMargins(0, 0, 0, 0)
        pills_row.setSpacing(3)

        self.pill_left = self._create_pill("◀ L", "#00FF66")
        self.pill_gas = self._create_pill("▲ GAS", "#00FF66")
        self.pill_brk = self._create_pill("▼ BRK", "#FF3333")
        self.pill_right = self._create_pill("R ▶", "#00FF66")
        self.pill_nos = self._create_pill("🔥 NOS", "#00E5FF")
        self.pill_drift = self._create_pill("🛞 DRIFT", "#FFAA00")

        pills_row.addWidget(self.pill_left)
        pills_row.addWidget(self.pill_gas)
        pills_row.addWidget(self.pill_brk)
        pills_row.addWidget(self.pill_right)
        pills_row.addWidget(self.pill_nos)
        pills_row.addWidget(self.pill_drift)

        layout.addLayout(pills_row)

    def _create_pill(self, text: str, active_color: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        lbl._active_color = active_color
        self._set_pill_active(lbl, False)
        return lbl

    def _set_pill_active(self, lbl: QLabel, active: bool) -> None:
        color = getattr(lbl, "_active_color", "#00FF66")
        if active:
            lbl.setStyleSheet(f"""
                QLabel {{
                    background: {color};
                    color: #111111;
                    border: 1px solid {color};
                    border-radius: 3px;
                    padding: 2px 3px;
                    font-weight: bold;
                }}
            """)
        else:
            lbl.setStyleSheet("""
                QLabel {{
                    background: #14141a;
                    color: #555566;
                    border: 1px solid #22222a;
                    border-radius: 3px;
                    padding: 2px 3px;
                }}
            """)

    def update_telemetry(
        self,
        gas: bool,
        brake: bool,
        steer_left: bool,
        steer_right: bool,
        nos: bool,
        drift: bool,
    ) -> None:
        """Update live telemetry pill indicators."""
        self._set_pill_active(self.pill_gas, gas)
        self._set_pill_active(self.pill_brk, brake)
        self._set_pill_active(self.pill_left, steer_left)
        self._set_pill_active(self.pill_right, steer_right)
        self._set_pill_active(self.pill_nos, nos)
        self._set_pill_active(self.pill_drift, drift)

    def _handle_toggle_fallback(self) -> None:
        if self.on_toggle_fallback:
            self.on_toggle_fallback()

    def set_fallback_mode(self, fallback: bool) -> None:
        """Update toggle button visual state."""
        if hasattr(self, "btn_fallback"):
            if fallback:
                self.btn_fallback.setText("🟣 NOSE")
                self.btn_fallback.setStyleSheet("""
                    QPushButton {
                        background: #2a0033;
                        color: #D500F9;
                        border: 1px solid #D500F9;
                        border-radius: 3px;
                        padding: 1px 5px;
                    }
                    QPushButton:hover {
                        background: #3d0049;
                    }
                """)
            else:
                self.btn_fallback.setText("🔄 MPU")
                self.btn_fallback.setStyleSheet("""
                    QPushButton {
                        background: #0d2818;
                        color: #00FF66;
                        border: 1px solid #00FF66;
                        border-radius: 3px;
                        padding: 1px 5px;
                    }
                    QPushButton:hover {
                        background: #143d24;
                    }
                """)

    def _dock_top_right(self) -> None:
        """Place overlay at top-right corner of primary screen."""
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            self.move(geom.width() - self.width() - 15, geom.top() + 15)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Windows API: Apply WS_EX_NOACTIVATE to completely prevent focus stealing
        if sys.platform == "win32":
            try:
                hwnd = int(self.winId())
                GWL_EXSTYLE = -20
                WS_EX_NOACTIVATE = 0x08000000
                WS_EX_TOPMOST = 0x00000008
                cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, cur | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
            except Exception:
                pass

    def paintEvent(self, event) -> None:
        """Draw dark rounded glass backdrop."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 6, 6)
        painter.fillPath(path, QColor(10, 10, 15, 230))
        painter.setPen(QColor(255, 102, 0, 180))
        painter.drawPath(path)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None

    def _handle_zero(self) -> None:
        if self.on_zero:
            self.on_zero()

    def _handle_exit(self) -> None:
        if self.on_exit:
            self.on_exit()
