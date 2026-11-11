"""tools/latency_probe.py (Binore): makes the gateway flush point visible for filming.

Two numbers exist and they are NOT the same thing:

  SOFTWARE-ONLY (honest lower bound, quotable in a report):
      camera-frame timestamp -> gateway flush(), measured inside the process.
      This is what LatencyLog.ev_ms_* reports: it starts when the adapter stamps the
      event and ends when flush() completes. It EXCLUDES sensor exposure, USB transfer,
      the game engine, and your screen's pixel response.

  END-TO-END (the number that matters for the demo, only measurable with a camera):
      face moves -> photons -> webcam -> our pipeline -> SendInput -> game reacts ->
      photons on screen -> phone camera filming in slow motion.
      This tool marks every flush with an on-screen flash + console marker + a
      millisecond-precision timestamp in probe_marks.jsonl so you can align the video
      frames with the software log afterwards.

Usage:
    python tools/latency_probe.py --replay demo.jsonl --speed 2 --minutes 1
    python tools/latency_probe.py --live --profile supertuxkart
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app
from config import AppConfig
from core import clock
from gateway import Gateway, RecordingBackend
from state_machine import SMConfig, StateMachine
from watchdog import FrameClock, LatencyLog, Watchdog

log = logging.getLogger("omnicontrol.probe")


class ProbeGateway(Gateway):
    """Gateway that marks every flush: console marker, optional beep, JSONL timestamp."""

    def __init__(self, backend, marks_path: str, flash: bool):
        super().__init__(backend)
        self._marks = open(marks_path, "a", encoding="utf-8")
        self._flash = flash
        self._last_mark = 0.0

    def flush(self, now: float) -> None:
        before = (frozenset(self.held), self.sent_moves,
                  len(getattr(self.backend, "log", ())))
        super().flush(now)
        after = (frozenset(self.held), self.sent_moves,
                 len(getattr(self.backend, "log", ())))
        if after == before:                            # idle tick: nothing to film
            return
        t = clock.now()
        if t - self._last_mark < 0.008:                # cap ~120 marks/s so frames stay countable
            return
        self._last_mark = t
        line = {"t": round(t, 4), "held": sorted(self.held)}
        self._marks.write(json.dumps(line) + "\n")
        self._marks.flush()
        if self._flash:
            sys.stdout.write("\r\x1b[2J\x1b[H>>> FLUSH <<<   ")
            sys.stdout.flush()
            try:
                sys.stdout.write("\a")
                sys.stdout.flush()
            except Exception:
                pass

    def close(self):
        self._marks.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniControl latency probe")
    ap.add_argument("--minutes", type=float, default=1.0)
    ap.add_argument("--replay")
    ap.add_argument("--speed", type=float, default=2.0)
    ap.add_argument("--hz", type=float, default=60.0)
    ap.add_argument("--profile", default="default")
    ap.add_argument("--marks", default="probe_marks.jsonl")
    ap.add_argument("--no-flash", action="store_true", help="JSONL marks only, no console flash")
    a = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)
    cfg = AppConfig()
    gw = ProbeGateway(RecordingBackend(), a.marks, flash=not a.no_flash)
    sm = StateMachine(SMConfig(cancel_events=tuple(cfg.cancel_events)))
    latency = LatencyLog()
    mapper = app._load_mapper(a.profile)
    producers = app._load_producers() or [app.BasicAssist()]
    engine = app.Engine(sm, mapper, gw, producers, latency, cfg=cfg)

    if a.replay:
        class _Once:
            def __init__(self, p, s):
                self._s = app.ReplaySource(p, s)
            def poll(self, now): return self._s.poll(now)
            def close(self): return None
            @property
            def finished(self): return self._s.finished
        sources = [_Once(a.replay, a.speed)]
    else:
        sources = app._load_live_sources(live=True)
    if not sources:
        log.error("no sources: pass --replay or use --live adapters")
        return 2

    wd = Watchdog(check_ms=10)
    wd.add_channel("loop", cfg.watchdog_ms, lambda n: gw.release_all("deadman: probe"))
    wd.start()
    fc = FrameClock(a.hz)
    t_end = clock.now() + a.minutes * 60.0
    next_report = clock.now() + 5.0
    try:
        while clock.now() < t_end:
            now = fc.wait()
            events = []
            for s in sources:
                try:
                    events.extend(s.poll(now))
                except Exception:
                    pass
            engine.step(events, now)
            wd.beat("loop")
            if now >= next_report:
                s = latency.summary()
                print(f"probe fps={s['fps']} ev_ms_p50={s['ev_ms_p50']} "
                      f"ev_ms_p95={s['ev_ms_p95']} ev_ms_max={s['ev_ms_max']} "
                      f"(software-only; see docstring for the honest number)")
                next_report = now + 5.0
    finally:
        wd.stop()
        gw.release_all("probe end")
        gw.close()
        latency.close()
    print(f"marks written to {a.marks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
