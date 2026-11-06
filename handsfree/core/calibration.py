"""Calibration manager for neutral pose, max ranges, and blendshape thresholds per user."""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Optional


@dataclass
class AxisRange:
    """Range configuration for a single degree of freedom."""
    neutral: float = 0.0
    min_val: float = -25.0
    max_val: float = 25.0
    deadzone: float = 3.0


@dataclass
class GestureThreshold:
    """Activation thresholds for blendshapes or audio triggers."""
    activation: float = 0.4
    release: float = 0.25
    min_hold_ms: float = 50.0


@dataclass
class CalibrationProfile:
    """Complete calibration profile for a user."""
    user_id: str = "default"
    yaw: AxisRange = field(default_factory=lambda: AxisRange(0.0, -25.0, 25.0, 3.0))
    pitch: AxisRange = field(default_factory=lambda: AxisRange(0.0, -20.0, 20.0, 3.0))
    roll: AxisRange = field(default_factory=lambda: AxisRange(0.0, -15.0, 15.0, 2.5))
    blendshapes: Dict[str, GestureThreshold] = field(default_factory=lambda: {
        "jawOpen": GestureThreshold(0.45, 0.25),
        "browInnerUp": GestureThreshold(0.40, 0.20),
        "mouthPucker": GestureThreshold(0.50, 0.30),
        "mouthSmileLeft": GestureThreshold(0.40, 0.20),
        "mouthSmileRight": GestureThreshold(0.40, 0.20),
        "eyeBlinkLeft": GestureThreshold(0.60, 0.30),
        "eyeBlinkRight": GestureThreshold(0.60, 0.30),
    })
    pop_energy_threshold: float = 0.08
    speech_vad_threshold: float = 0.5


class CalibrationManager:
    """Handles calibration procedures, normalization of sensor signals, and persistence."""

    def __init__(self, profile: Optional[CalibrationProfile] = None):
        self.profile = profile or CalibrationProfile()

    def set_neutral(self, yaw: float, pitch: float, roll: float) -> None:
        """Sets current head pose as user's neutral reference."""
        self.profile.yaw.neutral = yaw
        self.profile.pitch.neutral = pitch
        self.profile.roll.neutral = roll

    def normalize_axis(self, value: float, axis: AxisRange) -> float:
        """Normalizes an axis value to [-1.0, 1.0] relative to neutral with deadzone."""
        diff = value - axis.neutral

        if abs(diff) <= axis.deadzone:
            return 0.0

        if diff > 0:
            eff_max = axis.max_val - axis.neutral - axis.deadzone
            if eff_max <= 0:
                return 0.0
            norm = (diff - axis.deadzone) / eff_max
            return min(1.0, norm)
        else:
            eff_min = abs(axis.min_val - axis.neutral) - axis.deadzone
            if eff_min <= 0:
                return 0.0
            norm = (diff + axis.deadzone) / eff_min
            return max(-1.0, norm)

    def get_threshold(self, gesture_name: str) -> GestureThreshold:
        """Returns threshold config for a gesture, or a sensible default."""
        return self.profile.blendshapes.get(
            gesture_name,
            GestureThreshold(activation=0.4, release=0.25)
        )

    def save(self, filepath: Path) -> None:
        """Serializes calibration profile to JSON."""
        data = asdict(self.profile)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, filepath: Path) -> "CalibrationManager":
        """Loads calibration profile from JSON."""
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        profile = CalibrationProfile(
            user_id=data.get("user_id", "default"),
            yaw=AxisRange(**data.get("yaw", {})),
            pitch=AxisRange(**data.get("pitch", {})),
            roll=AxisRange(**data.get("roll", {})),
            blendshapes={
                k: GestureThreshold(**v) for k, v in data.get("blendshapes", {}).items()
            },
            pop_energy_threshold=data.get("pop_energy_threshold", 0.08),
            speech_vad_threshold=data.get("speech_vad_threshold", 0.5),
        )
        return cls(profile)

