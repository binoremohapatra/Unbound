"""Replay adapter: plays recorded landmark streams from disk for deterministic testing & replay."""

import json
import logging
from pathlib import Path
import threading
import time
from typing import Callable, Optional
from handsfree.core.events import SignalEvent, SignalType

logger = logging.getLogger("handsfree.adapter.replay")


class ReplayAdapter:
    """Replays recorded streams of SignalEvents from JSON Lines (.jsonl) files."""

    def __init__(
        self,
        recording_path: Path,
        speed_factor: float = 1.0,
        loop: bool = False,
        on_event: Optional[Callable[[SignalEvent], None]] = None,
        on_heartbeat: Optional[Callable[[], None]] = None,
    ):
        self.recording_path = Path(recording_path)
        self.speed_factor = speed_factor
        self.loop = loop
        self.on_event = on_event
        self.on_heartbeat = on_heartbeat

        self.is_running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.recording_path.exists():
            raise FileNotFoundError(f"Recording file not found: {self.recording_path}")

        self.is_running = True
        self._thread = threading.Thread(target=self._replay_loop, daemon=True, name="ReplayThread")
        self._thread.start()
        logger.info(f"Replay started for file: {self.recording_path.name}")

    def stop(self) -> None:
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        logger.info("Replay stopped.")

    def _replay_loop(self) -> None:
        while self.is_running:
            with open(self.recording_path, "r", encoding="utf-8") as f:
                last_ts: Optional[float] = None

                for line in f:
                    if not self.is_running:
                        break

                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)
                    event_ts = data.get("timestamp", time.time())

                    if last_ts is not None:
                        dt = (event_ts - last_ts) / max(0.1, self.speed_factor)
                        if 0 < dt < 1.0:
                            time.sleep(dt)

                    last_ts = event_ts

                    if self.on_heartbeat:
                        self.on_heartbeat()

                    if self.on_event:
                        event = SignalEvent(
                            signal_type=SignalType(data.get("signal_type", "head_pose")),
                            name=data.get("name", ""),
                            value=data.get("value"),
                            timestamp=time.time(),
                            metadata=data.get("metadata", {}),
                        )
                        self.on_event(event)

            if not self.loop:
                break

        logger.info("Replay completed.")

