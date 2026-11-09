"""tray.py (Binore): the system-tray icon. Optional: needs pystray and Pillow.

Thread rule: the tray runs on its own thread. It NEVER touches the gateway or the engine. A menu
click only (a) puts a SignalEvent on a queue that the main loop drains every tick, or (b) sets a
threading.Event the main loop checks. So "Freeze" from the tray travels the exact same path as the
F12 kill switch, and cannot race the 60 Hz loop.

The icon shows the state by COLOUR and by SHAPE, so it is readable without telling red from green:
    ACTIVE  green  play triangle      LATCHED        amber  solid dot
    SAFE_IDLE red  cross              CONVERSE_LOCK  blue   pause bars
A yellow "!" badge means our input is being dropped by UIPI (foreground app is elevated).

Verification: the pure parts and the Tray wrapper (against a fake pystray) are unit-tested. The
real pystray Win32 backend, icon updates from a non-tray thread, and menu clicks have only been
reviewed against its documentation, not run: see the Windows checklist.
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from core import clock
from core.events import SignalEvent

log = logging.getLogger("omnicontrol.tray")

STYLES = {
    "ACTIVE": ((46, 160, 67), "play"),
    "LATCHED": ((224, 150, 10), "dot"),
    "CONVERSE_LOCK": ((60, 100, 200), "pause"),
    "SAFE_IDLE": ((200, 45, 45), "cross"),
}
UNKNOWN_STYLE = ((110, 110, 110), "dot")
MAX_TOOLTIP = 127                      # Windows NOTIFYICONDATA.szTip holds 127 characters + NUL


# ------------------------------------------------------------------ pure: tooltip and menu
def tooltip(status: dict) -> str:
    parts = [f"OmniControl: {status.get('state', 'starting')}"]
    if status.get("mock_sources"):
        parts.append("MOCK INPUT: not real camera/mic")
    if status.get("latched"):
        parts.append("holding " + ", ".join(status["latched"]))
    if status.get("input_blocked"):
        parts.append("INPUT BLOCKED: restart as admin")
    return " | ".join(parts)[:MAX_TOOLTIP]


@dataclass(frozen=True)
class MenuEntry:
    action: str            # "-" = separator
    label: str = ""
    enabled: bool = True


def menu_spec(status: dict, is_admin: bool) -> list[MenuEntry]:
    state = status.get("state", "starting")
    entries = [MenuEntry("status", f"State: {state}", False),
               MenuEntry("toggle_freeze", "Resume" if status.get("locked") else "Freeze")]
    if not is_admin:
        label = ("Restart as administrator (needed for this app)" if status.get("input_blocked")
                 else "Restart as administrator")
        entries.append(MenuEntry("elevate", label))
    entries += [MenuEntry("-"),
                MenuEntry("open_config", "Open settings file"),
                MenuEntry("open_logs", "Open logs folder"),
                MenuEntry("doctor", "Run doctor (quick)"),
                MenuEntry("-"),
                MenuEntry("quit", "Quit OmniControl")]
    return entries


def doctor_command() -> list[str]:
    """Command line that runs the quick doctor in a separate process."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--doctor", "--quick"]
    return [sys.executable, str(Path(__file__).with_name("main.py")), "--doctor", "--quick"]


# ------------------------------------------------------------------ icon drawing (Pillow)
def draw_icon(state: str, size: int = 64, blocked: bool = False):
    """A PIL RGBA image: coloured disc + white glyph (+ "!" badge when input is blocked)."""
    from PIL import Image, ImageDraw
    colour, glyph = STYLES.get(state, UNKNOWN_STYLE)
    big = size * 4                                         # draw large, shrink = smooth edges
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def p(v: float) -> float:
        return v * big / 100.0

    d.ellipse((p(3), p(3), p(97), p(97)), fill=colour + (255,))
    white = (255, 255, 255, 255)
    if glyph == "play":
        d.polygon([(p(38), p(27)), (p(38), p(73)), (p(75), p(50))], fill=white)
    elif glyph == "dot":
        d.ellipse((p(33), p(33), p(67), p(67)), fill=white)
    elif glyph == "pause":
        d.rounded_rectangle((p(30), p(27), p(44), p(73)), radius=p(3), fill=white)
        d.rounded_rectangle((p(56), p(27), p(70), p(73)), radius=p(3), fill=white)
    elif glyph == "cross":
        d.line((p(31), p(31), p(69), p(69)), fill=white, width=int(p(11)))
        d.line((p(69), p(31), p(31), p(69)), fill=white, width=int(p(11)))
    if blocked:
        d.polygon([(p(76), p(46)), (p(99), p(92)), (p(53), p(92))],
                  fill=(255, 205, 0, 255), outline=(0, 0, 0, 255), width=max(1, int(p(2))))
        d.line((p(76), p(61), p(76), p(77)), fill=(0, 0, 0, 255), width=int(p(5)))
        d.ellipse((p(73.5), p(81), p(78.5), p(86)), fill=(0, 0, 0, 255))
    return img.resize((size, size), Image.Resampling.LANCZOS)


# ------------------------------------------------------------------ controller (thread-safe)
def _open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _spawn_doctor() -> None:
    flags = 0x00000010 if os.name == "nt" and not getattr(sys, "frozen", False) else 0  # new console
    subprocess.Popen(doctor_command(), creationflags=flags)


class TrayController:
    """Everything a menu click may do. Safe to call from the tray thread."""

    def __init__(self, config_file: Optional[Path] = None, logs: Optional[Path] = None,
                 open_path: Callable[[Path], None] = _open_path,
                 run_doctor: Callable[[], None] = _spawn_doctor,
                 now: Callable[[], float] = clock.now) -> None:
        if config_file is None:
            from config import config_path
            config_file = config_path()
        if logs is None:
            from config import log_dir
            logs = log_dir()
        self.config_file, self.logs = config_file, logs
        self._open, self._doctor, self._now = open_path, run_doctor, now
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.quit_requested = threading.Event()
        self.elevate_requested = threading.Event()
        self.status: dict = {}                 # latest engine.status(), replaced whole (atomic)

    def push_status(self, status: dict) -> None:
        self.status = status

    def drain(self) -> list[SignalEvent]:
        """Main loop calls this every tick."""
        out: list[SignalEvent] = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def handle(self, action: str) -> None:
        try:
            if action == "toggle_freeze":
                name = "voice_resume" if self.status.get("locked") else "kill"
                self.events.put(SignalEvent("switch", name, 1.0, self._now()))
            elif action == "quit":
                self.quit_requested.set()
            elif action == "elevate":
                self.elevate_requested.set()
            elif action == "open_config":
                self._open(self.config_file)
            elif action == "open_logs":
                self._open(self.logs)
            elif action == "doctor":
                self._doctor()
            else:
                log.warning("unknown tray action %r", action)
        except Exception:
            log.exception("tray action %r failed", action)


# ------------------------------------------------------------------ pystray wrapper
class Tray:
    """pystray is injected so the logic can be tested against a fake."""

    def __init__(self, controller: TrayController, is_admin: bool, pystray_module,
                 icon_factory: Callable[..., object] = draw_icon) -> None:
        self._ctl, self._is_admin, self._ps = controller, is_admin, pystray_module
        self._draw = icon_factory
        self._key: Optional[tuple] = None
        self._failed = False
        self._icon = pystray_module.Icon("OmniControl", icon_factory("ACTIVE"), "OmniControl",
                                         pystray_module.Menu(self._items))

    def _items(self) -> Iterator:
        for e in menu_spec(self._ctl.status, self._is_admin):
            if e.action == "-":
                yield self._ps.Menu.SEPARATOR
            else:
                yield self._ps.MenuItem(e.label, self._action(e.action), enabled=e.enabled)

    def _action(self, name: str) -> Callable:
        return lambda icon, item: self._ctl.handle(name)

    def start(self) -> None:
        self._icon.run_detached()

    def update(self, status: dict) -> None:
        """Main loop, a few times a second. Redraws only when something visible changed."""
        self._ctl.push_status(status)
        key = (status.get("state"), bool(status.get("input_blocked")), bool(status.get("locked")),
               tooltip(status))
        if key == self._key or self._failed:
            return
        self._key = key
        try:
            self._icon.icon = self._draw(status.get("state", ""), blocked=bool(status.get("input_blocked")))
            self._icon.title = tooltip(status)
            self._icon.update_menu()
        except Exception:
            self._failed = True                # one log line, then stay quiet; the engine matters more
            log.exception("tray update failed; tray frozen at its last state")

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception:
            log.exception("tray stop failed")


def create_tray(controller: TrayController, is_admin: bool) -> Optional[Tray]:
    """A Tray, or None when pystray/Pillow are missing or the tray cannot start. Never raises."""
    try:
        import PIL  # noqa: F401
        import pystray
    except ImportError:
        log.info("pystray/Pillow not installed: running without a tray icon")
        return None
    try:
        return Tray(controller, is_admin, pystray)
    except Exception:
        log.exception("tray could not be created; running without it")
        return None
