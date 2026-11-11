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
        deadzone_deg: float = 0.8,
        screen_width: int = 1920,
        screen_height: int = 1080,
        invert_x: bool = False,
        invert_y: bool = False,
        enable_dwell: bool = True,
        dwell_time_ms: float = 650.0,
    ):
        self.sensitivity_x = sensitivity_x
        self.sensitivity_y = sensitivity_y
        self.curve_exponent = curve_exponent
        self.deadzone_deg = deadzone_deg

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

        # Sub-pixel accumulator
        self._rem_x = 0.0
        self._rem_y = 0.0

        # Calibrated neutral head angles
        self.neutral_yaw = 0.0
        self.neutral_pitch = 0.0

        # One-Euro filters for velocity stability
        self.filter_yaw = OneEuroFilter(freq=60.0, min_cutoff=1.2, beta=0.012)
        self.filter_pitch = OneEuroFilter(freq=60.0, min_cutoff=1.2, beta=0.012)

        # Dwell clicker integration
        self.dwell_clicker: Optional[DwellClicker] = (
            DwellClicker(dwell_time_ms=dwell_time_ms) if enable_dwell else None
        )

    def set_neutral(self, yaw: float, pitch: float) -> None:
        """Sets the zero-point reference for head aiming."""
        self.neutral_yaw = yaw
        self.neutral_pitch = pitch
        self.filter_yaw.reset()
        self.filter_pitch.reset()

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
        
        Returns:
            (delta_x, delta_y, (cursor_x, cursor_y), dwell_status)
        """
        now = timestamp if timestamp is not None else time.time()

        smooth_yaw = self.filter_yaw.filter(raw_yaw, now)
        smooth_pitch = self.filter_pitch.filter(raw_pitch, now)

        # Displacements from neutral
        disp_yaw = smooth_yaw - self.neutral_yaw
        disp_pitch = smooth_pitch - self.neutral_pitch

        # Apply Gameface curve
        curved_yaw = self._apply_acceleration(disp_yaw)
        curved_pitch = self._apply_acceleration(disp_pitch)

        # Inversion
        if self.invert_x:
            curved_yaw = -curved_yaw
        if self.invert_y:
            curved_pitch = -curved_pitch

        # Compute raw continuous pixel movement
        raw_dx = curved_yaw * (self.sensitivity_x / 10.0)
        raw_dy = curved_pitch * (self.sensitivity_y / 10.0)

        # Sub-pixel accumulation
        total_dx = raw_dx + self._rem_x
        total_dy = raw_dy + self._rem_y
        int_dx = int(total_dx)
        int_dy = int(total_dy)
        self._rem_x = total_dx - int_dx
        self._rem_y = total_dy - int_dy

        # Update absolute cursor clamp
        self.cursor_x = max(0.0, min(float(self.screen_width - 1), self.cursor_x + int_dx))
        self.cursor_y = max(0.0, min(float(self.screen_height - 1), self.cursor_y + int_dy))

        dwell_status = None
        if self.dwell_clicker:
            dwell_status = self.dwell_clicker.update(self.cursor_x, self.cursor_y, timestamp=now)

        return int_dx, int_dy, (self.cursor_x, self.cursor_y), dwell_status

