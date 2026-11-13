"""text_focus_watcher.py - Background Windows Text Focus & Caret Detection.

Monitors active Windows UI focus for text input fields (Notepad, WhatsApp, browsers,
search boxes) by checking:
1. Cursor shape == IDC_IBEAM (0x00010007 / 32513) via GetCursorInfo
2. Win32 GUI Thread info (GetGUIThreadInfo flags & GUI_CARETBLINKING 0x00000001)

Auto-triggers FloatingKeyboardWindow dock when typing focus is detected.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("omniband.app.text_focus")

GUI_CARETBLINKING = 0x00000001
IDC_IBEAM_INT = 32513


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HICON),
        ("ptScreenPos", wintypes.POINT),
    ]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class TextFocusWatcher:
    """Monitors OS text input focus at 4-10 Hz and notifies callbacks."""

    def __init__(
        self,
        on_text_focus_changed: Optional[Callable[[bool], None]] = None,
        poll_hz: float = 6.0,
    ) -> None:
        self.on_text_focus_changed = on_text_focus_changed
        self.poll_hz = poll_hz
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._is_text_focused = False
        self._lock = threading.Lock()
        self._ibeam_handle = 0

        if os.name == "nt":
            try:
                # Load system default IBEAM cursor handle
                self._ibeam_handle = ctypes.windll.user32.LoadCursorW(0, IDC_IBEAM_INT)
            except Exception:
                pass

    @property
    def is_text_focused(self) -> bool:
        with self._lock:
            return self._is_text_focused

    def start(self) -> bool:
        if self._running or os.name != "nt":
            return False
        self._running = True
        self._thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="TextFocusWatcherLoop",
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.6)

    def _worker_loop(self) -> None:
        user32 = ctypes.windll.user32
        dt = 1.0 / max(1.0, self.poll_hz)

        while self._running:
            focused = False
            try:
                # 1. Check active caret blinking via GetGUIThreadInfo
                hwnd_fg = user32.GetForegroundWindow()
                if hwnd_fg:
                    pid = ctypes.c_ulong(0)
                    tid = user32.GetWindowThreadProcessId(hwnd_fg, ctypes.byref(pid))
                    if tid:
                        gti = GUITHREADINFO()
                        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
                        if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
                            if (gti.flags & GUI_CARETBLINKING) or (gti.hwndCaret != 0):
                                focused = True

                # 2. Check cursor icon (IDC_IBEAM)
                if not focused:
                    ci = CURSORINFO()
                    ci.cbSize = ctypes.sizeof(CURSORINFO)
                    if user32.GetCursorInfo(ctypes.byref(ci)):
                        # If cursor matches IBEAM handle or flags indicate text cursor
                        if ci.hCursor and (ci.hCursor == self._ibeam_handle):
                            focused = True

            except Exception:
                pass

            if focused != self._is_text_focused:
                with self._lock:
                    self._is_text_focused = focused
                if self.on_text_focus_changed:
                    try:
                        self.on_text_focus_changed(focused)
                    except Exception:
                        pass

            time.sleep(dt)
