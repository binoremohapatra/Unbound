"""adapters/telemetry/arena_http.py - 60 Hz WebGL Arena HTTP/JSON telemetry listener (GM-06)."""
from __future__ import annotations

import threading
from typing import Optional, Tuple
from .base import TelemetryAdapter
from ...copilot.types import VehicleState


class ArenaTelemetryAdapter(TelemetryAdapter):
    """Receives 60 Hz VehicleState JSON pushes from the 3D WebGL Arena browser tab."""

    def __init__(self, stale_timeout_s: float = 0.150):
        self.stale_timeout_s = stale_timeout_s
        self._lock = threading.Lock()
        self._latest_state: Optional[VehicleState] = None
        self._last_rx_t = 0.0

    def ingest_json(self, data: dict, now: float) -> bool:
        """Called by HTTP POST /api/telemetry or WebSocket endpoint."""
        try:
            kappa_ahead = tuple(float(k) for k in data.get("kappa_ahead", (0.0,)))
            raw_gaps = data.get("gaps", [])
            parsed_gaps: list[Optional[Tuple[float, float]]] = []
            for g in raw_gaps:
                if g and isinstance(g, (list, tuple)) and len(g) >= 2:
                    parsed_gaps.append((float(g[0]), float(g[1])))
                else:
                    parsed_gaps.append(None)

            state = VehicleState(
                t=now,
                speed=float(data.get("speed", 0.0)),
                e=float(data.get("e", 0.0)),
                psi_e=float(data.get("psi_e", 0.0)),
                kappa_ahead=kappa_ahead,
                lane_index=int(data.get("lane_index", 1)),
                lane_count=int(data.get("lane_count", 3)),
                gaps=tuple(parsed_gaps),
                offroad=bool(data.get("offroad", False)),
                paused=bool(data.get("paused", False)),
                steer_applied=data.get("steer_applied"),
                source_id="arena_webgl",
            )
            with self._lock:
                self._latest_state = state
                self._last_rx_t = now
            return True
        except Exception:
            return False

    def poll(self, now: float) -> Optional[VehicleState]:
        with self._lock:
            if self._latest_state is None:
                return None
            if (now - self._last_rx_t) > self.stale_timeout_s:
                return None
            return self._latest_state

    def is_healthy(self, now: float) -> bool:
        with self._lock:
            return (now - self._last_rx_t) <= self.stale_timeout_s

    def close(self) -> None:
        with self._lock:
            self._latest_state = None

