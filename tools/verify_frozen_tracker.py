"""verify_frozen_tracker.py — Automated verification gate for built Unbound.exe (B-03).

Runs:
  1. Unbound.exe --selftest-env
  2. Unbound.exe --selftest-resources
  3. Unbound.exe --selftest-tracker
  4. Unbound.exe --selftest-camera

Asserts:
  - Return code 0 for all self-tests
  - Output contains PASS
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def verify_executable(exe_path: Path) -> bool:
    if not exe_path.is_file():
        print(f"FAIL: Executable not found at {exe_path}")
        return False

    flags = [
        ("--selftest-env", "SELFTEST-ENV: PASS"),
        ("--selftest-resources", "SELFTEST-RESOURCES: PASS"),
        ("--selftest-tracker", "SELFTEST-TRACKER: PASS"),
        ("--selftest-camera", "SELFTEST-CAMERA: PASS"),
    ]

    all_passed = True
    for flag, expected in flags:
        cmd = [str(exe_path), flag]
        print(f"Running: {' '.join(cmd)}")
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            print(res.stdout)
            if res.stderr:
                print("STDERR:", res.stderr)

            if res.returncode != 0:
                print(f"FAIL: {flag} exited with code {res.returncode}")
                all_passed = False
            elif expected not in res.stdout:
                print(f"FAIL: Expected '{expected}' not found in output")
                all_passed = False
            else:
                print(f"OK: {flag} passed")
        except Exception as e:
            print(f"FAIL: Exception running {flag}: {e}")
            all_passed = False

    return all_passed


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    exe_candidates = [
        root / "dist" / "Unbound" / "Unbound.exe",
        root / "dist" / "Unbound.exe",
    ]
    target_exe = None
    for c in exe_candidates:
        if c.is_file():
            target_exe = c
            break

    if target_exe is None:
        print("FAIL: No Unbound executable found in dist/")
        print("Run tools/build_exe.py first.")
        return 1

    print(f"Verifying frozen build: {target_exe}")
    success = verify_executable(target_exe)
    if success:
        print("\n=== BUILD GATE VERIFICATION: PASS ===")
        return 0
    else:
        print("\n=== BUILD GATE VERIFICATION: FAIL ===")
        return 1


if __name__ == "__main__":
    sys.exit(main())

