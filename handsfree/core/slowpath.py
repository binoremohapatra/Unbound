"""Slowpath pipeline: maps facial blendshapes and discrete gestures into Intent events."""

from typing import Dict, List, Optional
from handsfree.core.events import Intent
from handsfree.core.calibration import CalibrationManager
from handsfree.core.filters import HysteresisFilter, DebounceFilter


class BlendshapeTracker:
    """State tracker for an individual blendshape gesture with hysteresis and debounce."""

    def __init__(self, name: str, activation_thresh: float, release_thresh: float, hold_sec: float = 0.05):
        self.name = name
        self.hysteresis = HysteresisFilter(low_threshold=release_thresh, high_threshold=activation_thresh)
        self.debounce = DebounceFilter(hold_seconds=hold_sec, cooldown_seconds=0.1)
        self.is_active = False

    def update(self, raw_value: float, timestamp: Optional[float] = None) -> bool:
        schmitt_out = self.hysteresis.update(raw_value)
        self.is_active = self.debounce.update(schmitt_out, timestamp)
        return self.is_active


class SlowPath:
    """Processes facial blendshapes and produces discrete gesture events."""

    def __init__(self, calibration: CalibrationManager):
        self.calibration = calibration
        self.trackers: Dict[str, BlendshapeTracker] = {}
        self._init_trackers()
        self.speech_active = False

    def _init_trackers(self) -> None:
        for name, cfg in self.calibration.profile.blendshapes.items():
            self.trackers[name] = BlendshapeTracker(
                name=name,
                activation_thresh=cfg.activation,
                release_thresh=cfg.release,
                hold_sec=cfg.min_hold_ms / 1000.0,
            )

    def set_speech_active(self, active: bool) -> None:
        """Inhibits facial gesture activations if user is talking."""
        self.speech_active = active

    def process_blendshapes(
        self,
        blendshapes: Dict[str, float],
        timestamp: Optional[float] = None,
    ) -> List[Intent]:
        intents: List[Intent] = []
        suppressed_gestures = {"jawOpen", "mouthPucker", "mouthSmileLeft", "mouthSmileRight"}

        for name, value in blendshapes.items():
            if name not in self.trackers:
                cfg = self.calibration.get_threshold(name)
                self.trackers[name] = BlendshapeTracker(
                    name=name,
                    activation_thresh=cfg.activation,
                    release_thresh=cfg.release,
                    hold_sec=cfg.min_hold_ms / 1000.0,
                )

            tracker = self.trackers[name]
            was_active = tracker.is_active
            is_active = tracker.update(value, timestamp)

            if self.speech_active and name in suppressed_gestures:
                is_active = False

            if is_active != was_active:
                intent_val = 1.0 if is_active else 0.0
                intents.append(
                    Intent(
                        action=name,
                        value=intent_val,
                        source="slowpath",
                        metadata={"raw": value, "state": "pressed" if is_active else "released"},
                    )
                )

        return intents

