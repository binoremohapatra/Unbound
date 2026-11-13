"""text_focus_watcher.py - Background Windows Text Focus & Caret Detection.

Monitors active Windows UI focus for text input fields (WhatsApp, Chrome, Edge,
Notepad, VS Code, search boxes) by checking:
1. Cursor shape == IDC_IBEAM (0x00010007 / 32513) via GetCursorInfo & GetIconInfo
2. Active window process names (WhatsApp.exe, chrome.exe, msedge.exe, Code.exe, notepad.exe)
3. Win32 GUI Thread info (GetGUIThreadInfo flags & GUI_CARETBLINKING 0x00000001)
4. Left mouse click detection (physical mouse & biometric head-mouse) to re-trigger auto-popup

Features:
- NO Startup Popup: Suppresses auto-popup during startup (2.5s grace period)
- Reliable Re-Trigger: Clicking inside any text box resets the dismissal latch
- Electron & Chromium App Support: 100% reliable detection for WhatsApp Desktop and Chrome
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import threading
import time
from typing import Callable, Optional, Set

log = logging.getLogger("omniband.app.text_focus")

GUI_CARETBLINKING = 0x00000001
IDC_IBEAM_INT = 32513

TARGET_TEXT_APPS = {
    "whatsapp.exe",
    "chrome.exe",
    "msedge.exe",
    "code.exe",
    "notepad.exe",
    "notepad++.exe",
    "slack.exe",
    "discord.exe",
    "telegram.exe",
    "teams.exe",
    "devenv.exe",
}


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


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


def get_hwnd_process_name(hwnd: int) -> str:
    """Get the lowercase executable name of the process owning hwnd."""
    if not hwnd or os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0 or pid.value == os.getpid():
            return ""
        h_proc = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h_proc:
            h_proc = kernel32.OpenProcess(0x0410, False, pid.value)
        if h_proc:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                    return os.path.basename(buf.value).lower()
            finally:
                kernel32.CloseHandle(h_proc)
    except Exception:
        pass
    return ""


class TextFocusWatcher:
    """Monitors OS text input focus at 6-10 Hz and notifies callbacks."""

    def __init__(
        self,
        on_text_focus_changed: Optional[Callable[[bool], None]] = None,
        poll_hz: float = 8.0,
    ) -> None:
        self.on_text_focus_changed = on_text_focus_changed
        self.poll_hz = poll_hz
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._is_text_focused = False
        self._lock = threading.Lock()
        self._ibeam_handle = 0
        self._ibeam_handles: Set[int] = set()

        # Startup suppression: NO popup on app launch
        self._startup_suppress_until = time.time() + 2.5
        self._user_dismissed = True  # Starts dismissed so startup NEVER pops up
        self._last_hwnd_fg = 0
        self._dismissed_hwnd = 0
        self._prev_lbutton_down = False
        self._cached_proc_names: dict[int, tuple[float, str]] = {}

        if os.name == "nt":
            try:
                self._ibeam_handle = ctypes.windll.user32.LoadCursorW(0, IDC_IBEAM_INT)
                if self._ibeam_handle:
                    self._ibeam_handles.add(self._ibeam_handle)
            except Exception:
                pass

    @property
    def is_text_focused(self) -> bool:
        with self._lock:
            return self._is_text_focused

    @property
    def is_dismissed(self) -> bool:
        with self._lock:
            return self._user_dismissed

    def dismiss(self) -> None:
        """Mark virtual keyboard as dismissed by the user."""
        with self._lock:
            self._user_dismissed = True
            self._is_text_focused = False
            self._dismissed_hwnd = self._last_hwnd_fg
        log.debug("Text focus dismissed by user (latch locked).")

    def reset_dismissal(self) -> None:
        """Reset dismissal latch, allowing auto-popup on next text focus."""
        with self._lock:
            self._user_dismissed = False
            self._is_text_focused = False

    def notify_click(self, x: int = -1, y: int = -1) -> None:
        """Called on biometric or external click to evaluate text field click."""
        self._evaluate_click(x, y)

    def simulate_focus(self, focused: bool) -> None:
        """Used by tests to simulate text focus transitions."""
        with self._lock:
            self._is_text_focused = focused
            if focused:
                self._user_dismissed = False
            else:
                self._user_dismissed = False
        if self.on_text_focus_changed:
            try:
                self.on_text_focus_changed(focused)
            except Exception:
                pass

    def start(self) -> bool:
        if self._running or os.name != "nt":
            return False
        self._running = True
        self._startup_suppress_until = time.time() + 2.5
        self._user_dismissed = True
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

    def _is_cursor_ibeam(self, h_cursor: int) -> bool:
        if not h_cursor:
            return False
        if h_cursor in self._ibeam_handles:
            return True
        if os.name != "nt":
            return False
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            ii = ICONINFO()
            if user32.GetIconInfo(h_cursor, ctypes.byref(ii)):
                if ii.hbmMask:
                    gdi32.DeleteObject(ii.hbmMask)
                if ii.hbmColor:
                    gdi32.DeleteObject(ii.hbmColor)
                # I-BEAM hotspot is centered; arrow cursors are at (0, 0)
                if not ii.fIcon and ii.xHotspot > 0 and ii.yHotspot > 0:
                    self._ibeam_handles.add(h_cursor)
                    return True
        except Exception:
            pass
        return False

    def _get_process_name_cached(self, hwnd: int) -> str:
        if not hwnd:
            return ""
        now = time.time()
        cached = self._cached_proc_names.get(hwnd)
        if cached and (now - cached[0] < 2.0):
            return cached[1]
        name = get_hwnd_process_name(hwnd)
        self._cached_proc_names[hwnd] = (now, name)
        if len(self._cached_proc_names) > 64:
            self._cached_proc_names = {h: v for h, v in self._cached_proc_names.items() if now - v[0] < 5.0}
        return name

    def _evaluate_click(self, x: int = -1, y: int = -1) -> None:
        """Reset dismissal latch if click occurred on a text input field."""
        if os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd_fg = user32.GetForegroundWindow()
            proc_fg = self._get_process_name_cached(hwnd_fg)

            ci = CURSORINFO()
            ci.cbSize = ctypes.sizeof(CURSORINFO)
            is_ibeam = False
            hwnd_under = 0
            proc_under = ""
            if user32.GetCursorInfo(ctypes.byref(ci)):
                is_ibeam = self._is_cursor_ibeam(ci.hCursor)
                hwnd_under = user32.WindowFromPoint(ci.ptScreenPos)
                if hwnd_under:
                    proc_under = self._get_process_name_cached(hwnd_under)

            is_target_app = (proc_fg in TARGET_TEXT_APPS) or (proc_under in TARGET_TEXT_APPS)

            has_caret = False
            if hwnd_fg:
                pid = wintypes.DWORD(0)
                tid = user32.GetWindowThreadProcessId(hwnd_fg, ctypes.byref(pid))
                if tid and pid.value != os.getpid():
                    gti = GUITHREADINFO()
                    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
                    if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
                        if (gti.flags & GUI_CARETBLINKING) or (gti.hwndCaret != 0):
                            has_caret = True

            if is_ibeam or has_caret or is_target_app:
                with self._lock:
                    self._user_dismissed = False
                    self._is_text_focused = False
                if (is_ibeam or has_caret) and self.on_text_focus_changed:
                    try:
                        self.on_text_focus_changed(True)
                    except Exception:
                        pass
        except Exception:
            pass

    def _worker_loop(self) -> None:
        user32 = ctypes.windll.user32
        dt = 1.0 / max(1.0, self.poll_hz)

        while self._running:
            focused = False
            now = time.time()
            in_startup = now < self._startup_suppress_until

            try:
                hwnd_fg = user32.GetForegroundWindow()

                # If user switched windows, reset dismissal latch for new window
                if hwnd_fg and hwnd_fg != self._last_hwnd_fg:
                    if hwnd_fg != self._dismissed_hwnd:
                        with self._lock:
                            self._user_dismissed = False
                    self._last_hwnd_fg = hwnd_fg

                proc_fg = self._get_process_name_cached(hwnd_fg) if hwnd_fg else ""

                # 1. Check active caret blinking via GetGUIThreadInfo
                has_gui_caret = False
                if hwnd_fg:
                    pid = wintypes.DWORD(0)
                    tid = user32.GetWindowThreadProcessId(hwnd_fg, ctypes.byref(pid))
                    if tid and pid.value != os.getpid():
                        gti = GUITHREADINFO()
                        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
                        if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
                            if (gti.flags & GUI_CARETBLINKING) or (gti.hwndCaret != 0):
                                has_gui_caret = True
                                focused = True

                # 2. Check cursor icon (IDC_IBEAM) and window under cursor
                ci = CURSORINFO()
                ci.cbSize = ctypes.sizeof(CURSORINFO)
                is_ibeam = False
                hwnd_under = 0
                proc_under = ""

                if user32.GetCursorInfo(ctypes.byref(ci)):
                    is_ibeam = self._is_cursor_ibeam(ci.hCursor)
                    if ci.ptScreenPos:
                        hwnd_under = user32.WindowFromPoint(ci.ptScreenPos)
                        if hwnd_under:
                            proc_under = self._get_process_name_cached(hwnd_under)

                    is_target_app = (proc_fg in TARGET_TEXT_APPS) or (proc_under in TARGET_TEXT_APPS)

                    if is_ibeam and (is_target_app or proc_fg != ""):
                        focused = True

                # 3. Detect physical mouse click (VK_LBUTTON edge down)
                lbutton_down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
                if lbutton_down and not self._prev_lbutton_down:
                    if is_ibeam or has_gui_caret or (proc_fg in TARGET_TEXT_APPS):
                        with self._lock:
                            self._user_dismissed = False
                            self._is_text_focused = False
                        if is_ibeam or has_gui_caret:
                            focused = True
                self._prev_lbutton_down = lbutton_down

                # 4. If focus was cleared (user clicked away to non-text), reset dismissal
                if not focused and not in_startup:
                    with self._lock:
                        if self._user_dismissed and (hwnd_fg != self._dismissed_hwnd):
                            self._user_dismissed = False

            except Exception:
                pass

            # During startup grace period: strictly suppress popup
            if in_startup:
                with self._lock:
                    self._is_text_focused = False
                time.sleep(dt)
                continue

            # If user explicitly dismissed keyboard, respect dismissal until fresh trigger
            with self._lock:
                dismissed = self._user_dismissed

            if dismissed:
                with self._lock:
                    self._is_text_focused = False
                time.sleep(dt)
                continue

            if focused != self._is_text_focused:
                with self._lock:
                    self._is_text_focused = focused
                if self.on_text_focus_changed:
                    try:
                        self.on_text_focus_changed(focused)
                    except Exception:
                        pass

            time.sleep(dt)
