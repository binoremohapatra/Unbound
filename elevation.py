"""elevation.py (Binore): Windows privilege detection and controlled UAC re-launch.

Design rules (never violated):
  - Never disable security software, never edit the registry for UIPI.
  - Never use drivers or kernel tricks to bypass integrity levels.
  - If the user declines UAC, continue non-elevated with a clear warning.
  - uiAccess="true" requires a code-signed exe in a trusted location; documented only.

Known hard limits (by OS design, not bugs):
  - Input cannot reach the UAC secure desktop (Ctrl+Alt+Del, UAC prompt).
  - Input cannot reach the Windows lock screen or logonUI.exe.
  - Games protected by kernel anti-cheat (BattlEye, EAC, Vanguard kernel driver) block
    SendInput at a level below any user-mode process — admin does not help.
  - Online games may ban accounts that inject synthetic input, regardless of method.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

log = logging.getLogger("omnicontrol.elevation")

UIPI_LIMITS = """
Known limits (by Windows design — not bugs we can work around):
  • Input cannot reach the UAC secure desktop (Ctrl+Alt+Del, UAC prompts, Credential UI).
  • Input cannot reach the Windows lock screen or LogonUI.exe.
  • Kernel anti-cheat (BattlEye, EAC, Vanguard kernel-driver mode) blocks SendInput below
    user-mode; running as admin does NOT help against these.
  • Online games: injected input may trigger anti-cheat bans. Use ONLY story / offline modes.
  • uiAccess="true" (inject into higher-integrity contexts) requires a code-signed .exe
    installed under C:\\Program Files\\... — documented as an optional advanced path only.
"""


# ── mockable OS wrappers ─────────────────────────────────────────────────────
# Every Windows API call goes through one of these thin functions so unit tests
# can patch them without touching ctypes.

def _is_user_an_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _shell_execute_w(hwnd, op: str, file: str, params: str,
                     directory: Optional[str], show: int) -> int:
    """ShellExecuteW returns > 32 on success."""
    if os.name != "nt":
        return 0
    import ctypes
    return ctypes.windll.shell32.ShellExecuteW(hwnd, op, file, params, directory, show)


def _open_process(access: int, inherit: bool, pid: int) -> int:
    if os.name != "nt":
        return 0
    import ctypes
    return ctypes.windll.kernel32.OpenProcess(access, inherit, pid)


def _close_handle(h: int) -> None:
    if os.name != "nt" or not h:
        return
    import ctypes
    ctypes.windll.kernel32.CloseHandle(h)


def _open_process_token(process: int, access: int, token_ref) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    return bool(ctypes.windll.advapi32.OpenProcessToken(process, access, token_ref))


def _get_token_information(token, info_class: int, buf, buf_size: int, ret_len) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    return bool(ctypes.windll.advapi32.GetTokenInformation(
        token, info_class, buf, buf_size, ret_len))


def _get_foreground_window_pid() -> int:
    """Returns PID of the current foreground window, or 0 on failure."""
    if os.name != "nt":
        return 0
    import ctypes
    u32 = ctypes.windll.user32
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = ctypes.c_ulong(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


# ── public API ───────────────────────────────────────────────────────────────

def is_admin() -> bool:
    """True if the current process holds administrator privileges."""
    return _is_user_an_admin()


def is_pid_elevated(pid: int) -> bool:
    """True if the process `pid` is running elevated (high integrity).

    Uses OpenProcess → OpenProcessToken → GetTokenInformation(TokenElevation).
    Returns False on any error — safe default.
    """
    if os.name != "nt" or not pid:
        return False
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x0008
    TokenElevation = 20          # TOKEN_INFORMATION_CLASS value

    h_proc = _open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_proc:
        return False
    try:
        token = ctypes.c_void_p(0)
        if not _open_process_token(h_proc, TOKEN_QUERY, ctypes.byref(token)):
            return False
        try:
            elevation = ctypes.c_uint(0)
            ret_len = ctypes.c_ulong(0)
            ok = _get_token_information(
                token, TokenElevation,
                ctypes.byref(elevation), ctypes.sizeof(elevation),
                ctypes.byref(ret_len),
            )
            return bool(elevation.value) if ok else False
        finally:
            _close_handle(token.value)
    finally:
        _close_handle(h_proc)


def check_foreground_elevation() -> tuple[bool, str]:
    """Check whether the foreground window's process is elevated while we are not.

    Returns (problem, message). problem=True means UIPI will block our input.
    """
    if is_admin():
        return False, ""
    pid = _get_foreground_window_pid()
    if not pid:
        return False, ""
    try:
        if is_pid_elevated(pid):
            msg = (
                "Target app is elevated: input will be ignored (UIPI). "
                "Restart OmniControl as administrator."
            )
            log.warning(msg)
            return True, msg
    except Exception:
        log.exception("foreground elevation check failed")
    return False, ""


def ensure_admin(force: bool = True) -> bool:
    """Re-launch with 'runas' elevation if not already admin and force=True.

    Returns True  → caller should sys.exit(0) immediately (new elevated copy is starting).
    Returns False → already admin, user declined UAC, force=False, or non-Windows.

    If the user clicks "No" on the UAC prompt, ShellExecuteW returns <= 32 and we
    continue non-elevated with a clear logged warning — we never retry in a loop.
    """
    if is_admin():
        return False
    if not force or os.name != "nt":
        log.warning(
            "Not running as admin. Input to elevated apps will be ignored (UIPI). "
            "Restart OmniControl as administrator to fix this."
        )
        return False

    exe = sys.executable
    # Reconstruct argv so the relaunched copy gets the same flags
    args = " ".join(
        f'"{a}"' if (" " in a or not a) else a
        for a in sys.argv
    )

    log.info("Requesting elevation via UAC (ShellExecuteW runas) ...")
    result = _shell_execute_w(None, "runas", exe, args, None, 1)

    if result > 32:
        log.info("Elevated process launched (hInstance=%d); exiting non-elevated copy.", result)
        return True  # caller must exit

    # User declined or error (e.g. result == 5 = ERROR_ACCESS_DENIED)
    log.warning(
        "UAC elevation declined or failed (ShellExecuteW=%d). "
        "Continuing non-elevated — input to elevated apps will be ignored (UIPI).",
        result,
    )
    return False

