"""tray.py (Binore): Windows system tray icon using pystray and Pillow.

Falls back gracefully to a console-only stub if pystray or Pillow is missing.

THREAD SAFETY RULE (never violated):
  Tray callbacks run on the tray message thread. They must ONLY:
    - Call gateway.release_all()
    - Set boolean flags / queues for the main loop
  They must NEVER touch the state machine directly.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Callable, Optional

log = logging.getLogger("omnicontrol.tray")

# Colors for state icon
_COLORS = {
    "ACTIVE":        (40, 180, 80),    # green
    "LATCHED":       (230, 140, 20),   # orange
    "CONVERSE_LOCK": (210, 40, 40),    # red
    "SAFE_IDLE":     (160, 40, 40),    # dark red
    "PAUSED":        (120, 120, 120),  # grey
}


def _create_color_icon(color_rgb: tuple[int, int, int], size: int = 64):
    """Draw a simple smooth colored circle with Pillow."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=color_rgb,
        outline=(255, 255, 255, 200),
        width=2,
    )
    return img


class TrayIcon:
    """System tray icon.

    If pystray or Pillow is not installed, all methods become safe no-ops
    and a warning is logged once.
    """

    def __init__(
        self,
        on_panic: Callable[[], None],
        on_toggle_pause: Callable[[], None],
        on_run_doctor: Callable[[], None],
        on_open_logs: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._on_panic = on_panic
        self._on_toggle_pause = on_toggle_pause
        self._on_run_doctor = on_run_doctor
        self._on_open_logs = on_open_logs
        self._on_quit = on_quit

        self._icon = None
        self._thread: Optional[threading.Thread] = None
        self._current_state = "ACTIVE"
        self._paused = False
        self._available = False

        self._init_pystray()

    def _init_pystray(self) -> None:
        try:
            import pystray
            from PIL import Image  # noqa: F401

            self._pystray = pystray
            self._available = True
        except ImportError:
            log.warning("pystray or Pillow not installed — tray icon disabled. "
                        "Install with: pip install pystray pillow")
            self._available = False

    def start(self) -> None:
        if not self._available:
            return

        def _make_menu():
            p = self._pystray
            return p.Menu(
                p.MenuItem("Panic Release (All Keys)", lambda: self._on_panic()),
                p.MenuItem(
                    lambda item: "Resume" if self._paused else "Pause (Lock)",
                    lambda: self._on_toggle_pause(),
                ),
                p.Menu.SEPARATOR,
                p.MenuItem("Run Doctor (Diagnostics)", lambda: self._on_run_doctor()),
                p.MenuItem("Open Logs Folder", lambda: self._on_open_logs()),
                p.Menu.SEPARATOR,
                p.MenuItem("Quit OmniControl", lambda: self._quit()),
            )

        color = _COLORS.get(self._current_state, (40, 180, 80))
        img = _create_color_icon(color)
        self._icon = self._pystray.Icon(
            "OmniControl",
            img,
            "OmniControl — Hands-Free Controller",
            menu=_make_menu(),
        )

        self._thread = threading.Thread(
            target=self._icon.run, name="tray-icon", daemon=True
        )
        self._thread.start()
        log.info("System tray icon started.")

    def set_state(self, state_name: str) -> None:
        """Update icon color to reflect current state. Safe to call from any thread."""
        self._current_state = state_name
        if not self._available or not self._icon:
            return
        color = _COLORS.get(state_name, (120, 120, 120))
        try:
            self._icon.icon = _create_color_icon(color)
            self._icon.title = f"OmniControl — {state_name}"
        except Exception:
            pass

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self.set_state("CONVERSE_LOCK")

    def _quit(self) -> None:
        self._on_panic()
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
        self._on_quit()

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass


def open_logs_folder() -> None:
    """Open the logs directory in Windows Explorer / default file manager."""
    from config import logs_dir
    d = str(logs_dir())
    if os.name == "nt":
        os.startfile(d)
    else:
        subprocess.Popen(["xdg-open", d])

