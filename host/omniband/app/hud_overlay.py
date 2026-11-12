"""hud_overlay.py - Frameless, Click-Through, Always-On-Top Tactical HUD Overlay Window.

Uses Qt.WindowTransparentForInput | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint,
augmented with native Win32 WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOPMOST.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout, QProgressBar
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPalette

from host.omniband.app.theme import (
    BURNT_ORANGE, STATUS_OK, STATUS_WARN, STATUS_ERR,
    INDUSTRIAL_IVORY, WARM_GRAPHITE, SURFACE_0, FONT_MONO
)

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008


class HudOverlay(QWidget):
    """Click-through tactical overlay designed to float over Cyberpunk 2077 or desktop."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Set transparent & non-interactive window hints
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self.setFixedSize(320, 160)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        # Header row: Tier Badge + Mode
        header = QHBoxLayout()
        self._lbl_tier = QLabel("TIER 1 (USB)")
        self._lbl_tier.setFont(QFont("Cascadia Code", 10, QFont.Bold))
        self._lbl_tier.setStyleSheet(f"color: {STATUS_OK}; background: rgba(37,37,37,0.7); padding: 2px 6px; border-radius: 3px;")

        self._lbl_mode = QLabel("MODE 2 (RACING)")
        self._lbl_mode.setFont(QFont("Cascadia Code", 10))
        self._lbl_mode.setStyleSheet(f"color: {BURNT_ORANGE}; background: rgba(37,37,37,0.7); padding: 2px 6px; border-radius: 3px;")

        header.addWidget(self._lbl_tier)
        header.addStretch()
        header.addWidget(self._lbl_mode)
        layout.addLayout(header)

        # Co-Pilot Assist Status
        self._lbl_assist = QLabel("CO-PILOT: LANE-CENTERING (FULL)")
        self._lbl_assist.setFont(QFont("Cascadia Code", 9))
        self._lbl_assist.setStyleSheet(f"color: {INDUSTRIAL_IVORY};")
        layout.addWidget(self._lbl_assist)

        # Veto / Injection status toast
        self._lbl_veto = QLabel("STATUS: ARMED & READY")
        self._lbl_veto.setFont(QFont("Cascadia Code", 8))
        self._lbl_veto.setStyleSheet(f"color: {WARM_GRAPHITE};")
        layout.addWidget(self._lbl_veto)

        # 4 Mini Piezo Bars
        bars_row = QHBoxLayout()
        self._bars = []
        for i in range(4):
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(10)
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setStyleSheet(f"""
                QProgressBar {{
                    border: 1px solid #444;
                    background: rgba(24, 26, 27, 0.8);
                    border-radius: 2px;
                }}
                QProgressBar::chunk {{
                    background-color: {BURNT_ORANGE};
                }}
            """)
            bars_row.addWidget(bar)
            self._bars.append(bar)
        layout.addLayout(bars_row)

        self.setStyleSheet("background-color: rgba(24, 26, 27, 0.85); border: 1px solid #3a3d40; border-radius: 6px;")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_win32_clickthrough()

    def _apply_win32_clickthrough(self) -> None:
        """Reinforce click-through via Win32 WS_EX_TRANSPARENT on Windows."""
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOPMOST)
        except Exception:
            pass

    def update_telemetry(self, tier_name: str, mode_name: str, assist_name: str, veto_text: str, bar_vals: list[float]) -> None:
        self._lbl_tier.setText(tier_name)
        self._lbl_mode.setText(mode_name)
        self._lbl_assist.setText(f"CO-PILOT: {assist_name}")
        self._lbl_veto.setText(veto_text)
        for i, val in enumerate(bar_vals[:4]):
            self._bars[i].setValue(int(val * 100))
