"""doctor.py (Binore): hardware and permission diagnostics for OmniControl.

Usage:  python main.py --doctor
Exit code 0 if no FAILs; non-zero if any FAIL present.

Every OS call is in a thin wrapper function so tests can mock it without touching ctypes.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import List

log = logging.getLogger("omnicontrol.doctor")


class Status(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    fix: str = ""


# ── mockable OS wrappers ──────────────────────────────────────────────────────

def _python_version_info():
    return sys.version_info


def _is_64bit() -> bool:
    return ctypes.sizeof(ctypes.c_void_p) == 8


def _sizeof_input() -> int:
    from gateway import INPUT
    return ctypes.sizeof(INPUT)


def _is_admin() -> bool:
    from elevation import is_admin
    return is_admin()


def _check_foreground_elevation() -> tuple[bool, str]:
    from elevation import check_foreground_elevation
    return check_foreground_elevation()


def _open_camera(index: int):
    """Returns (width, height, fps). Raises on failure."""
    import cv2  # noqa: ImportError handled by caller
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise RuntimeError("Camera did not open")
    ret, frame = cap.read()
    if not ret or frame is None:
        cap.release()
        raise RuntimeError("No frame received")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    return w, h, fps


def _open_mic(sample_rate: int, duration_s: float) -> float:
    """Returns RMS level. Raises on failure."""
    import numpy as np       # noqa
    import sounddevice as sd  # noqa
    data = sd.rec(
        int(sample_rate * duration_s), samplerate=sample_rate,
        channels=1, dtype="float32", blocking=True,
    )
    return float(np.sqrt(np.mean(data ** 2)))


def _get_dpi_awareness() -> int:
    """Returns GetProcessDpiAwareness value: 0=unaware, 1=system, 2=per-monitor. -1 on error."""
    if os.name != "nt":
        return -1
    try:
        val = ctypes.c_int(0)
        hr = ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(val))
        return val.value if hr == 0 else -1
    except Exception:
        return -1


def _get_monitor_count() -> int:
    if os.name != "nt":
        return 1
    try:
        return ctypes.windll.user32.GetSystemMetrics(80)  # SM_CMONITORS
    except Exception:
        return 1


def _get_primary_resolution() -> tuple[int, int]:
    if os.name != "nt":
        return (1920, 1080)
    try:
        return (
            ctypes.windll.user32.GetSystemMetrics(0),
            ctypes.windll.user32.GetSystemMetrics(1),
        )
    except Exception:
        return (0, 0)


def _send_input_self_test() -> int:
    """Inject F24 down+up. Returns count sent (expect 2). Non-Windows: returns 2."""
    if os.name != "nt":
        return 2
    from gateway import INPUT, INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE

    F24_SCAN = 0x76  # F24 — not bound to anything in games or Windows

    def _ki(flags: int) -> INPUT:
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki.wVk = 0
        inp.u.ki.wScan = F24_SCAN
        inp.u.ki.dwFlags = flags
        return inp

    arr = (INPUT * 2)(_ki(KEYEVENTF_SCANCODE), _ki(KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP))
    return ctypes.windll.user32.SendInput(2, arr, ctypes.sizeof(INPUT))


def _time_begin_period_1() -> bool:
    """Returns True if timeBeginPeriod(1) succeeded (TIMERR_NOERROR == 0)."""
    if os.name != "nt":
        return True
    try:
        return ctypes.windll.winmm.timeBeginPeriod(1) == 0
    except Exception:
        return False


def _measure_60hz_jitter_p99(duration_s: float = 3.0) -> float:
    """Returns p99 tick interval (ms) of a real 60 Hz FrameClock run."""
    from watchdog import FrameClock

    fc = FrameClock(hz=60.0)
    intervals: list[float] = []
    prev = time.perf_counter()
    end = prev + duration_s
    while time.perf_counter() < end:
        fc.wait()
        t = time.perf_counter()
        intervals.append((t - prev) * 1000.0)
        prev = t
    if not intervals:
        return 0.0
    intervals.sort()
    idx = min(len(intervals) - 1, int(0.99 * (len(intervals) - 1)))
    return intervals[idx]


def _module_importable(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


# ── individual check functions ────────────────────────────────────────────────

def check_python() -> CheckResult:
    vi = _python_version_info()
    ver = f"{vi[0]}.{vi[1]}.{vi[2]}"
    if (vi[0], vi[1]) < (3, 11):
        return CheckResult(
            "Python version", Status.FAIL, f"Python {ver} (need ≥ 3.11)",
            "Install Python 3.11+ from https://python.org",
        )
    return CheckResult("Python version", Status.PASS, f"Python {ver}")


def check_64bit() -> CheckResult:
    if _is_64bit():
        return CheckResult("64-bit process", Status.PASS, "64-bit OK")
    return CheckResult(
        "64-bit process", Status.FAIL, "Running as 32-bit process",
        "Use the 64-bit Python installer from python.org",
    )


def check_input_struct() -> CheckResult:
    sz = _sizeof_input()
    if sz == 40:
        return CheckResult("INPUT struct size", Status.PASS, f"sizeof(INPUT) = {sz} ✓")
    return CheckResult(
        "INPUT struct size", Status.FAIL,
        f"sizeof(INPUT) = {sz} bytes (expected 40)",
        "Ensure 64-bit Python; check ctypes field alignment in gateway.py",
    )


def check_admin() -> CheckResult:
    if _is_admin():
        return CheckResult("Admin status", Status.PASS, "Running as administrator")
    return CheckResult(
        "Admin status", Status.WARN, "Not running as administrator",
        "Right-click OmniControl → 'Run as administrator', or use the Admin build",
    )


def check_uipi() -> CheckResult:
    problem, msg = _check_foreground_elevation()
    if problem:
        return CheckResult(
            "Foreground app / UIPI", Status.WARN, msg,
            "Restart OmniControl as administrator",
        )
    return CheckResult(
        "Foreground app / UIPI", Status.PASS,
        "Foreground app not elevated (or we are admin)",
    )


def check_camera() -> CheckResult:
    try:
        w, h, fps = _open_camera(0)
        fps_note = " — limited to 30 fps, face latency may be higher" if fps and fps <= 30 else ""
        return CheckResult(
            "Camera", Status.PASS, f"{w}×{h} @ {fps:.0f} fps{fps_note}"
        )
    except ImportError:
        return CheckResult(
            "Camera", Status.WARN, "opencv-python not installed",
            "pip install opencv-python",
        )
    except Exception as exc:
        return CheckResult(
            "Camera", Status.FAIL, f"Cannot open camera index 0: {exc}",
            "Settings → Privacy & security → Camera → "
            "'Let desktop apps access your camera'",
        )


def check_microphone() -> CheckResult:
    try:
        rms = _open_mic(16000, 1.0)
        if rms < 0.001:
            return CheckResult(
                "Microphone", Status.WARN,
                f"Opened OK but RMS={rms:.5f} (is the mic muted or disconnected?)",
                "Check microphone levels in Sound settings",
            )
        return CheckResult("Microphone", Status.PASS, f"1 s audio RMS={rms:.4f}")
    except ImportError:
        return CheckResult(
            "Microphone", Status.WARN, "sounddevice not installed",
            "pip install sounddevice numpy",
        )
    except Exception as exc:
        return CheckResult(
            "Microphone", Status.FAIL, f"Cannot open microphone: {exc}",
            "Settings → Privacy & security → Microphone → "
            "'Let desktop apps access your microphone'",
        )


def check_display() -> CheckResult:
    awareness = _get_dpi_awareness()
    monitors = _get_monitor_count()
    w, h = _get_primary_resolution()
    awareness_str = {0: "unaware", 1: "system-DPI", 2: "per-monitor-v2"}.get(
        awareness, f"unknown({awareness})"
    )
    ok = awareness == 2 or os.name != "nt"
    status = Status.PASS if ok else Status.WARN
    msg = f"DPI={awareness_str}, {monitors} monitor(s), primary={w}×{h}"
    fix = "Call SetProcessDpiAwarenessContext at startup (already done in main.py)" if not ok else ""
    return CheckResult("Display / DPI", status, msg, fix)


def check_send_input() -> CheckResult:
    if os.name != "nt":
        return CheckResult(
            "SendInput self-test", Status.WARN,
            "Non-Windows: skipped", "Run on Windows",
        )
    try:
        sent = _send_input_self_test()
        if sent == 2:
            return CheckResult(
                "SendInput self-test", Status.PASS,
                "F24 down+up: 2/2 sent OK",
            )
        return CheckResult(
            "SendInput self-test", Status.FAIL,
            f"SendInput sent {sent}/2 (GetLastError={ctypes.GetLastError()})",
            "Run as admin; check if kernel anti-cheat is active",
        )
    except Exception as exc:
        return CheckResult(
            "SendInput self-test", Status.FAIL,
            f"SendInput raised: {exc}",
            "Check gateway.py struct layout (must be 40 bytes on x64)",
        )


def check_timer() -> CheckResult:
    if not _time_begin_period_1():
        return CheckResult(
            "Timer resolution", Status.WARN,
            "timeBeginPeriod(1) failed — sleep granularity will be ~15 ms",
            "Ensure winmm.dll is present (should always be on Windows)",
        )
    try:
        p99 = _measure_60hz_jitter_p99(3.0)
        status = Status.PASS if p99 < 3.0 else Status.WARN
        fix = "High system load; close background apps or GPU-intensive processes" if p99 >= 3.0 else ""
        return CheckResult(
            "60 Hz loop p99 jitter", status,
            f"p99 tick = {p99:.2f} ms (threshold 3.0 ms)", fix,
        )
    except Exception as exc:
        return CheckResult("60 Hz loop p99 jitter", Status.WARN, f"Jitter test failed: {exc}")


_OPTIONAL_DEPS = [
    ("mediapipe",    "pip install mediapipe",         "face/pose tracking (Antas)"),
    ("vosk",         "pip install vosk",               "offline speech recognition (Dishti)"),
    ("sounddevice",  "pip install sounddevice numpy",  "microphone input"),
    ("brainflow",    "pip install brainflow",           "BCI / EEG input"),
    ("pystray",      "pip install pystray pillow",      "system tray icon"),
    ("PIL",          "pip install pillow",              "image support for tray"),
    ("cv2",          "pip install opencv-python",       "webcam capture"),
]


def check_optional_deps() -> List[CheckResult]:
    results = []
    for mod, install_cmd, purpose in _OPTIONAL_DEPS:
        present = _module_importable(mod)
        results.append(CheckResult(
            f"Optional: {mod}",
            Status.PASS if present else Status.WARN,
            f"{'installed' if present else 'missing'} — {purpose}",
            install_cmd if not present else "",
        ))
    return results


def run_all() -> List[CheckResult]:
    """Run every check sequentially. Returns list of CheckResult."""
    results: List[CheckResult] = [
        check_python(),
        check_64bit(),
        check_input_struct(),
        check_admin(),
        check_uipi(),
        check_camera(),
        check_microphone(),
        check_display(),
        check_send_input(),
        check_timer(),
    ]
    results.extend(check_optional_deps())
    return results


# ── pretty-print ─────────────────────────────────────────────────────────────

_RESET  = "\033[0m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"


def _col(s: Status) -> str:
    return {Status.PASS: _GREEN, Status.WARN: _YELLOW, Status.FAIL: _RED}[s]


def print_table(results: List[CheckResult]) -> None:
    width = 70
    print(f"\n{'OmniControl Doctor':=^{width}}")
    for r in results:
        col = _col(r.status)
        badge = f"[{r.status.value}]"
        print(f"  {col}{badge:6}{_RESET} {r.name:<32} {r.message}")
        if r.fix:
            print(f"{'':9}↳ Fix: {r.fix}")
    print("=" * width)
    fails = sum(1 for r in results if r.status == Status.FAIL)
    warns = sum(1 for r in results if r.status == Status.WARN)
    print(f"  Result: {fails} FAIL(s), {warns} WARN(s)\n")
    if fails:
        print(f"{_RED}FAIL: Critical checks failed — see fixes above.{_RESET}")
    else:
        print(f"{_GREEN}All critical checks passed.{_RESET}")


def doctor_main() -> int:
    """Run from main.py --doctor. Returns shell exit code (0 = all clear)."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    print("\nOmniControl Hardware & Permission Diagnostics")
    print("(Camera/mic/timer tests take a few seconds)\n")
    results = run_all()
    print_table(results)
    return 1 if any(r.status == Status.FAIL for r in results) else 0

