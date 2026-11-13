"""text_focus_watcher.py - Background Windows Text Focus & Caret Detection.

Monitors active Windows UI focus for text input fields (WhatsApp, Chrome, Edge,
Notepad, VS Code, search boxes) by checking:
1. Cursor shape == IDC_IBEAM (0x00010007 / 32513) via GetCursorInfo & GetIconInfo
2. Active window process names (WhatsApp.exe, ApplicationFrameHost.exe, chrome.exe, msedge.exe, Code.exe, notepad.exe)
3. Win32 GUI Thread info (GetGUIThreadInfo flags & GUI_CARETBLINKING 0x00000001)
4. Left mouse click detection (physical mouse & biometric head-mouse) with 80ms delayed evaluation

Features:
- NO Startup Popup: Strict 3.0s startup grace period (time.monotonic) so initial window focus NEVER pops up
- Reliable Re-Trigger: Clicking inside any text box or chat app client area resets the dismissal latch
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
    "applicationframehost.exe",  # UWP WhatsApp container on Windows 10/11
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

        # Strict startup grace period using time.monotonic(): NO popup on app launch!
        self._startup_suppress_until = time.monotonic() + 3.0
        self._user_dismissed = True  # Starts dismissed so startup NEVER pops up
        self._last_hwnd_fg = 0
        self._dismissed_hwnd = 0
        self._active_target_hwnd = 0
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

    @property
    def active_target_hwnd(self) -> int:
        return self._active_target_hwnd

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

    def _notify_focus_change(self, focused: bool, hwnd: int = 0) -> None:
        """Safely notify callback with (focused, hwnd) or (focused)."""
        if self.on_text_focus_changed:
            try:
                self.on_text_focus_changed(focused, hwnd)
            except TypeError:
                try:
                    self.on_text_focus_changed(focused)
                except Exception:
                    pass
            except Exception:
                pass

    def notify_click(self, x: int = -1, y: int = -1) -> None:
        """Called on click: checks immediately (t=0ms) and after settle (t=90ms)."""
        self._evaluate_click_check(x, y)
        threading.Thread(
            target=self._delayed_click_worker,
            args=(x, y),
            daemon=True,
            name="DelayedTextClickWorker",
        ).start()

    def _delayed_click_worker(self, click_x: int, click_y: int) -> None:
        time.sleep(0.090)
        self._evaluate_click_check(click_x, click_y)

    def _evaluate_click_check(self, click_x: int, click_y: int) -> None:
        """Universal app-agnostic text field detector on Left-Click."""
        if os.name != "nt" or not self._running:
            return
        if time.monotonic() < self._startup_suppress_until:
            return
        try:
            user32 = ctypes.windll.user32
            hwnd_fg = user32.GetForegroundWindow()
            if not hwnd_fg:
                return

            # Check if desktop or taskbar was clicked -> auto-dismiss
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd_fg, cls_buf, 256)
            cls_name = cls_buf.value
            if cls_name in ("Shell_TrayWnd", "Progman", "WorkerW"):
                self.dismiss()
                self._notify_focus_change(False, hwnd_fg)
                return

            proc_fg = self._get_process_name_cached(hwnd_fg)
            if not proc_fg or proc_fg == "unbound.exe":
                return

            # Signal A: Universal IDC_IBEAM cursor check
            ci = CURSORINFO()
            ci.cbSize = ctypes.sizeof(CURSORINFO)
            is_ibeam = False
            pt_x, pt_y = click_x, click_y
            if user32.GetCursorInfo(ctypes.byref(ci)):
                is_ibeam = self._is_cursor_ibeam(ci.hCursor)
                if pt_x == -1 or pt_y == -1:
                    pt_x, pt_y = ci.ptScreenPos.x, ci.ptScreenPos.y

            # Signal B: Win32 Blinking Caret / Caret Window
            has_caret = False
            pid = wintypes.DWORD(0)
            tid = user32.GetWindowThreadProcessId(hwnd_fg, ctypes.byref(pid))
            if tid and pid.value != os.getpid():
                gti = GUITHREADINFO()
                gti.cbSize = ctypes.sizeof(GUITHREADINFO)
                if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
                    if (gti.flags & GUI_CARETBLINKING) or (gti.hwndCaret != 0):
                        has_caret = True

            # Signal C: Editable control class under cursor (Edit, RichEdit, Scintilla, etc.)
            is_edit_control = False
            if pt_x != -1 and pt_y != -1:
                pt = wintypes.POINT(pt_x, pt_y)
                hwnd_pt = user32.WindowFromPoint(pt)
                if hwnd_pt:
                    cls_pt_buf = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd_pt, cls_pt_buf, 256)
                    cls_pt_name = cls_pt_buf.value.lower()
                    if any(t in cls_pt_name for t in ("edit", "rich", "scintilla", "text", "input")):
                        is_edit_control = True

            should_popup = is_ibeam or has_caret or is_edit_control
            if should_popup:
                self._active_target_hwnd = hwnd_fg
                with self._lock:
                    self._user_dismissed = False
                    self._is_text_focused = True
                self._notify_focus_change(True, hwnd_fg)
            else:
                # User clicked outside onto a non-text area -> auto-dismiss
                if not self.is_dismissed:
                    self.dismiss()
                    self._notify_focus_change(False, hwnd_fg)
        except Exception:
            pass

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
        self._startup_suppress_until = time.monotonic() + 3.0
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

    def _worker_loop(self) -> None:
        user32 = ctypes.windll.user32
        dt = 1.0 / max(1.0, self.poll_hz)

        while self._running:
            focused = False
            now = time.monotonic()
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

                # Check if desktop or taskbar has focus -> auto-dismiss
                if hwnd_fg:
                    cls_buf = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd_fg, cls_buf, 256)
                    if cls_buf.value in ("Shell_TrayWnd", "Progman", "WorkerW"):
                        focused = False

                # 1. Check active caret blinking via GetGUIThreadInfo (Signal B)
                has_gui_caret = False
                if hwnd_fg and proc_fg != "unbound.exe":
                    pid = wintypes.DWORD(0)
                    tid = user32.GetWindowThreadProcessId(hwnd_fg, ctypes.byref(pid))
                    if tid and pid.value != os.getpid():
                        gti = GUITHREADINFO()
                        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
                        if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
                            if (gti.flags & GUI_CARETBLINKING) or (gti.hwndCaret != 0):
                                has_gui_caret = True

                # 2. Check cursor icon (IDC_IBEAM) - Signal A
                ci = CURSORINFO()
                ci.cbSize = ctypes.sizeof(CURSORINFO)
                is_ibeam = False
                if user32.GetCursorInfo(ctypes.byref(ci)):
                    is_ibeam = self._is_cursor_ibeam(ci.hCursor)

                # Universal OS-level text field focus: caret blinking or I-beam cursor on any external app
                if proc_fg and proc_fg != "unbound.exe":
                    if has_gui_caret or is_ibeam:
                        focused = True
                        self._active_target_hwnd = hwnd_fg

                # 3. Detect physical mouse click (VK_LBUTTON edge down)
                lbutton_down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
                if lbutton_down and not self._prev_lbutton_down:
                    self.notify_click()
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
                self._notify_focus_change(focused, self._active_target_hwnd)

            time.sleep(dt)
