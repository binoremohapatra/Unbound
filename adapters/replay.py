"""adapters/replay.py (Binore): record a live session, replay it later through the whole pipeline.

Why: demo-day safety and deterministic tests. A recorded face-loss-while-holding session must
always end with zero keys held.

File format: JSONL, one SignalEvent per line: {"src","name","value","t","conf"}.
"""
from __future__ import annotations

import json
from typing import Optional

from core.events import SignalEvent


class ReplaySource:
    """Same poll() shape as the live adapters. Event times are rebased onto the live clock, so
    ages and timers behave exactly as they did when recorded (divide by `speed` to fast-forward)."""

    def __init__(self, path: str, speed: float = 1.0):
        self.speed = max(1e-3, speed)
        self._events: list[SignalEvent] = []
        with open(path, "r", encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    d = json.loads(line)
                    self._events.append(SignalEvent("replay", str(d["name"]), float(d["value"]),
                                                    float(d["t"]), float(d.get("conf", 1.0))))
                except (ValueError, KeyError) as e:
                    raise ValueError(f"{path}:{n}: bad replay line ({e})") from None
        self._events.sort(key=lambda e: e.t)
        self._i = 0
        self._t0 = self._events[0].t if self._events else 0.0
        self._base: Optional[float] = None
        self.name = "replay"
        self.is_mock = True                     # a recording is simulated input, never real
        self.mock_reason = "replay file"

    @property
    def finished(self) -> bool:
        return self._i >= len(self._events)

    def poll(self, now: float) -> list[SignalEvent]:
        if self._base is None:
            self._base = now
        out: list[SignalEvent] = []
        while self._i < len(self._events):
            e = self._events[self._i]
            due = self._base + (e.t - self._t0) / self.speed
            if due > now:
                break
            out.append(SignalEvent("replay", e.name, e.value, due, e.conf))
            self._i += 1
        return out

    def close(self) -> None:
        pass

    @property
    def duration_s(self) -> float:
        return 0.0 if not self._events else (self._events[-1].t - self._t0) / self.speed


class Recorder:
    def __init__(self, path: str):
        self._f = open(path, "w", encoding="utf-8")

    def write(self, ev: SignalEvent) -> None:
        self._f.write(json.dumps({"src": ev.src, "name": ev.name, "value": ev.value,
                                  "t": ev.t, "conf": ev.conf}) + "\n")

    def close(self) -> None:
        self._f.flush()
        self._f.close()
