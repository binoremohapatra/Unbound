"""hardware_calibration.py - Hardware Auto-Calibration & Skull Cross-Talk Suppression.

Maintains:
1. Resting baseline (mean mu_i and noise std sigma_i).
2. Per-channel max flex (peak_i).
3. 4x4 cross-talk matrix C[i][j] (leakage into channel j when channel i is flexed).
4. Auto-gain normalization: gain_i = 0.85 / max(peak_i - mu_i, 1e-3).
5. Dynamic optimal threshold: thresh_i = max(mu_i + 4 * sigma_i, 0.42).
6. Cross-talk dominance gate: When CH4 (Jaw Clench) fires strongly (> 0.60),
   suppress leakage C[3][j] * ch4_val from CH1/CH2/CH3 within a 30 ms window.
7. Persistence to config/hardware_calibration.json (or fallback host/profiles).
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("omniband.hardware_calibration")

CONFIG_PATH = Path("config/hardware_calibration.json")
FALLBACK_CONFIG_PATH = Path("host/profiles/hardware_calibration.json")


def _get_save_path() -> Path:
    if CONFIG_PATH.parent.exists() or not FALLBACK_CONFIG_PATH.parent.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        return CONFIG_PATH
    return FALLBACK_CONFIG_PATH


@dataclass
class HardwareCalibrationProfile:
    mu: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    sigma: List[float] = field(default_factory=lambda: [0.01, 0.01, 0.01, 0.01])
    peak: List[float] = field(default_factory=lambda: [1.0, 1.0, 1.0, 1.0])
    gains: List[float] = field(default_factory=lambda: [0.85, 0.85, 0.85, 0.85])
    thresholds: List[float] = field(default_factory=lambda: [0.50, 0.50, 0.50, 0.50])
    # 4x4 cross-talk matrix: C[i][j] is leakage into j when i is triggered
    crosstalk: List[List[float]] = field(default_factory=lambda: [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ])
    calibrated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "mu": self.mu,
            "sigma": self.sigma,
            "peak": self.peak,
            "gains": self.gains,
            "thresholds": self.thresholds,
            "crosstalk": self.crosstalk,
            "calibrated_at": self.calibrated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> HardwareCalibrationProfile:
        return cls(
            mu=[float(x) for x in d.get("mu", [0.0] * 4)[:4]],
            sigma=[float(x) for x in d.get("sigma", [0.01] * 4)[:4]],
            peak=[float(x) for x in d.get("peak", [1.0] * 4)[:4]],
            gains=[float(x) for x in d.get("gains", [0.85] * 4)[:4]],
            thresholds=[float(x) for x in d.get("thresholds", [0.5] * 4)[:4]],
            crosstalk=[
                [float(c) for c in row[:4]] for row in d.get("crosstalk", [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ])[:4]
            ],
            calibrated_at=float(d.get("calibrated_at", 0.0)),
        )


class HardwareCalibrator:
    """Manages online calibration steps and real-time cross-talk suppression."""

    def __init__(self) -> None:
        self.profile = HardwareCalibrationProfile()
        self._last_jaw_clench_t = -1e9
        self.load()

    def reset_defaults(self) -> None:
        self.profile = HardwareCalibrationProfile()
        self.save()

    def load(self) -> bool:
        for p in (CONFIG_PATH, FALLBACK_CONFIG_PATH):
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.profile = HardwareCalibrationProfile.from_dict(data)
                    log.info("Loaded hardware calibration from %s", p)
                    return True
                except Exception as e:
                    log.warning("Failed to load calibration from %s: %s", p, e)
        return False

    def save(self) -> bool:
        p = _get_save_path()
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self.profile.to_dict(), f, indent=2)
            log.info("Saved hardware calibration to %s", p)
            return True
        except Exception as e:
            log.error("Failed to save hardware calibration to %s: %s", p, e)
            return False

    def compute_baseline(self, samples: List[List[float]]) -> None:
        """Step 1: Compute mean mu_i and noise std sigma_i from resting samples."""
        if not samples:
            return
        n = len(samples)
        for i in range(4):
            ch_vals = [s[i] for s in samples]
            mean = sum(ch_vals) / n
            var = sum((x - mean) ** 2 for x in ch_vals) / max(1, n - 1)
            std = math.sqrt(var)
            self.profile.mu[i] = mean
            self.profile.sigma[i] = max(1e-4, std)
            # Baseline threshold update
            self.profile.thresholds[i] = max(mean + 4.0 * self.profile.sigma[i], 0.42)

    def register_flex_step(self, target_ch: int, flex_peaks: List[List[float]]) -> None:
        """
        Record maximum flex peak on target_ch and compute cross-talk row C[target_ch][j].
        flex_peaks: list of 4-element samples captured during peak flexes.
        """
        if not flex_peaks or target_ch < 0 or target_ch >= 4:
            return

        # Find sample with highest target channel value
        best_sample = max(flex_peaks, key=lambda s: s[target_ch])
        target_amp = best_sample[target_ch] - self.profile.mu[target_ch]
        self.profile.peak[target_ch] = best_sample[target_ch]

        # Compute software gain so flex reaches ~0.85
        gain = 0.85 / max(target_amp, 1e-3)
        self.profile.gains[target_ch] = max(1e-5, min(100.0, gain))

        # Compute cross-talk ratios into other channels
        if target_amp > 1e-3:
            for j in range(4):
                if j == target_ch:
                    self.profile.crosstalk[target_ch][j] = 1.0
                else:
                    leak = max(0.0, best_sample[j] - self.profile.mu[j])
                    ratio = min(1.0, leak / target_amp)
                    self.profile.crosstalk[target_ch][j] = ratio

        # Recalculate optimal threshold
        self.profile.thresholds[target_ch] = max(
            self.profile.mu[target_ch] + 4.0 * self.profile.sigma[target_ch],
            0.42
        )
        self.profile.calibrated_at = time.time()

    def apply_filter(self, raw_channels: List[float], now: Optional[float] = None) -> List[float]:
        """
        Apply software gains, baseline subtraction, and cross-talk dominance gate.
        Returns normalized 4-channel envelopes in range [0.0, 1.0].
        """
        if now is None:
            now = time.perf_counter()

        # Step A: Baseline subtract & software gain
        processed = [0.0] * 4
        for i in range(4):
            val = (raw_channels[i] - self.profile.mu[i]) * self.profile.gains[i]
            processed[i] = max(0.0, min(1.0, val))

        # Step B: Cross-Talk Dominance Gate
        # When CH4 (Jaw Clench) fires strongly (> 0.60), subtract leakage from CH1/CH2/CH3
        ch4_val = processed[3]
        if ch4_val > 0.60:
            self._last_jaw_clench_t = now

        # Within 30 ms of jaw clench, actively suppress leakage from other channels
        if (now - self._last_jaw_clench_t) <= 0.030:
            for j in (0, 1, 2):
                leakage = self.profile.crosstalk[3][j] * ch4_val
                processed[j] = max(0.0, processed[j] - leakage)

        return processed
