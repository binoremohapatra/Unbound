"""serial_manager.py - Threaded Serial & Transport Manager for Piezo Sensor Testing.

Handles USB COM port discovery, robust streaming, error recovery, and auto-reconnection.
"""
from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from typing import Callable, List, Optional, Tuple

import serial
import serial.tools.list_ports

from .config import DEFAULT_BAUD, DEFAULT_TIMEOUT, WIFI_UDP_PORT

log = logging.getLogger("piezo_testing.serial_manager")


def list_serial_ports() -> List[Tuple[str, str, str]]:
    """Enumerate all available physical and virtual COM ports."""
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append((p.device, p.description or "Generic Serial Device", p.hwid or ""))
    return ports


class SerialManager:
    """Manages thread-safe communication over USB Serial with error recovery and reconnection."""

    def __init__(
        self,
        port: str = "",
        baudrate: int = DEFAULT_BAUD,
        on_status_change: Optional[Callable[[bool, str], None]] = None,
        max_queue_size: int = 4000,
    ) -> None:
        self.port: str = port
        self.baudrate: int = baudrate
        self.on_status_change = on_status_change

        self._ser: Optional[serial.Serial] = None
        self._udp_sock: Optional[socket.socket] = None
        self._running: bool = False
        self._connected: bool = False
        self._is_wifi_mode: bool = False

        self._lock = threading.RLock()
        self._worker_thread: Optional[threading.Thread] = None

        # Unbounded queue prevention: drop oldest packets if GUI stalls
        self.raw_queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self.bytes_received: int = 0
        self.packets_received: int = 0
        self.error_count: int = 0
        self.reconnect_attempts: int = 0

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def connect(self, port: str, baudrate: int = DEFAULT_BAUD) -> bool:
        """Connect to the specified serial port or Wi-Fi UDP listener."""
        with self._lock:
            if self._connected:
                self.disconnect()

            self.port = port
            self.baudrate = baudrate
            self._running = True

            if port.upper().startswith("WIFI") or port.upper() == "UDP":
                self._is_wifi_mode = True
                return self._start_udp_mode()
            else:
                self._is_wifi_mode = False
                return self._start_serial_mode()

    def disconnect(self) -> None:
        """Safely close active connections and terminate the worker thread."""
        with self._lock:
            self._running = False
            self._connected = False

            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception as e:
                    log.debug("Error closing serial port: %s", e)
                self._ser = None

            if self._udp_sock is not None:
                try:
                    self._udp_sock.close()
                except Exception as e:
                    log.debug("Error closing UDP socket: %s", e)
                self._udp_sock = None

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
            self._worker_thread = None

        self._emit_status(False, "Disconnected")

    def _start_serial_mode(self) -> bool:
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=DEFAULT_TIMEOUT,
                write_timeout=DEFAULT_TIMEOUT,
            )
            # Flush existing hardware buffers
            self._ser.reset_input_buffer()
            self._connected = True
            self.reconnect_attempts = 0
            self._emit_status(True, f"Connected to {self.port} @ {self.baudrate} baud")

            self._worker_thread = threading.Thread(
                target=self._serial_worker_loop, daemon=True, name="PiezoSerialWorker"
            )
            self._worker_thread.start()
            return True
        except Exception as e:
            self.error_count += 1
            self._connected = False
            self._ser = None
            self._emit_status(False, f"Connection Failed: {e}")
            return False

    def _start_udp_mode(self) -> bool:
        try:
            self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, "SO_REUSEADDR"):
                self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_BROADCAST"):
                self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._udp_sock.settimeout(0.5)
            self._udp_sock.bind(("", WIFI_UDP_PORT))

            self._connected = True
            self._emit_status(True, f"Connected to Wi-Fi UDP on port {WIFI_UDP_PORT}")

            self._worker_thread = threading.Thread(
                target=self._udp_worker_loop, daemon=True, name="PiezoUdpWorker"
            )
            self._worker_thread.start()
            return True
        except Exception as e:
            self.error_count += 1
            self._connected = False
            self._udp_sock = None
            self._emit_status(False, f"Wi-Fi UDP Bind Failed: {e}")
            return False

    def _serial_worker_loop(self) -> None:
        """Continuous serial read loop with graceful reconnection."""
        while self._running:
            ser = self._ser
            if not ser or not ser.is_open:
                # Attempt auto-reconnect if unexpectedly disconnected
                if self._running:
                    self._attempt_reconnect()
                time.sleep(0.5)
                continue

            try:
                line = ser.readline()
                if line:
                    self.bytes_received += len(line)
                    self.packets_received += 1
                    text = line.decode("utf-8", errors="replace").strip()
                    if text:
                        self._enqueue_line(text, time.perf_counter())
            except serial.SerialException as se:
                log.warning("Serial read exception on %s: %s", self.port, se)
                self.error_count += 1
                self._connected = False
                self._emit_status(False, f"Serial Error: {se}. Reconnecting...")
                try:
                    if self._ser:
                        self._ser.close()
                except Exception:
                    pass
                self._ser = None
                time.sleep(1.0)
            except Exception as e:
                log.debug("Serial loop error: %s", e)
                self.error_count += 1
                time.sleep(0.01)

    def _udp_worker_loop(self) -> None:
        """Continuous UDP receive loop."""
        # Broadcast ping to prompt device to respond
        try:
            ping_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ping_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            ping_sock.sendto(b"PING\n", ("255.255.255.255", WIFI_UDP_PORT))
            ping_sock.sendto(b"PING\n", ("192.168.4.1", WIFI_UDP_PORT))
            ping_sock.close()
        except Exception:
            pass

        while self._running:
            sock = self._udp_sock
            if not sock:
                break
            try:
                data, addr = sock.recvfrom(2048)
                if data:
                    self.bytes_received += len(data)
                    self.packets_received += 1
                    text = data.decode("utf-8", errors="replace").strip()
                    if text and text not in ("PING", "PONG"):
                        self._enqueue_line(text, time.perf_counter())
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    log.debug("UDP receive error: %s", e)
                time.sleep(0.05)

    def _attempt_reconnect(self) -> None:
        with self._lock:
            self.reconnect_attempts += 1
            log.info("Attempting reconnection #%d to %s...", self.reconnect_attempts, self.port)
            try:
                self._ser = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    timeout=DEFAULT_TIMEOUT,
                    write_timeout=DEFAULT_TIMEOUT,
                )
                self._connected = True
                self._emit_status(True, f"Reconnected to {self.port}")
                log.info("Successfully reconnected to %s", self.port)
            except Exception as e:
                self._connected = False
                self._ser = None
                self._emit_status(False, f"Reconnection #{self.reconnect_attempts} failed: {e}")

    def _enqueue_line(self, line: str, arrival_time: float) -> None:
        """Push decoded packet into thread-safe queue without blocking."""
        if self.raw_queue.full():
            try:
                # Discard oldest to keep freshest real-time data
                self.raw_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.raw_queue.put_nowait((line, arrival_time))
        except queue.Full:
            pass

    def _emit_status(self, connected: bool, message: str) -> None:
        if self.on_status_change:
            try:
                self.on_status_change(connected, message)
            except Exception as e:
                log.debug("Error in status callback: %s", e)
