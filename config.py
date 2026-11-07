"""config.py (Binore): JSON config in %APPDATA%\\OmniControl with validation and hot-reload.

On first run the file is created with safe defaults. Each field is validated independently;
bad values are reset to their default with a warning, so a corrupt config never crashes the app.
Hot-reload polls the mtime every 2 s and calls on_change(new_cfg) without a restart.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("omnicontrol.config")

APP_NAME = "OmniControl"


def appdata_dir() -> Path:
    """Return %APPDATA%\\OmniControl on Windows, ~/.config/OmniControl elsewhere."""
    if os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    d = appdata_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Config:
    # Loop timing
    hz: float = 60.0
    profile: str = "default"

    # Elevation / permissions
    elevate: bool = True          # auto-UAC on startup in the packaged .exe

    # Panic hotkeys
    panic_vk: int = 0x7B         # F12 virtual-key code
    panic_chord: bool = True      # Ctrl+Alt+Shift+Q as hardware-free backup

    # Safety
    max_key_transitions_per_second: int = 200
    deny_list: list = field(default_factory=lambda: [
        "consent.exe", "logonui.exe", "credentialuiprompt.exe",
        "1password.exe", "keepass.exe", "bitwarden.exe",
    ])

    # Logging
    log_level: str = "INFO"

    # Modes
    safe_mode: bool = False       # dry-run: no real input injected
    tray_enabled: bool = True

    # Autostart (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run, OFF by default)
    autostart: bool = False

    # Hardware
    webcam_index: int = 0
    audio_sample_rate: int = 16000


# Per-field validators: return True if the value is acceptable.
_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "hz":                              lambda v: 10.0 <= float(v) <= 240.0,
    "max_key_transitions_per_second":  lambda v: 1 <= int(v) <= 2000,
    "log_level":                       lambda v: v in ("DEBUG", "INFO", "WARNING", "ERROR"),
    "webcam_index":                    lambda v: 0 <= int(v) <= 16,
    "audio_sample_rate":               lambda v: int(v) in (8000, 16000, 22050, 44100, 48000),
    "panic_vk":                        lambda v: 0 < int(v) <= 0xFF,
}


def _default_path() -> Path:
    return appdata_dir() / "config.json"


def load_config(path: Optional[Path] = None) -> "Config":
    """Load and validate config.  Creates the file with defaults if missing."""
    path = path or _default_path()
    defaults = Config()

    if not path.exists():
        _write(defaults, path)
        log.info("Created default config at %s", path)
        return defaults

    try:
        raw: dict = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Config parse error (%s); using all defaults.", exc)
        return defaults

    defaults_dict = asdict(defaults)
    kwargs: dict[str, Any] = {}
    for key, default_val in defaults_dict.items():
        raw_val = raw.get(key, default_val)
        validator = _VALIDATORS.get(key)
        if validator is not None:
            try:
                ok = validator(raw_val)
            except Exception:
                ok = False
            if not ok:
                log.warning(
                    "Config field %r has invalid value %r; using default %r.",
                    key, raw_val, default_val,
                )
                raw_val = default_val
        kwargs[key] = raw_val

    return Config(**kwargs)


def save_config(cfg: "Config", path: Optional[Path] = None) -> None:
    _write(cfg, path or _default_path())


def _write(cfg: "Config", path: Path) -> None:
    path.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")


class ConfigWatcher:
    """Background thread that polls the config file for mtime changes.

    Calls ``on_change(new_config)`` whenever the file is updated.
    Exceptions in on_change are caught and logged — they never stop the watcher.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        poll_s: float = 2.0,
        on_change: Optional[Callable[["Config"], None]] = None,
    ) -> None:
        self._path = path or _default_path()
        self._poll = poll_s
        self._on_change = on_change
        self._mtime: float = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="config-watcher", daemon=True
        )

    def start(self) -> None:
        try:
            self._mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            pass
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                mtime = self._path.stat().st_mtime
                if mtime != self._mtime:
                    self._mtime = mtime
                    new_cfg = load_config(self._path)
                    log.info("Config hot-reloaded from %s", self._path)
                    if self._on_change:
                        try:
                            self._on_change(new_cfg)
                        except Exception:
                            log.exception("config on_change callback failed")
            except FileNotFoundError:
                pass
            except Exception:
                log.exception("config watcher error")

