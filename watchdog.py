"""watchdog.py (Binore): deadman timer + latency logging.

Watchdog: a background thread. If the main loop stops beating (frozen, GC pause, GPU stall,
exception swallowed somewhere), it calls the trip callback FROM ITS OWN THREAD, which releases
every key. This is the only thing that can save the user when the main loop itself is stuck.

Channels are armed on their first beat, so a source that never starts (no webcam plugged in)
does not trip anything.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

from core.clock import now as clock_now

log = logging.getLogger("omnicontrol.watchdog")


class Watchdog:
    def __init__(self, check_ms: int = 20):
        self._period = check_ms / 1000.0
        self._timeout: dict[str, float] = {}
        self._cb: dict[str, Callable[[str], None]] = {}
        self._last: dict[str, float] = {}
        self._tripped: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)

    def add_channel(self, name: str, timeout_ms: int, on_trip: Callable[[str], None]) -> None:
        with self._lock:
            self._timeout[name] = timeout_ms / 1000.0
            self._cb[name] = on_trip

    def beat(self, name: str) -> None:
        t = clock_now()
        with self._lock:
            self._last[name] = t
            self._tripped.discard(name)          # re-arm after recovery

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def check(self, t: Optional[float] = None) -> list[str]:
        """One pass. Public so tests can drive it without a thread."""
        t = clock_now() if t is None else t
        fire: list[tuple[str, Callable]] = []
        with self._lock:
            for name, last in self._last.items():
                if name in self._tripped:
                    continue
                if t - last > self._timeout.get(name, 1e9):
                    self._tripped.add(name)
                    fire.append((name, self._cb[name]))
        for name, cb in fire:
            log.error("watchdog: channel %r silent, tripping", name)
            try:
                cb(name)
            except Exception:
                log.exception("watchdog callback failed")
        return [n for n, _ in fire]

    def _run(self) -> None:
        while not self._stop.wait(self._period):
            self.check()


class LatencyLog:
    """Ring buffer of per-tick timings. ev_to_out = event timestamp -> command flushed (ms).
    tick = time spent inside one engine step (ms)."""

    def __init__(self, size: int = 600, jsonl_path: Optional[str] = None, every_s: float = 1.0):
        self._ev: deque = deque(maxlen=size)
        self._tick: deque = deque(maxlen=size)
        self._f = open(jsonl_path, "a", encoding="utf-8") if jsonl_path else None
        self._every = every_s
        self._next = 0.0
        self.frames = 0
        self._t0: Optional[float] = None
        self._fps_frames = 0
        self._fps = 0.0

    def record(self, now: float, tick_ms: float, ev_to_out_ms: Optional[float]) -> None:
        self._tick.append(tick_ms)
        if ev_to_out_ms is not None:
            self._ev.append(ev_to_out_ms)
        self.frames += 1
        if self._t0 is None:
            self._t0, self._next = now, now + self._every
        self._fps_frames += 1
        if now >= self._next:
            self._fps = self._fps_frames / max(1e-6, now - self._t0)
            self._t0, self._fps_frames, self._next = now, 0, now + self._every
            if self._f:
                self._f.write(json.dumps({"t": round(now, 3), **self.summary()}) + "\n")
                self._f.flush()

    @staticmethod
    def _pct(data, p: float) -> float:
        if not data:
            return 0.0
        s = sorted(data)
        return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))]

    def summary(self) -> dict:
        return {
            "fps": round(self._fps, 1),
            "tick_ms_p50": round(self._pct(self._tick, 50), 3),
            "tick_ms_p99": round(self._pct(self._tick, 99), 3),
            "ev_ms_p50": round(self._pct(self._ev, 50), 2),
            "ev_ms_p95": round(self._pct(self._ev, 95), 2),
            "ev_ms_max": round(max(self._ev), 2) if self._ev else 0.0,
        }

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None


class FrameClock:
    """Fixed-rate loop timing: sleep most of the wait, spin the last ~1.5 ms.
    On Windows, call winmm.timeBeginPeriod(1) once at startup so sleep() is accurate."""

    def __init__(self, hz: float = 60.0, spin_s: float = 0.0015):
        self.period = 1.0 / hz
        self.spin = spin_s
        self._next = clock_now() + self.period
        self.overruns = 0

    def wait(self) -> float:
        t = clock_now()
        if t > self._next + self.period:       # fell behind: resync, do not burst to catch up
            self.overruns += 1
            self._next = t
        remaining = self._next - t
        if remaining > self.spin:
            time.sleep(remaining - self.spin)
        while clock_now() < self._next:
            pass
        self._next += self.period
        return clock_now()
