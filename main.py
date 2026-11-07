"""main.py (Binore): wires everything together and runs the 60 Hz loop.

Per tick:
  sources.poll()  ->  state_machine.on_event (safety)  ->  intent producers  ->  state_machine.gate
  -> state_machine.tick (expiry)  ->  mapper (Dishti)  ->  gateway.apply + flush

Pluggable pieces (so teammates can drop their work in without touching this file):
  * Live sources: adapters/webcam.py (Antas), adapters/audio.py, adapters/switch.py (Dishti).
    Each module exposes  create_source() -> object with poll() -> list[SignalEvent] and close().
  * Intent producers (Antas fastpath/slowpath/assist): objects with
    process(events: list[SignalEvent], now: float) -> list[Intent].
  * Mapper (Dishti): handle(intent, now) -> list[OutputCommand]; optional tick(now) -> list.
If a piece is missing, a small fallback is used and a warning is logged, so this runs today.

Usage:
  python main.py --dry-run                       live sources, no real input injected
  python main.py --replay session.jsonl --dry-run --speed 4
  python main.py --record session.jsonl          live + save every event
"""
from __future__ import annotations

import argparse
import collections
import importlib
import logging
import os
import sys
from typing import Optional

from adapters.replay import Recorder, ReplaySource
from config import ConfigWatcher, appdata_dir, load_config
from core import clock
from core.events import Intent, OutputCommand, SignalEvent
from doctor import doctor_main
from elevation import check_foreground_elevation, ensure_admin, is_admin
from gateway import (ForegroundWatcher, Gateway, KillSwitch, RecordingBackend,
                     SendInputBackend)
from logging_setup import install_crash_handler, setup_logging
from session_guard import SessionLockWindow, SingleInstanceGuard
from state_machine import SMConfig, StateMachine
from tray import TrayIcon, open_logs_folder
from watchdog import FrameClock, LatencyLog, Watchdog

log = logging.getLogger("omnicontrol.main")


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
            targets = [self._target(it.name)] if it.name else list(self._held)
            out = []
            for k, n in targets:
                if (k, n) in self._held:
                    self._held.discard((k, n))
                    out.append(OutputCommand(k, n, pressed=False))
            return out
        if it.kind in ("cancel_all", "pause"):
            self._held.clear()
            return [OutputCommand("release_all", "")]
        return []

    def tick(self, now: float) -> list[OutputCommand]:
        return []


class DefaultProducer:
    """Stand-in for Antas's pipeline: head angles -> stick, jaw_open -> timed hold."""

    def __init__(self, range_deg: float = 20.0, jaw_on: float = 0.55, jaw_off: float = 0.32,
                 hold_ms: int = 1800):
        self.range, self.on, self.off, self.hold_ms = range_deg, jaw_on, jaw_off, hold_ms
        self._yaw = self._pitch = 0.0
        self._jaw = False

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
            elif e.name == "brow_raise" and e.value >= 0.58:
                out.append(Intent("action", "interact"))
        if moved:
            c = lambda v: max(-1.0, min(1.0, v / self.range))
            out.append(Intent("stick", "look", x=c(self._yaw), y=c(self._pitch)))
        return out


# ------------------------------------------------------------------ engine
class Engine:
    def __init__(self, sm: StateMachine, mapper, gateway: Gateway, producers: list,
                 latency: Optional[LatencyLog] = None, recorder: Optional[Recorder] = None,
                 deny_list: Optional[list[str]] = None):
        self.sm, self.mapper, self.gw = sm, mapper, gateway
        self.producers = producers
        self.latency = latency or LatencyLog()
        self.recorder = recorder
        self.deny_list = [d.lower() for d in (deny_list or [])]
        self.last_commands: list[OutputCommand] = []

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
            ctx_lower = context.lower()
            # If foreground process is in deny-list, force CONVERSE_LOCK and cancel all
            if self.deny_list and any(ctx_lower == d or ctx_lower.endswith(d) for d in self.deny_list):
                log.warning("Foreground app %r is in deny-list: locking input", context)
                out, _ = self.sm.on_event(SignalEvent("switch", "kill", 1.0, now), now)
                direct.extend(out)
            else:
                direct.extend(self.sm.on_context_change(context, now))

        proposed: list[Intent] = []
        for p in self.producers:
            try:
                proposed.extend(p.process(pending, now))
            except Exception:
                log.exception("producer %r failed; skipping this tick", p)
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

    def status(self) -> dict:
        """Read-only dict for Shaury's dashboard: state, latency, FPS, held keys."""
        s = self.sm.snapshot(clock.now())
        return {"state": s.state.value, "reason": s.reason, "latched": s.latched,
                "remaining_ms": s.remaining_ms, "face_ok": s.face_ok, "locked": s.locked,
                "held": sorted(self.gw.held), "dropped": self.sm.dropped, **self.latency.summary()}


# ------------------------------------------------------------------ assembly
def _load_mapper(profile: str):
    try:
        mod = importlib.import_module("mapping")
        return mod.Mapper(profile)
    except ModuleNotFoundError:
        log.warning("mapping.py not found: using BasicMapper fallback")
    except Exception:
        log.exception("mapping.Mapper(%r) failed: using BasicMapper fallback", profile)
    return BasicMapper()


def _load_live_sources() -> list:
    srcs = []
    for name in ("adapters.webcam", "adapters.audio", "adapters.switch"):
        try:
            srcs.append(importlib.import_module(name).create_source())
        except ModuleNotFoundError:
            log.warning("%s not available yet, skipping", name)
        except Exception:
            log.exception("%s failed to start, skipping", name)
    return srcs


def _windows_realtime() -> None:
    if os.name != "nt":
        return
    import ctypes
    try:
        # DPI awareness: per-monitor v2
        try:
            u32 = ctypes.windll.user32
            u32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass

        ctypes.windll.winmm.timeBeginPeriod(1)                       # 1 ms sleep granularity
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x80)
        ctypes.windll.shell32.IsUserAnAdmin() or log.warning(
            "not running as admin: elevated games will ignore our input (UIPI)")
    except Exception:
        log.exception("realtime setup failed")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="OmniControl engine")
    ap.add_argument("--doctor", action="store_true", help="run hardware & permission diagnostics")
    ap.add_argument("--replay", help="replay recorded session JSONL")
    ap.add_argument("--record", help="record live session to JSONL")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true", help="do not inject real input")
    ap.add_argument("--safe", action="store_true", help="safe mode: dry-run, no real input")
    ap.add_argument("--elevate", action=argparse.BooleanOptionalAction, default=None,
                    help="request UAC admin elevation if not running as admin")
    ap.add_argument("--hz", type=float, default=None)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--config", default=None, help="path to config.json")
    ap.add_argument("--latency-log")
    ap.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (0 = run)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    if a.doctor:
        return doctor_main()

    # Load configuration
    cfg_path = None
    if a.config:
        from pathlib import Path
        cfg_path = Path(a.config)
    cfg = load_config(cfg_path)

    # Elevation check / re-launch
    should_elevate = a.elevate if a.elevate is not None else cfg.elevate
    if should_elevate and not a.replay and not a.dry_run and not a.safe:
        if ensure_admin(force=True):
            return 0  # Re-launched elevated process; parent exits cleanly

    # Setup logging (5 x 1MB rotating + console)
    log_level = "DEBUG" if a.verbose else cfg.log_level
    setup_logging(level_name=log_level, console=True)

    # Single-instance guard (skip for replay/dry-run/short duration tests)
    mutex_guard = None
    if not a.replay and not a.dry_run and not a.safe and a.duration == 0.0:
        mutex_guard = SingleInstanceGuard()
        if not mutex_guard.acquire():
            log.error("OmniControl is already running. Exiting.")
            return 1

    live = os.name == "nt" and not (a.dry_run or a.safe or cfg.safe_mode)
    backend = SendInputBackend() if live else RecordingBackend()
    if not live:
        log.info("safe / dry-run backend: no real input will be sent")

    loop_hz = a.hz if a.hz is not None else cfg.hz
    profile_name = a.profile if a.profile is not None else cfg.profile

    gw = Gateway(
        backend,
        max_transitions_per_s=cfg.max_key_transitions_per_second,
        on_rate_limit=lambda: log.warning("Rate limit tripped; input stopped"),
    )
    gw.install_exit_handlers()
    install_crash_handler(on_release=lambda r: gw.release_all(f"crash: {r}"))
    _windows_realtime()

    sm = StateMachine(SMConfig())
    sm.subscribe(lambda s: log.info("STATE %s (%s) latched=%s", s.state.value, s.reason, s.latched))
    latency = LatencyLog(jsonl_path=a.latency_log)
    recorder = Recorder(a.record) if a.record else None
    engine = Engine(sm, _load_mapper(profile_name), gw, [DefaultProducer()], latency, recorder,
                    deny_list=cfg.deny_list)

    # Config hot-reload watcher
    def _on_cfg_change(new_cfg):
        gw.max_transitions_per_s = new_cfg.max_key_transitions_per_second
        engine.deny_list = [d.lower() for d in new_cfg.deny_list]
        log.info("Applied live config updates (deny_list=%d items, max_tps=%d)",
                 len(engine.deny_list), gw.max_transitions_per_s)

    cfg_watcher = ConfigWatcher(path=cfg_path, poll_s=2.0, on_change=_on_cfg_change)
    cfg_watcher.start()

    # Session lock handler (workstation lock / sleep)
    def _on_session_lock():
        gw.release_all("session_lock")
        sm.on_event(SignalEvent("switch", "kill", 1.0, clock.now()), clock.now())

    session_wnd = SessionLockWindow(on_lock=_on_session_lock)
    session_wnd.start()

    # System tray icon
    tray = None
    tray_pause_flag = collections.deque()
    if cfg.tray_enabled and not a.dry_run and not a.safe and not a.replay and a.duration == 0.0:
        tray = TrayIcon(
            on_panic=lambda: gw.release_all("tray_panic"),
            on_toggle_pause=lambda: tray_pause_flag.append(True),
            on_run_doctor=doctor_main,
            on_open_logs=open_logs_folder,
            on_quit=lambda: None,
        )
        sm.subscribe(lambda s: tray.set_state(s.state.value))
        tray.start()

    replay = ReplaySource(a.replay, a.speed) if a.replay else None
    sources = [replay] if replay else _load_live_sources()
    if not sources:
        log.error("no input sources available; nothing to do")
        cfg_watcher.stop()
        session_wnd.stop()
        if mutex_guard:
            mutex_guard.release()
        return 1

    stale: collections.deque = collections.deque()
    wd = Watchdog()
    wd.add_channel("loop", 250, lambda n: gw.release_all("deadman: main loop stalled"))
    wd.add_channel("webcam", 500, lambda n: stale.append(n))
    wd.start()
    wd.beat("loop")

    fg = ForegroundWatcher()
    kill = KillSwitch(vk=cfg.panic_vk, chord_enabled=cfg.panic_chord)
    fc = FrameClock(loop_hz)
    t_end = clock.now() + a.duration if a.duration else None
    next_uipi_check = 0.0

    try:
        while True:
            now = fc.wait()
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
            while tray_pause_flag:
                tray_pause_flag.popleft()
                if sm.locked:
                    events.append(SignalEvent("mic", "voice_resume", 1.0, now))
                else:
                    events.append(SignalEvent("switch", "kill", 1.0, now))
            while stale:
                stale.popleft()
                events.append(SignalEvent("webcam", "face", 0.0, now))

            # UIPI elevation check (throttled ~1 Hz)
            if now >= next_uipi_check:
                next_uipi_check = now + 1.0
                check_foreground_elevation()

            engine.step(events, now, context=fg.poll(now) or None)
            wd.beat("loop")
            if (replay and replay.finished and not engine.sm._latches) or (t_end and now >= t_end):
                break
    finally:
        wd.stop()
        cfg_watcher.stop()
        session_wnd.stop()
        if tray:
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
        if mutex_guard:
            mutex_guard.release()
        log.info("summary: %s overruns=%d", latency.summary(), fc.overruns)
    return 0


if __name__ == "__main__":
    sys.exit(main())
