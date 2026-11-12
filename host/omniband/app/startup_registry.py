"""startup_registry.py - Manage Windows Startup Registry Run Key for Unbound.

Writes or removes user-level registry entry:
  HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\Unbound
Points to: "path\\to\\Unbound.exe" --minimized
Requires zero admin privileges.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("omniband.startup")

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Unbound"


def is_run_at_startup_enabled() -> bool:
    """Check if the HKCU Run key for Unbound is currently registered."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except Exception as e:
        log.debug("error reading startup registry: %s", e)
        return False


def set_run_at_startup(enable: bool, exe_path: str | Path | None = None) -> bool:
    """Add or remove Unbound from HKCU Run key."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                if exe_path is None:
                    if getattr(sys, "frozen", False):
                        p = Path(sys.executable)
                    else:
                        p = Path(__file__).resolve().parent.parent.parent / "dist" / "Unbound" / "Unbound.exe"
                else:
                    p = Path(exe_path)
                
                cmd = f'"{p}" --minimized'
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
                log.info("Registered startup command: %s", cmd)
                return True
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                    log.info("Removed startup command for Unbound")
                except FileNotFoundError:
                    pass
                return True
    except Exception as e:
        log.error("error setting startup registry: %s", e)
        return False

