"""Cursor tracking & Dwell Clicker engine.

Abstracted and synthesized from:
  1. Google Project Gameface:
     - Nonlinear speed acceleration curve (power transfer function)
     - Deadzone suppression for fine motor tremor resistance
     - 1€ Filter jitter-reduction
     - Sub-pixel remainder delta accumulation
     - Neutral reference & drift recentering
  2. Tracky Mouse:
     - Dwell clicker state machine (IDLE -> DWELLING -> TRIGGERED -> COOLDOWN)
     - Radial stability boundary detection
     - Dwell progress reporting (0.0 -> 1.0) for visual feedback
     - Multimode actions (left click, right click, double click, drag)
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Tuple

from handsfree.core.events import Intent
from handsfree.core.filters import OneEuroFilter


class DwellAction(str, Enum):
    LEFT_CLICK = "left_click"
    RIGHT_CLICK = "right_click"
    DOUBLE_CLICK = "double_click"
    DRAG_TOGGLE = "drag_toggle"


class DwellState(str, Enum):
    IDLE = "idle"
    DWELLING = "dwelling"
    TRIGGERED = "triggered"
    COOLDOWN = "cooldown"


@dataclass
class DwellStatus:
    state: DwellState = DwellState.IDLE
    progress: float = 0.0          # 0.0 to 1.0
    action: DwellAction = DwellAction.LEFT_CLICK
    target_x: float = 0.0
    target_y: float = 0.0
    is_dragging: bool = False


class DwellClicker:
    """Tracky-Mouse inspired dwell clicking engine.
    
    Triggers clicks when the cursor hovers within a small radius for a configured duration.
    """

    def __init__(
        self,
        dwell_time_ms: float = 650.0,
        dwell_radius: float = 20.0,
        cooldown_ms: float = 400.0,
        on_action: Optional[Callable[[DwellAction, float, float], None]] = None,
    ):
        self.dwell_time_s = dwell_time_ms / 1000.0
        self.dwell_radius = dwell_radius
        self.cooldown_s = cooldown_ms / 1000.0
        self.on_action = on_action

        self.current_action = DwellAction.LEFT_CLICK
        self.state = DwellState.IDLE
        self.anchor_x = 0.0
        self.anchor_y = 0.0
        self.dwell_start_time = 0.0
        self.cooldown_end_time = 0.0
        self.is_dragging = False

    def set_action(self, action: DwellAction) -> None:
        self.current_action = action

    def update(self, cursor_x: float, cursor_y: float, timestamp: Optional[float] = None) -> DwellStatus:
        now = timestamp if timestamp is not None else time.time()

        # Cooldown state check
        if self.state == DwellState.COOLDOWN:
            if now < self.cooldown_end_time:
                return DwellStatus(
                    state=DwellState.COOLDOWN,
                    progress=0.0,
                    action=self.current_action,
                    target_x=cursor_x,
                    target_y=cursor_y,
                    is_dragging=self.is_dragging,
                )
            self.state = DwellState.IDLE

        dist_from_anchor = math.hypot(cursor_x - self.anchor_x, cursor_y - self.anchor_y)

        if self.state == DwellState.IDLE:
            # Re-anchor whenever cursor stops or moves
            self.anchor_x = cursor_x
            self.anchor_y = cursor_y
            self.dwell_start_time = now
            self.state = DwellState.DWELLING
            progress = 0.0

        elif self.state == DwellState.DWELLING:
            if dist_from_anchor > self.dwell_radius:
                # User moved cursor outside dwell circle; reset anchor
                self.anchor_x = cursor_x
                self.anchor_y = cursor_y
                self.dwell_start_time = now
                progress = 0.0
            else:
                elapsed = now - self.dwell_start_time
                progress = min(1.0, elapsed / max(0.001, self.dwell_time_s))

                if progress >= 1.0:
                    # Trigger action
                    self.state = DwellState.TRIGGERED
                    if self.current_action == DwellAction.DRAG_TOGGLE:
                        self.is_dragging = not self.is_dragging
                    if self.on_action:
                        self.on_action(self.current_action, cursor_x, cursor_y)

                    self.state = DwellState.COOLDOWN
                    self.cooldown_end_time = now + self.cooldown_s
                    progress = 1.0
        else:
            progress = 0.0

        return DwellStatus(
            state=self.state,
            progress=progress,
            action=self.current_action,
            target_x=cursor_x,
            target_y=cursor_y,
            is_dragging=self.is_dragging,
        )

    def cancel(self) -> None:
        self.state = DwellState.IDLE
        self.dwell_start_time = 0.0


class CursorTracker:
    """Project Gameface-inspired head pose to smooth cursor motion pipeline.
    
    Applies deadzone filtering, power acceleration curves, 1€ smoothing,
    and sub-pixel remainder accumulation.
    """

    def __init__(
        self,
        sensitivity_x: float = 35.0,
        sensitivity_y: float = 30.0,
        curve_exponent: float = 1.4,
        deadzone_deg: float = 1.5,
        screen_width: int = 1920,
        screen_height: int = 1080,
        invert_x: bool = False,
        invert_y: bool = False,
        enable_dwell: bool = True,
        dwell_time_ms: float = 650.0,
        angle_range_x: float = 14.0,
        angle_range_y: float = 10.0,
        auto_calibrate: bool = False,
    ):
        # Legacy delta-based sensitivity params (kept for API compat)
        self.sensitivity_x = sensitivity_x
        self.sensitivity_y = sensitivity_y
        self.curve_exponent = curve_exponent
        self.deadzone_deg = deadzone_deg

        # Absolute mapping: head angle range that spans full screen.
        # e.g. angle_range_x=14 means tilting head 14° left/right reaches screen edges comfortably.
        self.angle_range_x = max(1.0, angle_range_x)
        self.angle_range_y = max(1.0, angle_range_y)

        # Auto-detect native display resolution on Windows
        import os
        if os.name == "nt" and screen_width == 1920 and screen_height == 1080:
            try:
                import ctypes
                w = ctypes.windll.user32.GetSystemMetrics(0)
                h = ctypes.windll.user32.GetSystemMetrics(1)
                if w > 0 and h > 0:
                    screen_width, screen_height = w, h
            except Exception:
                pass

        self.screen_width = screen_width
        self.screen_height = screen_height
        self.invert_x = invert_x
        self.invert_y = invert_y

        # Screen position tracking (starts center screen)
        self.cursor_x = float(screen_width // 2)
        self.cursor_y = float(screen_height // 2)

        # Calibrated neutral head angles & auto-calibration
        self.neutral_yaw = 0.0
        self.neutral_pitch = 0.0
        self.auto_calibrate = auto_calibrate
        self.calibrated = not auto_calibrate
        self._calib_samples: list[Tuple[float, float]] = []
        self._calib_target_samples = 20

        # One-Euro filters for jitter suppression
        self.filter_yaw = OneEuroFilter(freq=60.0, min_cutoff=0.8, beta=0.008)
        self.filter_pitch = OneEuroFilter(freq=60.0, min_cutoff=0.8, beta=0.008)

        # Dwell clicker integration
        self.dwell_clicker: Optional[DwellClicker] = (
            DwellClicker(dwell_time_ms=dwell_time_ms) if enable_dwell else None
        )

    def set_neutral(self, yaw: float, pitch: float) -> None:
        """Sets the zero-point reference for head aiming."""
        self.neutral_yaw = yaw
        self.neutral_pitch = pitch
        self.calibrated = True
        self.filter_yaw.reset()
        self.filter_pitch.reset()

    def recenter(self, yaw: Optional[float] = None, pitch: Optional[float] = None) -> None:
        """Recenters head aiming to provided angles or re-triggers auto-calibration."""
        if yaw is not None and pitch is not None:
            self.set_neutral(yaw, pitch)
        else:
            self.calibrated = False
            self._calib_samples = []

    def _apply_acceleration(self, displacement_deg: float) -> float:
        """Applies deadzone and nonlinear power acceleration transfer function."""
        abs_val = abs(displacement_deg)
        if abs_val <= self.deadzone_deg:
            return 0.0
        effective = abs_val - self.deadzone_deg
        curved = effective ** self.curve_exponent
        return math.copysign(curved, displacement_deg)

    def process_head_pose(
        self,
        raw_yaw: float,
        raw_pitch: float,
        timestamp: Optional[float] = None,
    ) -> Tuple[int, int, Tuple[float, float], Optional[DwellStatus]]:
        """Processes head pose into integer mouse step deltas (dx, dy),
        absolute cursor coordinates (x, y), and dwell status.

        Uses absolute angle-to-screen mapping with Gameface power curve:
          head tilt of ±angle_range_x degrees → cursor at screen left/right edge
          head tilt of ±angle_range_y degrees → cursor at screen top/bottom edge
        """
        now = timestamp if timestamp is not None else time.time()

        smooth_yaw = self.filter_yaw.filter(raw_yaw, now)
        smooth_pitch = self.filter_pitch.filter(raw_pitch, now)

        # Handle auto-calibration on startup
        if self.auto_calibrate and not self.calibrated:
            self._calib_samples.append((smooth_yaw, smooth_pitch))
            if len(self._calib_samples) >= self._calib_target_samples:
                avg_y = sum(s[0] for s in self._calib_samples) / len(self._calib_samples)
                avg_p = sum(s[1] for s in self._calib_samples) / len(self._calib_samples)
                self.set_neutral(avg_y, avg_p)
            else:
                self.cursor_x = float(self.screen_width // 2)
                self.cursor_y = float(self.screen_height // 2)
                return 0, 0, (self.cursor_x, self.cursor_y), None

        # Displacement from calibrated neutral
        disp_yaw = smooth_yaw - self.neutral_yaw
        disp_pitch = smooth_pitch - self.neutral_pitch

        # Inversion
        if self.invert_x:
            disp_yaw = -disp_yaw
        if self.invert_y:
            disp_pitch = -disp_pitch

        # Power curve mapping within [-angle_range, +angle_range]
        def _map_axis(disp: float, angle_range: float) -> float:
            abs_d = abs(disp)
            if abs_d <= self.deadzone_deg:
                return 0.0
            span = max(0.1, angle_range - self.deadzone_deg)
            linear_norm = min(1.0, (abs_d - self.deadzone_deg) / span)
            curved = linear_norm ** self.curve_exponent
            return math.copysign(curved, disp)

        eff_x = _map_axis(disp_yaw, self.angle_range_x)
        eff_y = _map_axis(disp_pitch, self.angle_range_y)

        norm_x = (eff_x + 1.0) / 2.0
        norm_y = (eff_y + 1.0) / 2.0
        norm_x = max(0.0, min(1.0, norm_x))
        norm_y = max(0.0, min(1.0, norm_y))

        new_x = norm_x * float(self.screen_width - 1)
        new_y = norm_y * float(self.screen_height - 1)

        prev_x = self.cursor_x
        prev_y = self.cursor_y
        self.cursor_x = new_x
        self.cursor_y = new_y

        int_dx = int(new_x - prev_x)
        int_dy = int(new_y - prev_y)

        dwell_status = None
        if self.dwell_clicker:
            dwell_status = self.dwell_clicker.update(self.cursor_x, self.cursor_y, timestamp=now)

        return int_dx, int_dy, (self.cursor_x, self.cursor_y), dwell_status

