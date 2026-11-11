"""tools/soak.py (Binore): N-minute stability soak for the REAL engine stack.

Runs the same Engine + StateMachine + Gateway + mapper + producers as main.py, fed by either
a looping --replay file or the live adapters, and every 10 s prints:

    fps | tick p50/p99 ms | event->output p50/p95 ms | RSS MB | threads | overruns | wd trips

Also writes a CSV log and ends with PASS/FAIL:
    FAIL if RSS grows > 20% over the run, average fps < 55, or the watchdog trips at all.

--inject-stall sleeps the loop thread for 400 ms once (mid-run) and verifies the watchdog's
deadman release fired and nothing stayed held afterwards.

Usage:
    python tools/soak.py --replay demo.jsonl --minutes 10 --csv soak.csv
    python tools/soak.py --live --minutes 30 --inject-stall
    python tools/soak.py --replay demo.jsonl --dry-run        # RecordingBackend (default)
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app
from config import AppConfig
from core import clock
from gateway import Gateway, RecordingBackend, SendInputBackend
from state_machine import SMConfig, StateMachine
from watchdog import FrameClock, LatencyLog, Watchdog

log = logging.getLogger("omnicontrol.soak")


def _rss_mb() -> float:
    """Resident set size in MB. psutil if present, else Win32 psapi, else -1."""
    try:
        import psutil  # type: ignore
        return psutil.Process().memory_info().rss / 1e6
    except ImportError:
        pass
    if os.name == "nt":
        try:
            import ctypes
            class _PMC(ctypes.Structure):
                _fields_ = [(n, ctypes.c_size_t) for n in
                            ("cb", "PageFaultCount", "PeakWorkingSetSize", "WorkingSetSize",
                             "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                             "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                             "PagefileUsage", "PeakPagefileUsage")]
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(pmc)
            ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
            return pmc.WorkingSetSize / 1e6
        except Exception:
            return -1.0
    try:
        import resource  # type: ignore
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return -1.0


class LoopingReplay:
    """ReplaySource that restarts when the recording ends, so soak can run for hours."""

    def __init__(self, path: str, speed: float):
        self.path, self.speed = path, speed
        self._src = app.ReplaySource(path, speed)

    @property
    def finished(self):
        return self._src.finished

    @property
    def is_mock(self):
        return getattr(self._src, "is_mock", False)

    @property
    def mock_reason(self):
        return getattr(self._src, "mock_reason", "")

    def poll(self, now):
        if self._src.finished:
            self._src = app.ReplaySource(self.path, self.speed)
        return self._src.poll(now)

    def close(self):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniControl soak test")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--replay", help="jsonl recording to loop (omit for live adapters)")
    ap.add_argument("--speed", type=float, default=4.0, help="replay speed multiplier")
    ap.add_argument("--hz", type=float, default=60.0)
    ap.add_argument("--profile", default="default")
    ap.add_argument("--csv", default="", help="CSV output path")
    ap.add_argument("--inject-stall", action="store_true",
                    help="block the loop 400 ms once and verify the deadman release")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="RecordingBackend (default; no real input is injected)")
    ap.add_argument("--live-input", action="store_true",
                    help="use SendInputBackend (REAL key/mouse injection - careful)")
    a = ap.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = AppConfig()
    backend = SendInputBackend() if a.live_input else RecordingBackend()
    gw = Gateway(backend)
    gw.install_exit_handlers()
    sm = StateMachine(SMConfig(cancel_events=tuple(cfg.cancel_events)))
    latency = LatencyLog()
    mapper = app._load_mapper(a.profile)
    producers = app._load_producers() or [app.BasicAssist()]
    engine = app.Engine(sm, mapper, gw, producers, latency, cfg=cfg)

    sources = []
    if a.replay:
        sources = [LoopingReplay(a.replay, a.speed)]
    else:
        sources = app._load_live_sources(live=True)
    if not sources:
        log.error("no sources: pass --replay or run on a machine with adapters")
        return 2

    wd = Watchdog(check_ms=10)
    wd_trips: list = []
    wd.add_channel("loop", cfg.watchdog_ms,
                   lambda n: (wd_trips.append(n), gw.release_all("deadman: soak")))
    wd.start()

    fc = FrameClock(a.hz)
    t_end = clock.now() + a.minutes * 60.0
    next_report = clock.now() + 10.0
    stall_done = False
    stall_at = clock.now() + (a.minutes * 60.0) / 2.0 if a.inject_stall else None
    rows = []
    csv_f = open(a.csv, "w", newline="") if a.csv else None
    if csv_f:
        csv.writer(csv_f).writerow(
            ["t", "fps", "tick_p50", "tick_p99", "ev_p50", "ev_p95", "rss_mb", "threads",
             "overruns", "wd_trips", "held"])

    rss_first = _rss_mb()
    t_start = clock.now()
    try:
        while clock.now() < t_end:
            now = fc.wait()
            if stall_at is not None and not stall_done and now >= stall_at:
                stall_done = True
                log.warning("INJECTING 400 ms loop stall")
                time.sleep(0.4)                      # the loop thread itself blocks
            events = []
            for s in sources:
                try:
                    events.extend(s.poll(now))
                except Exception:
                    log.exception("source failed")
            engine.step(events, now)
            wd.beat("loop")
            if now >= next_report:
                s = latency.summary()
                rss = _rss_mb()
                row = [round(now - t_start, 1), s["fps"], s["tick_ms_p50"], s["tick_ms_p99"],
                       s["ev_ms_p50"], s["ev_ms_p95"], round(rss, 1), threading.active_count(),
                       fc.overruns, len(wd_trips), len(gw.held)]
                rows.append(row)
                print(f"soak t={row[0]:5.0f}s fps={row[1]:5.1f} tick_p99={row[3]:6.2f}ms "
                      f"ev_p95={row[5]:6.2f}ms rss={row[6]:6.1f}MB threads={row[7]} "
                      f"overruns={row[8]} wd_trips={row[9]} held={row[10]}")
                if csv_f:
                    csv.writer(csv_f).writerow(row)
                    csv_f.flush()
                next_report = now + 10.0
        # end-of-run stall verification: the loop is done, so stop the watchdog first -
        # otherwise the deadman correctly fires on the shutdown silence and inflates wd_trips
        if a.inject_stall:
            wd.stop()
            time.sleep(0.5)                          # let any in-flight watchdog check settle
            held_after = bool(gw.held)
            print(f"stall check: watchdog trips={len(wd_trips)} held_after={held_after}")
            if not wd_trips or held_after:
                print("STALL CHECK: FAIL (deadman did not release)")
                return 1
            print("STALL CHECK: PASS (deadman released everything)")
    finally:
        wd.stop()
        gw.release_all("soak end")
        for s in sources:
            try:
                s.close()
            except Exception:
                pass
        latency.close()
        if csv_f:
            csv_f.close()

    rss_last = _rss_mb()
    avg_fps = sum(r[1] for r in rows) / len(rows) if rows else 0.0
    fails = []
    if rss_first > 0 and rss_last > 0 and (rss_last - rss_first) / rss_first > 0.20:
        fails.append(f"memory grew {(rss_last - rss_first) / rss_first * 100:.0f}% "
                     f"({rss_first:.0f}->{rss_last:.0f} MB)")
    if rows and avg_fps < 55.0:
        fails.append(f"average fps {avg_fps:.1f} < 55")
    if wd_trips and not a.inject_stall:
        fails.append(f"watchdog tripped {len(wd_trips)} time(s) without --inject-stall")
    verdict = "PASS" if not fails else "FAIL: " + "; ".join(fails)
    print(f"SOAK {a.minutes}min avg_fps={avg_fps:.1f} rss {rss_first:.0f}->{rss_last:.0f}MB "
          f"overruns={fc.overruns} -> {verdict}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
