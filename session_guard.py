"""session_guard.py (Binore): Windows session events and single-instance mutex.

SessionLockWindow: a hidden message-only HWND that receives:
  WM_WTSSESSION_CHANGE  → workstation lock / remote disconnect
  WM_POWERBROADCAST     → sleep / hibernate / display-off

SingleInstanceGuard: named kernel mutex — if another copy holds it, we exit cleanly.

Both classes degrade gracefully on non-Windows (no-ops / always-available).

All Win32 calls are in thin wrapper functions so tests can mock without ctypes.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional

log = logging.getLogger("omnicontrol.session_guard")


# ── mockable wrappers ─────────────────────────────────────────────────────────

def _create_mutex(name: str) -> tuple[int, bool]:
    """Returns (handle, already_exists). handle=0 on failure."""
    if os.name != "nt":
        return (1, False)
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    h = ctypes.windll.kernel32.CreateMutexW(None, True, name)
    exists = ctypes.GetLastError() == ERROR_ALREADY_EXISTS
    return (h, exists)


def _release_mutex(h: int) -> None:
    if os.name != "nt" or not h:
        return
    import ctypes
    ctypes.windll.kernel32.ReleaseMutex(h)
    ctypes.windll.kernel32.CloseHandle(h)


def _register_wts_notification(hwnd: int) -> bool:
    """WTSRegisterSessionNotification — returns True on success."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        NOTIFY_FOR_THIS_SESSION = 0
        return bool(ctypes.windll.wtsapi32.WTSRegisterSessionNotification(
            hwnd, NOTIFY_FOR_THIS_SESSION
        ))
    except Exception:
        return False


def _create_message_window(wndproc_type, wndproc, class_name: str) -> int:
    """Register a window class and create a message-only window. Returns hwnd or 0."""
    if os.name != "nt":
        return 0
    import ctypes

    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32
    HWND_MESSAGE = -3

    wc = ctypes.create_unicode_buffer(class_name)
    atom = u32.RegisterClassExW(ctypes.byref(_make_wndclassex(u32, k32, wndproc, class_name)))
    if not atom:
        # May already be registered — still try to create
        pass
    hwnd = u32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0,
                                HWND_MESSAGE, 0, k32.GetModuleHandleW(None), 0)
    return hwnd or 0


def _make_wndclassex(u32, k32, wndproc, class_name: str):
    """Build a WNDCLASSEX structure."""
    import ctypes

    class WNDCLASSEX(ctypes.Structure):
        _fields_ = [
            ("cbSize",        ctypes.c_uint),
            ("style",         ctypes.c_uint),
            ("lpfnWndProc",   ctypes.c_void_p),
            ("cbClsExtra",    ctypes.c_int),
            ("cbWndExtra",    ctypes.c_int),
            ("hInstance",     ctypes.c_void_p),
            ("hIcon",         ctypes.c_void_p),
            ("hCursor",       ctypes.c_void_p),
            ("hbrBackground", ctypes.c_void_p),
            ("lpszMenuName",  ctypes.c_wchar_p),
            ("lpszClassName", ctypes.c_wchar_p),
            ("hIconSm",       ctypes.c_void_p),
        ]

    wc = WNDCLASSEX()
    wc.cbSize = ctypes.sizeof(WNDCLASSEX)
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    wc.hInstance = k32.GetModuleHandleW(None)
    wc.lpszClassName = class_name
    return wc


def _pump_messages(hwnd: int, stop_event: threading.Event) -> None:
    """Run a Windows message loop until stop_event is set."""
    if os.name != "nt" or not hwnd:
        stop_event.wait()
        return
    import ctypes

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_size_t),
                    ("time", ctypes.c_uint), ("pt", ctypes.c_uint * 2)]

    u32 = ctypes.windll.user32
    msg = MSG()
    while not stop_event.is_set():
        # PeekMessage so we can honour stop_event
        if u32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, 1):  # PM_REMOVE=1
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        else:
            stop_event.wait(timeout=0.02)


# ── public classes ────────────────────────────────────────────────────────────

class SingleInstanceGuard:
    """Named mutex that ensures only one OmniControl runs at a time.

    Usage::
        guard = SingleInstanceGuard()
        if not guard.acquire():
            print("Already running"); sys.exit(1)
        # ... run app ...
        guard.release()
    """

    MUTEX_NAME = "Global\\OmniControl_SingleInstance_v1"

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self._name = name
        self._handle: int = 0

    def acquire(self) -> bool:
        """Returns True if we got the mutex (we are the first instance)."""
        h, exists = _create_mutex(self._name)
        self._handle = h
        if exists:
            log.error(
                "OmniControl is already running (mutex %r is held). "
                "Close the other copy first.",
                self._name,
            )
            return False
        return True

    def release(self) -> None:
        if self._handle:
            _release_mutex(self._handle)
            self._handle = 0


class SessionLockWindow:
    """Hidden message-only window that calls on_lock() on workstation lock or sleep.

    Degrades to a no-op on non-Windows.

    on_lock is called from the window-thread — it must only call gateway.release_all()
    or set a flag; never touch the state machine directly.
    """

    _CLASS_NAME = "OmniControlSessionWnd"

    def __init__(self, on_lock: Callable[[], None]) -> None:
        self._on_lock = on_lock
        self._hwnd: int = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="session-wnd", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if os.name != "nt":
            self._stop.wait()
            return
        import ctypes

        WM_WTSSESSION_CHANGE = 0x02B1
        WM_POWERBROADCAST    = 0x0218
        WTS_SESSION_LOCK     = 0x7
        PBT_APMSUSPEND       = 0x4
        PBT_APMRESUMEAUTOMATIC = 0x12
        DEFWINDOWPROC = ctypes.windll.user32.DefWindowProcW

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_size_t, ctypes.c_size_t,
        )

        def _wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_WTSSESSION_CHANGE and wparam == WTS_SESSION_LOCK:
                log.info("Workstation locked — releasing all keys")
                try:
                    self._on_lock()
                except Exception:
                    log.exception("on_lock callback failed")
            elif msg == WM_POWERBROADCAST and wparam in (PBT_APMSUSPEND,):
                log.info("System suspending — releasing all keys")
                try:
                    self._on_lock()
                except Exception:
                    log.exception("on_lock callback failed (suspend)")
            return DEFWINDOWPROC(hwnd, msg, wparam, lparam)

        cb = WNDPROC(_wndproc)
        self._cb_ref = cb  # keep alive
        self._hwnd = _create_message_window(WNDPROC, cb, self._CLASS_NAME)
        if self._hwnd:
            _register_wts_notification(self._hwnd)
        _pump_messages(self._hwnd, self._stop)

