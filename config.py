"""config.py (Binore): where our files live, and the user-editable settings file.

Locations
  Windows   config  %APPDATA%\\OmniControl\\config.json
            logs    %LOCALAPPDATA%\\OmniControl\\logs     (logs are not roaming data)
  Elsewhere ~/.config/omnicontrol
  Override  OMNICONTROL_HOME=<dir>  puts everything under <dir> (portable mode, tests)

Rules that protect the user
  * A broken, half-saved or hostile config NEVER crashes the app or changes behaviour: the last good
    config stays active and the problem is logged once.
  * Numbers are clamped into safe ranges. The hold ceiling (HARD_MAX_HOLD_MS) lives in code, so a typo
    cannot create a 10 minute latch. The face-regain dwell has a floor so it cannot be switched off.
  * Key names are checked against the real scancode table, so a typo is reported at load time, not
    silently ignored at 60 Hz.

Hot reload: ConfigWatcher.poll(now) from the main loop (stat() twice a second). Applying a change is
Engine.apply_config()'s job; this module only parses and validates.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
from copy import deepcopy
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping, Optional

from gateway import AXIS_MAP, MOUSE_FLAGS, SCANCODES

log = logging.getLogger("omnicontrol.config")

APP_DIR_NAME = "OmniControl"
HARD_MAX_HOLD_MS = 10_000
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")   # no slashes: mapping builds a path


# ------------------------------------------------------------------ locations
def config_dir() -> Path:
    home = os.environ.get("OMNICONTROL_HOME")
    if home:
        return Path(home)
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_DIR_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_DIR_NAME.lower()


def log_dir() -> Path:
    home = os.environ.get("OMNICONTROL_HOME")
    if home:
        return Path(home) / "logs"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / APP_DIR_NAME / "logs"
    return config_dir() / "logs"


def config_path() -> Path:
    return config_dir() / "config.json"


def resource_dir() -> Path:
    """Folder holding bundled read-only files (profiles/*.json): the PyInstaller bundle when
    frozen, otherwise the source folder."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


# ------------------------------------------------------------------ the settings
@dataclass(frozen=True)
class AppConfig:
    profile: str = "default"
    hz: float = 60.0                  # restart required
    log_level: str = "INFO"
    default_hold_ms: int = 1800
    max_hold_ms: int = 10_000
    face_regain_ms: int = 300
    button_map: Mapping[str, tuple] = field(default_factory=dict)   # name -> (kind, target)
    axis_tuning: Mapping[str, Mapping] = field(default_factory=dict)  # axis -> {speed|invert|threshold}


RESTART_REQUIRED = ("hz",)
_FIELD_NAMES = {f.name for f in fields(AppConfig)}


class ConfigError(ValueError):
    """The file as a whole is unusable (not JSON, not an object)."""


def _num(raw: Any, lo: float, hi: float, name: str, warns: list[str], integer: bool) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("must be a number")
    v = float(raw)
    if not math.isfinite(v):
        raise ValueError("must be a finite number")
    clamped = min(hi, max(lo, v))
    if clamped != v:
        warns.append(f"{name}={raw} out of range, using {int(clamped) if integer else clamped}")
    return int(clamped) if integer else clamped


def _button_map(raw: Any, warns: list[str]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("must be an object like {\"A\": [\"key\", \"space\"]}")
    out: dict[str, tuple] = {}
    for name, tgt in raw.items():
        ok = (isinstance(name, str) and 0 < len(name) <= 32
              and isinstance(tgt, (list, tuple)) and len(tgt) == 2
              and tgt[0] in ("key", "mouse") and isinstance(tgt[1], str)
              and (tgt[1] in SCANCODES if tgt[0] == "key" else tgt[1] in MOUSE_FLAGS))
        if ok:
            out[name] = (tgt[0], tgt[1])
        else:
            warns.append(f"button_map[{name!r}]={tgt!r} ignored (unknown key or mouse button)")
    return out


def _axis_tuning(raw: Any, warns: list[str]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("must be an object like {\"right_x\": {\"speed\": 1200}}")
    out: dict[str, dict] = {}
    for axis, spec in raw.items():
        if axis not in AXIS_MAP or not isinstance(spec, dict):
            warns.append(f"axis_tuning[{axis!r}] ignored (unknown axis or not an object)")
            continue
        kind, clean = AXIS_MAP[axis]["type"], {}
        for k, v in spec.items():
            try:
                if kind == "mouse" and k == "speed":
                    clean[k] = _num(v, 10.0, 5000.0, f"{axis}.speed", warns, False)
                elif kind == "mouse" and k == "invert":
                    if not isinstance(v, bool):
                        raise ValueError("must be true or false")
                    clean[k] = v
                elif kind == "keys" and k == "threshold":
                    clean[k] = _num(v, 0.1, 0.9, f"{axis}.threshold", warns, False)
                else:
                    warns.append(f"axis_tuning[{axis!r}][{k!r}] ignored (not valid for a {kind} axis)")
            except ValueError as e:
                warns.append(f"axis_tuning[{axis!r}][{k!r}]: {e}; ignored")
        if clean:
            out[axis] = clean
    return out


def parse_config(data: Any, base: Optional[AppConfig] = None) -> tuple[AppConfig, list[str]]:
    """Validate a decoded JSON object. Bad fields fall back to `base` (previous good value) and
    produce a warning; only a non-object top level raises ConfigError."""
    base = base or AppConfig()
    if not isinstance(data, dict):
        raise ConfigError("top level must be a JSON object { ... }")
    warns: list[str] = []
    vals: dict[str, Any] = {}
    for key, raw in data.items():
        if key.startswith("_"):
            continue                                   # "_comment" style notes
        if key not in _FIELD_NAMES:
            warns.append(f"unknown setting {key!r} ignored")
            continue
        try:
            if key == "profile":
                if not isinstance(raw, str) or not _PROFILE_RE.match(raw):
                    raise ValueError("letters, digits, _ . - only, no slashes")
                vals[key] = raw
            elif key == "log_level":
                if not isinstance(raw, str) or raw.upper() not in LOG_LEVELS:
                    raise ValueError(f"one of {', '.join(LOG_LEVELS)}")
                vals[key] = raw.upper()
            elif key == "hz":
                vals[key] = _num(raw, 30.0, 240.0, key, warns, False)
            elif key == "default_hold_ms":
                vals[key] = _num(raw, 100, HARD_MAX_HOLD_MS, key, warns, True)
            elif key == "max_hold_ms":
                vals[key] = _num(raw, 500, HARD_MAX_HOLD_MS, key, warns, True)
            elif key == "face_regain_ms":
                vals[key] = _num(raw, 100, 2000, key, warns, True)
            elif key == "button_map":
                vals[key] = _button_map(raw, warns)
            elif key == "axis_tuning":
                vals[key] = _axis_tuning(raw, warns)
        except ValueError as e:
            warns.append(f"{key}: {e}; keeping {getattr(base, key)!r}")
    cfg = AppConfig(**{**{f.name: getattr(base, f.name) for f in fields(AppConfig)}, **vals})
    if cfg.default_hold_ms > cfg.max_hold_ms:
        warns.append("default_hold_ms is above max_hold_ms, lowering it")
        cfg = AppConfig(**{**cfg.__dict__, "default_hold_ms": cfg.max_hold_ms})
    return cfg, warns


def effective_axis_map(cfg: AppConfig) -> dict:
    """AXIS_MAP with the user's tuning merged in (full per-axis specs, ready for Gateway)."""
    out = deepcopy(AXIS_MAP)
    for axis, tune in cfg.axis_tuning.items():
        out[axis].update(tune)
    return out


def default_config_text() -> str:
    return json.dumps({
        "_comment": "OmniControl settings. Save the file and most changes apply within a second. "
                    "'hz' needs a restart. Out-of-range numbers are clamped; mistakes are logged.",
        "profile": "default",
        "hz": 60,
        "log_level": "INFO",
        "default_hold_ms": 1800,
        "max_hold_ms": 10000,
        "face_regain_ms": 300,
        "_button_map_example": {"A": ["key", "space"]},
        "button_map": {},
        "_axis_tuning_example": {"right_x": {"speed": 900}, "left_x": {"threshold": 0.35}},
        "axis_tuning": {},
    }, indent=2) + "\n"


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file + rename so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ loading + hot reload
class ConfigWatcher:
    def __init__(self, path: Optional[Path] = None, poll_s: float = 0.5) -> None:
        self.path = path or config_path()
        self.poll_s = poll_s
        self.current = AppConfig()
        self.warnings: list[str] = []
        self._stamp: Optional[tuple] = None
        self._next = 0.0

    def _stat(self) -> Optional[tuple]:
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _read(self) -> Optional[AppConfig]:
        """Parse the file against the current config. None = keep what we have (problem logged)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))   # tolerate a Notepad BOM
            cfg, warns = parse_config(data, self.current)
        except (OSError, json.JSONDecodeError, ConfigError) as e:
            log.error("config %s unusable (%s); keeping the last good settings", self.path, e)
            return None
        self.warnings = warns
        for w in warns:
            log.warning("config: %s", w)
        return cfg

    def load(self) -> AppConfig:
        """Initial load. Creates a default file on first run so the user has something to edit."""
        if not self.path.exists():
            try:
                write_atomic(self.path, default_config_text())
                log.info("created default config at %s", self.path)
            except OSError as e:
                log.warning("could not create %s (%s); using built-in defaults", self.path, e)
        self._stamp = self._stat()
        cfg = self._read() if self._stamp else None
        if cfg is not None:
            self.current = cfg
        return self.current

    def poll(self, now: float) -> Optional[AppConfig]:
        """The new config if the file changed AND parsed AND differs from the active one."""
        if now < self._next:
            return None
        self._next = now + self.poll_s
        stamp = self._stat()
        if stamp is None or stamp == self._stamp:      # deleted: keep going with what we have
            return None
        self._stamp = stamp                            # also on failure: no log spam, retry on next save
        cfg = self._read()
        if cfg is None or cfg == self.current:
            return None
        self.current = cfg
        return cfg
