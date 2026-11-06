"""Hardware Output Gateway: vgamepad and pydirectinput dispatch with safety release_all."""

import logging
from typing import Dict, Optional, Set
from handsfree.core.mapping import OutputCommand

logger = logging.getLogger("handsfree.gateway")

try:
    import vgamepad as vg
    HAS_VGAMEPAD = True
except ImportError:
    HAS_VGAMEPAD = False

try:
    import pydirectinput
    HAS_PYDIRECTINPUT = True
except ImportError:
    HAS_PYDIRECTINPUT = False


class OutputGateway:
    """Dispatches OutputCommands to virtual gamepad and keyboard/mouse, maintaining pressed set."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.pressed_buttons: Set[str] = set()
        self.pressed_keys: Set[str] = set()
        self.axes: Dict[str, float] = {}

        self.gamepad = None
        if not dry_run and HAS_VGAMEPAD:
            try:
                self.gamepad = vg.VX360Gamepad()
                logger.info("Virtual Xbox 360 Gamepad initialized.")
            except Exception as e:
                logger.warning(f"Failed to initialize vgamepad: {e}. Falling back to dry-run mode.")
        elif not HAS_VGAMEPAD:
            logger.info("vgamepad not installed; operating in virtual/dry-run mode.")

    def dispatch(self, command: OutputCommand) -> None:
        if self.dry_run:
            logger.debug(f"[DRY_RUN] Dispatch: {command}")
            if command.command_type in ("button", "key"):
                if command.value > 0.5:
                    self.pressed_buttons.add(command.target)
                else:
                    self.pressed_buttons.discard(command.target)
            return

        if command.device == "gamepad":
            self._dispatch_gamepad(command)
        elif command.device == "keyboard":
            self._dispatch_keyboard(command)
        elif command.device == "mouse":
            self._dispatch_mouse(command)

    def _dispatch_gamepad(self, cmd: OutputCommand) -> None:
        if not self.gamepad:
            return

        if cmd.command_type == "axis":
            self.axes[cmd.target] = cmd.value
            if cmd.target == "X":
                val = int(cmd.value * 32767)
                self.gamepad.left_joystick(x_value=val, y_value=int(self.axes.get("Y", 0.0) * 32767))
            elif cmd.target == "Y":
                val = int(cmd.value * 32767)
                self.gamepad.left_joystick(x_value=int(self.axes.get("X", 0.0) * 32767), y_value=val)
            elif cmd.target == "LT":
                self.gamepad.left_trigger_float(value_float=max(0.0, cmd.value))
            elif cmd.target == "RT":
                self.gamepad.right_trigger_float(value_float=max(0.0, cmd.value))
            self.gamepad.update()

        elif cmd.command_type == "button":
            btn_const = getattr(vg.XUSB_BUTTON, cmd.target, None) if HAS_VGAMEPAD else None
            if cmd.value > 0.5:
                self.pressed_buttons.add(cmd.target)
                if btn_const and self.gamepad:
                    self.gamepad.press_button(button=btn_const)
                    self.gamepad.update()
            else:
                self.pressed_buttons.discard(cmd.target)
                if btn_const and self.gamepad:
                    self.gamepad.release_button(button=btn_const)
                    self.gamepad.update()

    def _dispatch_keyboard(self, cmd: OutputCommand) -> None:
        if not HAS_PYDIRECTINPUT:
            return

        if cmd.value > 0.5:
            if cmd.target not in self.pressed_keys:
                pydirectinput.keyDown(cmd.target)
                self.pressed_keys.add(cmd.target)
        else:
            if cmd.target in self.pressed_keys:
                pydirectinput.keyUp(cmd.target)
                self.pressed_keys.discard(cmd.target)

    def _dispatch_mouse(self, cmd: OutputCommand) -> None:
        if not HAS_PYDIRECTINPUT:
            return
        if cmd.command_type == "button":
            if cmd.value > 0.5:
                pydirectinput.mouseDown(button=cmd.target)
            else:
                pydirectinput.mouseUp(button=cmd.target)

    def release_all(self) -> None:
        """Emergency safety procedure: releases all pressed buttons, keys, and centers axes."""
        logger.warning("Safety: executing release_all() on all virtual devices.")

        if self.gamepad and HAS_VGAMEPAD:
            try:
                self.gamepad.reset()
                self.gamepad.update()
            except Exception as e:
                logger.error(f"Error resetting gamepad: {e}")

        if HAS_PYDIRECTINPUT:
            for key in list(self.pressed_keys):
                try:
                    pydirectinput.keyUp(key)
                except Exception as e:
                    logger.error(f"Error releasing key {key}: {e}")

        self.pressed_buttons.clear()
        self.pressed_keys.clear()
        self.axes.clear()

