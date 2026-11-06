"""Safety Watchdog: monitors adapter heartbeats and enforces emergency release on signal loss."""

import logging
import threading
import time
from typing import Callable, Optional
from handsfree.core.gateway import OutputGateway

logger = logging.getLogger("handsfree.watchdog")


class Watchdog:
    """Monitors heartbeat from pipeline adapters and triggers emergency release if signals stall."""

    def __init__(
        self,
        gateway: OutputGateway,
        timeout_seconds: float = 0.35,
        on_timeout: Optional[Callable[[], None]] = None,
    ):
        self.gateway = gateway
        self.timeout_seconds = timeout_seconds
        self.on_timeout = on_timeout
        self.last_heartbeat = time.time()
        self.is_running = False
        self.is_tripped = False
        self._thread: Optional[threading.Thread] = None

    def beat(self) -> None:
        self.last_heartbeat = time.time()
        if self.is_tripped:
            logger.info("Watchdog signal recovered.")
            self.is_tripped = False

    def start(self) -> None:
        self.is_running = True
        self.last_heartbeat = time.time()
        self.is_tripped = False
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="HandsfreeWatchdog")
        self._thread.start()
        logger.info(f"Watchdog active (timeout={self.timeout_seconds}s).")

    def stop(self) -> None:
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _monitor_loop(self) -> None:
        while self.is_running:
            time.sleep(0.05)
            elapsed = time.time() - self.last_heartbeat
            if elapsed > self.timeout_seconds and not self.is_tripped:
                logger.error(
                    f"Watchdog timeout! No heartbeat for {elapsed:.3f}s (> {self.timeout_seconds}s). "
                    f"Triggering emergency release."
                )
                self.is_tripped = True
                self.gateway.release_all()
                if self.on_timeout:
                    try:
                        self.on_timeout()
                    except Exception as e:
                        logger.error(f"Error in watchdog on_timeout callback: {e}")

