"""win32_output_sink.py - zero-dependency Win32 output for OmniControl OS.

Everything here is pure ctypes + stdlib: no pywin32, no vgamepad, no drivers.

  * INPUT structs are the fixed-width x64 layout; sizeof(INPUT) == 40 is asserted
    at import (a wrong size makes SendInput return 0 with ERROR_INVALID_PARAMETER).
  * StatefulOutputSink NEVER takes raw key-down/key-up calls from the pipeline.
    Callers only change the DESIRED state (want_key/want_mouse/axis); flush()
    diffs desired vs actually-held, releases first, presses second - the same
    rule that keeps a stuck key impossible in the Unbound engine.
  * panic_release_all() is registered with atexit and is also safe to call from
    any thread (the internal lock makes watchdog-thread release legal).
  * WindowContextInspector reads the foreground window via user32 ctypes and
    matches its title/exe against config.json window_profiles - no focus steal,
    read-only.
  * inject_unicode_string() sends UTF-16 code units with KEYEVENTF_UNICODE for
    instant multi-word typing in Workstation mode.

Non-Windows: every Win32 call is guarded; the module imports cleanly and the
sink simply records nothing (RecordingBackend-style NullBackend fallback is
provided for tests/off-Windows dev).
"""
from __future__ import annotations

import atexit
import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from typing import Iterable, Optional

log = logging.getLogger("omnicontrol_os.output")

# ---------------------------------------------------------------- Win32 structs
# Fixed-width types so the layout is right on x64: INPUT must be exactly 40 bytes.
_DWORD, _LONG, _WORD = ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint16
_ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", _LONG), ("dy", _LONG), ("mouseData", _DWORD),
                ("dwFlags", _DWORD), ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", _WORD), ("wScan", _WORD), ("dwFlags", _DWORD),
                ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", _DWORD), ("wParamL", _WORD), ("wParamH", _WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", _DWORD), ("u", _INPUT_UNION)]


if ctypes.sizeof(ctypes.c_void_p) == 8:
    assert ctypes.sizeof(INPUT) == 40, f"INPUT is {ctypes.sizeof(INPUT)} bytes, expected 40"

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000

# Virtual-key codes we need for unicode-injection bookkeeping (not for scancode path).
VK_SHIFT = 0x10
VK_MENU = 0x12          # Alt
VK_CONTROL = 0x11
VK_RETURN = 0x0D

MOUSE_FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010),
               "middle": (0x0020, 0x0040), "x1": (0x0080, 0x0100), "x2": (0x0100, 0x0200)}

# Default scancode table; config.json "scancodes" overrides/extends it.
DEFAULT_SCANCODES = {
    "esc": (0x01, False), "tab": (0x0F, False), "enter": (0x1C, False),
    "space": (0x39, False), "backspace": (0x0E, False),
    "shift": (0x2A, False), "lshift": (0x2A, False), "rshift": (0x36, False),
    "ctrl": (0x1D, False), "lctrl": (0x1D, False), "rctrl": (0x1D, True),
    "alt": (0x38, False), "lalt": (0x38, False), "ralt": (0x38, True),
    "up": (0x48, True), "down": (0x50, True), "left": (0x4B, True), "right": (0x4D, True),
    "home": (0x47, True), "end": (0x4F, True), "pgup": (0x49, True), "pgdn": (0x51, True),
    "insert": (0x52, True), "delete": (0x53, True), "capslock": (0x3A, False),
    "lwin": (0x5B, True), "apps": (0x5D, True),
}
for _i, _c in enumerate("1234567890"):
    DEFAULT_SCANCODES[_c] = (0x02 + _i, False)
for _row, _start in (("qwertyuiop", 0x10), ("asdfghjkl", 0x1E), ("zxcvbnm", 0x2C)):
    for _i, _c in enumerate(_row):
        DEFAULT_SCANCODES[_c] = (_start + _i, False)
for _i in range(12):
    DEFAULT_SCANCODES[f"f{_i + 1}"] = (0x3B + _i, False)
for _i in range(13, 25):                      # F13-F24: OmniControl-only bindings
    DEFAULT_SCANCODES[f"f{_i}"] = (0x64 + (_i - 13), False)


def build_key_input_scancode(name: str, down: bool, scancodes: dict) -> Optional[INPUT]:
    ent = scancodes.get(name)
    if ent is None:
        return None
    code, ext = ent
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if ext else 0) | (0 if down else KEYEVENTF_KEYUP)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(0, code, flags, 0, 0)
    return inp


def build_mouse_button_input(name: str, down: bool) -> Optional[INPUT]:
    if name not in MOUSE_FLAGS:
        return None
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(0, 0, 0, MOUSE_FLAGS[name][0 if down else 1], 0, 0)
    return inp


def build_mouse_move_input(dx: int, dy: int) -> INPUT:
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, 0)
    return inp


def _unicode_input(code_unit: int, down: bool) -> INPUT:
    """One UTF-16 code unit as a KEYEVENTF_UNICODE keystroke."""
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(0, code_unit & 0xFFFF,
                          KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP), 0, 0)
    return inp


def inject_unicode_string(text: str) -> int:
    """Type `text` via KEYEVENTF_UNICODE. Returns number of SendInput events accepted
    (2 per UTF-16 code unit). Windows-only; returns 0 elsewhere."""
    if os.name != "nt":
        return 0
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    events: list[INPUT] = []
    data = text.encode("utf-16-le", "surrogatepass")   # one INPUT pair per UTF-16 unit
    for i in range(0, len(data), 2):
        cu = data[i] | (data[i + 1] << 8)
        events.append(_unicode_input(cu, True))
        events.append(_unicode_input(cu, False))
    if not events:
        return 0
    arr = (INPUT * len(events))(*events)
    return int(user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)))


# ---------------------------------------------------------------- backends
class _NullBackend:
    """Off-Windows / test sink: accepts everything, sends nothing."""

    def __init__(self) -> None:
        self.log: list[tuple] = []
        self.keys: set[str] = set()
        self.mouse: set[str] = set()

    def key(self, name: str, down: bool) -> None:
        self.log.append(("key", name, down))
        (self.keys.add if down else self.keys.discard)(name)

    def mouse_button(self, name: str, down: bool) -> None:
        self.log.append(("mouse", name, down))
        (self.mouse.add if down else self.mouse.discard)(name)

    def mouse_move(self, dx: int, dy: int) -> None:
        self.log.append(("move", dx, dy))

    def commit(self) -> None:
        pass


class Win32Backend:
    """Real SendInput backend. Batches queued events into ONE SendInput call per commit."""

    def __init__(self, scancodes: Optional[dict] = None) -> None:
        if os.name != "nt":
            raise OSError("Win32Backend only works on Windows")
        self._user32 = ctypes.windll.user32
        self._user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self._user32.SendInput.restype = wintypes.UINT
        self.scancodes = dict(DEFAULT_SCANCODES, **(scancodes or {}))
        self._q: list[INPUT] = []
        self.sent_events = 0
        self.send_errors = 0

    def key(self, name: str, down: bool) -> None:
        inp = build_key_input_scancode(name, down, self.scancodes)
        if inp is not None:
            self._q.append(inp)
        else:
            log.warning("no scancode for key %r", name)

    def mouse_button(self, name: str, down: bool) -> None:
        inp = build_mouse_button_input(name, down)
        if inp is not None:
            self._q.append(inp)

    def mouse_move(self, dx: int, dy: int) -> None:
        if dx or dy:
            self._q.append(build_mouse_move_input(int(dx), int(dy)))

    def commit(self) -> None:
        if not self._q:
            return
        n = len(self._q)
        arr = (INPUT * n)(*self._q)
        self._q.clear()
        sent = self._user32.SendInput(n, arr, ctypes.sizeof(INPUT))
        self.sent_events += int(sent)
        if sent != n:
            self.send_errors += 1
            log.error("SendInput sent %d/%d (GetLastError=%d)", sent, n,
                      ctypes.GetLastError())


# ---------------------------------------------------------------- the sink
class StatefulOutputSink:
    """Desired-state -> actual-input diff engine. The ONLY object allowed to hold keys.

    want_key(name, down) / want_mouse(name, down) / set_axis(name, value) mutate the
    desired state; flush(now) pushes the diff. release_all(reason) drops everything
    immediately and is thread-safe (watchdog/atexit/console-close safe).
    """

    def __init__(self, backend=None, scancodes: Optional[dict] = None,
                 axis_map: Optional[dict] = None, auto_release: bool = True):
        self.backend = backend
        if self.backend is None:
            try:
                self.backend = Win32Backend(scancodes=scancodes)
            except OSError:
                self.backend = _NullBackend()
        self.scancodes = dict(DEFAULT_SCANCODES, **(scancodes or {}))
        # axis_map: name -> {"type": "mouse"|"keys", ...} same convention as Unbound
        self.axis_map = {k: v for k, v in (axis_map or {}).items()
                         if isinstance(v, dict) and not k.startswith("_")}
        self._lock = threading.RLock()
        self._want_keys: set[str] = set()
        self._want_mouse: set[str] = set()
        self._axes: dict[str, float] = {}
        self._axis_keys: dict[str, set[str]] = {}
        self._held_keys: set[str] = set()
        self._held_mouse: set[str] = set()
        self._rem = [0.0, 0.0]
        self._last_flush: Optional[float] = None
        self._panic = False
        if auto_release:
            atexit.register(self.panic_release_all, "atexit")

    # -- desired state -----------------------------------------------------
    def want_key(self, name: str, down: bool) -> None:
        with self._lock:
            (self._want_keys.add if down else self._want_keys.discard)(name)

    def want_mouse(self, name: str, down: bool) -> None:
        with self._lock:
            (self._want_mouse.add if down else self._want_mouse.discard)(name)

    def set_axis(self, name: str, value: float) -> None:
        with self._lock:
            self._axes[name] = max(-1.0, min(1.0, float(value)))

    def release_all(self, reason: str = "") -> None:
        """Desired state -> empty; next flush releases everything. Thread-safe."""
        with self._lock:
            had = bool(self._held_keys or self._held_mouse)
            self._want_keys.clear()
            self._want_mouse.clear()
            self._axes.clear()
            self._axis_keys.clear()
            self._rem = [0.0, 0.0]
        if had:
            log.warning("release_all (%s)", reason or "unspecified")

    def panic_release_all(self, reason: str = "panic") -> None:
        """Emergency: clear desired AND push releases immediately (no flush needed)."""
        with self._lock:
            if self._panic:
                return
            self._panic = True
            try:
                for k in list(self._held_keys):
                    self.backend.key(k, False)
                for m in list(self._held_mouse):
                    self.backend.mouse_button(m, False)
                self.backend.commit()
                self._held_keys.clear()
                self._held_mouse.clear()
                self._want_keys.clear()
                self._want_mouse.clear()
                self._axes.clear()
                self._axis_keys.clear()
                log.warning("PANIC release_all (%s)", reason)
            except Exception:
                log.exception("panic_release_all failed")
            finally:
                self._panic = False

    @property
    def held(self) -> frozenset:
        with self._lock:
            return frozenset(self._want_keys | self._want_mouse | self._held_keys | self._held_mouse)

    # -- the tick -----------------------------------------------------------
    def flush(self, now: float) -> None:
        """Push desired->actual diff. Call every tick at loop rate."""
        with self._lock:
            dt = 0.0 if self._last_flush is None else min(0.05, max(0.0, now - self._last_flush))
            self._last_flush = now
            dx = dy = 0
            for name, spec in self.axis_map.items():
                v = self._axes.get(name, 0.0)
                if spec.get("type") == "mouse":
                    d = v * spec.get("speed", 900.0) * dt * (-1.0 if spec.get("invert") else 1.0)
                    i = 0 if spec.get("axis", "x") == "x" else 1
                    d += self._rem[i]
                    whole = int(d)
                    self._rem[i] = d - whole
                    if i == 0:
                        dx += whole
                    else:
                        dy += whole
            want_k = self._want_keys | self._axis_desired_keys()
            b = self.backend
            for k in self._held_keys - want_k:              # releases FIRST
                b.key(k, False)
            for k in want_k - self._held_keys:
                b.key(k, True)
            for m in self._held_mouse - self._want_mouse:
                b.mouse_button(m, False)
            for m in self._want_mouse - self._held_mouse:
                b.mouse_button(m, True)
            if dx or dy:
                b.mouse_move(dx, dy)
            b.commit()
            self._held_keys = set(want_k)
            self._held_mouse = set(self._want_mouse)

    def _axis_desired_keys(self) -> set[str]:
        keys: set[str] = set()
        for name, spec in self.axis_map.items():
            if spec.get("type") != "keys":
                continue
            v = self._axes.get(name, 0.0)
            thr = spec.get("threshold", 0.35)
            cur = self._axis_keys.get(name, set())
            new: set[str] = set()
            for key, sign in ((spec["pos"], 1.0), (spec["neg"], -1.0)):
                x = v * sign
                if x >= thr or (key in cur and x > thr * 0.6):   # Schmitt trigger
                    new.add(key)
            self._axis_keys[name] = new
            keys |= new
        return keys


# ---------------------------------------------------------------- window context
class WindowContextInspector:
    """Read-only foreground-window probe. Returns (title, exe_name, mode_hint).

    Uses raw ctypes user32/kernel32 - no pywin32 needed. On non-Windows or any
    failure it returns ("", "", "") so callers fall back to the last known mode.
    """

    def __init__(self, profiles: Optional[dict] = None, poll_hz: float = 4.0):
        self.profiles = profiles or {}
        self._period = 1.0 / poll_hz
        self._next = 0.0
        self._cached = ("", "", "")

    def poll(self, now: float) -> tuple[str, str, str]:
        if os.name != "nt" or now < self._next:
            return self._cached
        self._next = now + self._period
        try:
            title, exe = self._foreground()
            mode = self.match(title, exe)
            self._cached = (title, exe, mode)
        except Exception:
            log.debug("foreground probe failed", exc_info=True)
        return self._cached

    @staticmethod
    def _foreground() -> tuple[str, str]:
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return "", ""
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value
        pid = wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        h = k32.OpenProcess(0x1000, False, pid.value)        # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            try:
                buf2 = ctypes.create_unicode_buffer(512)
                size = wintypes.DWORD(512)
                if k32.QueryFullProcessImageNameW(h, 0, buf2, ctypes.byref(size)):
                    exe = buf2.value.rsplit("\\", 1)[-1]
            finally:
                k32.CloseHandle(h)
        return title, exe

    def match(self, title: str, exe: str = "") -> str:
        """First profile whose match strings appear in title or exe name."""
        hay = f"{title} {exe}".lower()
        for name, prof in self.profiles.items():
            needles = prof.get("match", [])
            if any(n.lower() in hay for n in needles):
                return prof.get("mode", name)
        return ""


def load_scancode_table(cfg: dict) -> dict:
    """Merge config.json scancodes over DEFAULT_SCANCODES -> {name: (code, ext)}."""
    out = {k: tuple(v) for k, v in DEFAULT_SCANCODES.items()}
    for name, ent in (cfg.get("scancodes") or {}).items():
        if name.startswith("_") or not isinstance(ent, (list, tuple)) or len(ent) != 2:
            continue
        out[name] = (int(ent[0]), bool(ent[1]))
    return out
