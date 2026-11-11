"""run_native_tests.py - build and run firmware/tests/native on the host.

Finds g++ / clang++ / cl (MSVC via clang-cl or g++ only - no MSVC setup here).
Compiles test_core.cpp with -std=c++17 -Wall -Wextra -Werror and runs it against
tests/vectors/gesture_vectors.json.

  python tools/run_native_tests.py [--compiler PATH] [--keep]

Exit 0 = all native checks pass. Exit 2 = no usable C++17 compiler found
(label NEEDS_TOOLCHAIN honestly - never fake a pass).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "firmware" / "tests" / "native" / "test_core.cpp"
INC = ROOT / "firmware" / "omniband_v2_firmware"
VECTORS = ROOT / "tests" / "vectors" / "gesture_vectors.json"


def find_compiler(preferred: str = "") -> list[str]:
    if preferred:
        return [preferred]
    cands = []
    for name in ("g++", "clang++", "c++", "cl"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    # common MinGW locations on Windows
    local = os.environ.get("LOCALAPPDATA", "")
    pats = [r"C:\mingw*\bin\g++.exe", r"C:\msys64\mingw64\bin\g++.exe",
            r"C:\msys64\ucrt64\bin\g++.exe", r"C:\TDM-GCC*\bin\g++.exe",
            r"C:\winlibs*\mingw64\bin\g++.exe"]
    if local:
        pats.append(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                 "*", "llvm-mingw-*", "bin", "g++.exe"))
        pats.append(os.path.join(local, "Microsoft", "WinGet", "Links", "g++.exe"))
    import glob
    for pat in pats:
        cands += sorted(glob.glob(pat))
    # dedupe preserve order
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compiler", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    compilers = find_compiler(args.compiler)
    if not compilers:
        print("NEEDS_TOOLCHAIN: no g++/clang++ found.")
        print("Install one (e.g. `winget install -e --id MartinStorsjo.LLVM-MinGW.UCRT`")
        print("or winlibs) and re-run. Native firmware tests were NOT executed.")
        return 2

    build = ROOT / "build" / "native_test.exe"
    build.parent.mkdir(exist_ok=True)
    last_err = ""
    for comp in compilers:
        name = Path(comp).name.lower()
        if name.startswith("cl"):
            cmd = [comp, "/std:c++17", "/W4", "/WX", "/EHsc", "/I", str(INC),
                   str(SRC), f"/Fe:{build}"]
        else:
            cmd = [comp, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                   f"-I{INC}", str(SRC), "-o", str(build)]
        print("+", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            break
        last_err = r.stderr or r.stdout
        print(f"  compiler {comp} failed:\n{last_err[:2000]}")
    else:
        print("all compilers failed")
        print(last_err[:2000])
        return 1

    print(f"built {build}")
    env = dict(os.environ)
    comp_bin = str(Path(comp).resolve().parent)
    env["PATH"] = comp_bin + os.pathsep + env.get("PATH", "")
    r = subprocess.run([str(build), str(VECTORS)], text=True, env=env)
    if not args.keep:
        try:
            build.unlink()
        except OSError:
            pass
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
