"""gateway.py (Binore): the way out to games.

Takes Dishti's OutputCommands and turns them into real Windows input, with one rule above all:
NOTHING stays pressed that the pipeline does not currently want pressed.

  * No caller ever sends key-down / key-up. Commands only change the DESIRED state.
    flush() diffs desired vs what the backend actually holds, and releases first, presses second.
  * release_all() is thread-safe and is wired to the watchdog, atexit, signals, console close.
  * Pure Win32 SendInput (scancodes + relative mouse), no vgamepad / ViGEm driver.
  * Backends: SendInputBackend (Windows), RecordingBackend (tests / --dry-run / non-Windows).

Command vocabulary (agree with Dishti):
    key    "w", "space", "enter" ...                 pressed True/False
    button "A","B","X","Y","LB","RB","LT","RT",...   pressed True/False  -> BUTTON_MAP below
    axis   "left_x","left_y","right_x","right_y"     value -1..1         -> AXIS_MAP below
           y is "up positive": right_y > 0 looks up, left_y > 0 walks forward.
    release_all
"""
from __future__ import annotations

import atexit
import ctypes
import logging
import os
import signal
import threading
from typing import Optional, Protocol

from core.events import OutputCommand

log = logging.getLogger("omnicontrol.gateway")

# ------------------------------------------------------------------ win32 structs
# Fixed-width types so the layout is right on x64: INPUT must be exactly 40 bytes,
# otherwise SendInput silently returns 0 (ERROR_INVALID_PARAMETER).
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


class _INPUT_UNION(ctypes.Union):          # MOUSEINPUT is the largest member: it sets the size
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", _DWORD), ("u", _INPUT_UNION)]


if ctypes.sizeof(ctypes.c_void_p) == 8:
    assert ctypes.sizeof(INPUT) == 40, f"INPUT is {ctypes.sizeof(INPUT)} bytes, expected 40"

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSE_FLAGS = {  # name -> (down, up)
    "left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040),
}

# DirectX / set-1 hardware scancodes. (code, extended)
SCANCODES: dict[str, tuple[int, bool]] = {
    "esc": (0x01, False), "tab": (0x0F, False), "enter": (0x1C, False), "space": (0x39, False),
    "backspace": (0x0E, False), "shift": (0x2A, False), "ctrl": (0x1D, False), "alt": (0x38, False),
    "up": (0x48, True), "down": (0x50, True), "left": (0x4B, True), "right": (0x4D, True),
    "home": (0x47, True), "end": (0x4F, True), "pgup": (0x49, True), "pgdn": (0x51, True),
    "insert": (0x52, True), "delete": (0x53, True),
}
for _i, _c in enumerate("1234567890"):
    SCANCODES[_c] = (0x02 + _i, False)
for _row, _start in (("qwertyuiop", 0x10), ("asdfghjkl", 0x1E), ("zxcvbnm", 0x2C)):
    for _i, _c in enumerate(_row):
        SCANCODES[_c] = (_start + _i, False)
for _i in range(10):
    SCANCODES[f"f{_i + 1}"] = (0x3B + _i, False)

# Gamepad-style names -> keyboard/mouse. Override per game via Gateway(button_map=...).
BUTTON_MAP: dict[str, tuple[str, str]] = {
    "A": ("key", "space"), "B": ("key", "c"), "X": ("key", "r"), "Y": ("key", "f"),
    "LB": ("key", "q"), "RB": ("key", "e"), "LT": ("mouse", "right"), "RT": ("mouse", "left"),
    "START": ("key", "esc"), "BACK": ("key", "tab"), "L3": ("key", "shift"), "R3": ("key", "v"),
    "DPAD_UP": ("key", "1"), "DPAD_DOWN": ("key", "2"), "DPAD_LEFT": ("key", "3"),
    "DPAD_RIGHT": ("key", "4"),
}
AXIS_MAP: dict[str, dict] = {
    "right_x": {"type": "mouse", "axis": "x", "speed": 900.0},                  # px/s at full deflection
    "right_y": {"type": "mouse", "axis": "y", "speed": 700.0, "invert": True},  # up positive -> dy < 0
    "left_x": {"type": "keys", "neg": "a", "pos": "d", "threshold": 0.35},
    "left_y": {"type": "keys", "neg": "s", "pos": "w", "threshold": 0.35},
}


# ------------------------------------------------------------------ backends
class Backend(Protocol):
    def key(self, name: str, down: bool) -> None: ...
    def mouse_button(self, name: str, down: bool) -> None: ...
    def mouse_move(self, dx: int, dy: int) -> None: ...
    def commit(self) -> None: ...


class RecordingBackend:
    """Dry-run / test backend. Keeps the log and the set of things currently 'held'."""

    def __init__(self) -> None:
        self.log: list[tuple] = []
        self.keys: set[str] = set()
        self.mouse: set[str] = set()
        self.total_dx = 0
        self.total_dy = 0

    def key(self, name: str, down: bool) -> None:
        self.log.append(("key", name, down))
        (self.keys.add if down else self.keys.discard)(name)

    def mouse_button(self, name: str, down: bool) -> None:
        self.log.append(("mouse", name, down))
        (self.mouse.add if down else self.mouse.discard)(name)

    def mouse_move(self, dx: int, dy: int) -> None:
        self.log.append(("move", dx, dy))
        self.total_dx += dx
        self.total_dy += dy

    def commit(self) -> None:
        pass


def build_key_input(name: str, down: bool) -> INPUT:
    code, ext = SCANCODES[name]
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if ext else 0) | (0 if down else KEYEVENTF_KEYUP)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(0, code, flags, 0, 0)
    return inp


def build_mouse_button_input(name: str, down: bool) -> INPUT:
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(0, 0, 0, MOUSE_FLAGS[name][0 if down else 1], 0, 0)
    return inp


def build_mouse_move_input(dx: int, dy: int) -> INPUT:
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, 0)
    return inp


class SendInputBackend:
    """Real injection. Calls are queued and sent as ONE SendInput batch in commit()."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("SendInputBackend only works on Windows")
        self._user32 = ctypes.windll.user32
        self._user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
        self._user32.SendInput.restype = ctypes.c_uint
        self._q: list[INPUT] = []

    def key(self, name: str, down: bool) -> None:
        if name in SCANCODES:
            self._q.append(build_key_input(name, down))
        else:
            log.warning("no scancode for key %r", name)

    def mouse_button(self, name: str, down: bool) -> None:
        if name in MOUSE_FLAGS:
            self._q.append(build_mouse_button_input(name, down))

    def mouse_move(self, dx: int, dy: int) -> None:
        if dx or dy:
            self._q.append(build_mouse_move_input(dx, dy))

    def commit(self) -> None:
        if not self._q:
            return
        n = len(self._q)
        arr = (INPUT * n)(*self._q)
        self._q.clear()
        sent = self._user32.SendInput(n, arr, ctypes.sizeof(INPUT))
        # sent != n means a bad INPUT size or input already blocked by another thread. A UIPI block
        # (elevated foreground window, we are not) is NOT reported here: Microsoft documents that
        # neither the return value nor GetLastError reveals it. elevation.ElevationMonitor handles it.
        if sent != n:
            log.error("SendInput sent %d/%d (GetLastError=%d)", sent, n, ctypes.GetLastError())


# ------------------------------------------------------------------ gateway
class Gateway:
    def __init__(self, backend: Backend, button_map: Optional[dict] = None,
                 axis_map: Optional[dict] = None):
        self.backend = backend
        self.button_map = dict(BUTTON_MAP, **(button_map or {}))
        self.axis_map = dict(AXIS_MAP, **(axis_map or {}))
        self._lock = threading.RLock()
        self._want_keys: set[str] = set()
        self._want_mouse: set[str] = set()
        self._axes: dict[str, float] = {}
        self._axis_keys: dict[str, set[str]] = {}      # axis name -> keys it currently wants
        self._held_keys: set[str] = set()              # what the backend really holds
        self._held_mouse: set[str] = set()
        self._rem = [0.0, 0.0]                         # sub-pixel remainders
        self._last_flush: Optional[float] = None
        self._warned: set[str] = set()
        self.sent_moves = 0

    # --- state in -----------------------------------------------------------
    def apply(self, cmds: list[OutputCommand]) -> None:
        with self._lock:
            for c in cmds:
                if c.kind == "release_all":
                    self._clear_locked()
                    self._sync_locked()
                elif c.kind == "key":
                    (self._want_keys.add if c.pressed else self._want_keys.discard)(c.name)
                elif c.kind == "button":
                    tgt = self.button_map.get(c.name)
                    if tgt is None:
                        self._warn_once(f"button {c.name}", "unmapped button %r", c.name)
                    elif tgt[0] == "key":
                        (self._want_keys.add if c.pressed else self._want_keys.discard)(tgt[1])
                    else:
                        (self._want_mouse.add if c.pressed else self._want_mouse.discard)(tgt[1])
                elif c.kind == "axis":
                    if c.name in self.axis_map:
                        self._axes[c.name] = max(-1.0, min(1.0, float(c.value)))
                    else:
                        self._warn_once(f"axis {c.name}", "unmapped axis %r", c.name)

    def flush(self, now: float) -> None:
        """Call every tick, even with no new commands: held axes keep the mouse moving."""
        with self._lock:
            dt = 0.0 if self._last_flush is None else min(0.05, max(0.0, now - self._last_flush))
            self._last_flush = now
            dx = dy = 0
            for name, spec in self.axis_map.items():
                v = self._axes.get(name, 0.0)
                if spec["type"] == "mouse":
                    d = v * spec["speed"] * dt * (-1.0 if spec.get("invert") else 1.0)
                    i = 0 if spec["axis"] == "x" else 1
                    d += self._rem[i]
                    whole = int(d)                      # truncate toward zero
                    self._rem[i] = d - whole
                    if i == 0:
                        dx += whole
                    else:
                        dy += whole
            self._sync_locked(dx, dy)

    def release_all(self, reason: str = "") -> None:
        """Thread-safe emergency release. Safe to call from the watchdog thread."""
        with self._lock:
            held = bool(self._held_keys or self._held_mouse)
            self._clear_locked()
            self._sync_locked()
        if held:
            log.warning("release_all (%s)", reason or "unspecified")

    def update_maps(self, button_map: Optional[dict] = None, axis_map: Optional[dict] = None) -> None:
        """Swap key bindings while running (config hot-reload). Everything is released FIRST, so
        nothing can stay pressed under the old mapping. Same merge rules as the constructor."""
        with self._lock:
            self._clear_locked()
            self._sync_locked()
            self.button_map = dict(BUTTON_MAP, **(button_map or {}))
            self.axis_map = dict(AXIS_MAP, **(axis_map or {}))

    @property
    def held(self) -> frozenset:
        with self._lock:
            return frozenset(self._held_keys | {f"mouse_{m}" for m in self._held_mouse})

    # --- exit safety ----------------------------------------------------------
    def install_exit_handlers(self) -> None:
        atexit.register(self.release_all, "atexit")

        def _handler(signum, frame):
            self.release_all(f"signal {signum}")
            raise SystemExit(0)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):             # not in main thread
                pass
        if os.name == "nt":                           # console window closed / logoff / shutdown
            try:
                proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

                def _ctrl(_evt):
                    self.release_all("console ctrl")
                    return 0
                self._ctrl_cb = proto(_ctrl)          # keep a reference or it gets GC'd
                ctypes.windll.kernel32.SetConsoleCtrlHandler(self._ctrl_cb, 1)
            except Exception:
                log.exception("SetConsoleCtrlHandler failed")

    # --- internals ----------------------------------------------------------------
    def _clear_locked(self) -> None:
        self._want_keys.clear()
        self._want_mouse.clear()
        self._axes.clear()
        self._axis_keys.clear()
        self._rem = [0.0, 0.0]

    def _axis_desired_keys(self) -> set[str]:
        keys: set[str] = set()
        for name, spec in self.axis_map.items():
            if spec["type"] != "keys":
                continue
            v = self._axes.get(name, 0.0)
            thr = spec.get("threshold", 0.35)
            cur = self._axis_keys.get(name, set())
            new: set[str] = set()
            for key, sign in ((spec["pos"], 1.0), (spec["neg"], -1.0)):
                x = v * sign
                # Schmitt trigger: on at thr, off at 0.6*thr, so it cannot chatter near the edge
                if x >= thr or (key in cur and x > thr * 0.6):
                    new.add(key)
            self._axis_keys[name] = new
            keys |= new
        return keys

    def _sync_locked(self, dx: int = 0, dy: int = 0) -> None:
        want_k = self._want_keys | self._axis_desired_keys()
        b = self.backend
        for k in self._held_keys - want_k:             # releases first
            b.key(k, False)
        for k in want_k - self._held_keys:
            b.key(k, True)
        for m in self._held_mouse - self._want_mouse:
            b.mouse_button(m, False)
        for m in self._want_mouse - self._held_mouse:
            b.mouse_button(m, True)
        if dx or dy:
            b.mouse_move(dx, dy)
            self.sent_moves += 1
        b.commit()
        self._held_keys = set(want_k)
        self._held_mouse = set(self._want_mouse)

    def _warn_once(self, key: str, msg: str, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)


# ------------------------------------------------------------------ windows helpers
class ForegroundWatcher:
    """Returns the foreground process name (cached ~4 Hz). '' on non-Windows."""

    def __init__(self, poll_hz: float = 4.0):
        self._period = 1.0 / poll_hz
        self._next = 0.0
        self._cached = ""
        self.pid = 0                       # pid of the foreground process (0 = unknown)

    def poll(self, now: float) -> str:
        if os.name != "nt" or now < self._next:
            return self._cached
        self._next = now + self._period
        try:
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            hwnd = u32.GetForegroundWindow()
            pid = ctypes.c_ulong(0)
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            self.pid = pid.value
            h = k32.OpenProcess(0x1000, False, pid.value)      # PROCESS_QUERY_LIMITED_INFORMATION
            name = f"hwnd:{hwnd}"
            if h:
                buf = ctypes.create_unicode_buffer(520)
                size = ctypes.c_ulong(520)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    name = os.path.basename(buf.value).lower()
                k32.CloseHandle(h)
            self._cached = name
        except Exception:
            log.exception("foreground poll failed")
        return self._cached


class KillSwitch:
    """Edge-detected global hotkey (default F12). Works even when a game has focus."""

    def __init__(self, vk: int = 0x7B):
        self.vk, self._was = vk, False

    def pressed_edge(self) -> bool:
        if os.name != "nt":
            return False
        down = bool(ctypes.windll.user32.GetAsyncKeyState(self.vk) & 0x8000)
        edge = down and not self._was
        self._was = down
        return edge
