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


BasicAssist = DefaultProducer


# ------------------------------------------------------------------ engine
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
                **self.latency.summary()}


# ------------------------------------------------------------------ assembly
def _try_load_mapper(profile: str):
    """Dishti's Mapper for `profile`, or None if it is missing or fails."""
    try:
        mod = importlib.import_module("mapping")
        return mod.Mapper(profile)
    except ModuleNotFoundError:
        log.warning("mapping.py not found")
    except Exception:
        log.exception("mapping.Mapper(%r) failed", profile)
    return None


def _load_mapper(profile: str):
    return _try_load_mapper(profile) or _fallback_mapper()


def _fallback_mapper():
    log.warning("using BasicMapper fallback")
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
                                face_regain_ms=cfg.face_regain_ms))
    sm.subscribe(lambda s: log.info("STATE %s (%s) latched=%s", s.state.value, s.reason, s.latched))
    latency = LatencyLog(jsonl_path=a.latency_log)
    recorder = Recorder(a.record) if a.record else None
    mapper = _fallback_mapper() if a.safe else _load_mapper(cfg.profile)

    producers: list = []
    if not a.safe:
        try:
            producers.append(importlib.import_module("producers.fastpath").create_producer())
        except Exception:
            pass
        try:
            producers.append(importlib.import_module("producers.slowpath").create_producer())
        except Exception:
            pass
    if not producers:
        log.warning("using BasicAssist producer fallback")
        producers = [BasicAssist()]

    engine = Engine(sm, mapper, gw, producers, latency, recorder, cfg)

    replay = ReplaySource(a.replay, a.speed) if a.replay else None
    sources = [replay] if replay else _load_live_sources()
    wd = Watchdog()
    fc: Optional[FrameClock] = None
    ctl = TrayController(config_path(), log_dir())
    tray = None
    try:
        if not sources:
            log.error("no input sources available; nothing to do")
            return 1

        stale: collections.deque = collections.deque()
        wd.add_channel("loop", 250, lambda n: gw.release_all("deadman: main loop stalled"))
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
        return doctor.run_cli(as_json=a.json, slow=not a.quick)

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
