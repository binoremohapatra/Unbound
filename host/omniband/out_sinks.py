"""out_sinks.py - gesture routing tables and output sinks for OmniBand V2.

A gesture is an event; actions are (kind, name, hold_ms) triples translated
into OutputCommand press/release pairs and fed to the existing Gateway
(gateway.py) which owns all Win32 SendInput safety (release-first diffing,
held-key tracking, atexit/console-ctrl release).

Sinks:
  - GatewaySink: wraps gateway.Gateway (SendInputBackend or RecordingBackend)
  - DryRunSink: logs every command, injects nothing
  - TTSSink: optional spoken cues via pyttsx3 (guarded import)
  - SirenSink: winsound beeps (stdlib, Windows-only, guarded)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from core.events import OutputCommand

log = logging.getLogger("omniband.sink")


# ------------------------------------------------------------------ actions
@dataclass(frozen=True)
class Action:
    """One output effect of a gesture. hold_ms=0 -> momentary tap."""
    kind: str          # "key" | "button" | "axis" | "release_all" | "tts" | "siren"
    name: str = ""
    value: float = 0.0
    hold_ms: int = 80
    text: str = ""     # for tts


# Gesture -> actions per mode. Modes without an entry inherit mode 0 defaults
# for SINGLE/DOUBLE/TRIPLE; safety gestures (ESTOP/SOS_*/PICKER_*/REJECT/
# NOISE_BURST/BRUX*) are handled globally by the engine, not per-mode.
MODE_ACTIONS: dict[int, dict[str, tuple[Action, ...]]] = {
    0: {  # ESPORTS_GAMING
        "SINGLE": (Action("button", "left", hold_ms=60),),
        "DOUBLE": (Action("button", "right", hold_ms=60),),
        "TRIPLE": (Action("key", "space", hold_ms=50),),
    },
    1: {  # SURGICAL_STERILE_OT_WORKSTATION - conservative: clicks + enter
        "SINGLE": (Action("button", "left", hold_ms=60),),
        "DOUBLE": (Action("button", "right", hold_ms=60),),
        "TRIPLE": (Action("key", "enter", hold_ms=50),),
    },
    2: {  # ASSISTIVE_ACCESSIBILITY - TRIPLE reserved for SOS (engine suppresses)
        "SINGLE": (Action("button", "left", hold_ms=80),),
        "DOUBLE": (Action("key", "tab", hold_ms=60),),
    },
    3: {  # INDUSTRIAL_FIREFIGHTER
        "SINGLE": (Action("button", "left", hold_ms=60),),
        "DOUBLE": (Action("button", "right", hold_ms=60),),
        "TRIPLE": (Action("key", "e", hold_ms=60),),
    },
    4: {  # ICU_SILENT_SOS - no local actions; SOS handled globally, silently
    },
    5: {  # DAILY_LIFE_RIDER_MEDIA
        "SINGLE": (Action("key", "playpause", hold_ms=50),),
        "DOUBLE": (Action("key", "nexttrack", hold_ms=50),),
        "TRIPLE": (Action("key", "volumemute", hold_ms=50),),
    },
}

# Gestures that are pure information / safety - never routed to MODE_ACTIONS.
INFO_GESTURES = {
    "REJECT", "NOISE_BURST", "PICKER_OPEN", "PICKER_CANCEL", "MODE_SET",
    "BRUX", "BRUX_END", "SOS_PENDING", "SOS_CONFIRMED", "SOS_CANCELLED",
    "ESTOP", "LONG",
}


@dataclass
class PendingTap:
    """A pressed output scheduled for release after hold_ms."""
    kind: str
    name: str
    release_at: float
    value: float = 0.0


class GestureRouter:
    """gesture name -> OutputCommands for the current mode."""

    def __init__(self, mode: int = 0) -> None:
        self.mode = mode
        self._pending: list[PendingTap] = []

    def set_mode(self, mode: int) -> None:
        self.mode = mode if mode in MODE_ACTIONS or mode in range(6) else 0

    def commands_for(self, ges: str, now: float) -> list[OutputCommand]:
        """Expand a gesture into press commands; releases are scheduled."""
        out: list[OutputCommand] = []
        table = MODE_ACTIONS.get(self.mode) or MODE_ACTIONS[0]
        # TRIPLE doubles as SOS trigger in sos modes - do not also fire it
        if ges == "TRIPLE" and self._mode_sos():
            return out
        for act in table.get(ges, ()):
            if act.kind in ("key", "button"):
                out.append(OutputCommand(kind=act.kind, name=act.name,
                                         pressed=True))
                self._pending.append(PendingTap(
                    act.kind, act.name, now + act.hold_ms / 1000.0))
            elif act.kind == "axis":
                out.append(OutputCommand(kind="axis", name=act.name,
                                         value=act.value, pressed=True))
                self._pending.append(PendingTap(
                    "axis", act.name, now + act.hold_ms / 1000.0,
                    act.value))
            elif act.kind == "release_all":
                out.append(OutputCommand(kind="release_all", name="",
                                         pressed=False))
        return out

    def due_releases(self, now: float) -> list[OutputCommand]:
        out: list[OutputCommand] = []
        keep: list[PendingTap] = []
        for p in self._pending:
            if now >= p.release_at:
                if p.kind == "axis":
                    out.append(OutputCommand(kind="axis", name=p.name,
                                             value=0.0, pressed=False))
                else:
                    out.append(OutputCommand(kind=p.kind, name=p.name,
                                             pressed=False))
            else:
                keep.append(p)
        self._pending = keep
        return out

    def cancel_pending(self) -> list[OutputCommand]:
        out = [OutputCommand(kind=p.kind, name=p.name, pressed=False)
               for p in self._pending]
        self._pending = []
        return out

    def _mode_sos(self) -> bool:
        from .constants_gen import mode_of
        return bool(mode_of(self.mode).get("sos"))


# ------------------------------------------------------------------ sinks
class GatewaySink:
    """Routes OutputCommands into the existing unbound Gateway."""

    def __init__(self, gateway) -> None:
        self.gw = gateway

    def apply(self, cmds: list[OutputCommand]) -> None:
        if cmds:
            self.gw.apply(cmds)

    def flush(self, now: float) -> None:
        self.gw.flush(now)

    def release_all(self, reason: str = "") -> None:
        self.gw.release_all(reason)


class DryRunSink:
    """Logs commands; injects nothing. Used by --dry-run and tests."""

    def __init__(self) -> None:
        self.commands: list[OutputCommand] = []

    def apply(self, cmds: list[OutputCommand]) -> None:
        self.commands.extend(cmds)
        for c in cmds:
            log.info("DRYRUN %s %s pressed=%s value=%s",
                     c.kind, c.name, c.pressed, c.value)

    def flush(self, now: float) -> None:
        return None

    def release_all(self, reason: str = "") -> None:
        self.commands.append(OutputCommand(kind="release_all", name="",
                                           pressed=False))
        log.warning("DRYRUN release_all (%s)", reason or "unspecified")


class TTSSink:
    """Optional spoken cues. pyttsx3 is imported lazily; absence = silent."""

    def __init__(self, enabled: bool = True, rate: int = 170) -> None:
        self.enabled = enabled
        self._engine = None
        self._last: dict[str, float] = {}
        self.dedupe_ms = 1500
        if enabled:
            try:
                import pyttsx3  # type: ignore
                self._engine = pyttsx3.init()
                self._engine.setProperty("rate", rate)
            except Exception as e:  # noqa: BLE001
                log.warning("tts unavailable: %s", e)
                self._engine = None
                self.enabled = False

    def say(self, text: str, key: str = "", now: float = 0.0) -> bool:
        if not self.enabled or self._engine is None:
            return False
        if key:
            last = self._last.get(key, -1e9)
            if now - last < self.dedupe_ms / 1000.0:
                return False
            self._last[key] = now
        try:
            self._engine.say(text)
            self._engine.runAndWait()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("tts failed: %s", e)
            return False


class SirenSink:
    """Audible alarm via winsound (Windows stdlib). Silent elsewhere."""

    def __init__(self, enabled: bool = True, freq_a: int = 800,
                 freq_b: int = 1200, ms: int = 500) -> None:
        self.enabled = enabled and os.name == "nt"
        self.freq_a, self.freq_b, self.ms = freq_a, freq_b, ms

    def ring(self, cycles: int = 6) -> None:
        if not self.enabled:
            return
        try:
            import winsound  # type: ignore
            for i in range(cycles):
                winsound.Beep(self.freq_a if i % 2 == 0 else self.freq_b, self.ms)
        except Exception as e:  # noqa: BLE001
            log.warning("siren failed: %s", e)
