#!/usr/bin/env python3
"""build_exe.py - PyInstaller build script for Unbound.exe.

Compiles the OmniBand V2 Native Windows Desktop Application & Somatic Daemon
into dist/Unbound/Unbound.exe.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build():
    print("=== Building Native Windows Desktop Executable (Unbound.exe) ===")
    
    spec_path = ROOT / "Unbound.spec"
    if not spec_path.exists():
        print(f"Error: Spec file not found at {spec_path}")
        return 1

    cmd = [
        sys.executable,
        "-m", "PyInstaller",
        str(spec_path),
        "--clean",
        "--noconfirm",
    ]
    
    print(f"Running: {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=str(ROOT))
    if res.returncode == 0:
        exe_path = ROOT / "dist" / "Unbound" / "Unbound.exe"
        print(f"SUCCESS: Build complete. Binary generated at:\n  {exe_path}")
    else:
        print(f"FAILED: PyInstaller exited with code {res.returncode}")
    return res.returncode


if __name__ == "__main__":
    sys.exit(build())

