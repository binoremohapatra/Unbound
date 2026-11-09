"""adapters/switch.py: Adaptive Switch / Keyboard adapter (Step 11).

Provides an event source that turns keyboard keypresses or adaptive switches into SignalEvents.
Works with no special hardware (any keyboard key acts as the switch).
Injectable for 100% deterministic testing without physical keys.
"""
from __future__ import annotations

import collections
import os
from typing import Callable, List, Optional, Sequence, Union

from core.events import SignalEvent

# Virtual-Key codes for common keys on Windows
VK_MAP: dict[str, int] = {
    "space": 0x20,
    "f12": 0x7B,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "escape": 0x1B,
    "esc": 0x1B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}


def _get_vk(key: str) -> Optional[int]:
    k = key.lower()
    if k in VK_MAP:
        return VK_MAP[k]
    if len(k) == 1 and ("a" <= k <= "z" or "0" <= k <= "9"):
        return ord(k.upper())
    return None


def _win32_key_down(key: str) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        vk = _get_vk(key)
        if vk is not None:
            return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        pass
    return False


class SwitchSource:
    """Poll-based switch adapter conforming to Binore's main.py sources contract."""

    def __init__(
        self,
        monitored_keys: Sequence[str] = ("space", "f12", "switch"),
        key_state_fn: Optional[Callable[[str], bool]] = None,
    ):
        self.monitored_keys = [str(k).lower() for k in monitored_keys]
        self._key_state_fn = key_state_fn or _win32_key_down
        self._was_down: dict[str, bool] = {k: False for k in self.monitored_keys}
        self._injected: collections.deque[SignalEvent] = collections.deque()
        self.name = "switch"
        self.is_mock = (os.name != "nt" and key_state_fn is None)
        self.mock_reason = "simulated switch (non-Windows)" if self.is_mock else ""

    def inject(self, key: str, pressed: bool, t: float = 0.0) -> None:
        """Inject a switch state change programmatically (ideal for tests)."""
        k = key.lower()
        val = 1.0 if pressed else 0.0
        self._injected.append(SignalEvent("switch", k, val, t))
        self._was_down[k] = pressed

    def poll(self, now: float) -> List[SignalEvent]:
        events: List[SignalEvent] = []

        # 1. Drain any explicitly injected events
        injected_keys = set()
        while self._injected:
            ev = self._injected.popleft()
            events.append(ev)
            injected_keys.add(ev.name)
            self._was_down[ev.name] = (ev.value >= 0.5)

        # 2. Poll monitored physical/virtual keys for state transitions (edge detection)
        for key in self.monitored_keys:
            if key in injected_keys:
                continue
            try:
                down = bool(self._key_state_fn(key))
            except Exception:
                down = False

            was = self._was_down.get(key, False)
            if down != was:
                self._was_down[key] = down
                events.append(SignalEvent("switch", key, 1.0 if down else 0.0, now))

        return events

    def close(self) -> None:
        self._injected.clear()


# Callback adapter wrapper for backward compatibility with handsfree architecture
class SwitchAdapter:
    def __init__(
        self,
        monitored_keys: Optional[Sequence[str]] = None,
        on_event: Optional[Callable[[SignalEvent], None]] = None,
    ):
        self._source = SwitchSource(monitored_keys or ("space", "f12", "escape"))
        self.on_event = on_event
        self.is_running = False

    def start(self) -> None:
        self.is_running = True

    def stop(self) -> None:
        self.is_running = False
        self._source.close()


def create_source(
    monitored_keys: Sequence[str] = ("space", "f12", "switch"),
    key_state_fn: Optional[Callable[[str], bool]] = None,
    force: bool = False,
) -> SwitchSource:
    """Factory called by main.py _load_live_sources()."""
    if not force and os.environ.get("OMNICONTROL_SWITCH", "").lower() not in ("1", "true", "yes"):
        raise RuntimeError("No hardware adaptive switch configured (set OMNICONTROL_SWITCH=1 to enable keyboard switch)")
    return SwitchSource(monitored_keys=monitored_keys, key_state_fn=key_state_fn)
