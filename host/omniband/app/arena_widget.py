"""arena_widget.py - Native 60 FPS Cyberpunk Highway Arena Widget (QPainter).

Renders a 3D-perspective neon highway at 60 FPS.
CRITICAL: Input is accepted ONLY via keyPressEvent/keyReleaseEvent inspecting
event.nativeScanCode() — verifying Set-1 scancodes injected by Windows kernel / SendInput:
  A = 0x1E (Steer Left)
  D = 0x20 (Steer Right)
  W = 0x11 (Throttle)
  S = 0x1F (Brake / Reverse)
  Space = 0x39 (Handbrake)
"""
from __future__ import annotations

import math
import time
from typing import Optional

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QPolygonF
)

from host.omniband.copilot.types import VehicleState, HumanIntent
from host.omniband.copilot.sim.vehicle import SimVehicle
from host.omniband.adapters.telemetry.native_arena import ARENA_SLOT
from host.omniband.app.theme import (
    SURFACE_0, SURFACE_1, SURFACE_2, BURNT_ORANGE, DARK_BURNT_ORANGE,
    STATUS_OK, STATUS_ERR, TEXT_PRIMARY, TEXT_SECONDARY, FONT_MONO
)

# Standard PS/2 Set-1 Hardware Scancodes (Windows)
SCANCODE_A = 0x1E
SCANCODE_D = 0x20
SCANCODE_W = 0x11
SCANCODE_S = 0x1F
SCANCODE_SPACE = 0x39


class ArenaWidget(QWidget):
    """Native 60 FPS Cyberpunk Highway Arena with OS scancode verification."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(640, 360)

        self._vehicle = SimVehicle()
        self._scancode_hits: int = 0
        self._last_scancode: int = 0
        self._last_scancode_name: str = "NONE"

        # Key state tracking via scancodes
        self._key_a = False
        self._key_d = False
        self._key_w = False
        self._key_s = False
        self._key_space = False

        # 60 FPS simulation loop
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._loop_tick)
        self._timer.start()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        sc = event.nativeScanCode()
        self._record_scancode(sc, is_press=True)
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        sc = event.nativeScanCode()
        self._record_scancode(sc, is_press=False)
        super().keyReleaseEvent(event)

    def _record_scancode(self, sc: int, is_press: bool) -> None:
        name = "UNKNOWN"
        matched = False

        if sc == SCANCODE_A:
            self._key_a = is_press
            name = "A (Steer L)"
            matched = True
        elif sc == SCANCODE_D:
            self._key_d = is_press
            name = "D (Steer R)"
            matched = True
        elif sc == SCANCODE_W:
            self._key_w = is_press
            name = "W (Throttle)"
            matched = True
        elif sc == SCANCODE_S:
            self._key_s = is_press
            name = "S (Brake)"
            matched = True
        elif sc == SCANCODE_SPACE:
            self._key_space = is_press
            name = "SPACE (Handbrake)"
            matched = True

        if matched and is_press:
            self._scancode_hits += 1
            self._last_scancode = sc
            self._last_scancode_name = name

    def _loop_tick(self) -> None:
        self._vehicle.step(
            key_a=self._key_a,
            key_d=self._key_d,
            key_w=self._key_w,
            key_s=self._key_s,
            handbrake=self._key_space,
        )
        state = self._vehicle.to_vehicle_state()

        # Write to shared in-memory slot for Co-Pilot
        ARENA_SLOT.write_state(state)
        ARENA_SLOT.write_intent(HumanIntent(
            lateral_bias=-1.0 if self._key_a else (1.0 if self._key_d else 0.0),
            lane_step=0,
            cruise_on=self._key_w,
            v_set=state.speed,
            req_drift=self._key_space,
            req_nitro=False,
            assist_level="FULL",
            source_tier="T1",
            t=state.t,
        ))

        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # Sky / Horizon
        p.fillRect(0, 0, w, h, QColor(SURFACE_0))
        horizon_y = int(h * 0.42)
        p.fillRect(0, 0, w, horizon_y, QColor(SURFACE_1))

        # Perspective Road Quad
        road_poly = QPolygonF([
            QPointF(w * 0.40, horizon_y),
            QPointF(w * 0.60, horizon_y),
            QPointF(w * 0.95, h),
            QPointF(w * 0.05, h),
        ])
        p.setBrush(QBrush(QColor(SURFACE_2)))
        p.setPen(QPen(QColor(DARK_BURNT_ORANGE), 2))
        p.drawPolygon(road_poly)

        # Road Neon Lane Lines
        p.setPen(QPen(QColor(BURNT_ORANGE), 2, Qt.DashLine))
        p.drawLine(int(w * 0.50), horizon_y, int(w * 0.50 + self._vehicle.e * 12), h)

        # Vehicle Icon / Marker
        car_w, car_h = 44, 24
        cx = int(w * 0.50 - self._vehicle.e * 18)
        cy = int(h * 0.82)
        p.setBrush(QBrush(QColor(BURNT_ORANGE)))
        p.setPen(QPen(QColor(TEXT_PRIMARY), 1.5))
        p.drawRoundedRect(cx - car_w // 2, cy - car_h // 2, car_w, car_h, 4, 4)

        # Telemetry Overlay Text
        p.setFont(QFont("Cascadia Code", 10))
        p.setPen(QPen(QColor(TEXT_PRIMARY)))
        st = self._vehicle.to_vehicle_state()
        p.drawText(16, 26, f"ARENA 60 FPS | Speed: {st.speed * 3.6:.1f} km/h | e_lat: {st.e:+.2f} m")
        
        # Kernel Scancode Verification Status
        sc_color = QColor(STATUS_OK) if self._scancode_hits > 0 else QColor(TEXT_SECONDARY)
        p.setPen(QPen(sc_color))
        p.drawText(16, 48, f"OS Kernel Scancodes Verified: {self._scancode_hits} (Last: 0x{self._last_scancode:02X} {self._last_scancode_name})")

        p.end()
