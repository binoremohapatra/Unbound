"""link.py - host-side transports and the dual-link arbiter.

Two physical transports may be open at once (USB serial + BT SPP, each a COM
port). The device duplicates every line to both with one global `seq`; the
arbiter dedupes by seq and fails over when the primary goes silent
(kLinkPrimarySilenceMs). Commands are written to the freshest transport only -
the device has no host-side dedupe.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Protocol

from .constants_gen import CONSTANTS
from . import protocol

log = logging.getLogger("omniband.link")

K = CONSTANTS
PRIMARY_SILENCE_MS = K["link"]["primary_silence_ms"]
BACKOFF_MIN_MS = K["link"]["reconnect_backoff_min_ms"]
BACKOFF_MAX_MS = K["link"]["reconnect_backoff_max_ms"]


class Transport(Protocol):
    name: str
    def is_open(self) -> bool: ...
    def open(self) -> bool: ...
    def close(self) -> None: ...
    def read_line(self, timeout: float = 0.0) -> Optional[bytes]: ...
    def write(self, line: str) -> bool: ...


class SerialTransport:
    """pyserial wrapper. COMx on Windows; /dev/tty* elsewhere."""

    def __init__(self, port: str, baud: int = K["link"]["baud_boot"],
                 name: str = "serial") -> None:
        self.port = port
        self.baud = baud
        self.name = name
        self._ser = None
        self._fail_count = 0

    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def open(self) -> bool:
        if self.is_open():
            return True
        try:
            import serial  # pyserial
            self._ser = serial.Serial(self.port, self.baud, timeout=0)
            self._fail_count = 0
            log.info("%s: opened %s @%d", self.name, self.port, self.baud)
            return True
        except Exception as e:  # noqa: BLE001 - log and retry with backoff
            self._fail_count += 1
            wait = min(BACKOFF_MAX_MS,
                       BACKOFF_MIN_MS * (2 ** min(self._fail_count, 4))) / 1000.0
            log.warning("%s: open %s failed (%s); retry in %.0f ms",
                        self.name, self.port, e, wait * 1000)
            time.sleep(wait)
            return False

    def close(self) -> None:
        try:
            if self._ser is not None:
                self._ser.close()
        except Exception as e:  # noqa: BLE001
            log.warning("%s: close failed: %s", self.name, e)
        self._ser = None

    def read_line(self, timeout: float = 0.0) -> Optional[bytes]:
        if not self.is_open():
            return None
        try:
            if self._ser.in_waiting <= 0:
                return None
            raw = self._ser.readline()
            return raw if raw else None
        except Exception as e:  # noqa: BLE001 - device unplugged etc.
            log.warning("%s: read failed: %s", self.name, e)
            self.close()
            return None

    def write(self, line: str) -> bool:
        if not self.is_open():
            return False
        try:
            self._ser.write(line.encode("utf-8", "replace"))
            self._ser.flush()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("%s: write failed: %s", self.name, e)
            self.close()
            return False


class SimTransport:
    """In-memory transport pair for --simulate. `peer` is the SimDevice."""

    def __init__(self, peer, name: str = "sim") -> None:
        self.peer = peer
        self.name = name
        self._open = False
        self._rx: list[bytes] = []
        self._lock = threading.Lock()

    def attach(self) -> None:
        self.peer.attach_host(self._push)

    def _push(self, line: bytes) -> None:
        with self._lock:
            self._rx.append(line)

    def is_open(self) -> bool:
        return self._open

    def open(self) -> bool:
        self._open = True
        self.attach()
        self.peer.start()
        return True

    def close(self) -> None:
        self._open = False
        self.peer.stop()

    def read_line(self, timeout: float = 0.0) -> Optional[bytes]:
        with self._lock:
            if self._rx:
                return self._rx.pop(0)
        return None

    def write(self, line: str) -> bool:
        if not self._open:
            return False
        self.peer.feed_line(line)
        return True


class Arbiter:
    """Owns transports, dedupes device lines by seq, picks the TX transport."""

    def __init__(self, transports: list[Transport],
                 on_line: Optional[Callable[[dict], None]] = None) -> None:
        self.transports = transports
        self.on_line = on_line or (lambda obj: None)
        self._dedupe = protocol.SeqDeduper(K["link"]["seq_window"])
        self._last_rx_ms: dict[str, float] = {}
        self._tx_pref = 0
        self.lock = threading.Lock()
        self.stats = {"lines_in": 0, "dupes": 0, "malformed": 0,
                      "failovers": 0}

    # ------------------------------------------------------------- IO
    def open_all(self) -> None:
        for t in self.transports:
            t.open()

    def close_all(self) -> None:
        for t in self.transports:
            t.close()

    def poll(self, now_ms: float) -> None:
        """Drain every transport; dispatch fresh device lines."""
        for t in self.transports:
            if not t.is_open():
                continue
            while True:
                raw = t.read_line()
                if raw is None:
                    break
                self._last_rx_ms[t.name] = now_ms
                self.stats["lines_in"] += 1
                obj = protocol.loads(raw)
                if obj is None:
                    self.stats["malformed"] += 1
                    continue
                seq = obj.get("seq")
                if seq is not None and not self._dedupe.fresh(seq):
                    self.stats["dupes"] += 1
                    continue
                try:
                    self.on_line(obj)
                except Exception:  # noqa: BLE001 - never let dispatch kill IO
                    log.exception("line dispatch failed")

    def send(self, line: str) -> bool:
        """Write to the freshest transport (failover on silence)."""
        with self.lock:
            t = self._pick_tx()
            if t is None:
                return False
            return t.write(line)

    def send_cmd(self, line: str) -> bool:
        return self.send(line)

    # ------------------------------------------------------------- failover
    def _pick_tx(self):
        now = time.monotonic() * 1000.0
        best, best_age = None, None
        for i, t in enumerate(self.transports):
            if not t.is_open():
                continue
            age = now - self._last_rx_ms.get(t.name, -1e18)
            # prefer transports that have ever spoken; silence => failover
            if best is None or age < best_age:
                best, best_age = t, age
        if best is not None and best_age > PRIMARY_SILENCE_MS:
            # all transports stale: still send on the first open one
            for t in self.transports:
                if t.is_open():
                    return t
        return best

    def freshest_transport_name(self) -> str:
        with self.lock:
            t = self._pick_tx()
            return t.name if t else ""

    def any_open(self) -> bool:
        return any(t.is_open() for t in self.transports)
