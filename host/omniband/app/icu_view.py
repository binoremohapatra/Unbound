"""icu_view.py - High-contrast ICU Patient Board Widget.

Designed for paralyzed/bedridden patients in ICU/SOS mode:
- 4 Large high-contrast phrase cards mapped to CH1–CH4
- Live TTS Voice readout display
- Large Siren ACK button to silence alerts
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGridLayout
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from host.omniband.app.theme import (
    SURFACE_1, SURFACE_2, BURNT_ORANGE, STATUS_ERR, STATUS_OK,
    INDUSTRIAL_IVORY, WARM_GRAPHITE, PURE_WHITE, FONT_MONO
)


class IcuView(QWidget):
    """High-contrast ICU Patient Board with large actionable tiles."""

    sig_ack_siren = Signal()
    sig_trigger_phrase = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        header = QLabel("ICU & ASSISTIVE COMMUNICATION BOARD")
        header.setFont(QFont("Segoe UI", 14, QFont.Bold))
        header.setStyleSheet(f"color: {BURNT_ORANGE};")
        layout.addWidget(header)

        sub = QLabel("Trigger actions via facial gestures or direct selection. Press ACK to silence buzzer.")
        sub.setFont(QFont("Segoe UI", 9))
        sub.setStyleSheet(f"color: {WARM_GRAPHITE};")
        layout.addWidget(sub)

        grid = QGridLayout()
        grid.setSpacing(12)

        self._tiles = [
            ("CH1 (Bite / Nod)", "PAIN ALERT", "I am in severe pain. Need nurse.", STATUS_ERR),
            ("CH2 (Left Cheek)", "WATER / SUCTION", "I need water or airway suction.", BURNT_ORANGE),
            ("CH3 (Right Cheek)", "POSITION ADJUST", "Please adjust my bed position.", STATUS_OK),
            ("CH4 (Jaw Drop)", "EMERGENCY SOS", "CRITICAL EMERGENCY - SOUND SIREN", STATUS_ERR),
        ]

        for idx, (ch_tag, title, msg, color) in enumerate(self._tiles):
            tile_btn = QPushButton()
            tile_btn.setMinimumHeight(80)
            tile_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {SURFACE_2};
                    border: 2px solid {color};
                    border-radius: 6px;
                    text-align: left;
                    padding: 10px;
                }}
                QPushButton:hover {{
                    background-color: {SURFACE_1};
                    border-color: {PURE_WHITE};
                }}
            """)
            btn_layout = QVBoxLayout(tile_btn)
            btn_layout.setContentsMargins(4, 4, 4, 4)
            lbl_tag = QLabel(f"[{ch_tag}] {title}")
            lbl_tag.setFont(QFont("Segoe UI", 11, QFont.Bold))
            lbl_tag.setStyleSheet(f"color: {color};")
            lbl_msg = QLabel(f'"{msg}"')
            lbl_msg.setFont(QFont("Segoe UI", 9))
            lbl_msg.setStyleSheet(f"color: {INDUSTRIAL_IVORY};")
            btn_layout.addWidget(lbl_tag)
            btn_layout.addWidget(lbl_msg)

            # Connect click to emit phrase index
            tile_btn.clicked.connect(lambda _, ch=idx: self.sig_trigger_phrase.emit(ch))
            grid.addWidget(tile_btn, idx // 2, idx % 2)

        layout.addLayout(grid)

        # Bottom row: Live TTS status and Siren ACK button
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(12)

        self._lbl_status = QLabel("LAST EVENT: SYSTEM NORMAL | MONITORING ACTIVE")
        self._lbl_status.setFont(QFont("Cascadia Code", 10))
        self._lbl_status.setStyleSheet(f"color: {STATUS_OK}; background: {SURFACE_2}; padding: 8px; border-radius: 4px;")
        bottom_row.addWidget(self._lbl_status, stretch=2)

        btn_ack = QPushButton("SILENCE / ACK SIREN")
        btn_ack.setObjectName("danger_btn")
        btn_ack.setMinimumHeight(44)
        btn_ack.setFont(QFont("Segoe UI", 10, QFont.Bold))
        btn_ack.clicked.connect(self._on_ack)
        bottom_row.addWidget(btn_ack, stretch=1)

        layout.addLayout(bottom_row)

    def _on_ack(self) -> None:
        self._lbl_status.setText("LAST EVENT: SIREN SILENCED BY NURSE / OPERATOR")
        self._lbl_status.setStyleSheet(f"color: {WARM_GRAPHITE}; background: {SURFACE_2}; padding: 8px; border-radius: 4px;")
        self.sig_ack_siren.emit()

    def report_event(self, phrase: str, ch: int) -> None:
        color = STATUS_ERR if ch in (0, 3) else BURNT_ORANGE
        self._lbl_status.setText(f"ACTIVE CALL: [CH{ch+1}] {phrase}")
        self._lbl_status.setStyleSheet(f"color: {color}; background: {SURFACE_2}; padding: 8px; border-radius: 4px; font-weight: bold;")

