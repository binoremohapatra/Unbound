"""adapters/telemetry/recorded.py - Deterministic JSONL Replay Telemetry Adapter (RG-02)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional
from .base import TelemetryAdapter
from ...copilot.types import VehicleState


class RecordedTelemetryAdapter(TelemetryAdapter):
    """Replays pre-recorded golden telemetry frames from a JSONL file."""

    def __init__(self, file_path: str, loop: bool = True):
        self.file_path = file_path
        self.loop = loop
        self.frames: List[dict] = []
        self.index = 0
        self._load()

    def _load(self) -> None:
        p = Path(self.file_path)
        if not p.is_file():
            return
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        self.frames.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

    def poll(self, now: float) -> Optional[VehicleState]:
        if not self.frames:
            return None

        data = self.frames[self.index]
        self.index += 1
        if self.index >= len(self.frames):
            self.index = 0 if self.loop else len(self.frames) - 1

        kappa_ahead = tuple(float(k) for k in data.get("kappa_ahead", (0.0,)))
        return VehicleState(
            t=now,
            speed=float(data.get("speed", 20.0)),
            e=float(data.get("e", 0.0)),
            psi_e=float(data.get("psi_e", 0.0)),
            kappa_ahead=kappa_ahead,
            lane_index=int(data.get("lane_index", 1)),
            lane_count=int(data.get("lane_count", 3)),
            gaps=(None, None, None),
            offroad=bool(data.get("offroad", False)),
            paused=bool(data.get("paused", False)),
            steer_applied=data.get("steer_applied"),
            source_id="recorded_replay",
        )

    def is_healthy(self, now: float) -> bool:
        return len(self.frames) > 0

    def close(self) -> None:
        self.frames.clear()

