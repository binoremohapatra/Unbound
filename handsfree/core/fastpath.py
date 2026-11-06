"""Fastpath pipeline: low-latency conversion of head pose to analog stick vectors."""

from typing import Optional, Tuple
from handsfree.core.events import Intent
from handsfree.core.calibration import CalibrationManager
from handsfree.core.filters import OneEuroFilter


class FastPath:
    """Processes head pose (yaw/pitch/roll) with minimal latency to produce stick vectors.
    
    Target latency budget: < 10ms.
    Applies 1€ filter, deadzone, curve response, and emits steering/aiming intents.
    """

    def __init__(
        self,
        calibration: CalibrationManager,
        curve_exponent: float = 1.2,
        invert_x: bool = False,
        invert_y: bool = False,
    ):
        self.calibration = calibration
        self.curve_exponent = curve_exponent
        self.invert_x = invert_x
        self.invert_y = invert_y

        self.filter_yaw = OneEuroFilter(freq=60.0, min_cutoff=1.5, beta=0.015)
        self.filter_pitch = OneEuroFilter(freq=60.0, min_cutoff=1.5, beta=0.015)

    def _apply_curve(self, val: float) -> float:
        sign = 1.0 if val >= 0 else -1.0
        return sign * (abs(val) ** self.curve_exponent)

    def process_pose(
        self,
        raw_yaw: float,
        raw_pitch: float,
        raw_roll: float = 0.0,
        timestamp: Optional[float] = None,
    ) -> Tuple[Intent, Intent]:
        smooth_yaw = self.filter_yaw.filter(raw_yaw, timestamp)
        smooth_pitch = self.filter_pitch.filter(raw_pitch, timestamp)

        norm_x = self.calibration.normalize_axis(smooth_yaw, self.calibration.profile.yaw)
        norm_y = self.calibration.normalize_axis(smooth_pitch, self.calibration.profile.pitch)

        curved_x = self._apply_curve(norm_x)
        curved_y = self._apply_curve(norm_y)

        if self.invert_x:
            curved_x = -curved_x
        if self.invert_y:
            curved_y = -curved_y

        curved_x = max(-1.0, min(1.0, curved_x))
        curved_y = max(-1.0, min(1.0, curved_y))

        intent_x = Intent(
            action="stick_x",
            value=curved_x,
            source="fastpath",
            metadata={"raw": raw_yaw, "smooth": smooth_yaw},
        )
        intent_y = Intent(
            action="stick_y",
            value=curved_y,
            source="fastpath",
            metadata={"raw": raw_pitch, "smooth": smooth_pitch},
        )

        return intent_x, intent_y

    def reset(self) -> None:
        self.filter_yaw.reset()
        self.filter_pitch.reset()

