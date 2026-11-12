"""install_desktop_app.py - Installs Unbound.exe desktop shortcut and Start Menu entry.

1. Builds or verifies dist/Unbound/Unbound.exe.
2. Creates Windows .lnk shortcuts on:
   - User Desktop (Unbound.lnk)
   - User Start Menu Programs (Unbound.lnk)
Uses Windows Script Host Shell via Python or PowerShell fallback.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def create_shortcut(target_exe: Path, lnk_path: Path, description: str, args: str = "") -> bool:
    """Create a Windows .lnk shortcut using PowerShell WScript.Shell."""
    ps_cmd = f"""
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{lnk_path}')
$s.TargetPath = '{target_exe}'
$s.Arguments = '{args}'
$s.WorkingDirectory = '{target_exe.parent}'
$s.Description = '{description}'
$s.Save()
"""
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True)
        return res.returncode == 0
    except Exception as e:
        print(f"Error creating shortcut {lnk_path}: {e}")
        return False


def install():
    print("=== Installing Unbound Native Desktop Shortcuts ===")
    exe_path = ROOT / "dist" / "Unbound" / "Unbound.exe"

    if not exe_path.exists():
        print(f"Unbound.exe not found at {exe_path}. Building now via tools/build_exe.py...")
        from tools.build_exe import build
        ret = build()
        if ret != 0 or not exe_path.exists():
            print("Failed to build Unbound.exe")
            return 1

    desktop = Path(os.path.expanduser("~/Desktop"))
    appdata = os.environ.get("APPDATA")
    start_menu = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" if appdata else None

    # Desktop shortcut
    desktop_lnk = desktop / "Unbound.lnk"
    if create_shortcut(exe_path, desktop_lnk, "Unbound Somatic Operating System & Game Mod Daemon"):
        print(f"SUCCESS: Created Desktop Shortcut -> {desktop_lnk}")
    else:
        print(f"WARNING: Could not create shortcut on Desktop")

    # Start Menu shortcut
    if start_menu and start_menu.exists():
        start_lnk = start_menu / "Unbound.lnk"
        if create_shortcut(exe_path, start_lnk, "Unbound Somatic Operating System"):
            print(f"SUCCESS: Created Start Menu Shortcut -> {start_lnk}")

    print("\n[OK] Setup complete! You can now double-click 'Unbound' on your Desktop.")
    return 0


if __name__ == "__main__":
    sys.exit(install())

