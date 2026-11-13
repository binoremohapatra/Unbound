"""physical_keys.py - High-speed Win32 GetAsyncKeyState background poller.

Polls physical hardware keys:
  F7  -> CH1 (Steer Left  / 'a' / Key_A)
  F8  -> CH2 (Steer Right / 'd' / Key_D)
  F9  -> CH3 (Forward Cruise Latch / 'w' / Key_W)
  F10 -> CH4 (Gesture FSM Trigger: SINGLE=Left Click / Space, DOUBLE, TRIPLE)

Feeds both the visual oscilloscope and the Gateway real SendInput pipeline.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("omniband.physical_keys")

VK_F7  = 0x76
VK_F8  = 0x77
VK_F9  = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A
VK_F12 = 0x7B


class PhysicalKeyPoller:
    """Polls F7-F10 (Channels), F11 (Head-Mouse Toggle), F12 (Arm/Disarm)."""

    def __init__(
        self,
        on_edge: Optional[Callable[[int, bool, float], None]] = None,
        on_envelopes: Optional[Callable[[list[float], list[bool]], None]] = None,
        on_f11_toggle: Optional[Callable[[], None]] = None,
        on_f12_toggle: Optional[Callable[[], None]] = None,
    ) -> None:
        self.on_edge = on_edge
        self.on_envelopes = on_envelopes
        self.on_f11_toggle = on_f11_toggle
        self.on_f12_toggle = on_f12_toggle
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._f7 = False
        self._f8 = False
        self._f9 = False
        self._f10 = False
        self._f11 = False
        self._f12 = False

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="PhysicalKeyPoller")
        self._thread.start()
        log.info("PhysicalKeyPoller (F7-F10) started")
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def _poll_loop(self) -> None:
        user32 = ctypes.windll.user32

        while self._running:
            now = time.perf_counter()
            f7_down = bool(user32.GetAsyncKeyState(VK_F7) & 0x8000)
            f8_down = bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)
            f9_down = bool(user32.GetAsyncKeyState(VK_F9) & 0x8000)
            f10_down = bool(user32.GetAsyncKeyState(VK_F10) & 0x8000)
            f11_down = bool(user32.GetAsyncKeyState(VK_F11) & 0x8000)
            f12_down = bool(user32.GetAsyncKeyState(VK_F12) & 0x8000)

            # F11 Head-Mouse Toggle
            if f11_down and not self._f11:
                if self.on_f11_toggle:
                    self.on_f11_toggle()
            self._f11 = f11_down

            # F12 Master Arm/Disarm Toggle
            if f12_down and not self._f12:
                if self.on_f12_toggle:
                    self.on_f12_toggle()
            self._f12 = f12_down

            # Edge dispatch
            if f7_down != self._f7:
                self._f7 = f7_down
                if self.on_edge:
                    self.on_edge(1, f7_down, 950.0 if f7_down else 0.0)

            if f8_down != self._f8:
                self._f8 = f8_down
                if self.on_edge:
                    self.on_edge(2, f8_down, 950.0 if f8_down else 0.0)

            if f9_down != self._f9:
                self._f9 = f9_down
                if self.on_edge:
                    self.on_edge(3, f9_down, 950.0 if f9_down else 0.0)

            if f10_down != self._f10:
                self._f10 = f10_down
                if self.on_edge:
                    self.on_edge(4, f10_down, 950.0 if f10_down else 0.0)

            # Envelope dispatch for visual oscilloscope
            if any([self._f7, self._f8, self._f9, self._f10]) and self.on_envelopes:
                vals = [
                    0.95 if self._f7 else 0.0,
                    0.95 if self._f8 else 0.0,
                    0.95 if self._f9 else 0.0,
                    0.95 if self._f10 else 0.0,
                ]
                active = [self._f7, self._f8, self._f9, self._f10]
                self.on_envelopes(vals, active)

            time.sleep(0.004)  # ~250 Hz poll rate
