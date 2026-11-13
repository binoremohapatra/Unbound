"""optical_calibration.py - Dynamic Optical Calibration & Asymmetric Wink Profile.

Holds calibrated thresholds for:
- Left Eye Aspect Ratio (EAR) wink threshold & open companion floor
- Right Eye Aspect Ratio (EAR) wink threshold & open companion floor
- Neutral resting head anchor (cx0, cy0) & inter-ocular distance (scale_anchor)
- Head pitch tilt deadzone & scaling for proportional scroll/zoom
- Mouth Aspect Ratio (MAR) jaw click & drag threshold

Persists to config/optical_calibration.json.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("omniband.tracking.optical_calibration")

CONFIG_PATH = Path("config/optical_calibration.json")


@dataclass
class OpticalCalibrationProfile:
    """Optical threshold profile for asymmetric wink-clutch, pitch scroll, and doctor-zoom."""
    # Left eye wink thresholds (for Scroll Clutch: Left Closed + Right Open)
    left_wink_thresh: float = 0.18
    right_open_floor: float = 0.22
    left_open_baseline: float = 0.28

    # Right eye wink thresholds (for Doctor Zoom Clutch: Right Closed + Left Open)
    right_wink_thresh: float = 0.18
    left_open_floor: float = 0.22
    right_open_baseline: float = 0.28

    # Asymmetry margin between eyes
    asym_margin: float = 0.05

    # Neutral head resting position & scale
    neutral_cx: float = 0.50
    neutral_cy: float = 0.45
    baseline_eye_dist: float = 0.12

    # Pitch dynamics for Scroll and Zoom
    pitch_deadzone: float = 0.012
    pitch_scroll_scale: float = 1400.0
    jaw_click_thresh: float = 0.22

    # Clutch hold duration required before engaging (ms)
    clutch_min_hold_ms: float = 220.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> OpticalCalibrationProfile:
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: float(v) for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class OpticalCalibrator:
    """Manages recording, computing, and persisting optical facial thresholds."""

    def __init__(self, config_path: Path = CONFIG_PATH) -> None:
        self.config_path = config_path
        self.profile = self.load()

    def load(self) -> OpticalCalibrationProfile:
        if self.config_path.is_file():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                prof = OpticalCalibrationProfile.from_dict(data)
                log.info("Loaded optical calibration profile from %s", self.config_path)
                return prof
            except Exception as e:
                log.warning("Failed loading %s (%s); using defaults", self.config_path, e)
        return OpticalCalibrationProfile()

    def save(self) -> bool:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.profile.to_dict(), f, indent=2)
            log.info("Saved optical calibration profile to %s", self.config_path)
            return True
        except Exception as e:
            log.error("Failed saving optical calibration: %s", e)
            return False

    def reset_defaults(self) -> None:
        self.profile = OpticalCalibrationProfile()
        self.save()

    def compute_from_records(
        self,
        baseline_left: list[float],
        baseline_right: list[float],
        baseline_nose: list[tuple[float, float]],
        baseline_dist: list[float],
        wink_l_left: list[float],
        wink_l_companion_r: list[float],
        wink_r_right: list[float],
        wink_r_companion_l: list[float],
        jaw_samples: list[float],
    ) -> OpticalCalibrationProfile:
        """Derive calibrated thresholds using standard formulas."""
        # Baseline eyes open
        open_l = float(sum(baseline_left) / len(baseline_left)) if baseline_left else 0.28
        open_r = float(sum(baseline_right) / len(baseline_right)) if baseline_right else 0.28

        # Left wink (Scroll)
        winked_l = float(min(wink_l_left)) if wink_l_left else 0.12
        comp_r = float(sum(wink_l_companion_r) / len(wink_l_companion_r)) if wink_l_companion_r else open_r

        # Right wink (Zoom)
        winked_r = float(min(wink_r_right)) if wink_r_right else 0.12
        comp_l = float(sum(wink_r_companion_l) / len(wink_r_companion_l)) if wink_r_companion_l else open_l

        # Compute optimal boundaries
        # left_wink_thresh = (open_ear_L + winked_ear_L) * 0.52
        # right_open_floor = companion_ear_R * 0.82
        l_thresh = max(0.12, min(0.24, (open_l + winked_l) * 0.52))
        r_floor = max(0.14, min(0.26, comp_r * 0.82))

        r_thresh = max(0.12, min(0.24, (open_r + winked_r) * 0.52))
        l_floor = max(0.14, min(0.26, comp_l * 0.82))

        # Neutral nose & scale
        if baseline_nose:
            cx = float(sum(p[0] for p in baseline_nose) / len(baseline_nose))
            cy = float(sum(p[1] for p in baseline_nose) / len(baseline_nose))
        else:
            cx, cy = 0.50, 0.45

        eye_d = float(sum(baseline_dist) / len(baseline_dist)) if baseline_dist else 0.12

        # Jaw threshold
        jaw_peak = float(max(jaw_samples)) if jaw_samples else 0.28
        jaw_thr = max(0.16, min(0.35, jaw_peak * 0.70))

        self.profile = OpticalCalibrationProfile(
            left_wink_thresh=round(l_thresh, 3),
            right_open_floor=round(r_floor, 3),
            left_open_baseline=round(open_l, 3),
            right_wink_thresh=round(r_thresh, 3),
            left_open_floor=round(l_floor, 3),
            right_open_baseline=round(open_r, 3),
            asym_margin=0.045,
            neutral_cx=round(cx, 3),
            neutral_cy=round(cy, 3),
            baseline_eye_dist=round(eye_d, 3),
            jaw_click_thresh=round(jaw_thr, 3),
        )
        self.save()
        return self.profile
