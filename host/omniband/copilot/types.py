"""copilot/types.py - Core data types for AI Racing Co-Pilot.

Frozen dataclasses without I/O. The co-pilot controller consumes ONLY these types;
it does not know which hardware tier, optical fallback, or game adapter produced them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class VehicleState:
    """Snapshot of vehicle physics and road geometry from game telemetry."""
    t: float                       # time.perf_counter() at receipt (seconds)
    speed: float                   # longitudinal speed (m/s)
    e: float                       # lateral error from lane centre (metres, +right)
    psi_e: float                   # heading error relative to road tangent (rad)
    kappa_ahead: Tuple[float, ...] # curvature samples at lookahead distances (1/m)
    lane_index: int                # active lane index (0 = leftmost)
    lane_count: int                # total available lanes
    gaps: Tuple[Optional[Tuple[float, float]], ...] # per-lane (distance_m, closing_speed_mps) or None
    offroad: bool                  # vehicle wheels off drivable road surface
    paused: bool                   # game is in pause menu or loading screen
    steer_applied: Optional[float] = None # actual steer feedback if provided [-1, 1]
    source_id: str = "arena"       # telemetry source identifier


@dataclass(frozen=True)
class HumanIntent:
    """Decoded somatic intention from human inputs (Piezo, Optical, Hybrid, Sim)."""
    lateral_bias: float            # desired offset within lane [-1, 1]
    lane_step: int                 # discrete lane change request {-1, 0, 1}
    cruise_on: bool                # cruise speed control latch
    v_set: float                   # target cruise speed (m/s)
    req_drift: bool                # edge-triggered handbrake / drift request
    req_nitro: bool                # edge-triggered boost / nitro request
    assist_level: str              # "OFF" | "LANE" | "FULL"
    source_tier: str               # "T1_USB" | "T1_BT" | "T2_HYBRID" | "T3_OPTICAL" | "T4_SIM" | "LIMP_HOME"
    t: float                       # time.perf_counter() when decoded


@dataclass(frozen=True)
class ActuatorCommand:
    """Normalized control commands output by the co-pilot controller."""
    steer: float                   # steering command [-1.0, 1.0] (+right)
    throttle: float                # throttle demand [0.0, 1.0]
    brake: float                   # braking demand [0.0, 1.0]
    handbrake_pulse_ms: int        # momentary handbrake pulse duration (ms)
    nitro_pulse_ms: int            # momentary nitro pulse duration (ms)
    reasons: Tuple[str, ...]       # human-readable operational reason codes

