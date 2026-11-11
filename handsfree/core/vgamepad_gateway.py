"""Virtual Gamepad Gateway.

Abstracted and synthesized from vgamepad (yannbouteiller/vgamepad):
  - Emulates Xbox 360 controller (VX360Gamepad) via ViGEmBus
  - Graceful built-in mock fallback for environments without ViGEm driver / dry-run / CI
  - Analog thumbsticks (-32768 to 32767), analog triggers (0 to 255), and digital buttons
  - Thread-safe batching and emergency release_all() integration
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, Optional, Set, Tuple

logger = logging.getLogger("handsfree.vgamepad")

try:
    import vgamepad as vg
    HAS_VGAMEPAD = True
except ImportError:
    HAS_VGAMEPAD = False


class X360Button(IntEnum):
    """Xbox 360 controller button constants matching vgamepad.XUSB_BUTTON."""
    UP = 0x0001
    DOWN = 0x0002
    LEFT = 0x0004
    RIGHT = 0x0008
    START = 0x0010
    BACK = 0x0020
    LEFT_THUMB = 0x0040
    RIGHT_THUMB = 0x0080
    LEFT_SHOULDER = 0x0100
    RIGHT_SHOULDER = 0x0200
    GUIDE = 0x0400
    A = 0x1000
    B = 0x2000
    X = 0x4000
    Y = 0x8000


# Name alias map
BUTTON_NAME_MAP: Dict[str, X360Button] = {
    "A": X360Button.A,
    "B": X360Button.B,
    "X": X360Button.X,
    "Y": X360Button.Y,
    "LB": X360Button.LEFT_SHOULDER,
    "RB": X360Button.RIGHT_SHOULDER,
    "LEFT_SHOULDER": X360Button.LEFT_SHOULDER,
    "RIGHT_SHOULDER": X360Button.RIGHT_SHOULDER,
    "START": X360Button.START,
    "BACK": X360Button.BACK,
    "GUIDE": X360Button.GUIDE,
    "LS": X360Button.LEFT_THUMB,
    "RS": X360Button.RIGHT_THUMB,
    "DPAD_UP": X360Button.UP,
    "DPAD_DOWN": X360Button.DOWN,
    "DPAD_LEFT": X360Button.LEFT,
    "DPAD_RIGHT": X360Button.RIGHT,
}


@dataclass
class MockGamepadState:
    """State snapshot for the mock fallback gamepad."""
    left_x: int = 0
    left_y: int = 0
    right_x: int = 0
    right_y: int = 0
    left_trigger: int = 0
    right_trigger: int = 0
    buttons: Set[str] = field(default_factory=set)


class VirtualGamepad:
    """High-level Xbox 360 controller interface with hardware and fallback mock backends."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._lock = threading.Lock()
        self.real_gamepad = None
        self.mock_state = MockGamepadState()
        self.is_real = False

        if not dry_run and HAS_VGAMEPAD:
            try:
                self.real_gamepad = vg.VX360Gamepad()
                self.is_real = True
                logger.info("Real ViGEm Xbox 360 virtual controller attached successfully.")
            except Exception as e:
                logger.warning(f"Could not attach ViGEm controller ({e}); using virtual fallback.")
                self.real_gamepad = None

        if not self.is_real:
            logger.info("Operating in software mock virtual gamepad mode.")

    def set_left_joystick(self, x: float, y: float) -> None:
        """Sets left thumbstick (-1.0 to 1.0)."""
        clamped_x = max(-1.0, min(1.0, x))
        clamped_y = max(-1.0, min(1.0, y))
        int_x = int(clamped_x * 32767)
        int_y = int(clamped_y * 32767)

        with self._lock:
            self.mock_state.left_x = int_x
            self.mock_state.left_y = int_y
            if self.is_real and self.real_gamepad:
                self.real_gamepad.left_joystick(x_value=int_x, y_value=int_y)
                self.real_gamepad.update()

    def set_right_joystick(self, x: float, y: float) -> None:
        """Sets right thumbstick (-1.0 to 1.0)."""
        clamped_x = max(-1.0, min(1.0, x))
        clamped_y = max(-1.0, min(1.0, y))
        int_x = int(clamped_x * 32767)
        int_y = int(clamped_y * 32767)

        with self._lock:
            self.mock_state.right_x = int_x
            self.mock_state.right_y = int_y
            if self.is_real and self.real_gamepad:
                self.real_gamepad.right_joystick(x_value=int_x, y_value=int_y)
                self.real_gamepad.update()

    def set_triggers(self, left: float = 0.0, right: float = 0.0) -> None:
        """Sets left and right analog triggers (0.0 to 1.0)."""
        clamped_l = max(0.0, min(1.0, left))
        clamped_r = max(0.0, min(1.0, right))
        int_l = int(clamped_l * 255)
        int_r = int(clamped_r * 255)

        with self._lock:
            self.mock_state.left_trigger = int_l
            self.mock_state.right_trigger = int_r
            if self.is_real and self.real_gamepad:
                self.real_gamepad.left_trigger(value=int_l)
                self.real_gamepad.right_trigger(value=int_r)
                self.real_gamepad.update()

    def press_button(self, button_name: str) -> bool:
        """Presses an Xbox button (e.g. 'A', 'B', 'LB', 'START')."""
        btn = BUTTON_NAME_MAP.get(button_name.upper())
        if not btn:
            logger.warning(f"Unknown gamepad button: {button_name}")
            return False

        with self._lock:
            self.mock_state.buttons.add(button_name.upper())
            if self.is_real and self.real_gamepad:
                real_btn = getattr(vg.XUSB_BUTTON, f"XUSB_GAMEPAD_{btn.name}", None)
                if real_btn:
                    self.real_gamepad.press_button(button=real_btn)
                    self.real_gamepad.update()
        return True

    def release_button(self, button_name: str) -> bool:
        """Releases an Xbox button."""
        btn = BUTTON_NAME_MAP.get(button_name.upper())
        if not btn:
            return False

        with self._lock:
            self.mock_state.buttons.discard(button_name.upper())
            if self.is_real and self.real_gamepad:
                real_btn = getattr(vg.XUSB_BUTTON, f"XUSB_GAMEPAD_{btn.name}", None)
                if real_btn:
                    self.real_gamepad.release_button(button=real_btn)
                    self.real_gamepad.update()
        return True

    def release_all(self) -> None:
        """Safety deadman release: centers all sticks, resets triggers, releases all buttons."""
        with self._lock:
            self.mock_state.left_x = 0
            self.mock_state.left_y = 0
            self.mock_state.right_x = 0
            self.mock_state.right_y = 0
            self.mock_state.left_trigger = 0
            self.mock_state.right_trigger = 0
            self.mock_state.buttons.clear()

            if self.is_real and self.real_gamepad:
                try:
                    self.real_gamepad.reset()
                    self.real_gamepad.update()
                except Exception as e:
                    logger.error(f"Error resetting real vgamepad: {e}")

