"""tools/acceptance.py (Binore): ONE command that runs every automated check for my part.

Prints a PASS / FAIL / SKIP table and exits 0 only when every REQUIRED check PASSes.
SKIP never counts as PASS: a required check that skips makes the run FAIL.

Checks: environment, full pytest, fuzz suite, full doctor, live SendInput self-test,
replay dry-run, 2-minute soak with injected stall, release-on-exit subprocess test,
config hot-reload, single-instance guard, PyInstaller bundle (when present) and the
clock rule (no time.time()/time.monotonic() in pipeline files).

Still MANUAL (printed at the end): real camera tracking quality, real game feel,
console-close handling, sleep/session-lock, phone slow-mo end-to-end latency, and a
hard TerminateProcess kill (the OS gives a process no chance to clean up - only
graceful exits are covered by the release-on-exit check).

Usage: python tools/acceptance.py [--quick]   (--quick skips the 2 min soak)
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import logging
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("omnicontrol.acceptance")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
MANUAL_ITEMS = [
    "real camera tracking quality (mediapipe on the demo laptop)",
    "real microphone / voice commands",
    "real game feel in SuperTuxKart (SendInput delivery, aim, latency)",
    "console window close / logoff / shutdown release",
    "sleep / session-lock release",
    "phone slow-mo end-to-end latency (tools/latency_probe.py)",
    "hard TerminateProcess/taskkill /F: Windows gives a killed process no cleanup "
    "chance; only graceful exits (atexit, signals, console ctrl) release keys",
]

# pipeline files the clock rule covers (tests excluded: HFEvent fixtures use the
# teammates' wall-clock contract; core/mapping.py + core/profile.py are Dishti's)
CLOCK_SCAN = [
    "config.py", "gateway.py", "main.py", "state_machine.py", "watchdog.py",
    "tray.py", "doctor.py", "elevation.py", "single_instance.py", "logsetup.py",
    "omnicontrol.py", "version.py",
    "core/clock.py", "core/events.py", "core/__init__.py",
    "adapters/replay.py",
    "tools/soak.py", "tools/latency_probe.py", "tools/camera_bench.py",
    "tools/verify_bundle.py", "tools/acceptance.py",
]


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    required: bool = True


def _run(cmd: list[str], timeout: float, cwd: Path = ROOT) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"


# ------------------------------------------------------------------ the checks
def check_environment() -> CheckResult:
    v = sys.version_info
    bits = struct.calcsize("P") * 8
    problems = []
    if (v.major, v.minor) < (3, 11):
        problems.append(f"Python {v.major}.{v.minor} < 3.11")
    if bits != 64:
        problems.append(f"{bits}-bit interpreter")
    if os.name == "nt":
        size = ctypes_sizeof_INPUT()
        if size != 40:
            problems.append(f"INPUT struct is {size} bytes, expected 40")
    if problems:
        return CheckResult("environment", FAIL, "; ".join(problems))
    return CheckResult("environment", PASS,
                       f"Python {v.major}.{v.minor}.{v.micro} {bits}-bit"
                       + (", INPUT=40B" if os.name == "nt" else ""))


def ctypes_sizeof_INPUT() -> int:
    import ctypes
    import gateway
    return ctypes.sizeof(gateway.INPUT)


def check_pytest() -> CheckResult:
    rc, out = _run([sys.executable, "-m", "pytest", "-q"], timeout=600)
    m = re.search(r"(\d+) passed(?:, (\d+) skipped)?(?:, (\d+) failed)?", out)
    tail = out.strip().splitlines()[-1] if out.strip() else "no output"
    if rc != 0 or not m or (m.group(3) and int(m.group(3)) > 0):
        return CheckResult("pytest (all)", FAIL, tail)
    return CheckResult("pytest (all)", PASS,
                       f"{m.group(1)} passed"
                       + (f", {m.group(2)} skipped" if m.group(2) else ""))


def check_fuzz() -> CheckResult:
    rc, out = _run([sys.executable, "-m", "pytest", "-q", "tests/test_fuzz_safety.py"],
                   timeout=300)
    m = re.search(r"(\d+) passed", out)
    n = int(m.group(1)) if m else 0
    if rc != 0 or n < 200:
        tail = out.strip().splitlines()[-1] if out.strip() else "no output"
        return CheckResult("fuzz suite", FAIL, f"{tail} (need >=200 seeds)")
    return CheckResult("fuzz suite", PASS, f"{n} seeds x 600 ticks")


def check_doctor() -> CheckResult:
    rc, out = _run([sys.executable, "main.py", "--doctor"], timeout=180)
    wanted = ("camera", "microphone", "sendinput", "mock input")
    bits = []
    for line in out.splitlines():
        m = re.match(r"\s*\[(OK|WARN|FAIL|SKIP)\]\s+([^:]+):", line)
        if m and m.group(2).strip() in wanted:
            bits.append(f"{m.group(1)} {m.group(2).strip()}")
    detail = "; ".join(bits) if bits else out.strip().splitlines()[-1] if out.strip() else ""
    if rc != 0:
        fails = [l.strip() for l in out.splitlines() if l.startswith("[FAIL]")]
        return CheckResult("doctor (full)", FAIL,
                           detail + " | failing: " + "; ".join(fails[:3]))
    return CheckResult("doctor (full)", PASS, detail or "exit 0")


def check_sendinput_live() -> CheckResult:
    if os.name != "nt":
        return CheckResult("sendinput live", SKIP, "Windows only", required=False)
    import ctypes
    from ctypes import wintypes
    import gateway
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(gateway.INPUT), ctypes.c_int)
    u32.SendInput.restype = wintypes.UINT
    u32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    u32.GetAsyncKeyState.restype = ctypes.c_short
    # shift down + shift up + a 3,-2 relative mouse move: nothing a user would notice
    evs = [gateway.build_key_input("shift", True),
           gateway.build_key_input("shift", False),
           gateway.build_mouse_move_input(3, -2)]
    arr = (gateway.INPUT * len(evs))(*evs)
    ctypes.set_last_error(0)
    sent = u32.SendInput(len(evs), arr, ctypes.sizeof(gateway.INPUT))
    if sent != len(evs):
        return CheckResult("sendinput live", FAIL,
                           f"SendInput accepted {sent}/{len(evs)} "
                           f"(GetLastError={ctypes.get_last_error()})")
    time.sleep(0.05)
    held = bool(u32.GetAsyncKeyState(0x10) & 0x8000)          # VK_SHIFT
    if held:
        return CheckResult("sendinput live", FAIL, "shift still held after key-up")
    return CheckResult("sendinput live", PASS,
                       f"SendInput {sent}/{len(evs)} events, nothing left held")


def check_replay() -> CheckResult:
    if not (ROOT / "demo.jsonl").is_file():
        return CheckResult("replay dry-run", SKIP, "demo.jsonl missing", required=False)
    rc, out = _run([sys.executable, "main.py", "--safe", "--replay", "demo.jsonl",
                    "--dry-run", "--speed", "8", "--duration", "10"], timeout=60)
    states = sorted(s for s in ("LATCHED", "SAFE_IDLE", "ACTIVE", "CONVERSE_LOCK")
                    if s in out)
    ok = rc == 0 and "LATCHED" in out and "SAFE_IDLE" in out
    detail = f"exit={rc}, states seen: {', '.join(states) or 'none'}"
    return CheckResult("replay dry-run", PASS if ok else FAIL, detail)


def check_soak(quick: bool) -> CheckResult:
    if quick:
        return CheckResult("soak 2min", SKIP, "--quick", required=False)
    csv_path = ROOT / "acceptance_soak.csv"
    try:
        rc, out = _run([sys.executable, "tools/soak.py", "--replay", "demo.jsonl",
                        "--minutes", "2", "--csv", str(csv_path), "--inject-stall"],
                       timeout=240)
    finally:
        csv_path.unlink(missing_ok=True)
    stall = "STALL CHECK: PASS" in out
    verdict = re.search(r"SOAK [\d.]+min .* -> (PASS|FAIL[^\n]*)", out)
    tail = verdict.group(0) if verdict else (out.strip().splitlines()[-1] if out.strip() else "")
    ok = rc == 0 and stall and verdict and verdict.group(1) == "PASS"
    return CheckResult("soak 2min", PASS if ok else FAIL, tail)


_RELEASE_CHILD = r"""
import os, sys, time, signal
sys.path.insert(0, {root!r})
from gateway import Gateway, SendInputBackend, RecordingBackend
from core.events import OutputCommand
gw = Gateway(SendInputBackend() if os.name == "nt" else RecordingBackend())
gw.install_exit_handlers()
gw.apply([OutputCommand(kind="key", name="shift", pressed=True)])
gw.flush(time.perf_counter())
print("READY", flush=True)
mode = {mode!r}
if mode == "sigint":
    time.sleep(0.5)
    signal.raise_signal(signal.SIGINT)
else:
    time.sleep(2.0)          # plain exit -> atexit release_all
"""


def _shift_held() -> bool:
    import ctypes
    u32 = ctypes.windll.user32
    u32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    u32.GetAsyncKeyState.restype = ctypes.c_short
    return bool(u32.GetAsyncKeyState(0x10) & 0x8000)


def check_release_on_exit() -> CheckResult:
    if os.name != "nt":
        return CheckResult("release-on-exit", SKIP, "Windows only", required=False)
    results = []
    for mode in ("atexit", "sigint"):
        code = _RELEASE_CHILD.format(root=str(ROOT), mode=mode)
        p = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            out, _ = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            return CheckResult("release-on-exit", FAIL, f"{mode}: child hung")
        time.sleep(0.3)                       # let the OS register the key-up
        held = _shift_held()
        results.append(f"{mode}: exit={p.returncode} shift_held={held}")
        if held:
            return CheckResult("release-on-exit", FAIL,
                               "; ".join(results) + " - key left held!")
    return CheckResult("release-on-exit", PASS, "; ".join(results))


def check_config_reload() -> CheckResult:
    import config as cfgmod
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "config.json"
        path.write_text(json.dumps({"profile": "default", "hz": 60}), encoding="utf-8")
        w = cfgmod.ConfigWatcher(path)
        first = w.load()
        path.write_text(json.dumps({"profile": "supertuxkart", "hz": 60}),
                        encoding="utf-8")
        time.sleep(0.05)                     # ensure mtime differs
        second = w.poll(time.perf_counter() + 1.0)
        path.write_text("{ not json", encoding="utf-8")
        third = w.poll(time.perf_counter() + 2.0)
    ok = (second is not None and second.profile == "supertuxkart"
          and third is None and w.current.profile == "supertuxkart")
    return CheckResult("config hot-reload", PASS if ok else FAIL,
                       f"reload={second is not None}, bad-file kept={w.current.profile}")


def check_single_instance() -> CheckResult:
    import single_instance
    name = f"omnicontrol-acceptance-{os.getpid()}"
    a = single_instance.SingleInstance(name)
    b = single_instance.SingleInstance(name)
    try:
        first = a.acquire()
        second = b.acquire()
    finally:
        a.release()
        b.release()
    ok = first and not second
    return CheckResult("single instance", PASS if ok else FAIL,
                       f"first={first} second_refused={not second}")


def check_bundle() -> CheckResult:
    if importlib.util.find_spec("PyInstaller") is None and importlib.util.find_spec("pyinstaller") is None:
        return CheckResult("bundle", SKIP, "PyInstaller not installed", required=False)
    bundle = ROOT / "dist" / "OmniControl"
    if not bundle.is_dir():
        return CheckResult("bundle", FAIL, "dist/OmniControl missing - run build.ps1")
    rc, out = _run([sys.executable, "tools/verify_bundle.py", "--bundle", str(bundle)],
                   timeout=120)
    fails = [l for l in out.splitlines() if "[FAIL]" in l]
    if rc != 0 or fails:
        return CheckResult("bundle", FAIL, "; ".join(fails[:3]) or out.strip().splitlines()[-1])
    exe = bundle / "OmniControl.exe"
    if not exe.is_file():
        return CheckResult("bundle", FAIL, "OmniControl.exe missing in bundle")
    rc1, _ = _run([str(exe), "--doctor", "--quick", "--allow-mock"], timeout=120)
    rc2, _ = _run([str(exe), "--safe", "--duration", "5"], timeout=60)
    ok = rc1 == 0 and rc2 == 0
    return CheckResult("bundle", PASS if ok else FAIL,
                       f"verify_bundle ok; exe doctor={rc1} safe5s={rc2}")


def clock_violations(path: Path) -> list[str]:
    """AST scan: real calls to time.time()/time.monotonic() (comments/strings ignored)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("time", "monotonic")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time"):
            bad.append(f"{path.name}:{node.lineno} time.{node.func.attr}()")
    return bad


def check_clock_rule() -> CheckResult:
    bad: list[str] = []
    for rel in CLOCK_SCAN:
        p = ROOT / rel
        if p.is_file():
            bad.extend(clock_violations(p))
    if bad:
        return CheckResult("clock rule", FAIL, "; ".join(bad[:5]))
    return CheckResult("clock rule", PASS,
                       f"{len(CLOCK_SCAN)} pipeline files clean "
                       "(core/mapping.py, core/profile.py, tests excluded: teammate contract)")


CHECKS = [
    ("environment", check_environment),
    ("pytest (all)", check_pytest),
    ("fuzz suite", check_fuzz),
    ("doctor (full)", check_doctor),
    ("sendinput live", check_sendinput_live),
    ("replay dry-run", check_replay),
    ("soak 2min", check_soak),
    ("release-on-exit", check_release_on_exit),
    ("config hot-reload", check_config_reload),
    ("single instance", check_single_instance),
    ("bundle", check_bundle),
    ("clock rule", check_clock_rule),
]


# ------------------------------------------------------------------ reporting
def summarize(results: list[CheckResult]) -> tuple[int, int]:
    """(fails, unpassed_required): a required SKIP counts as unpassed, never as PASS."""
    fails = sum(1 for r in results if r.status == FAIL)
    unpassed = sum(1 for r in results if r.required and r.status != PASS)
    return fails, unpassed


def exit_code(results: list[CheckResult]) -> int:
    fails, unpassed = summarize(results)
    return 0 if fails == 0 and unpassed == 0 else 1


def format_table(results: list[CheckResult]) -> str:
    lines = [f"{'check':<20} {'result':<5} detail",
             "-" * 100]
    for r in results:
        tag = r.status if r.status != SKIP or r.required else f"{SKIP}*"
        lines.append(f"{r.name:<20} {tag:<5} {r.detail}")
    n_pass = sum(1 for r in results if r.status == PASS)
    n_skip = sum(1 for r in results if r.status == SKIP)
    fails, unpassed = summarize(results)
    lines.append("-" * 100)
    lines.append(f"{n_pass} passed, {n_skip} skipped, {fails} failed")
    verdict = "PASS" if exit_code(results) == 0 else f"FAIL ({unpassed})"
    lines.append(f"ACCEPTANCE: {verdict}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniControl acceptance run")
    ap.add_argument("--quick", action="store_true", help="skip the 2-minute soak")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    results: list[CheckResult] = []
    for name, fn in CHECKS:
        if name == "soak 2min":
            print(f"\n=== running {name} (about 2 minutes) ===", flush=True)
            results.append(fn(a.quick))
        else:
            print(f"\n=== running {name} ===", flush=True)
            try:
                results.append(fn())
            except Exception as e:
                log.exception("check %s crashed", name)
                results.append(CheckResult(name, FAIL, f"check crashed: {e}"))
        print(f"    -> {results[-1].status}: {results[-1].detail}", flush=True)

    print("\n" + format_table(results))
    print("\nStill MANUAL (needs the demo laptop / a human):")
    for item in MANUAL_ITEMS:
        print(f"  - {item}")
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
