"""adapters/telemetry/forza_dataout.py - Forza Horizon 5 / Motorsport UDP Telemetry Parser (RG-03).

Detects packet length:
- 324 bytes: Forza Motorsport / Horizon "dash" format
- 323 bytes: Legacy sled format
Unpacks float fields into normalized VehicleState.
"""
from __future__ import annotations

import socket
import struct
import threading
from typing import Optional, Tuple
from .base import TelemetryAdapter
from ...copilot.types import VehicleState


class ForzaTelemetryAdapter(TelemetryAdapter):
    """Listens on UDP port (default 5300) for Forza Data Out packets."""

    def __init__(self, port: int = 5300, stale_timeout_s: float = 0.200):
        self.port = port
        self.stale_timeout_s = stale_timeout_s
        self._lock = threading.Lock()
        self._latest_state: Optional[VehicleState] = None
        self._last_rx_t = 0.0
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind(("127.0.0.1", self.port))
            self._sock.settimeout(0.1)
            self._running = True
            self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="forza_udp")
            self._thread.start()
        except OSError:
            pass

    def _listen_loop(self) -> None:
        while self._running and self._sock:
            try:
                data, _ = self._sock.recvfrom(512)
                self.parse_packet(data)
            except (socket.timeout, OSError):
                continue

    def parse_packet(self, data: bytes, now: float = 0.0) -> Optional[VehicleState]:
        if len(data) not in (311, 323, 324):
            return None

        try:
            # Forza Dash packet layout (324 bytes):
            # s32 is_race_on; u32 timestamp_ms;
            # f32 engine_max_rpm, engine_idle_rpm, current_engine_rpm;
            # f32 accel_x, accel_y, accel_z;
            # f32 vel_x, vel_y, vel_z;
            # f32 angular_vel_x, angular_vel_y, angular_vel_z;
            # f32 yaw, pitch, roll;
            is_race_on = struct.unpack_from("<i", data, 0)[0]
            if is_race_on == 0:
                return None

            # Velocities at offset 32 (vx, vy, vz)
            vx, vy, vz = struct.unpack_from("<fff", data, 32)
            speed_mps = (vx**2 + vy**2 + vz**2) ** 0.5

            # Steer input at offset 316 (s8 normalized) or offset 312
            steer_val = 0.0
            if len(data) >= 324:
                steer_raw = struct.unpack_from("<b", data, 316)[0]
                steer_val = steer_raw / 127.0

            state = VehicleState(
                t=now,
                speed=float(speed_mps),
                e=0.0,
                psi_e=0.0,
                kappa_ahead=(0.0,),
                lane_index=1,
                lane_count=3,
                gaps=(None, None, None),
                offroad=False,
                paused=False,
                steer_applied=steer_val,
                source_id="forza_dataout",
            )
            with self._lock:
                self._latest_state = state
                self._last_rx_t = now
            return state
        except Exception:
            return None

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
        self._running = False
        if self._sock:
            self._sock.close()
            self._sock = None

