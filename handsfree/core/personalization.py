"""AI Adaptive Personalization Service: Learns user-specific accessibility preferences.

Features:
  - Monitors dwell click cancellations, double-blink false activations, and speech command habits.
  - Generates intelligent, non-intrusive accessibility recommendations.
  - Strict user-in-the-loop approval: Never modifies sensitive thresholds without explicit user consent.
  - Persistent profile storage in handsfree/data/user_profile.json.
  - Instant reset to factory accessibility defaults.
"""

from __future__ import annotations

from collections import Counter
import json
import logging
from pathlib import Path
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("handsfree.personalization")


@dataclass
class UserProfile:
    angle_range_x: float = 20.0
    angle_range_y: float = 15.0
    deadzone_deg: float = 2.5
    dwell_time_ms: float = 1500.0
    blink_sensitivity: float = 0.5
    speech_rate: int = 150
    osk_dwell_ms: float = 1500.0
    command_counts: Dict[str, int] = field(default_factory=dict)
    frequent_words: Dict[str, int] = field(default_factory=dict)
    dwell_misses: int = 0
    blink_triggers: int = 0
    session_count: int = 1


class AdaptivePersonalizationService:
    """Manages continuous user interaction profiling, threshold recommendations, and preference persistence."""

    PROFILE_PATH = Path(__file__).resolve().parent.parent / "data" / "user_profile.json"

    def __init__(self):
        self.profile = self._load_profile()
        self.pending_recommendations: List[Dict[str, Any]] = []
        self._recent_dwell_misses: int = 0
        self._recent_dwell_hits: int = 0
        self._last_analysis_time: float = time.time()

    def _load_profile(self) -> UserProfile:
        try:
            if self.PROFILE_PATH.exists():
                raw = json.loads(self.PROFILE_PATH.read_text(encoding="utf-8"))
                return UserProfile(
                    angle_range_x=float(raw.get("angle_range_x", 20.0)),
                    angle_range_y=float(raw.get("angle_range_y", 15.0)),
                    deadzone_deg=float(raw.get("deadzone_deg", 2.5)),
                    dwell_time_ms=float(raw.get("dwell_time_ms", 1500.0)),
                    blink_sensitivity=float(raw.get("blink_sensitivity", 0.5)),
                    speech_rate=int(raw.get("speech_rate", 150)),
                    osk_dwell_ms=float(raw.get("osk_dwell_ms", 1500.0)),
                    command_counts=dict(raw.get("command_counts", {})),
                    frequent_words=dict(raw.get("frequent_words", {})),
                    dwell_misses=int(raw.get("dwell_misses", 0)),
                    blink_triggers=int(raw.get("blink_triggers", 0)),
                    session_count=int(raw.get("session_count", 1)) + 1,
                )
        except Exception as e:
            logger.warning(f"Error loading user profile: {e}")
        return UserProfile()

    def save_profile(self) -> None:
        """Persists profile state to JSON."""
        try:
            self.PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = asdict(self.profile)
            self.PROFILE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error saving user profile: {e}")

    def reset_defaults(self) -> UserProfile:
        """Resets all settings back to clean default accessibility profile."""
        self.profile = UserProfile()
        self.pending_recommendations.clear()
        self._recent_dwell_misses = 0
        self._recent_dwell_hits = 0
        self.save_profile()
        logger.info("Personalization profile reset to defaults.")
        return self.profile

    def record_command(self, cmd_key: str) -> None:
        """Tracks command frequency to personalize voice shortcut suggestions."""
        clean = cmd_key.strip().lower()
        if clean:
            self.profile.command_counts[clean] = self.profile.command_counts.get(clean, 0) + 1
            self.save_profile()

    def record_word(self, word: str) -> None:
        """Tracks vocabulary frequency for the Smart Keyboard."""
        clean = word.strip().lower()
        if clean:
            self.profile.frequent_words[clean] = self.profile.frequent_words.get(clean, 0) + 1
            self.save_profile()

    def record_dwell_miss(self) -> None:
        """Called when dwell progress starts but aborts before triggering (cursor slipped)."""
        self.profile.dwell_misses += 1
        self._recent_dwell_misses += 1
        self._evaluate_heuristics()

    def record_dwell_success(self) -> None:
        """Called when a dwell click actuates cleanly."""
        self._recent_dwell_hits += 1
        self._evaluate_heuristics()

    def record_blink_trigger(self) -> None:
        """Tracks double-blink actuations."""
        self.profile.blink_triggers += 1

    def _evaluate_heuristics(self) -> None:
        """Analyzes interaction patterns to generate assistive recommendations."""
        now = time.time()
        if (now - self._last_analysis_time) < 10.0:
            return
        self._last_analysis_time = now

        # Heuristic 1: If dwell slips occur frequently (> 3 misses with low hits), recommend longer dwell time
        if self._recent_dwell_misses >= 3 and self.profile.dwell_time_ms < 2500.0:
            rec_id = "rec_dwell_increase"
            if not any(r["id"] == rec_id for r in self.pending_recommendations):
                new_val = min(2500.0, self.profile.dwell_time_ms + 300.0)
                self.pending_recommendations.append({
                    "id": rec_id,
                    "setting": "dwell_time_ms",
                    "title": "Increase Dwell Duration",
                    "current": self.profile.dwell_time_ms,
                    "recommended": new_val,
                    "reason": f"Detected {self._recent_dwell_misses} dwell slips. A slightly longer hover time prevents accidental clicks while repositioning.",
                })

        # Heuristic 2: If dwell hits are high with 0 misses, suggest faster dwell time for advanced users
        if self._recent_dwell_hits >= 12 and self._recent_dwell_misses == 0 and self.profile.dwell_time_ms > 900.0:
            rec_id = "rec_dwell_decrease"
            if not any(r["id"] == rec_id for r in self.pending_recommendations):
                new_val = max(900.0, self.profile.dwell_time_ms - 200.0)
                self.pending_recommendations.append({
                    "id": rec_id,
                    "setting": "dwell_time_ms",
                    "title": "Boost Dwell Speed",
                    "current": self.profile.dwell_time_ms,
                    "recommended": new_val,
                    "reason": "Tracking is highly steady. You can speed up click actuation by 200ms to type faster.",
                })

    def apply_recommendation(self, rec_id: str) -> bool:
        """Applies a user-approved recommendation to the active profile."""
        target_rec = None
        for r in self.pending_recommendations:
            if r["id"] == rec_id:
                target_rec = r
                break

        if not target_rec:
            return False

        setting_name = target_rec["setting"]
        new_val = target_rec["recommended"]

        if hasattr(self.profile, setting_name):
            setattr(self.profile, setting_name, new_val)
            self.pending_recommendations = [r for r in self.pending_recommendations if r["id"] != rec_id]
            self._recent_dwell_misses = 0
            self._recent_dwell_hits = 0
            self.save_profile()
            logger.info(f"Applied recommendation: {setting_name} -> {new_val}")
            return True

        return False

    def dismiss_recommendation(self, rec_id: str) -> None:
        """Dismisses an unneeded recommendation."""
        self.pending_recommendations = [r for r in self.pending_recommendations if r["id"] != rec_id]

    def update_setting(self, setting_name: str, value: Any) -> bool:
        """Directly updates a setting from the HUD settings sliders."""
        if hasattr(self.profile, setting_name):
            val_type = type(getattr(self.profile, setting_name))
            try:
                setattr(self.profile, setting_name, val_type(value))
                self.save_profile()
                return True
            except Exception as e:
                logger.error(f"Error updating setting {setting_name}: {e}")
        return False

    def get_frequent_commands(self, top_n: int = 5) -> List[Dict[str, Any]]:
        """Returns most frequently used voice commands."""
        sorted_cmds = sorted(self.profile.command_counts.items(), key=lambda item: -item[1])
        return [{"command": cmd, "count": count} for cmd, count in sorted_cmds[:top_n]]

    def get_telemetry_dict(self) -> Dict[str, Any]:
        """Returns structured dictionary for HUD settings panel and recommendations."""
        return {
            "profile": {
                "angle_range_x": self.profile.angle_range_x,
                "angle_range_y": self.profile.angle_range_y,
                "deadzone_deg": self.profile.deadzone_deg,
                "dwell_time_ms": self.profile.dwell_time_ms,
                "blink_sensitivity": self.profile.blink_sensitivity,
                "speech_rate": self.profile.speech_rate,
                "osk_dwell_ms": self.profile.osk_dwell_ms,
                "session_count": self.profile.session_count,
            },
            "recommendations": self.pending_recommendations,
            "frequent_commands": self.get_frequent_commands(),
        }

