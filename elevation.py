"""elevation.py (Binore): admin awareness and UIPI detection.

Why this exists: Windows blocks input injection from a normal process into an elevated window
(UIPI). Microsoft documents that SendInput gives NO signal for this: neither the return value
nor GetLastError reports a UIPI block. So "SendInput returned n" does not prove the game got the
key. The only reliable defence is to know up front that the foreground app is elevated while we
are not, tell the user, and offer to restart as admin.

Pieces:
  is_admin()                 are WE elevated
  relaunch_as_admin(args)    UAC "runas" relaunch (the --elevate flag)
  process_elevation(pid)     elevated / normal / likely_elevated / unknown for any process
  ElevationMonitor           caches the probe per foreground pid; reports only on change

The token probe is a heuristic where noted. It has only been exercised through fakes on Linux.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

log = logging.getLogger("omnicontrol.elevation")

ELEVATE_FLAG = "--elevate"        # user asks: restart me as admin if I am not
ELEVATED_FLAG = "--elevated"      # marker on the relaunched copy so we can never loop

NORMAL, ELEVATED, LIKELY_ELEVATED, UNKNOWN = "normal", "elevated", "likely_elevated", "unknown"

ERROR_ACCESS_DENIED = 5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INFO_ELEVATION = 20          # TOKEN_INFORMATION_CLASS: TokenElevation
SW_SHOWNORMAL = 1
UNKNOWN_ERR = -1                   # "a Win32 call failed but GetLastError was 0"


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", ctypes.c_uint32)]


# ------------------------------------------------------------------ are we admin
def is_admin() -> bool:
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            log.exception("IsUserAnAdmin failed")
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


# ------------------------------------------------------------------ relaunch as admin
def build_relaunch(args: Sequence[str], *, frozen: bool, executable: str,
                   script: str) -> tuple[str, str]:
    """(file, parameters) for ShellExecuteW. Pure, so it is unit-tested.

    --elevate is stripped and --elevated is appended: the new copy knows it already tried."""
    clean = [a for a in args if a not in (ELEVATE_FLAG, ELEVATED_FLAG)]
    clean.append(ELEVATED_FLAG)
    if frozen:                                   # PyInstaller exe: the exe is the program
        return executable, subprocess.list2cmdline(clean)
    return executable, subprocess.list2cmdline([script, *clean])


def relaunch_as_admin(args: Sequence[str]) -> bool:
    """Ask Windows for a UAC prompt and start an elevated copy.

    True  = the request was accepted (user clicked Yes). Caller should exit this copy.
    False = not Windows, or user declined UAC, or it failed. Caller keeps running normally."""
    if os.name != "nt":
        return False
    from ctypes import wintypes
    file, params = build_relaunch(args, frozen=bool(getattr(sys, "frozen", False)),
                                  executable=sys.executable,
                                  script=os.path.abspath(sys.argv[0]))
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int)
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    rc = shell32.ShellExecuteW(None, "runas", file, params, os.getcwd(), SW_SHOWNORMAL)
    ok = int(rc or 0) > 32                       # <= 32 is an error code (5 = user said No)
    if not ok:
        log.warning("elevation request not accepted (ShellExecute code %s)", int(rc or 0))
    return ok


# ------------------------------------------------------------------ is that process elevated
def classify_probe(open_err: int, token_err: int, elevated: Optional[bool]) -> str:
    """Pure decision table for the Win32 probe results (0 = that step succeeded).

    ACCESS_DENIED while opening a same-session app's process or token means it runs at a higher
    privilege than us. That is a heuristic (a protected system process also denies), but a
    foreground window we cannot inspect is exactly one we probably cannot type into."""
    if open_err:
        return LIKELY_ELEVATED if open_err == ERROR_ACCESS_DENIED else UNKNOWN
    if token_err:
        return LIKELY_ELEVATED if token_err == ERROR_ACCESS_DENIED else UNKNOWN
    if elevated is None:
        return UNKNOWN
    return ELEVATED if elevated else NORMAL


def uipi_blocked(self_admin: bool, target_state: str) -> bool:
    """True when our injected input will silently be dropped."""
    return (not self_admin) and target_state in (ELEVATED, LIKELY_ELEVATED)


_api: Optional[tuple] = None


def _win_api():
    """Private WinDLL handles, so we never change argtypes on the shared ctypes.windll objects."""
    global _api
    if _api is None:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        adv.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE))
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        adv.GetTokenInformation.restype = wintypes.BOOL
        _api = (k32, adv, wintypes)
    return _api


def _probe_windows(pid: int) -> tuple[int, int, Optional[bool]]:
    """(open_err, token_err, token_is_elevated). Raw Win32; see classify_probe for the logic."""
    k32, adv, wintypes = _win_api()
    ctypes.set_last_error(0)
    proc = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not proc:
        return ctypes.get_last_error() or UNKNOWN_ERR, 0, None
    try:
        tok = wintypes.HANDLE()
        ctypes.set_last_error(0)
        if not adv.OpenProcessToken(proc, TOKEN_QUERY, ctypes.byref(tok)):
            return 0, ctypes.get_last_error() or UNKNOWN_ERR, None
        try:
            info, size = TOKEN_ELEVATION(), wintypes.DWORD()
            ok = adv.GetTokenInformation(tok, TOKEN_INFO_ELEVATION, ctypes.byref(info),
                                         ctypes.sizeof(info), ctypes.byref(size))
            return 0, 0, (bool(info.TokenIsElevated) if ok else None)
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(proc)



def process_elevation(pid: int) -> str:
    if os.name != "nt" or not pid:
        return UNKNOWN
    try:
        return classify_probe(*_probe_windows(pid))
    except Exception:
        log.exception("elevation probe failed for pid %s", pid)
        return UNKNOWN


# ------------------------------------------------------------------ foreground monitor
@dataclass(frozen=True)
class ForegroundInfo:
    pid: int
    state: str
    blocked: bool          # True = our input is being dropped by UIPI right now


class ElevationMonitor:
    """Call update(pid) every tick with the foreground pid. It probes only when the pid changes
    and returns the new ForegroundInfo then, otherwise None (cheap enough for a 60 Hz loop)."""

    def __init__(self, self_admin: Optional[bool] = None,
                 probe: Optional[Callable[[int], str]] = None) -> None:
        self.self_admin = is_admin() if self_admin is None else self_admin
        self._probe = probe or process_elevation
        self._pid = 0
        self.info = ForegroundInfo(0, UNKNOWN, False)

    def update(self, pid: int) -> Optional[ForegroundInfo]:
        if not pid or pid == self._pid:
            return None
        self._pid = pid
        state = self._probe(pid)
        self.info = ForegroundInfo(pid, state, uipi_blocked(self.self_admin, state))
        return self.info
