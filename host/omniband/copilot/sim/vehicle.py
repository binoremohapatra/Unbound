"""copilot/sim/vehicle.py - Deterministic 120 Hz Python vehicle physics port.

Enables automated testing of Co-Pilot closed loop performance (T-02, T-03, T-04)
at 100x real-time speed in pytest without opening a browser. Matches Arena WebGL physics.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple
from ..types import VehicleState
from .track import TrackSpline


class SimVehicle:
    """Bicycle model vehicle dynamics with tire slip, steering slew, and drag."""

    def __init__(self, track: Optional[TrackSpline] = None, dt: float = 1.0 / 120.0):
        self.track = track or TrackSpline()
        self.dt = dt

        # Vehicle states
        self.s = 0.0              # longitudinal distance along track (m)
        self.speed = 0.0          # longitudinal speed (m/s)
        self.e = 0.0              # lateral error from lane centre (m, +right)
        self.psi_e = 0.0          # heading error relative to road tangent (rad)
        self.steer_applied = 0.0  # actual road wheel angle fraction [-1, 1]
        self.lane_index = 1       # middle lane
        self.offroad = False
        self.paused = False

        # Physics constants
        self.steer_slew_rate = 6.0     # 1/s ramp rate of steering
        self.max_steer_rad = 0.45      # max wheel angle ~ 25 degrees
        self.wheelbase = 2.7           # wheelbase L (m)
        self.mass = 1450.0             # kg
        self.engine_accel = 6.0        # m/s^2 at full throttle
        self.brake_decel = 12.0        # m/s^2 at full brake
        self.drag_coeff = 0.0025       # aerodynamic drag factor
        self.rolling_friction = 0.8    # m/s^2 rolling resistance

    def step(self, key_a: bool, key_d: bool, key_w: bool, key_s: bool,
             handbrake: bool = False, nitro: bool = False) -> None:
        """Advance vehicle simulation by one timestep dt (120 Hz)."""
        dt = self.dt

        # 1. Steering slew from digital keys
        target_steer = 0.0
        if key_d and not key_a:
            target_steer = 1.0
        elif key_a and not key_d:
            target_steer = -1.0

        if self.steer_applied < target_steer:
            self.steer_applied = min(target_steer, self.steer_applied + self.steer_slew_rate * dt)
        elif self.steer_applied > target_steer:
            self.steer_applied = max(target_steer, self.steer_applied - self.steer_slew_rate * dt)

        # 2. Longitudinal acceleration & drag
        accel = 0.0
        if key_w:
            accel += self.engine_accel * (1.5 if nitro else 1.0)
        if key_s:
            accel -= self.brake_decel
        if handbrake:
            accel -= self.brake_decel * 1.5

        # Resistance forces
        accel -= (self.rolling_friction + self.drag_coeff * (self.speed ** 2))
        self.speed = max(0.0, self.speed + accel * dt)

        # 3. Kinematic lateral error and yaw evolution
        kappa_road = self.track.get_curvature(self.s)
        delta_f = self.steer_applied * self.max_steer_rad

        # Yaw rate relative to road tangent: d(psi_e)/dt = v*(delta_f / L) - v*kappa_road
        yaw_rate_vehicle = self.speed * math.tan(delta_f) / self.wheelbase
        d_psi_e = yaw_rate_vehicle - self.speed * kappa_road
        self.psi_e += d_psi_e * dt

        # Lateral displacement velocity: de/dt = v * sin(psi_e)
        self.e += (self.speed * math.sin(self.psi_e)) * dt

        # Longitudinal progress along track
        self.s += (self.speed * math.cos(self.psi_e)) * dt

        # Check off-road boundary (road boundary is half_road)
        half_road = self.track.road_width / 2.0
        self.offroad = abs(self.e) > (half_road + 0.5)

    def to_vehicle_state(self, now: float) -> VehicleState:
        """Produces a VehicleState immutable dataclass."""
        curvatures = self.track.get_lookahead_curvatures(self.s, (15.0, 30.0, 60.0))
        return VehicleState(
            t=now,
            speed=self.speed,
            e=self.e,
            psi_e=self.psi_e,
            kappa_ahead=curvatures,
            lane_index=self.lane_index,
            lane_count=self.track.lane_count,
            gaps=((None), (None), (None)),
            offroad=self.offroad,
            paused=self.paused,
            steer_applied=self.steer_applied,
            source_id="sim_vehicle",
        )
