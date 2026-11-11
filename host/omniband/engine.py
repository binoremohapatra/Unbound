"""engine.py - OmniBand V2 host engine.

Owns: arbiter (link), gesture router (mode actions), output sink (Gateway /
dry-run), safety watchdogs (link loss, foreground change, SOS/ESTOP), TTS and
siren cues, night/bruxism logging, calibration client.

Safety invariants (mirroring the unbound safety model):
  - any ESTOP gesture or SOS_CONFIRMED triggers immediate release_all
  - link silence beyond watchdog -> release_all + degraded state
  - foreground app change (optional) -> release_all
  - atexit/console-ctrl release is installed by gateway.Gateway itself
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .constants_gen import CONSTANTS, MODES, mode_of
from . import protocol
from .link import Arbiter
from .out_sinks import (GestureRouter, GatewaySink, DryRunSink, TTSSink,
                        SirenSink, INFO_GESTURES)

log = logging.getLogger("omniband.engine")

K = CONSTANTS
WD = K["watchdogs"]


@dataclass
class EngineStats:
    t0: float = field(default_factory=time.perf_counter)
    lines: int = 0
    evts: int = 0
    ges: int = 0
    releases: int = 0
    sos_events: int = 0
    estops: int = 0
    failovers: int = 0
    last_ges: str = ""
    last_ges_t: float = 0.0
    mode: int = 0


class OmniEngine:
    """Drives one Arbiter + one output sink. Call run() or tick() manually."""

    def __init__(self, arbiter: Arbiter, sink, *,
                 tts: Optional[TTSSink] = None,
                 siren: Optional[SirenSink] = None,
                 release_on_focus_change: bool = False,
                 foreground_poller: Optional[Callable[[], str]] = None,
                 bruxism_log: str = "logs/bruxism.csv",
                 keepalive_ms: int = 100,
                 on_event: Optional[Callable[[str, dict], None]] = None) -> None:
        self.arbiter = arbiter
        self.sink = sink
        self.tts = tts
        self.siren = siren
        self.release_on_focus = release_on_focus_change
        self._fg = foreground_poller
        self._fg_last = ""
        self.bruxism_log = bruxism_log
        self.keepalive_ms = keepalive_ms
        self.on_event = on_event or (lambda k, d: None)
        self.stats = EngineStats()
        self.mode = 0
        self.router = GestureRouter(0)
        self._last_line_ms = -1e18
        self._last_tx_ms = -1e18
        self._link_dead = False
        self._pending_pings: list[tuple[float, int]] = []
        self._ping_seq = 0
        self._pong_wait: dict[int, float] = {}
        self._cal_results: list[dict] = []
        self._running = False
        self.night_mode = False
        self.sos_pending = False
        arbiter.on_line = self._on_device_line

    # ------------------------------------------------------------- public API
    def run(self, duration_s: Optional[float] = None) -> None:
        self._running = True
        t0 = time.perf_counter()
        log.info("engine running (mode=%d, sink=%s)", self.mode,
                 type(self.sink).__name__)
        try:
            while self._running:
                self.tick()
                if duration_s is not None and \
                        time.perf_counter() - t0 >= duration_s:
                    break
                time.sleep(0.005)
        finally:
            self.emergency_release("engine stop")

    def stop(self) -> None:
        self._running = False

    def tick(self) -> None:
        """One engine iteration: link IO, dispatch, watchdogs, keepalive."""
        now = time.perf_counter()
        now_ms = now * 1000.0
        self.arbiter.poll(now_ms)
        self._watchdogs(now, now_ms)
        self._keepalive(now_ms)
        self.sink.flush(now)

    def emergency_release(self, reason: str) -> None:
        self.sink.release_all(reason)
        self.router.cancel_pending()
        self.stats.releases += 1
        log.warning("EMERGENCY RELEASE: %s", reason)

    def set_mode(self, mode: int, *, notify_device: bool = True) -> None:
        if not (0 <= mode < len(MODES)):
            log.warning("set_mode: bad mode %r", mode)
            return
        self.mode = mode
        self.router.set_mode(mode)
        self.stats.mode = mode
        cfg = mode_of(mode)
        log.info("mode -> %s (%s)", cfg["name"], cfg["tag"])
        self.on_event("mode", {"mode": mode, "name": cfg["name"]})
        self._maybe_tts(f"mode {cfg['tag']}", f"mode{mode}", "mode")
        if notify_device:
            self.send(protocol.cmd_set_mode(mode, cmd_id=f"m{mode}"))

    # ------------------------------------------------------------- dispatch
    def _on_device_line(self, obj: dict) -> None:
        k = obj.get("k")
        self.stats.lines += 1
        self._last_line_ms = time.perf_counter() * 1000.0
        self.on_event("line", {"k": k})
        if k == "tele":
            self._on_tele(obj)
        elif k == "evt":
            self._on_evt(obj)
        elif k == "ges":
            self._on_ges(obj)
        elif k == "ack":
            self._on_ack(obj)
        elif k == "pong":
            th = obj.get("th")
            sent = self._pong_wait.pop(th, None)
            if sent is not None:
                rtt = (time.perf_counter() * 1000.0) - sent
                log.debug("pong th=%s rtt=%.1f ms", th, rtt)
                self.on_event("pong", {"th": th, "rtt_ms": rtt})
        elif k == "cal":
            self._cal_results.append(dict(obj))
            self.on_event("cal", obj)
        elif k == "diag":
            self.on_event("diag", obj)
        elif k == "hello":
            log.info("device hello fw=%s mode=%s caps=%s",
                     obj.get("fw"), obj.get("mode"), obj.get("caps"))
            m = int(obj.get("mode", 0))
            if m != self.mode:
                self.set_mode(m, notify_device=False)

    def _on_tele(self, obj: dict) -> None:
        tele = protocol.parse_tele(obj)
        if tele is not None:
            self.on_event("tele", tele)
        flags = int(obj.get("s", 0))
        st = str(obj.get("st", ""))
        # device-side host_silent flag: we ARE the host; keepalive prevents it,
        # but if it still fires the link path is broken -> release.
        if flags & K["tele_flags"].get("host_silent", 64):
            log.warning("device reports host_silent; releasing")
            self.emergency_release("host_silent flag")
        # night flag mirrors device night state
        night = bool(flags & K["tele_flags"].get("night", 1))
        if night != self.night_mode:
            self.night_mode = night
            self.on_event("night", {"on": night})
        sos_pending = bool(flags & K["tele_flags"].get("sos_pending", 2))
        if sos_pending != self.sos_pending:
            self.sos_pending = sos_pending
            self.on_event("sos_pending", {"on": sos_pending})

    def _on_evt(self, obj: dict) -> None:
        self.stats.evts += 1
        if obj.get("veto"):
            log.debug("veto ch%d dur=%sms", obj.get("ch"), obj.get("d"))

    def _on_ges(self, obj: dict) -> None:
        g = str(obj.get("g", ""))
        self.stats.ges += 1
        self.stats.last_ges = g
        self.stats.last_ges_t = time.perf_counter()
        log.info("ges %s a=%s d=%s m=%s", g, obj.get("a"), obj.get("d"),
                 obj.get("m"))
        self.on_event("ges", dict(obj))

        if g == "MODE_SET":
            # device picker confirmed; adopt without echoing the command back
            self.set_mode(int(obj.get("m", self.mode)), notify_device=False)
            return
        if g == "ESTOP":
            self.stats.estops += 1
            self.emergency_release("ESTOP gesture")
            self._alarm("emergency stop")
            return
        if g == "SOS_CONFIRMED":
            self.stats.sos_events += 1
            self.emergency_release("SOS confirmed")
            self._alarm("SOS confirmed")
            return
        if g == "SOS_PENDING":
            self._maybe_tts("SOS pending - hold to cancel", "sos_pending", "sos")
            return
        if g == "SOS_CANCELLED":
            self._maybe_tts("SOS cancelled", "sos_cancelled", "sos")
            return
        if g == "BRUX":
            self._log_bruxism(obj)
            self._maybe_tts("bruxism detected", "bruxism", "brux")
            return
        if g in INFO_GESTURES:
            return
        # regular gesture -> routed actions for the active mode
        now = time.perf_counter()
        cmds = self.router.commands_for(g, now)
        if cmds:
            self.sink.apply(cmds)
        # tap releases are scheduled via router.due_releases() in _watchdogs

    def _on_ack(self, obj: dict) -> None:
        ok = bool(obj.get("ok"))
        if not ok:
            log.warning("device nack id=%s err=%s", obj.get("id"),
                        obj.get("err"))
        self.on_event("ack", dict(obj))

    # ------------------------------------------------------------- safety
    def _watchdogs(self, now: float, now_ms: float) -> None:
        # 1. scheduled tap releases
        rel = self.router.due_releases(now)
        if rel:
            self.sink.apply(rel)
        # 2. link silence -> release + degraded
        silence_ms = now_ms - self._last_line_ms
        # device tele is 30 Hz; allow ~1 s of total silence before release
        link_limit = 1000.0
        if self._last_line_ms > 0 and silence_ms > link_limit:
            if not self._link_dead:
                self._link_dead = True
                log.error("link silent %.0f ms - releasing all", silence_ms)
                self.emergency_release("link silence")
        elif self._link_dead and silence_ms <= link_limit:
            self._link_dead = False
            log.info("link recovered")
        # 3. foreground change -> release (opt-in, matches unbound safety)
        if self.release_on_focus and self._fg is not None:
            fg = self._fg()
            if fg and fg != self._fg_last:
                if self._fg_last:
                    log.info("foreground %s -> %s: release", self._fg_last, fg)
                    self.emergency_release("foreground change")
                self._fg_last = fg

    def _keepalive(self, now_ms: float) -> None:
        """Host->device ping so the device host_silent flag stays clear."""
        if not self.arbiter.any_open():
            return
        if now_ms - self._last_tx_ms >= self.keepalive_ms:
            th = self._ping_seq
            self._ping_seq += 1
            line = protocol.cmd_ping(th, cmd_id=f"ka{th}")
            if self.arbiter.send(line):
                self._last_tx_ms = now_ms
                self._pong_wait[th] = now_ms
                if len(self._pong_wait) > 32:
                    self._pong_wait.pop(next(iter(self._pong_wait)))

    # ------------------------------------------------------------- cues
    def _alarm(self, what: str) -> None:
        if self.siren is not None:
            self.siren.ring()
        if self.tts is not None:
            self.tts.say(what, "alarm", time.perf_counter())

    def _maybe_tts(self, text: str, key: str, kind: str) -> None:
        if self.tts is not None:
            self.tts.say(text, key, time.perf_counter())

    def _log_bruxism(self, obj: dict) -> None:
        """Append bruxism events to CSV (host-side; device has no storage)."""
        try:
            os.makedirs(os.path.dirname(self.bruxism_log) or ".", exist_ok=True)
            new = not os.path.exists(self.bruxism_log)
            with open(self.bruxism_log, "a", encoding="utf-8") as f:
                if new:
                    f.write("t_iso,gesture,amp,dur_ms,mode\n")
                import datetime
                f.write(f"{datetime.datetime.now().isoformat(timespec='milliseconds')},"
                        f"{obj.get('g','BRUX')},{obj.get('a',0)},"
                        f"{obj.get('d',0)},{self.mode}\n")
        except OSError as e:
            log.warning("bruxism log failed: %s", e)

    # ------------------------------------------------------------- helpers
    def send(self, line: str) -> bool:
        ok = self.arbiter.send(line)
        if ok:
            self._last_tx_ms = time.perf_counter() * 1000.0
        return ok

    def request_cal(self) -> None:
        self.send(protocol.cmd_cal_step("rest", cmd_id="cal0"))

    @property
    def cal_results(self) -> list[dict]:
        return list(self._cal_results)
