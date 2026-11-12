"""adapters/telemetry/base.py - Base interface for real-time telemetry adapters (RG-01)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional
from ...copilot.types import VehicleState


class TelemetryAdapter(ABC):
    """Abstract interface for game telemetry sources."""

    @abstractmethod
    def poll(self, now: float) -> Optional[VehicleState]:
        """Polls for latest vehicle state. Returns None if no new state or paused."""
        raise NotImplementedError

    @abstractmethod
    def is_healthy(self, now: float) -> bool:
        """Returns True if telemetry stream is actively receiving frames."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Cleans up sockets, files, or background listener threads."""
        raise NotImplementedError

