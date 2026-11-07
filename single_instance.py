"""single_instance.py (Binore): only one live OmniControl per Windows session.

Two copies would both inject input and fight over the same keys, and the loser's "release" would
cancel the winner's holds. So the second copy must refuse to start.

Windows: a named mutex in the session-local namespace.
Elsewhere (dev machines, CI): an flock on a file in the temp dir, so tests can exercise the logic.

Elevation subtlety: a mutex created by an ELEVATED process has a DACL that a normal (non-elevated)
process of the same user cannot open. CreateMutexW then fails with ERROR_ACCESS_DENIED. That is
not an error for us: it means "an elevated copy is already running", so it counts as a duplicate.
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Optional

log = logging.getLogger("omnicontrol.single")

ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183

OWNER, DUPLICATE, UNKNOWN = "owner", "duplicate", "unknown"
APP_INSTANCE_NAME = "OmniControl"     # shared by main.py and doctor.py


def classify_mutex_result(handle: Optional[int], last_error: int) -> str:
    """Pure decision table for CreateMutexW(handle, GetLastError()). Unit-tested without Windows."""
    if handle:
        return DUPLICATE if last_error == ERROR_ALREADY_EXISTS else OWNER
    if last_error == ERROR_ACCESS_DENIED:
        return DUPLICATE                  # created by an elevated copy we are not allowed to open
    return UNKNOWN


class SingleInstance:
    def __init__(self, name: str = APP_INSTANCE_NAME) -> None:
        self.name = name
        self._handle: Optional[int] = None
        self._fd: Optional[int] = None
        self.owned = False

    def acquire(self) -> bool:
        """True if this process may run. False if another copy is already running.

        If the OS gives an error we cannot interpret, we let the app start (fail-open) and log it:
        locking a person out of their own controller is worse than a rare duplicate."""
        self.owned = self._acquire_windows() if os.name == "nt" else self._acquire_posix()
        return self.owned

    def release(self) -> None:
        if os.name == "nt":
            if self._handle:
                self._close_handle(self._handle)
                self._handle = None
        elif self._fd is not None:
            try:
                os.close(self._fd)           # closing the fd drops the flock
            except OSError:
                pass
            self._fd = None
        self.owned = False

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    # ------------------------------------------------------------------ windows
    def _create_mutex(self) -> tuple[Optional[int], int]:
        """Returns (handle or None, GetLastError). Separate method so tests can fake it."""
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k32.CreateMutexW.restype = wintypes.HANDLE
        ctypes.set_last_error(0)
        handle = k32.CreateMutexW(None, False, f"Local\\{self.name}-SingleInstance")
        return handle, ctypes.get_last_error()

    def _close_handle(self, handle: int) -> None:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle(handle)

    def _acquire_windows(self) -> bool:
        handle, err = self._create_mutex()
        verdict = classify_mutex_result(handle, err)
        if verdict == DUPLICATE:
            if handle:
                self._close_handle(handle)   # we opened an existing mutex; drop our reference
            return False
        if verdict == UNKNOWN:
            log.warning("single-instance mutex failed (GetLastError=%d); starting anyway", err)
            return True
        self._handle = handle                # keep it open for the whole process lifetime
        return True

    # ------------------------------------------------------------------ posix (dev / CI)
    def _acquire_posix(self) -> bool:
        import fcntl
        uid = os.getuid() if hasattr(os, "getuid") else 0
        path = os.path.join(tempfile.gettempdir(), f"{self.name}-{uid}.lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:                      # BlockingIOError: someone else holds it
            os.close(fd)
            return False
        self._fd = fd
        return True
