"""main.py (Binore & Shaury): wires everything together and runs the 60 Hz loop.

Per tick:
  sources.poll()  ->  state_machine.on_event (safety)  ->  intent producers  ->  state_machine.gate
  -> state_machine.tick (expiry)  ->  mapper (Dishti)  ->  gateway.apply + flush

Pluggable pieces (so teammates can drop their work in without touching this file):
  * Live sources: adapters/webcam.py (Antas), adapters/audio.py, adapters/switch.py (Dishti).
    Each module exposes  create_source() -> object with poll() -> list[SignalEvent] and close().
  * Intent producers (Antas fastpath/slowpath/assist): objects with
    process(events: list[SignalEvent], now: float) -> list[Intent].
  * Mapper (Dishti): handle(intent, now) -> list[OutputCommand]; optional tick(now) -> list.
  * Dashboard & Calibration (Shaury): real-time HUD telemetry server and neutral calibration.
If a piece is missing, a small fallback is used and a warning is logged, so this runs today.

Usage:
  python main.py --dry-run                       live sources, no real input injected
  python main.py --replay session.jsonl --dry-run --speed 4
  python main.py --record session.jsonl          live + save every event
  python main.py --doctor [--json] [--quick]     check camera, mic, privacy, SendInput, jitter, deps
  python main.py --elevate                       restart as administrator (UAC) for elevated games
  python main.py --safe                          built-in defaults only: no config file, no profile
  python main.py --no-tray                       do not show the system-tray icon
  python main.py --dashboard                     launch Shaury's real-time telemetry HUD server
  python main.py --handsfree                     run standalone handsfree pipeline

Settings live in %APPDATA%\\OmniControl\\config.json and hot-reload (see config.py).
"""
from __future__ import annotations

import argparse
import collections
import importlib
import logging
import os
import queue
import sys
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent))

import elevation
import logsetup
from adapters.replay import Recorder, ReplaySource
from config import AppConfig, ConfigWatcher, config_path, effective_axis_map, log_dir
from core import clock
from core.events import Intent, OutputCommand, SignalEvent
from gateway import (
    SCANCODES,
    ForegroundWatcher,
    Gateway,
    KillSwitch,
    RecordingBackend,
    SendInputBackend,
)
from single_instance import APP_INSTANCE_NAME, SingleInstance
from state_machine import SMConfig, StateMachine
from tray import TrayController, create_tray
from version import __version__
from watchdog import FrameClock, LatencyLog, Watchdog

log = logging.getLogger("omnicontrol.main")

# --safe: shorter holds, longer face-regain dwell. Nothing from disk can change these.
SAFE_CONFIG = AppConfig(default_hold_ms=1200, max_hold_ms=3000, face_regain_ms=500)
SINGLE_INSTANCE_NAME = APP_INSTANCE_NAME     # tests override this


# ------------------------------------------------------------------ fallbacks
class BasicMapper:
    """Minimal stand-in until Dishti's mapping.py is in. Same handle()/tick() shape."""
    BUTTONS = {"fire": "RT", "aim": "LT", "interact": "RB", "reload": "X", "jump": "A",
               "sprint": "L3", "cruise": "key:w"}

    def __init__(self) -> None:
        self._held: set[tuple[str, str]] = set()

    def _target(self, name: str) -> tuple[str, str]:
        t = self.BUTTONS.get(name, name)
        return ("key", t[4:]) if t.startswith("key:") else ("button", t)

    def handle(self, it: Intent, now: float) -> list[OutputCommand]:
        if it.kind == "stick":
            lx = it.name == "move"
            return [OutputCommand("axis", "left_x" if lx else "right_x", value=it.x),
                    OutputCommand("axis", "left_y" if lx else "right_y", value=it.y)]
        if it.kind in ("hold", "action"):
            k, n = self._target(it.name)
            if it.kind == "action" and (k, n) in self._held:     # toggle
                self._held.discard((k, n))
                return [OutputCommand(k, n, pressed=False)]
            self._held.add((k, n))
            return [OutputCommand(k, n, pressed=True)]
        if it.kind == "release":
            k, n = self._target(it.name)
            self._held.discard((k, n))
            return [OutputCommand(k, n, pressed=False)]
        if it.kind == "cancel_all":
            out = [OutputCommand(k, n, pressed=False) for k, n in sorted(self._held)]
            self._held.clear()
            return out
        return []


class DefaultProducer:
    """Stand-in for Antas's pipeline: head angles -> stick, jaw_open -> timed hold."""

    def __init__(self, range_deg: float = 20.0, jaw_on: float = 0.55, jaw_off: float = 0.32,
                 hold_ms: int = 1800):
        self.range, self.on, self.off, self.hold_ms = range_deg, jaw_on, jaw_off, hold_ms
        self._yaw = self._pitch = 0.0
        self._jaw = False
        self._brow = False

    def process(self, events: list[SignalEvent], now: float) -> list[Intent]:
        out: list[Intent] = []
        moved = False
        for e in events:
            if e.name == "head_yaw":
                self._yaw, moved = e.value, True
            elif e.name == "head_pitch":
                self._pitch, moved = e.value, True
            elif e.name == "jaw_open":
                if not self._jaw and e.value >= self.on:
                    self._jaw = True
                    out.append(Intent("hold", "fire", ms=self.hold_ms))
                elif self._jaw and e.value <= self.off:
                    self._jaw = False
            elif e.name == "brow_raise":
                on = e.value >= 0.58
                if on and not self._brow:                      # rising edge only: no key chatter
                    out.append(Intent("action", "interact"))
                self._brow = on
        if moved:
            c = lambda v: max(-1.0, min(1.0, v / self.range))
            out.append(Intent("stick", "look", x=c(self._yaw), y=c(self._pitch)))
        return out


BasicAssist = DefaultProducer


# ------------------------------------------------------------------ handsfree bridge
# The handsfree/ package (teammates' modules) uses a callback API (start/on_event/stop) and its
# own SignalEvent/Intent/OutputCommand types. These shims adapt it to the poll()/process()/
# handle() contracts above WITHOUT editing their files. Every timestamp is re-stamped with
# core.clock.now() on receipt: their time.time() values are never trusted.
#
# Signal translation (handsfree -> root SignalEvent), src preserved:
#   HEAD_POSE  "pose" {yaw,pitch,roll}  -> "face"=1.0 + "head_yaw"/"head_pitch"/"head_roll"
#   BLENDSHAPE "blendshapes" {k: v}     -> one event per key, name kept verbatim ("jawOpen", ...)
#   AUDIO_POP  "pop"                    -> "pop"
#   AUDIO_SPEECH "speech_command"       -> "speech_command" value=1.0 (the text cannot cross the
#                                        float `value` contract; voice_* intents are NOT faked)
#   SWITCH     key name, press/release  -> "kill" on f12 press, other keys pass through lowercased
#   speech activity flag                -> "speech_active" 1.0/0.0 (consumed by the producer only)
def _hf_events(hf_ev, now: float, kind: str) -> list[SignalEvent]:
    """One handsfree SignalEvent -> zero or more root SignalEvents, stamped with `now`.
    `kind` is the adapter that produced it (webcam/audio/switch) -> root src."""
    from handsfree.core.events import SignalType
    src = {"webcam": "webcam", "audio": "mic", "switch": "switch"}.get(kind, "webcam")
    st = hf_ev.signal_type
    name, value = hf_ev.name, hf_ev.value

    def ev(n, v):
        return SignalEvent(src, n, float(v), now)

    if st == SignalType.HEAD_POSE and isinstance(value, dict):
        out = [ev("face", 1.0)]                     # a pose means a face was found
        for axis in ("yaw", "pitch", "roll"):
            if axis in value:
                out.append(ev(f"head_{axis}", value[axis]))
        return out
    if st == SignalType.BLENDSHAPE and isinstance(value, dict):
        return [ev(k, v) for k, v in value.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if st == SignalType.AUDIO_POP:
        v = value if isinstance(value, (int, float)) and not isinstance(value, bool) else 1.0
        return [ev("pop", v)]
    if st == SignalType.AUDIO_SPEECH:
        return [ev("speech_command", 1.0)]
    if st == SignalType.SWITCH:
        n = str(name).lower()
        if n == "f12" and float(value or 0.0) >= 0.5:
            return [ev("kill", 1.0)]
        if n == "f12":
            return []                               # key release of the kill switch: nothing
        return [ev(n, value if value is not None else 1.0)]
    return []


def _hf_mock_status(kind: str) -> tuple[bool, str]:
    """(is_mock, reason) for a handsfree adapter WITHOUT starting it. Mirrors the exact
    fallback conditions inside the teammates' adapters (their files are not edited)."""
    try:
        if kind == "webcam":
            import handsfree.adapters.webcam as w
            if not (w.HAS_CV2 and w.HAS_MEDIAPIPE):
                return True, "missing opencv-python or mediapipe"
            try:
                import cv2
                cap = cv2.VideoCapture(0, cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY)
                ok = cap.isOpened()
                cap.release()
            except Exception:
                ok = False
            if not ok:
                return True, "no camera opened at index 0"
            return False, ""
        if kind == "audio":
            import handsfree.adapters.audio as au
            if not au.HAS_PYAUDIO:
                return True, "missing pyaudio"
            return False, ""
        if kind == "switch":
            import handsfree.adapters.switch as sw
            if not sw.HAS_KEYBOARD:
                return True, "missing keyboard library"
            return False, ""
    except Exception as e:
        return True, f"adapter probe failed: {type(e).__name__}"
    return False, ""


class _HFSource:
    """poll()/close() wrapper around a handsfree callback adapter."""

    _BUILDERS = {
        "webcam": lambda push, speech: __import__("handsfree.adapters.webcam", fromlist=["x"])
                  .WebcamAdapter(on_event=push),
        "audio": lambda push, speech: __import__("handsfree.adapters.audio", fromlist=["x"])
                 .AudioAdapter(on_event=push, on_speech_activity=speech),
        "switch": lambda push, speech: __import__("handsfree.adapters.switch", fromlist=["x"])
                  .SwitchAdapter(on_event=push),
    }

    def __init__(self, kind: str):
        self.name = kind
        self.is_mock, self.mock_reason = _hf_mock_status(kind)
        self._q: queue.SimpleQueue = queue.SimpleQueue()

        def push(hf_ev):
            now = clock.now()
            for e in _hf_events(hf_ev, now, kind):
                self._q.put(e)

        def speech(active: bool):
            self._q.put(SignalEvent("mic", "speech_active", 1.0 if active else 0.0, clock.now()))

        self._adapter = self._BUILDERS[kind](push, speech)
        self._adapter.start()

    def poll(self, now: float) -> list[SignalEvent]:
        out: list[SignalEvent] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        try:
            self._adapter.stop()
        except Exception:
            pass


class _HandsfreeProducer:
    """process(events, now) wrapper around handsfree FastPath + SlowPath.

    Feeds the latest pose / blendshape / speech-activity values through the teammates' real
    pipeline and translates their Intents back into root Intents: sticks stay sticks, every
    active gesture becomes a refreshed `hold` (so it still expires if the producer dies), and
    transitions emit `release`."""

    def __init__(self):
        from handsfree.core.calibration import CalibrationManager
        from handsfree.core.fastpath import FastPath
        from handsfree.core.slowpath import SlowPath
        self.calibration = CalibrationManager()
        self.fastpath = FastPath(self.calibration)
        self.slowpath = SlowPath(self.calibration)
        self._yaw = self._pitch = self._roll = 0.0
        self._pose = False
        self._blend: dict = {}
        self._gestures: set = set()          # action names currently held (slowpath + switch)
        self._switch_down: set = set()

    def process(self, events: list[SignalEvent], now: float) -> list[Intent]:
        out: list[Intent] = []
        pose_now = False
        for e in events:
            if e.name == "head_yaw":
                self._yaw, pose_now = e.value, True
            elif e.name == "head_pitch":
                self._pitch, pose_now = e.value, True
            elif e.name == "head_roll":
                self._roll, pose_now = e.value, True
            elif e.name == "speech_active":
                self.slowpath.set_speech_active(e.value >= 0.5)
            elif e.name == "speech_command":
                pass                            # text cannot cross the float contract (see bridge)
            elif e.src == "switch":
                if e.value >= 0.5:
                    if e.name not in self._switch_down:
                        self._switch_down.add(e.name)
                        self._gestures.add(e.name)
                else:
                    if e.name in self._switch_down:
                        self._switch_down.discard(e.name)
                        self._gestures.discard(e.name)
                        out.append(Intent("release", e.name))
            else:
                self._blend[e.name] = e.value
        if pose_now:
            self._pose = True
            ix, iy = self.fastpath.process_pose(self._yaw, self._pitch, self._roll, now)
            out.append(Intent("stick", "head", x=ix.value, y=iy.value))
        if self._blend:
            try:
                for it in self.slowpath.process_blendshapes(dict(self._blend), now):
                    if it.value >= 0.5:
                        self._gestures.add(it.action)
                    else:
                        if it.action in self._gestures:
                            out.append(Intent("release", it.action))
                        self._gestures.discard(it.action)
            except Exception:
                log.exception("handsfree slowpath failed; skipping blendshapes this tick")
        for name in sorted(self._gestures):
            out.append(Intent("hold", name, ms=0))     # refreshed every tick; expires if we die
        return out

    def note_cancel(self, now: float) -> None:
        """Engine calls this when a safety cancel/release just went out. Without it the
        per-tick hold refresh would re-latch the same gesture on the very next tick and a
        pop/blink/face-loss would never stick. The user must re-trigger the gesture."""
        self._gestures.clear()
        self._switch_down.clear()


_HF_TARGET_ALIAS = {"LEFT_SHOULDER": "LB", "RIGHT_SHOULDER": "RB",
                    "LEFT_TRIGGER": "LT", "RIGHT_TRIGGER": "RT"}
_HF_AXIS = {"X": "left_x", "Y": "left_y", "LX": "left_x", "LY": "left_y",
            "RX": "right_x", "RY": "right_y"}


class _HandsfreeMapper:
    """handle(intent, now) wrapper around handsfree ProfileMapper.

    Translates root Intents -> handsfree Intents -> handsfree OutputCommands -> root
    OutputCommands for OUR gateway (SendInput keyboard/mouse). Gamepad button names go through
    the gateway's BUTTON_MAP; gamepad axes degrade to the digital/mouse axes that exist."""

    def __init__(self, profile_name: str):
        from handsfree.core.events import Intent as HFIntent
        from handsfree.core.mapping import ProfileMapper
        self._HFIntent = HFIntent
        from config import resource_dir
        safe = "".join(c for c in profile_name if c.isalnum() or c in "_.-") or "default"
        path = resource_dir() / "handsfree" / "profiles" / f"{safe}.json"
        self._mapper = ProfileMapper.from_file(path)
        self._layer = "base"
        self._pressed: set = set()               # (kind, name) the backend currently holds
        self._warned: set = set()

    def handle(self, it: Intent, now: float) -> list[OutputCommand]:
        k = it.kind
        if k in ("cancel_all", "pause"):
            return self._release_everything()
        if k == "resume":
            return []
        if k == "layer":
            self._layer = it.name or "base"
            return self._release_everything()
        if k == "release":
            return self._map(self._HFIntent(action=it.name, value=0.0, layer=self._layer))
        if k == "stick":
            out = self._map(self._HFIntent(action="stick_x", value=it.x, layer=self._layer))
            out += self._map(self._HFIntent(action="stick_y", value=it.y, layer=self._layer))
            return out
        return self._map(self._HFIntent(action=it.name, value=1.0, layer=self._layer))

    def _map(self, hf_intent) -> list[OutputCommand]:
        out: list[OutputCommand] = []
        for oc in self._mapper.map_intent(hf_intent):
            rc = self._translate(oc)
            if rc is None:
                continue
            out.append(rc)
            if rc.kind in ("key", "button") and rc.pressed is not None:
                if rc.pressed:
                    self._pressed.add((rc.kind, rc.name))
                else:
                    self._pressed.discard((rc.kind, rc.name))
        return out

    def _translate(self, oc) -> Optional[OutputCommand]:
        target = str(oc.target).upper()
        val = float(oc.value)
        if oc.command_type == "axis":
            ax = _HF_AXIS.get(target)
            if ax:
                return OutputCommand("axis", ax, value=max(-1.0, min(1.0, val)))
            if target in ("LT", "RT"):
                return OutputCommand("button", target, pressed=val > 0.5)
            self._warn_once(f"axis {target}", "unmapped handsfree axis %r", target)
            return None
        if oc.command_type in ("button", "key"):
            if oc.device == "keyboard":
                name = str(oc.target).lower()
                if name in SCANCODES:
                    return OutputCommand("key", name, pressed=val > 0.5)
                self._warn_once(f"key {name}", "unmapped handsfree key %r", name)
                return None
            return OutputCommand("button", _HF_TARGET_ALIAS.get(target, target),
                                 pressed=val > 0.5)
        self._warn_once(f"type {oc.command_type}", "unmapped handsfree command %r", oc)
        return None

    def _release_everything(self) -> list[OutputCommand]:
        out = [OutputCommand(k, n, pressed=False) for k, n in sorted(self._pressed)]
        out += [OutputCommand("axis", "left_x", value=0.0),
                OutputCommand("axis", "left_y", value=0.0),
                OutputCommand("axis", "right_x", value=0.0),
                OutputCommand("axis", "right_y", value=0.0)]
        self._pressed.clear()
        return out

    def _warn_once(self, key: str, msg: str, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)

    def tick(self, now: float) -> list[OutputCommand]:
        return []


# ------------------------------------------------------------------ engine
RATE_LIMIT_INTENTS_PER_S = 600       # runaway-producer trip: ~10 intents/tick sustained


class Engine:
    def __init__(self, sm: StateMachine, mapper, gateway: Gateway, producers: list,
                 latency: Optional[LatencyLog] = None, recorder: Optional[Recorder] = None,
                 cfg: Optional[AppConfig] = None):
        self.sm, self.mapper, self.gw = sm, mapper, gateway
        self.producers = producers
        self.latency = latency or LatencyLog()
        self.recorder = recorder
        self.cfg = cfg or AppConfig()
        self.last_commands: list[OutputCommand] = []
        self.input_blocked = False        # foreground app is elevated and we are not (UIPI)
        self.foreground = ""
        self._rate_window: collections.deque = collections.deque()   # (t, count)
        self.rate_trips = 0
        self.mock_sources: tuple = ()        # (("webcam", "missing mediapipe"), ...) set by _run

    def step(self, events: list[SignalEvent], now: float,
             context: Optional[str] = None) -> list[OutputCommand]:
        t0 = clock.now()
        direct: list[Intent] = []
        pending: list[SignalEvent] = []
        for ev in events:
            if self.recorder:
                self.recorder.write(ev)
            out, consumed = self.sm.on_event(ev, now)
            direct.extend(out)
            if not consumed:
                pending.append(ev)
        if context is not None:
            direct.extend(self.sm.on_context_change(context, now))

        cancelled = any(i.kind in ("cancel_all", "release") for i in direct)
        if cancelled:
            for p in self.producers:                # safety cancel: producers must not re-hold
                note = getattr(p, "note_cancel", None)
                if note is not None:
                    try:
                        note(now)
                    except Exception:
                        log.exception("producer %r note_cancel failed", p)

        proposed: list[Intent] = []
        if not cancelled:
            # On a cancel tick the producers are skipped entirely: a context change (Alt+Tab)
            # leaves the machine ACTIVE, so without this a gesture already in progress would
            # re-press on the very same tick the release went out.
            for p in self.producers:
                try:
                    proposed.extend(p.process(pending, now))
                except Exception:
                    log.exception("producer %r failed; skipping this tick", p)
        if self._rate_tripped(len(proposed), now):
            gated = self.sm.gate([Intent("pause")], now)   # runaway producer -> lock + release
        else:
            gated = self.sm.gate(proposed, now)
        expired = self.sm.tick(now)

        cmds: list[OutputCommand] = []
        for it in direct + gated + expired:          # safety intents first, in order
            cmds.extend(self.mapper.handle(it, now))
        tick_fn = getattr(self.mapper, "tick", None)
        if tick_fn:
            cmds.extend(tick_fn(now))
        self.gw.apply(cmds)
        self.gw.flush(now)

        t1 = clock.now()
        age = max(((t1 - e.t) * 1000.0 for e in events), default=None)
        self.latency.record(t1, (t1 - t0) * 1000.0, age)
        self.last_commands = cmds
        return cmds

    def _rate_tripped(self, count: int, now: float) -> bool:
        """Sliding 1 s window over producer intents. Over RATE_LIMIT_INTENTS_PER_S the machine
        is locked via a pause intent (cancel_all + pause) and this tick's intents are dropped."""
        self._rate_window.append((now, count))
        while self._rate_window and now - self._rate_window[0][0] > 1.0:
            self._rate_window.popleft()
        total = sum(c for _, c in self._rate_window)
        if total <= RATE_LIMIT_INTENTS_PER_S:
            return False
        if self.sm.locked:
            return True                              # already locked: keep dropping, no log spam
        self.rate_trips += 1
        log.error("runaway producer: %d intents in the last second (> %d); locking",
                  total, RATE_LIMIT_INTENTS_PER_S)
        return True

    def apply_config(self, cfg: AppConfig, now: float,
                     mapper_factory: Optional[Callable[[str], object]] = None) -> None:
        """Apply a hot-reloaded config. If key bindings or the profile change, everything is
        cancelled through the normal path FIRST (state machine, mapper and gateway all agree
        nothing is held); timing and log-level changes never disturb a hold in progress."""
        old = self.cfg
        rebind = (cfg.profile, cfg.button_map, cfg.axis_tuning) != (
            old.profile, old.button_map, old.axis_tuning)
        if rebind:
            cmds: list[OutputCommand] = []
            for it in self.sm.gate([Intent("cancel_all")], now):
                cmds.extend(self.mapper.handle(it, now))
            self.gw.apply(cmds)
            self.gw.release_all("config change")          # belt and braces: any mapper
        sm_cfg = self.sm.cfg
        sm_cfg.default_hold_ms, sm_cfg.max_hold_ms = cfg.default_hold_ms, cfg.max_hold_ms
        sm_cfg.face_regain_ms = cfg.face_regain_ms
        sm_cfg.cancel_events = tuple(cfg.cancel_events)
        if rebind:
            self.gw.update_maps(button_map=dict(cfg.button_map),
                                axis_map=effective_axis_map(cfg))
            if cfg.profile != old.profile and mapper_factory is not None:
                new_mapper = mapper_factory(cfg.profile)
                if new_mapper is None:
                    log.error("profile %r failed to load; keeping the current mapping", cfg.profile)
                else:
                    self.mapper = new_mapper
        logsetup.set_level(cfg.log_level)
        self.cfg = cfg

    def status(self) -> dict:
        """Read-only dict for Shaury's dashboard: state, latency, FPS, held keys."""
        s = self.sm.snapshot(clock.now())
        return {"state": s.state.value, "reason": s.reason, "latched": s.latched,
                "remaining_ms": s.remaining_ms, "face_ok": s.face_ok, "locked": s.locked,
                "held": sorted(self.gw.held), "dropped": self.sm.dropped,
                "input_blocked": self.input_blocked, "foreground": self.foreground,
                "mock_sources": [f"{n}: {r}" if r else n for n, r in self.mock_sources],
                **self.latency.summary()}


# ------------------------------------------------------------------ assembly
def _handsfree_mapper(profile: str):
    """Dishti's handsfree ProfileMapper wrapped for this engine, or None."""
    try:
        return _HandsfreeMapper(profile)
    except Exception:
        log.exception("handsfree profile %r failed to load", profile)
    return None


def _try_load_mapper(profile: str):
    """Root mapping.Mapper(profile) first (Dishti's drop-in), then the handsfree profile.
    None if neither is available."""
    try:
        mod = importlib.import_module("mapping")
        return mod.Mapper(profile)
    except ModuleNotFoundError:
        pass
    except Exception:
        log.exception("mapping.Mapper(%r) failed", profile)
    return _handsfree_mapper(profile)


def _load_mapper(profile: str):
    return _try_load_mapper(profile) or _fallback_mapper()


def _fallback_mapper():
    log.warning("using BasicMapper fallback")
    return BasicMapper()


def _load_live_sources(live: bool = True) -> list:
    """live=False (dry-run / non-Windows): only real poll()-sources count; the handsfree
    adapters' mock loops would feed fake pose data, so they are skipped."""
    srcs = []
    for name in ("webcam", "audio", "switch"):
        try:
            srcs.append(importlib.import_module(f"adapters.{name}").create_source())
            continue
        except ModuleNotFoundError:
            pass
        except Exception:
            log.exception("adapters.%s failed to start, skipping", name)
            continue
        if not live:
            continue
        try:
            srcs.append(_HFSource(name))             # teammates' handsfree adapters, bridged
        except ModuleNotFoundError:
            log.warning("adapters.%s and handsfree.adapters.%s not available, skipping",
                        name, name)
        except Exception:
            log.exception("handsfree.adapters.%s failed to start, skipping", name)
    return srcs


def _load_producers() -> list:
    """Antas's producers.* drop-in modules first; otherwise the bridged handsfree pipeline."""
    out: list = []
    for name in ("fastpath", "slowpath", "assist"):
        try:
            out.append(importlib.import_module(f"producers.{name}").create_producer())
        except ModuleNotFoundError:
            pass
        except Exception:
            log.exception("producers.%s failed to start, skipping", name)
    if out:
        return out
    try:
        return [_HandsfreeProducer()]
    except Exception:
        log.exception("handsfree producer pipeline failed to start")
    return []


def _windows_realtime() -> None:
    if os.name != "nt":
        return
    import ctypes
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)                       # 1 ms sleep granularity
    except Exception:
        pass
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x80)
    except Exception:
        log.debug("could not set high priority class")
    try:
        ctypes.windll.shell32.IsUserAnAdmin() or log.warning(
            "not running as admin: elevated games will ignore our input (UIPI)")
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="OmniControl engine")
    ap.add_argument("--version", action="version", version=f"OmniControl {__version__}")
    ap.add_argument("--replay"), ap.add_argument("--record")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true", help="do not inject real input")
    ap.add_argument("--hz", type=float, default=None, help="loop rate (default: config, 60)")
    ap.add_argument("--profile", default=None, help="pin a profile (overrides config.json)")
    ap.add_argument("--latency-log")
    ap.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (0 = run)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--doctor", action="store_true", help="check this machine and exit")
    ap.add_argument("--json", action="store_true", help="(with --doctor) JSON output")
    ap.add_argument("--quick", action="store_true",
                    help="(with --doctor) skip the camera, microphone and jitter tests")
    ap.add_argument("--allow-mock", action="store_true",
                    help="(with --doctor) report MOCK/simulated input as WARN instead of FAIL")
    ap.add_argument("--safe", action="store_true",
                    help="built-in defaults only: ignore config.json and profiles, shorter holds")
    ap.add_argument("--elevate", action="store_true", help="restart as administrator (UAC prompt)")
    ap.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)  # set by the relaunch
    ap.add_argument("--no-tray", action="store_true", help="no system-tray icon")
    ap.add_argument("--tray", action="store_true",
                    help="with --replay: show the tray icon, take the single-instance lock and keep "
                         "running until Quit (demos and testing without a camera)")
    ap.add_argument("--allow-multiple", action="store_true",
                    help="skip the single-instance check (development)")
    ap.add_argument("--dashboard", action="store_true", help="launch Shaury's telemetry HUD server")
    ap.add_argument("--dashboard-port", type=int, default=8080, help="port for telemetry dashboard (default: 8080)")
    ap.add_argument("--handsfree", action="store_true", help="run handsfree standalone pipeline directly")
    return ap


def _pin(cfg: AppConfig, a: argparse.Namespace) -> AppConfig:
    """Command-line choices beat the config file, including after a hot reload."""
    if a.profile:
        cfg = replace(cfg, profile=a.profile)
    if a.verbose:
        cfg = replace(cfg, log_level="DEBUG")
    return cfg


def _tray_wanted(a: argparse.Namespace) -> bool:
    """The tray is for real Windows sessions: not for plain replays, tests or --no-tray."""
    return os.name == "nt" and not a.no_tray and (a.tray or not a.replay)


def _needs_lock(a: argparse.Namespace) -> bool:
    """Plain replays and --allow-multiple skip the single-instance lock; everything else takes it."""
    return (not a.replay or a.tray) and not a.allow_multiple


def _elevate_from_tray(instance: SingleInstance, raw_args: list[str]) -> bool:
    """True = an administrator copy was started and this one should exit.

    The lock is dropped BEFORE asking, so the new copy can take it. If the user declines UAC we
    take the lock back and carry on."""
    held = instance.owned
    instance.release()
    if elevation.relaunch_as_admin(raw_args):
        return True
    if held:
        instance.acquire()
    log.warning("administrator rights not granted; staying as a normal user")
    return False


class _Releaser:
    """Crash handlers are installed before the gateway exists, so release is late-bound."""

    def __init__(self) -> None:
        self.gw: Optional[Gateway] = None

    def __call__(self, why: str) -> None:
        if self.gw is not None:
            self.gw.release_all(why)


def _run(a: argparse.Namespace, cfg: AppConfig, watcher: Optional[ConfigWatcher],
         releaser: _Releaser, instance: SingleInstance, raw_args: list[str]) -> int:
    live = os.name == "nt" and not a.dry_run
    backend = SendInputBackend() if live else RecordingBackend()
    if not live:
        log.info("dry-run backend: no real input will be sent")
    gw = Gateway(backend, button_map=dict(cfg.button_map), axis_map=effective_axis_map(cfg))
    releaser.gw = gw
    gw.install_exit_handlers()
    _windows_realtime()

    sm = StateMachine(SMConfig(default_hold_ms=cfg.default_hold_ms, max_hold_ms=cfg.max_hold_ms,
                                face_regain_ms=cfg.face_regain_ms,
                                cancel_events=tuple(cfg.cancel_events)))
    sm.subscribe(lambda s: log.info("STATE %s (%s) latched=%s", s.state.value, s.reason, s.latched))
    latency = LatencyLog(jsonl_path=a.latency_log)
    recorder = Recorder(a.record) if a.record else None
    mapper = _fallback_mapper() if a.safe else _load_mapper(cfg.profile)

    producers: list = []
    if not a.safe:
        producers = _load_producers()
    if not producers:
        log.warning("using BasicAssist producer fallback")
        producers = [BasicAssist()]

    engine = Engine(sm, mapper, gw, producers, latency, recorder, cfg)

    replay = ReplaySource(a.replay, a.speed) if a.replay else None
    sources = [replay] if replay else _load_live_sources(live=live)
    wd = Watchdog()
    fc: Optional[FrameClock] = None
    ctl = TrayController(config_path(), log_dir())
    tray = None
    try:
        if not sources:
            log.error("no input sources available; nothing to do")
            return 1

        engine.mock_sources = tuple(
            (getattr(s, "name", type(s).__name__), getattr(s, "mock_reason", ""))
            for s in sources if getattr(s, "is_mock", False))
        for s in sources:
            name = getattr(s, "name", type(s).__name__)
            if getattr(s, "is_mock", False):
                log.warning("SOURCE %s: MOCK INPUT: not real camera/mic (%s)",
                            name, getattr(s, "mock_reason", ""))
            else:
                log.info("SOURCE %s: REAL", name)

        stale: collections.deque = collections.deque()
        wd.add_channel("loop", cfg.watchdog_ms, lambda n: gw.release_all("deadman: main loop stalled"))
        wd.add_channel("webcam", 500, lambda n: stale.append(n))
        wd.start()
        wd.beat("loop")

        # Launch Shaury's telemetry HUD if requested
        dashboard_server = None
        if a.dashboard:
            try:
                from handsfree.dashboard.server import DashboardServer
                dashboard_server = DashboardServer(port=a.dashboard_port)
                dashboard_server.start()
                log.info("telemetry HUD dashboard active on http://127.0.0.1:%d", a.dashboard_port)
            except Exception:
                log.exception("failed to start telemetry dashboard")

        fg, kill, fc = ForegroundWatcher(), KillSwitch(), FrameClock(a.hz or cfg.hz)
        elev = elevation.ElevationMonitor()
        log.info("administrator: %s", "yes" if elev.self_admin else "no")
        if _tray_wanted(a):
            tray = create_tray(ctl, elev.self_admin)
            if tray is not None:
                tray.start()
        next_tray = 0.0
        t_end = clock.now() + a.duration if a.duration else None
        while True:
            now = fc.wait()
            if watcher is not None:
                new_cfg = watcher.poll(now)
                if new_cfg is not None:
                    engine.apply_config(_pin(new_cfg, a), now, _try_load_mapper)
                    log.info("config reloaded from %s", watcher.path)
            events: list[SignalEvent] = []
            for s in sources:
                try:
                    events.extend(s.poll(now))
                except Exception:
                    log.exception("source %r failed", s)
            if any(e.src == "webcam" for e in events):
                wd.beat("webcam")
            if kill.pressed_edge():
                events.append(SignalEvent("switch", "kill", 1.0, now))
            events.extend(ctl.drain())                    # Freeze / Resume clicked in the tray
            while stale:
                stale.popleft()
                events.append(SignalEvent("webcam", "face", 0.0, now))   # camera went silent
            context = fg.poll(now) or None
            info = elev.update(fg.pid)
            if info is not None:
                engine.foreground, engine.input_blocked = context or "", info.blocked
                if info.blocked:
                    log.warning("foreground app %s is elevated and we are not: Windows will drop "
                                "our input (UIPI). Restart as administrator (--elevate).", context)
            engine.step(events, now, context=context)
            wd.beat("loop")

            if dashboard_server:
                dashboard_server.update_telemetry("state", engine.sm.state.value)
                dashboard_server.update_telemetry("pressed", list(engine.gw.held))
                dashboard_server.update_telemetry("mock_sources", list(engine.mock_sources))

            if tray is not None and now >= next_tray:
                tray.update(engine.status())
                next_tray = now + 0.25
            if ctl.quit_requested.is_set():
                log.info("quit requested from the tray")
                break
            if ctl.elevate_requested.is_set():
                ctl.elevate_requested.clear()
                if _elevate_from_tray(instance, raw_args):
                    break
            replay_done = replay and replay.finished and not engine.sm._latches
            keep_open = a.tray and tray is not None          # demo mode: run until Quit
            if (replay_done and not keep_open) or (t_end and now >= t_end):
                break
        return 0
    finally:
        wd.stop()
        if tray is not None:
            tray.stop()
        gw.release_all("shutdown")
        for s in sources:
            try:
                s.close()
            except Exception:
                pass
        if recorder:
            recorder.close()
        latency.close()
        log.info("summary: %s overruns=%d", latency.summary(), fc.overruns if fc else 0)


def main(argv: Optional[list[str]] = None) -> int:
    raw_args = sys.argv[1:] if argv is None else list(argv)
    a = build_parser().parse_args(raw_args)

    if a.handsfree:
        from handsfree.main import main as handsfree_main
        handsfree_main()
        return 0

    if a.doctor:
        import doctor
        return doctor.run_cli(as_json=a.json, slow=not a.quick, allow_mock=a.allow_mock)

    watcher: Optional[ConfigWatcher] = None
    if a.safe:
        cfg = SAFE_CONFIG
    else:
        watcher = ConfigWatcher(config_path())
        cfg = watcher.load()
    cfg = _pin(cfg, a)

    ring = logsetup.setup_logging(log_dir(), cfg.log_level)
    log.info("OmniControl %s starting%s", __version__, " (safe mode)" if a.safe else "")
    if a.safe:
        log.warning("SAFE MODE: config.json and profiles ignored; holds capped at %d ms",
                    cfg.max_hold_ms)

    # --elevate: relaunch BEFORE claiming the single-instance lock, so the new copy can take it.
    if a.elevate and not a.elevated and not elevation.is_admin():
        if elevation.relaunch_as_admin(raw_args):
            log.info("administrator copy started; this copy exits")
            logsetup.teardown_logging()
            return 0
        log.warning("administrator rights not granted; continuing without them")

    releaser = _Releaser()
    uninstall_crash = logsetup.install_crash_handlers(releaser, log_dir(), ring)
    instance = SingleInstance(SINGLE_INSTANCE_NAME)
    try:
        if _needs_lock(a) and not instance.acquire():
            msg = ("OmniControl is already running (look for its tray icon). "
                   "Quit that copy first, then start this one.")
            log.error(msg)
            if getattr(sys, "frozen", False):          # a windowed exe has no console to read
                import doctor
                doctor.show_message_box("OmniControl", msg)
            return 2
        return _run(a, cfg, watcher, releaser, instance, raw_args)
    finally:
        instance.release()
        uninstall_crash()
        logsetup.teardown_logging()


if __name__ == "__main__":
    sys.exit(main())
