"""Adaptive Switch / Keyboard adapter: handles physical accessibility switches or key triggers."""

import logging
from typing import Callable, Optional, Set
from handsfree.core.events import SignalEvent, SignalType

logger = logging.getLogger("handsfree.adapter.switch")

try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False


class SwitchAdapter:
    """Listens for keypresses or USB adaptive switch buttons to trigger discrete events."""

    def __init__(
        self,
        monitored_keys: Optional[Set[str]] = None,
        on_event: Optional[Callable[[SignalEvent], None]] = None,
    ):
        self.monitored_keys = monitored_keys or {"space", "f12", "escape"}
        self.on_event = on_event
        self.is_running = False

    def start(self) -> None:
        self.is_running = True
        if not HAS_KEYBOARD:
            logger.warning("Keyboard library not installed. Switch adapter in passive mode.")
            return

        try:
            for key in self.monitored_keys:
                keyboard.on_press_key(key, self._on_key_press)
                keyboard.on_release_key(key, self._on_key_release)
            logger.info(f"Switch adapter listening on keys: {self.monitored_keys}")
        except Exception as e:
            logger.error(f"Failed to hook keyboard switches: {e}")

    def stop(self) -> None:
        self.is_running = False
        if HAS_KEYBOARD:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
        logger.info("Switch adapter stopped.")

    def _on_key_press(self, event) -> None:
        if self.on_event:
            self.on_event(
                SignalEvent(
                    signal_type=SignalType.SWITCH,
                    name=event.name,
                    value=1.0,
                    metadata={"state": "pressed"},
                )
            )

    def _on_key_release(self, event) -> None:
        if self.on_event:
            self.on_event(
                SignalEvent(
                    signal_type=SignalType.SWITCH,
                    name=event.name,
                    value=0.0,
                    metadata={"state": "released"},
                )
            )

