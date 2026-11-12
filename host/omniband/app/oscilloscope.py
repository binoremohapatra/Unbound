"""oscilloscope.py - 60 FPS 4-channel real-time oscilloscope widget (QPainter).

Channels map to OmniBand CH1–CH4 (bite, left-cheek, right-cheek, jaw-drop).
Threshold lines are draggable via QSlider.  Runs a QTimer at 60 Hz.
"""
from __future__ import annotations

import collections
import time
from typing import List

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QGroupBox,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QFont

from host.omniband.app.theme import (
    SURFACE_1, SURFACE_2, BORDER, ACCENT, BURNT_ORANGE,
    WARM_GRAPHITE, INDUSTRIAL_IVORY, STATUS_ERR, STATUS_OK,
    FONT_MONO, FONT_SIZE_XS, FONT_SIZE_SM,
)

_CH_COLORS = [ACCENT, "#a8ff78", "#ffcc00", STATUS_ERR]
_CH_NAMES  = ["CH1 Bite", "CH2 L-Cheek", "CH3 R-Cheek", "CH4 Jaw"]
_HISTORY   = 300  # samples to display


class _WaveCanvas(QWidget):
    """QPainter waveform canvas for 4 channels."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setMinimumWidth(400)
        self._buffers: List[collections.deque] = [
            collections.deque([0.0] * _HISTORY, maxlen=_HISTORY) for _ in range(4)
        ]
        self._thresholds: List[float] = [0.5, 0.5, 0.5, 0.5]
        self._active: List[bool] = [False] * 4

    def push_sample(self, values: List[float], active: List[bool]) -> None:
        for i, v in enumerate(values[:4]):
            self._buffers[i].append(max(0.0, min(1.0, float(v))))
        self._active = list(active[:4])

    def set_threshold(self, ch: int, value: float) -> None:
        self._thresholds[ch] = value

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # Background
        p.fillRect(0, 0, w, h, QColor(SURFACE_2))

        # Grid lines (horizontal)
        p.setPen(QPen(QColor(BORDER), 1))
        for frac in (0.25, 0.5, 0.75):
            y = int(h * frac)
            p.drawLine(0, y, w, y)

        ch_height = h // 4
        for ch in range(4):
            top = ch * ch_height
            bot = top + ch_height
            mid = (top + bot) // 2
            color = QColor(_CH_COLORS[ch])

            # Channel label
            p.setPen(QPen(color, 1))
            p.setFont(QFont("Cascadia Code", FONT_SIZE_XS))
            label = f"{'▶' if self._active[ch] else ' '} {_CH_NAMES[ch]}"
            p.drawText(4, top + 14, label)

            # Threshold line
            thr = self._thresholds[ch]
            ty = int(bot - thr * ch_height)
            pen_thr = QPen(color.darker(150), 1, Qt.DashLine)
            p.setPen(pen_thr)
            p.drawLine(0, ty, w, ty)

            # Waveform
            buf = list(self._buffers[ch])
            pen_wave = QPen(color, 1.5)
            p.setPen(pen_wave)
            pts = []
            for i, v in enumerate(buf):
                x = int(i * w / _HISTORY)
                y = int(bot - v * ch_height)
                pts.append((x, y))
            for i in range(1, len(pts)):
                p.drawLine(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])

        p.end()


class OscilloscopeWidget(QWidget):
    """4-channel 60 FPS oscilloscope with per-channel threshold sliders."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._canvas = _WaveCanvas()
        self._sliders: List[QSlider] = []
        self._labels: List[QLabel] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._canvas)

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(16)
        for ch in range(4):
            grp = QGroupBox(f"{_CH_NAMES[ch]} threshold")
            grp.setObjectName("secondary")
            inner = QHBoxLayout(grp)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, 100)
            sl.setValue(50)
            sl.setFixedWidth(100)
            sl.setMinimumHeight(28)
            lbl = QLabel("0.50")
            lbl.setObjectName("mono")
            lbl.setFixedWidth(36)
            lbl_ch = ch  # capture
            sl.valueChanged.connect(lambda v, c=lbl_ch: self._on_threshold(c, v))
            inner.addWidget(sl)
            inner.addWidget(lbl)
            self._sliders.append(sl)
            self._labels.append(lbl)
            ctrl_row.addWidget(grp)
        layout.addLayout(ctrl_row)

        # 60 Hz repaint timer
        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ~60 fps
        self._timer.timeout.connect(self._canvas.update)
        self._timer.start()

        # Demo noise timer (removed when real data hooked in)
        self._demo_timer = QTimer(self)
        self._demo_timer.setInterval(16)
        self._demo_timer.timeout.connect(self._demo_tick)
        self._demo_timer.start()

        self._t: float = 0.0

    def _demo_tick(self) -> None:
        """Generate synthetic waveform until real data is connected."""
        import math
        self._t += 0.05
        vals = [
            0.5 + 0.4 * math.sin(self._t),
            0.5 + 0.3 * math.sin(self._t * 1.3 + 1),
            0.5 + 0.35 * math.sin(self._t * 0.7 + 2),
            0.5 + 0.25 * math.sin(self._t * 2.1 + 3),
        ]
        active = [v > self._canvas._thresholds[i] for i, v in enumerate(vals)]
        self._canvas.push_sample(vals, active)

    def push_sample(self, ch_values: List[float], active: List[bool]) -> None:
        """External call: push live telemetry values (0.0–1.0 each channel)."""
        self._demo_timer.stop()
        self._canvas.push_sample(ch_values, active)

    def _on_threshold(self, ch: int, slider_val: int) -> None:
        frac = slider_val / 100.0
        self._canvas.set_threshold(ch, frac)
        self._labels[ch].setText(f"{frac:.2f}")

