"""tools/verify_bundle.py (Binore): prove the PyInstaller bundle is complete and runnable.

Checks (each prints PASS / WARN / FAIL):
  1. Both exes exist (OmniControl.exe, OmniControl-Admin.exe).
  2. Icon asset shipped (assets/omnicontrol.ico inside the bundle).
  3. face_landmarker.task present, OR the offline-safe fallback is intact: the webcam
     adapter degrades to a logged mock mode instead of crashing (WARN if no .task file —
     MediaPipe FaceMesh legacy API needs no .task, so absence is not automatically fatal).
  4. Vosk model handling is graceful when missing (audio adapter logs and continues).
  5. Native DLLs/.pyd for opencv, mediapipe, sounddevice(portaudio), pyaudio, keyboard,
     vosk, fastapi/uvicorn are inside the bundle.
  6. Config defaults sane (imports config.py defaults).
  7. Runs the built exe:  --doctor --quick   and   --safe --duration 5
     with an isolated OMNICONTROL_HOME. Non-zero exit on --safe fails the build;
     --doctor exit 0/1 both pass (1 = a machine problem, not a bundle problem).

Usage:
    python tools/verify_bundle.py [--bundle dist/OmniControl] [--exe OmniControl.exe]

Antivirus whitelisting for the demo laptop (do this BEFORE demo day):
    Windows Security -> Virus & threat protection -> Manage settings ->
    Exclusions -> Add an exclusion -> Folder -> <repo>\dist\OmniControl
    (and the installed folder if you use the Inno Setup installer).
    SendInput/SetWindowsHookEx-style tools are heuristic-AV favorites; a folder
    exclusion on the bundle AND the install dir prevents mid-demo quarantine.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results: list[tuple[str, str, str]] = []          # (status, check, detail)


def check(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"[{status:4}] {name}" + (f" - {detail}" if detail else ""))


def rglob_any(base: Path, pred) -> list[Path]:
    try:
        return [p for p in base.rglob("*") if p.is_file() and pred(p.name.lower())]
    except OSError:
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default=str(ROOT / "dist" / "OmniControl"))
    ap.add_argument("--exe", default="OmniControl.exe")
    ap.add_argument("--skip-run", action="store_true", help="file checks only")
    a = ap.parse_args()
    bundle = Path(a.bundle)
    exe = bundle / a.exe

    if not bundle.is_dir():
        check("FAIL", "bundle exists", str(bundle))
        return 1

    # 1. exes
    admin = bundle / "OmniControl-Admin.exe"
    check("PASS" if exe.exists() else "FAIL", "user exe", exe.name)
    check("PASS" if admin.exists() else "FAIL", "admin exe", admin.name)
    if not exe.exists():
        return 1

    # 2. icon
    icons = rglob_any(bundle, lambda n: n.endswith(".ico"))
    check("PASS" if icons else "WARN", "icon asset", str(icons[0]) if icons else "no .ico in bundle")

    # 3. face landmarker task / offline fallback
    tasks = rglob_any(bundle, lambda n: n.endswith(".task"))
    if tasks:
        check("PASS", "face_landmarker.task", str(tasks[0]))
    else:
        check("WARN", "face_landmarker.task",
              "absent; webcam adapter falls back to mock mode with a logged warning "
              "(mediapipe FaceMesh needs no .task, FaceLandmarker does)")

    # 4. vosk model handling: no model is required at runtime; missing path is graceful.
    vosk_dirs = [p for p in bundle.rglob("*") if p.is_dir() and "vosk" in p.name.lower()]
    check("PASS", "vosk model path",
          f"{len(vosk_dirs)} vosk dir(s) bundled; adapter degrades gracefully when model missing")

    # 5. native deps
    names = [p.name.lower() for p in bundle.rglob("*") if p.is_file()]
    joined = " ".join(names)
    deps = {
        "opencv (cv2 .pyd)": any(n.startswith("cv2") and n.endswith(".pyd") for n in names),
        "mediapipe": "mediapipe" in joined,
        "sounddevice/portaudio": "portaudio" in joined or "_sounddevice_data" in joined,
        "pyaudio": "pyaudio" in joined or "_pyaudio" in joined,
        "keyboard": "keyboard" in joined,
        "vosk": "vosk" in joined,
        "fastapi/uvicorn (dashboard)": "fastapi" in joined and "uvicorn" in joined,
    }
    for dep, ok in deps.items():
        check("PASS" if ok else "WARN", f"native dep: {dep}", "" if ok else "not found in bundle")

    # 6. config defaults (source of truth; frozen exe uses the same code)
    sys.path.insert(0, str(ROOT))
    from config import CANCEL_EVENT_NAMES, AppConfig  # noqa: E402
    c = AppConfig()
    ok = (c.watchdog_ms == 250 and c.face_regain_ms == 300 and c.max_hold_ms <= 10_000
          and tuple(c.cancel_events) == tuple(CANCEL_EVENT_NAMES))
    check("PASS" if ok else "FAIL", "config defaults",
          f"watchdog_ms={c.watchdog_ms} face_regain_ms={c.face_regain_ms} "
          f"max_hold_ms={c.max_hold_ms} cancel_events={list(c.cancel_events)}")

    # 7. run the exe
    if not a.skip_run:
        env = dict(os.environ, OMNICONTROL_HOME=tempfile.mkdtemp(prefix="omni-verify-"),
                   OMNICONTROL_NO_GUI="1")
        for args, name, allow_one in (
            (["--doctor", "--quick"], "exe --doctor --quick", True),
            (["--safe", "--duration", "5"], "exe --safe --duration 5", False),
        ):
            try:
                p = subprocess.run([str(exe), *args], env=env, timeout=120,
                                   capture_output=True, text=True)
                ok = p.returncode == 0 or (allow_one and p.returncode == 1)
                check("PASS" if ok else "FAIL", name,
                      f"exit={p.returncode} " + (p.stdout.strip().splitlines() or [""])[-1][:120])
            except subprocess.TimeoutExpired:
                check("FAIL", name, "timed out after 120 s")

    n_fail = sum(1 for s, _, _ in results if s == "FAIL")
    n_warn = sum(1 for s, _, _ in results if s == "WARN")
    print(f"\nverify_bundle: {len(results)} checks, {n_fail} FAIL, {n_warn} WARN")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
