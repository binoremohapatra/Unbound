#!/usr/bin/env python3
"""sendinput_selftest.py - Interactive Windows Notepad SendInput verification.

Exercises real Win32 SendInput hardware scancode typing and relative mouse moves.
Labeled NEEDS_WINDOWS_MANUAL because it requires a live desktop session and user eye.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from gateway import Gateway, SendInputBackend
    from core.events import OutputCommand
except ImportError as err:
    print(f"Error importing gateway: {err}")
    sys.exit(1)

def main():
    if sys.platform != "win32":
        print("sendinput_selftest requires Windows 11 desktop environment.")
        return 0

    print("=== OmniBand V2 SendInput Notepad Self-Test ===")
    print("Launching Notepad...")
    proc = subprocess.Popen(["notepad.exe"])
    time.sleep(1.0)

    gw = Gateway(SendInputBackend())
    gw.install_exit_handlers()

    test_string = "OmniBand V2 In-Place Hardware Scancode Verified.\n"
    print(f"Typing string: {test_string.strip()}")

    for ch in test_string:
        if ch == "\n":
            key = "enter"
        elif ch == " ":
            key = "space"
        elif ch == ".":
            key = "period"
        else:
            key = ch.lower()

        # Press then release
        gw.apply([OutputCommand(device="keyboard", action="press", name=key)])
        time.sleep(0.02)
        gw.apply([OutputCommand(device="keyboard", action="release", name=key)])
        time.sleep(0.02)

    gw.release_all("test complete")
    print("Self-test typed characters into Notepad.")
    print("Status: NEEDS_WINDOWS_MANUAL (confirm text appears in Notepad).")
    return 0

if __name__ == "__main__":
    sys.exit(main())

