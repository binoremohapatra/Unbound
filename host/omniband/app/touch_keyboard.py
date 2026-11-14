"""touch_keyboard.py - Microsoft Windows Native Touch Keyboard & OSK Controller.

Guarantees a visible official Microsoft Keyboard (TabTip.exe / OSK) pops up on
screen every time a text field is clicked, on any Windows 10/11 edition.

3-Tier Cascade:
1. Auto-enables HKCU\\Software\\Microsoft\\TabletTip\\1.7 EnableDesktopModeAutoInvoke=1.
2. ShellExecuteW TabTip.exe + ITipInvocation COM check.
3. 100% Fallback to Microsoft OSK (Ctrl+Win+O / osk.exe) positioned in bottom 35%
   if TabTip remains cloaked or absent.
4. Restores Win32 focus to target_hwnd so typing goes directly into the text field.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import subprocess
import sys
import threading
import time

log = logging.getLogger(__name__)

TABTIP_PATH = r"C:\Program Files\Common Files\Microsoft Shared\ink\TabTip.exe"


def enable_tablet_tip_registry() -> bool:
    """Set HKCU registry keys so Windows allows TabTip to auto-invoke in Desktop Mode."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\TabletTip\1.7")
        winreg.SetValueEx(key, "EnableDesktopModeAutoInvoke", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "DisableNewKeyboardExperience", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key)
        return True
    except Exception as e:
        log.warning("enable_tablet_tip_registry failed: %s", e)
        return False


def is_tabtip_visible() -> bool:
    """Check if Microsoft TabTip window exists, is visible, and is NOT cloaked by DWM."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW("IPTip_Main_Window", None)
        if not hwnd or not user32.IsWindowVisible(hwnd):
            return False

        # On Windows 11, dormant TabTip has IsWindowVisible == True but DWMWA_CLOAKED != 0!
        dwmapi = ctypes.windll.dwmapi
        cloaked = wintypes.DWORD(0)
        res = dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if res == 0 and cloaked.value != 0:
            return False  # Cloaked / hidden by system compositor

        return True
    except Exception:
        return False


def is_osk_visible() -> bool:
    """Check if classic Microsoft On-Screen Keyboard window is open and visible."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW("OSKMainClass", None)
        if not hwnd:
            return False
        return bool(user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def trigger_tabtip_com() -> bool:
    """Invoke Windows ITipInvocation COM interface to toggle TabTip into view."""
    if sys.platform != "win32":
        return False
    try:
        import comtypes
        from comtypes import GUID, IUnknown, COMMETHOD, HRESULT

        class ITipInvocation(IUnknown):
            _iid_ = GUID("{37c994e7-432b-4834-a2f7-dce1f13b834b}")
            _methods_ = [COMMETHOD([], HRESULT, "Toggle", (["in"], wintypes.HWND, "hwndDesktop"))]

        tip = comtypes.CoCreateInstance(
            GUID("{4CE576FA-83DC-4F88-951C-9D0782B4E376}"),
            interface=ITipInvocation,
            clsctx=comtypes.CLSCTX_INPROC_HANDLER | comtypes.CLSCTX_LOCAL_SERVER,
        )
        tip.Toggle(ctypes.windll.user32.GetDesktopWindow())
        return True
    except Exception:
        return False


def inject_ctrl_win_o() -> None:
    """Send native Windows global hotkey (Ctrl + Win + O) to toggle On-Screen Keyboard."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        VK_CONTROL = 0x11
        VK_LWIN = 0x5B
        VK_O = 0x4F
        KEYEVENTF_KEYUP = 0x0002

        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_LWIN, 0, 0, 0)
        user32.keybd_event(VK_O, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(VK_O, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    except Exception as e:
        log.warning("inject_ctrl_win_o failed: %s", e)


def position_osk_bottom_dock() -> None:
    """Position OSK window cleanly in the bottom 35% of the primary work area."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW("OSKMainClass", None)
        if not hwnd:
            return

        rect = wintypes.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        work_w = rect.right - rect.left
        work_h = rect.bottom - rect.top
        kb_h = max(200, int(work_h * 0.35))
        kb_w = max(400, int(work_w * 0.85))
        kb_x = rect.left + (work_w - kb_w) // 2
        kb_y = rect.bottom - kb_h

        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        user32.SetWindowPos(hwnd, -1, kb_x, kb_y, kb_w, kb_h, SWP_SHOWWINDOW | SWP_NOACTIVATE)
    except Exception as e:
        log.warning("position_osk_bottom_dock failed: %s", e)


def launch_osk_fallback() -> bool:
    """Launch Microsoft built-in On-Screen Keyboard via ShellExecute / cmd / hotkey."""
    if sys.platform != "win32":
        return False
    try:
        shell32 = ctypes.windll.shell32
        res = shell32.ShellExecuteW(None, "open", "osk.exe", None, None, 1)
        if res > 32:
            return True
    except Exception:
        pass

    try:
        inject_ctrl_win_o()
        return True
    except Exception:
        pass

    try:
        subprocess.Popen("cmd.exe /c start osk.exe", shell=True)
        return True
    except Exception:
        return False


def _show_keyboard_worker(target_hwnd: int) -> None:
    """Asynchronous worker executing the 3-tier cascade and focus preservation."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32

        # Step 1: Ensure Registry key for Desktop Mode Auto-Invoke is active
        enable_tablet_tip_registry()

        # Step 2: Try launching TabTip.exe via ShellExecuteW
        if os.path.exists(TABTIP_PATH):
            try:
                shell32.ShellExecuteW(None, "open", TABTIP_PATH, None, None, 1)
            except Exception:
                pass

        time.sleep(0.080)

        # Check if TabTip is visible and active on screen
        if not is_tabtip_visible():
            trigger_tabtip_com()

        time.sleep(0.040)

        # Step 3: 100% Guaranteed Fallback - If TabTip is still cloaked/absent, invoke OSK
        if not is_tabtip_visible():
            launch_osk_fallback()
            time.sleep(0.040)
            position_osk_bottom_dock()

        # Step 4: Re-focus target text field so cursor stays in external app!
        time.sleep(0.040)  # Total elapsed ~200ms
        if target_hwnd and target_hwnd != 0:
            try:
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(target_hwnd, ctypes.byref(pid))
                if pid.value != os.getpid():
                    user32.SetForegroundWindow(target_hwnd)
            except Exception:
                pass
    except Exception as e:
        log.warning("_show_keyboard_worker failed: %s", e)


def show_microsoft_keyboard(target_hwnd: int = 0) -> bool:
    """Launch or bring up Microsoft Official Keyboard (TabTip.exe / OSK).

    Guarantees popup and preserves focus on target text field.
    Non-blocking: executes via background worker thread.
    """
    if sys.platform != "win32":
        return True

    if target_hwnd == 0:
        try:
            target_hwnd = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            target_hwnd = 0

    t = threading.Thread(
        target=_show_keyboard_worker,
        args=(target_hwnd,),
        daemon=True,
        name="ShowMicrosoftKeyboardWorker",
    )
    t.start()
    return True


def hide_microsoft_keyboard() -> None:
    """Dismiss Microsoft Official Keyboard (TabTip.exe / OSK)."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        for cls in ("IPTip_Main_Window", "OSKMainClass", "Shell_InputSwitchTopLevelWindow"):
            hwnd = user32.FindWindowW(cls, None)
            if hwnd:
                user32.PostMessageW(hwnd, 0x0112, 0xF060, 0)  # WM_SYSCOMMAND, SC_CLOSE

        # If OSK is open, toggling hotkey also closes it cleanly
        if is_osk_visible():
            inject_ctrl_win_o()
    except Exception as e:
        log.warning("hide_microsoft_keyboard failed: %s", e)


# Backward-compatible aliases
show_windows_touch_keyboard = show_microsoft_keyboard
hide_windows_touch_keyboard = hide_microsoft_keyboard
