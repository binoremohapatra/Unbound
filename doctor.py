"""doctor.py (Binore): `main.py --doctor` checks the machine before a demo or a bug report.

Each check returns one Check(name, status, detail, fix). A check that crashes becomes a FAIL line,
so the doctor itself never crashes. Exit code is 1 when any check FAILs, else 0.

Verification honesty: on Linux the Windows-only checks report SKIP. Their Win32 code and the
registry paths below were written from API documentation and have NOT run on a real Windows 11
machine yet. Treat the first Windows run of --doctor as part of the manual test checklist.

Assumption to confirm with Antas / Dishti: DEPENDENCIES lists the libraries I expect the webcam and
audio adapters to use. Edit the tuple if their real requirements differ.
"""
from __future__ import annotations

import ctypes
import importlib.util
import json
import logging
import os
import platform
import struct
import sys
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional

import config
import elevation
import single_instance
from version import __version__

log = logging.getLogger("omnicontrol.doctor")

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"

# (import name, pip name, what needs it)
DEPENDENCIES = (
    ("cv2", "opencv-python", "webcam adapter"),
    ("mediapipe", "mediapipe", "face and head tracking"),
    ("numpy", "numpy", "webcam and audio adapters"),
    ("sounddevice", "sounddevice", "microphone adapter"),
    ("pystray", "pystray", "tray icon"),
    ("PIL", "pillow", "tray icon"),
)

JITTER_OK_MS, JITTER_WARN_MS = 2.0, 5.0
_CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\{}"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _windows_only(name: str) -> Optional[Check]:
    return None if os.name == "nt" else Check(name, SKIP, "Windows only")


# ------------------------------------------------------------------ environment
def check_python() -> Check:
    v, bits = sys.version_info, struct.calcsize("P") * 8
    detail = f"Python {v.major}.{v.minor}.{v.micro}, {bits}-bit" + (", bundled" if getattr(sys, "frozen", False) else "")
    if (v.major, v.minor) < (3, 11):
        return Check("python", WARN, detail, "OmniControl targets Python 3.11")
    return Check("python", OK, detail)


def check_platform() -> Check:
    if os.name != "nt":
        return Check("platform", WARN, f"{platform.platform()} (not Windows)",
                     "Input injection, F12 kill switch and window tracking only work on Windows")
    build = sys.getwindowsversion().build
    name = "Windows 11" if build >= 22000 else "Windows 10"
    return Check("platform", OK, f"{name} (build {build})")


def check_elevation() -> Check:
    if elevation.is_admin():
        return Check("elevation", OK, "running as administrator: can control elevated games and apps")
    return Check("elevation", WARN, "running as a normal user",
                 "Elevated apps (some games, Task Manager) ignore our input. "
                 "Start with --elevate or use OmniControl-Admin.exe if you need them.")


def check_running_instance(name: str = single_instance.APP_INSTANCE_NAME) -> Check:
    """The camera and microphone can only be opened by one program, so a running OmniControl makes
    the device checks below report 'busy'. Say so up front instead of blaming the hardware."""
    probe = single_instance.SingleInstance(name)
    if probe.acquire():
        probe.release()
        return Check("running instance", OK, "no other OmniControl is running")
    return Check("running instance", WARN, "another OmniControl is already running",
                 "Quit it first (tray icon > Quit): it holds the camera, so camera checks may fail")


def check_folders() -> Check:
    problems, shown = [], []
    for label, folder in (("config", config.config_dir()), ("logs", config.log_dir())):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / f".doctor-{os.getpid()}.tmp"
            probe.write_text("x")
            probe.unlink()
            shown.append(f"{label}: {folder}")
        except OSError as e:
            problems.append(f"{label} {folder}: {e}")
    if problems:
        return Check("folders", FAIL, "; ".join(problems),
                     "Check disk space and folder permissions, or set OMNICONTROL_HOME to a writable folder")
    return Check("folders", OK, "; ".join(shown))


def check_dependencies() -> Check:
    missing = [(imp, pip, why) for imp, pip, why in DEPENDENCIES
               if importlib.util.find_spec(imp) is None]
    if not missing:
        return Check("dependencies", OK, f"all {len(DEPENDENCIES)} optional libraries present")
    names = ", ".join(f"{pip} ({why})" for _, pip, why in missing)
    pips = " ".join(pip for _, pip, _ in missing)
    if getattr(sys, "frozen", False):          # nothing can be pip-installed into a built exe
        fix = (f"This build does not include them. Run 'pip install {pips}' in the build "
               "environment, then rebuild (build.ps1).")
    else:
        fix = f"pip install {pips}"
    return Check("dependencies", WARN, f"missing: {names}",
                 fix + ". The engine still runs on its built-in fallbacks without them.")


# ------------------------------------------------------------------ privacy settings
def _read_registry(hive: str, path: str, value: str) -> Optional[str]:
    import winreg
    root = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}[hive]
    try:
        with winreg.OpenKey(root, path) as key:
            return str(winreg.QueryValueEx(key, value)[0])
    except OSError:
        return None


def check_privacy(kind: str, reader: Optional[Callable[[str, str, str], Optional[str]]] = None) -> Check:
    """kind: 'webcam' or 'microphone'. Only an explicit "Deny" fails: a missing value just means
    Windows has never been asked, which is fine. `reader` is injectable for tests."""
    label = {"webcam": "Camera", "microphone": "Microphone"}[kind]
    name = f"{kind} privacy"
    if reader is None:
        skip = _windows_only(name)
        if skip:
            return skip
        reader = _read_registry
    path = _CONSENT.format(kind)
    places = (("HKLM", path, "system-wide switch"),
              ("HKCU", path, "your account's switch"),
              ("HKCU", path + r"\NonPackaged", "'desktop apps' switch"))
    denied = [what for hive, p, what in places if (reader(hive, p, "Value") or "").lower() == "deny"]
    if denied:
        return Check(name, FAIL, f"{label} access is denied by the {', '.join(denied)}",
                     f"Settings > Privacy & security > {label}: turn on access, including "
                     f"'Let desktop apps access your {label.lower()}'")
    return Check(name, OK, f"{label} access is not blocked")


# ------------------------------------------------------------------ devices
def _probe_camera_cv2(index: int = 0) -> Optional[dict]:
    import cv2
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY)
    try:
        if not cap.isOpened():
            return None
        got, t0 = 0, time.perf_counter()
        for _ in range(15):
            got += 1 if cap.read()[0] else 0
        dt = max(1e-6, time.perf_counter() - t0)
        return {"frames": got, "fps": got / dt,
                "size": (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))}
    finally:
        cap.release()


def check_camera(probe: Optional[Callable[[], Optional[dict]]] = None) -> Check:
    if probe is None:
        if importlib.util.find_spec("cv2") is None:
            return Check("camera", SKIP, "OpenCV not installed, cannot open the camera from here")
        probe = _probe_camera_cv2
    info = probe()
    fix_busy = "Plug it in, close apps that use it (Teams, Zoom, OBS, a running OmniControl), check privacy settings"
    if info is None:
        return Check("camera", FAIL, "no camera opened at index 0", fix_busy)
    w, h = info["size"]
    detail = f"{w}x{h}, {info['fps']:.0f} fps measured, {info['frames']}/15 frames"
    if info["frames"] == 0:
        return Check("camera", FAIL, detail, fix_busy)
    if info["fps"] < 20:
        return Check("camera", WARN, detail,
                     "Low frame rate adds latency. Improve lighting (cameras slow down in the dark) "
                     "and turn off auto low-light in the camera app")
    return Check("camera", OK, detail)


def _probe_microphone_sd() -> list:
    import sounddevice as sd
    return [d["name"] for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]


def check_microphone(probe: Optional[Callable[[], list]] = None) -> Check:
    if probe is None:
        if importlib.util.find_spec("sounddevice") is None:
            return Check("microphone", SKIP, "sounddevice not installed, cannot list input devices")
        probe = _probe_microphone_sd
    try:
        names = probe()
    except Exception as e:                        # PortAudio can fail in odd ways
        return Check("microphone", FAIL, f"could not query audio devices: {e}")
    if not names:
        return Check("microphone", FAIL, "no input device found",
                     "Plug in a microphone and check Settings > System > Sound > Input")
    return Check("microphone", OK, f"{len(names)} input device(s), first: {names[0]}")


# ------------------------------------------------------------------ Windows input / display
def check_sendinput() -> Check:
    skip = _windows_only("sendinput")
    if skip:
        return skip
    from ctypes import wintypes

    from gateway import INPUT, build_mouse_move_input
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    arr = (INPUT * 1)(build_mouse_move_input(0, 0))       # a zero move: nothing visible happens
    ctypes.set_last_error(0)
    sent = user32.SendInput(1, arr, ctypes.sizeof(INPUT))
    detail = f"INPUT is {ctypes.sizeof(INPUT)} bytes; SendInput accepted {sent}/1 test event"
    if sent != 1:
        err = ctypes.get_last_error()
        if err == 5:
            return Check("sendinput", WARN,
                         f"{detail} (GetLastError=5: non-interactive session or UIPI)",
                         "Elevated apps or non-interactive sessions block SendInput. Start with --elevate or run from an interactive desktop.")
        return Check("sendinput", FAIL, f"{detail} (GetLastError={err})",
                     "Another program may be blocking input injection (some security software does)")
    return Check("sendinput", OK, detail + ". Cannot detect UIPI: that is checked per foreground app")


def check_display() -> Check:
    skip = _windows_only("display")
    if skip:
        return skip
    try:
        dpi = ctypes.WinDLL("user32").GetDpiForSystem()
    except (AttributeError, OSError):
        return Check("display", SKIP, "GetDpiForSystem unavailable (older Windows)")
    return Check("display", OK, f"system scale {dpi / 96 * 100:.0f}%. Relative mouse aim is unaffected "
                 "by DPI; dashboard windows should be per-monitor DPI aware to stay sharp")


def check_pointer_precision() -> Check:
    skip = _windows_only("pointer precision")
    if skip:
        return skip
    SPI_GETMOUSE, SPI_GETMOUSESPEED = 0x0003, 0x0070
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    params, speed = (ctypes.c_int * 3)(), ctypes.c_int()
    if not user32.SystemParametersInfoW(SPI_GETMOUSE, 0, params, 0):
        return Check("pointer precision", SKIP, "could not read mouse settings")
    user32.SystemParametersInfoW(SPI_GETMOUSESPEED, 0, ctypes.byref(speed), 0)
    detail = f"'Enhance pointer precision' is {'ON' if params[2] else 'off'}, pointer speed {speed.value}/20"
    if params[2]:
        return Check("pointer precision", WARN, detail,
                     "Injected head-aim moves get accelerated in desktop apps and some games, so aim feels "
                     "non-linear. Settings > Bluetooth & devices > Mouse > Additional mouse settings > "
                     "Pointer Options > untick 'Enhance pointer precision'")
    return Check("pointer precision", OK, detail)


# ------------------------------------------------------------------ timing
def jitter_stats(stamps: list[float], hz: float) -> tuple[float, float]:
    """(p99, worst) in ms: how far each frame landed from its slot on the 1/hz grid.

    Measured against the schedule, not against the previous frame: one late frame would otherwise
    count twice (late interval, then a short one) and p99 would always equal the worst."""
    period = 1.0 / hz
    dev = [(t - stamps[0]) - i * period for i, t in enumerate(stamps)]
    mid = sorted(dev)[len(dev) // 2]
    errs = sorted(abs(d - mid) * 1000.0 for d in dev)
    return errs[int(0.99 * (len(errs) - 1))], errs[-1]


def check_loop_jitter(seconds: float = 1.5, hz: float = 60.0) -> Check:
    """Run the real FrameClock and report how far frames land from their 16.7 ms slots."""
    from watchdog import FrameClock
    winmm = None
    if os.name == "nt":
        try:
            winmm = ctypes.WinDLL("winmm")
            winmm.timeBeginPeriod(1)
        except OSError:
            winmm = None
    try:
        fc, stamps = FrameClock(hz), []
        for _ in range(max(3, int(seconds * hz))):
            stamps.append(fc.wait())
    finally:
        if winmm is not None:
            winmm.timeEndPeriod(1)
    p99, worst = jitter_stats(stamps, hz)
    detail = f"{hz:.0f} Hz loop: p99 jitter {p99:.2f} ms, worst {worst:.2f} ms, overruns {fc.overruns}"
    if p99 <= JITTER_OK_MS and fc.overruns == 0:
        return Check("loop jitter", OK, detail)
    fix = "Close heavy background apps, plug in the laptop, set the power plan to High performance"
    return Check("loop jitter", WARN if p99 <= JITTER_WARN_MS else FAIL, detail, fix)


# ------------------------------------------------------------------ run + report
def run_all(slow: bool = True) -> list[Check]:
    """slow=False skips the camera open and the 1.5 s jitter run (quick checks only)."""
    plan: list[tuple[str, Callable[[], Check]]] = [
        ("python", check_python), ("platform", check_platform), ("elevation", check_elevation),
        ("running instance", check_running_instance), ("folders", check_folders),
        ("dependencies", check_dependencies),
        ("webcam privacy", lambda: check_privacy("webcam")),
        ("microphone privacy", lambda: check_privacy("microphone")),
        ("sendinput", check_sendinput), ("display", check_display),
        ("pointer precision", check_pointer_precision),
    ]
    if slow:
        plan += [("camera", check_camera), ("microphone", check_microphone),
                 ("loop jitter", check_loop_jitter)]
    out: list[Check] = []
    for name, fn in plan:
        try:
            out.append(fn())
        except Exception as e:                    # the doctor must never crash
            log.exception("check %s crashed", name)
            out.append(Check(name, FAIL, f"check crashed: {type(e).__name__}: {e}"))
    return out


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0


def format_report(checks: list[Check]) -> str:
    tag = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[skip]"}
    lines = [f"OmniControl {__version__} doctor", ""]
    for c in checks:
        lines.append(f"{tag[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status in (WARN, FAIL):
            lines.append(f"       fix: {c.fix}")
    n = {s: sum(c.status == s for c in checks) for s in (OK, WARN, FAIL, SKIP)}
    lines += ["", f"{n[OK]} ok, {n[WARN]} warnings, {n[FAIL]} failures, {n[SKIP]} skipped"]
    return "\n".join(lines)


def attach_parent_console() -> bool:
    """A windowed (no console) exe started from cmd/PowerShell: borrow that terminal for output."""
    if os.name != "nt" or sys.stdout is not None:
        return False
    try:
        if not ctypes.WinDLL("kernel32", use_last_error=True).AttachConsole(0xFFFFFFFF):
            return False                          # ATTACH_PARENT_PROCESS: no parent console (double-click)
        sys.stdout = sys.stderr = open("CONOUT$", "w", encoding="utf-8")
        return True
    except OSError:
        return False


def show_message_box(title: str, text: str) -> None:
    """Skipped when OMNICONTROL_NO_GUI is set (build scripts and CI must never block on a dialog)."""
    if os.name == "nt" and not os.environ.get("OMNICONTROL_NO_GUI"):
        try:
            ctypes.windll.user32.MessageBoxW(None, text[:1800], title, 0x40)   # MB_ICONINFORMATION
        except Exception:
            log.exception("message box failed")


def run_cli(as_json: bool = False, slow: bool = True) -> int:
    """Entry point for --doctor. Always saves doctor-report.txt in the log folder: a windowed exe
    started by double-click has no terminal, so it also shows the result in a message box."""
    attach_parent_console()
    checks = run_all(slow)
    text = json.dumps([asdict(c) for c in checks], indent=2) if as_json else format_report(checks)
    try:
        config.log_dir().mkdir(parents=True, exist_ok=True)
        (config.log_dir() / "doctor-report.txt").write_text(format_report(checks) + "\n", encoding="utf-8")
    except OSError:
        log.warning("could not save doctor-report.txt")
    if sys.stdout is not None:
        print(text)
    else:
        show_message_box("OmniControl doctor", format_report(checks))
    return exit_code(checks)
