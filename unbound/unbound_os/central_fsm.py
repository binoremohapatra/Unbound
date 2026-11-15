"""central_fsm.py - deterministic finite state machine for OmniControl OS.

Priority hierarchy (highest first):
  P0 SAFE_IDLE        - face lost > face_lost_ms -> instant panic release;
                        re-engage needs face_present_ms of continuous face.
  P1 CALIBRATING      - double-eye-close within the window -> 1.0 s recenter,
                        all triggers muted.
  P2 LATCHED          - timed auto-hold; hard_blink / acoustic click / voice_stop
                        are INSTANT CANCEL, always.
  P3 CONVERSE_LOCK    - voice "freeze" mutes every facial/head trigger so the
                        user can talk; "resume"/"continue" unlocks.
  P4 ACTIVE_ESPORTS   - 3D mode; Schmitt-hysteresis sub-state FULL_TRACKING vs
                        GLASSES_FALLBACK (degraded tracking > glasses_fallback_ms).
  P4 ACTIVE_WORKSTATION - 2D mode; gaze dwell drives the predictive bar.

The FSM NEVER touches the OS. It returns Action objects; main_hud applies them
to StatefulOutputSink. Every transition is logged through on_transition.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("omnicontrol_os.fsm")

# states
SAFE_IDLE = "SAFE_IDLE"
CALIBRATING = "CALIBRATING"
LATCHED = "LATCHED"
CONVERSE_LOCK = "CONVERSE_LOCK"
ACTIVE_ESPORTS = "ACTIVE_ESPORTS"
ACTIVE_WORKSTATION = "ACTIVE_WORKSTATION"

FULL_TRACKING = "FULL_TRACKING"
GLASSES_FALLBACK = "GLASSES_FALLBACK"


@dataclass(frozen=True)
class Action:
    """One thing the output layer should do. kind drives the sink/HUD."""
    kind: str            # "panic" | "key" | "mouse" | "axis" | "unicode" |
                         # "predict" | "mode" | "release_keys" | "notice"
    name: str = ""
    value: float = 0.0
    text: str = ""
    extra: tuple = ()


class OmniFSM:
    def __init__(self, cfg: dict,
                 on_transition: Optional[Callable[[str, str, float], None]] = None):
        self.cfg = cfg
        safety = cfg.get("safety", {})
        aim = cfg.get("aim", {})
        vis = cfg.get("vision", {})
        self.face_lost_s = safety.get("face_lost_ms", 300) / 1000.0
        self.face_present_s = safety.get("face_present_ms", 300) / 1000.0
        self.calibrate_s = safety.get("calibrate_double_blink_ms", 1000) / 1000.0
        self.latch_hold_s = safety.get("latch_hold_ms", 3000) / 1000.0
        self.blink_window_s = 1.5                       # two blinks this close = calibrate
        self.glasses_fallback_s = aim.get("glasses_fallback_ms", 800) / 1000.0
        self.deadzone = aim.get("deadzone", 0.06)
        self.curve = aim.get("curve", "cubic")
        self.full_scale = aim.get("full_scale_deg", 25.0)
        self.mouse_speed = aim.get("mouse_speed_px_per_s", 1400.0)
        self.schmitt_on = aim.get("schmitt_on", 0.55)
        self.schmitt_off = aim.get("schmitt_off", 0.33)
        self.cancel_names = {"hard_blink", "click", "voice_stop", "kill"}
        self.latch_names = {"clench", "dwell"}
        self.latch_keys = safety.get("latch_keys", ["lalt"])
        self.on_transition = on_transition or (lambda a, b, t: None)

        self.state = SAFE_IDLE
        self.substate = FULL_TRACKING           # esports only
        self._prev_mode = ACTIVE_WORKSTATION
        self._face_bad_since: Optional[float] = None
        self._face_good_since: Optional[float] = None
        self._last_blink_t: Optional[float] = None
        self._cal_end: Optional[float] = None
        self._latch_end: Optional[float] = None
        self._gaze_zero_since: Optional[float] = None
        self._gaze_full = False
        self._degraded_since: Optional[float] = None
        self._clean_since: Optional[float] = None
        self._calib_offset = (0.0, 0.0)         # gaze recenter from CALIBRATING
        self.predict_slot = 0
        self.predict_words = cfg.get("workstation", {}).get("words", [])

    # ------------------------------------------------------------------ public
    def set_mode(self, mode: str, now: float) -> list[Action]:
        """Window inspector says the foreground app changed profile."""
        if mode not in (ACTIVE_ESPORTS, ACTIVE_WORKSTATION) or mode == self.state \
                or self.state in (SAFE_IDLE, CALIBRATING, LATCHED, CONVERSE_LOCK):
            return []
        if mode != self.state:
            self._transition(mode, f"window profile -> {mode}", now)
            self._prev_mode = mode
        return [Action("mode", mode), Action("release_keys")]

    def handle_event(self, ev, now: float) -> list[Action]:
        """Edge events from PsychicIntentAdapter (gesture/voice/safety)."""
        out: list[Action] = []
        name = ev.name

        # P3 converse lock controls - always honored, even while locked
        if ev.kind == "voice" and name == "voice_freeze":
            if self.state == LATCHED:
                self._latch_end = None                     # freeze supersedes the hold
            if self.state != CONVERSE_LOCK:
                self._transition(CONVERSE_LOCK, "voice freeze", now)
                out += [Action("mode", CONVERSE_LOCK), Action("release_keys")]
            return out
        if self.state == CONVERSE_LOCK:
            if ev.kind == "voice" and name in ("voice_resume", "voice_stop"):
                self._transition(self._prev_mode, "voice resume", now)
                out.append(Action("mode", self._prev_mode))
            # everything else is muted: judges can talk, triggers stay dead
            return out

        # P0 safety: face-loss is handled in handle_sample/tick, not here.
        # P2 latch instant-cancel: ALWAYS wins while latched
        if self.state == LATCHED:
            if name in self.cancel_names:
                self._transition(self._prev_mode, f"{name} cancel", now)
                out += [Action("mode", self._prev_mode), Action("release_keys")]
                return out
            return out                                     # other gestures muted while latched

        # P1 calibrating: only the timer exits it
        if self.state == CALIBRATING:
            return out

        if self.state == SAFE_IDLE:
            return out                                     # nothing fires until face returns

        # ---- ACTIVE_* ----
        if name in ("hard_blink", "click"):
            # double-blink -> recalibrate (two blinks inside the window)
            if name == "hard_blink":
                if self._last_blink_t is not None and now - self._last_blink_t <= self.blink_window_s:
                    self._transition(CALIBRATING, "double blink recalibrate", now)
                    self._cal_end = now + self.calibrate_s
                    out.append(Action("mode", CALIBRATING))
                    self._last_blink_t = None
                    return out
                self._last_blink_t = now
            # single blink/click in active mode: small "notice" for HUD, no fire
            out.append(Action("notice", name))
            return out

        if name in self.latch_names:
            self._transition(LATCHED, f"{name} latch", now)
            self._latch_end = now + self.latch_hold_s
            out.append(Action("mode", LATCHED))
            for k in self.latch_keys:
                out.append(Action("key", k, 1.0))
            return out

        if ev.kind == "voice" and name == "voice_stop":
            out += [Action("release_keys"), Action("notice", "voice_stop")]
            return out

        if name == "kill":
            self._transition(CONVERSE_LOCK, "kill", now)
            out += [Action("panic",), Action("mode", CONVERSE_LOCK), Action("release_keys")]
            return out

        return out

    def handle_sample(self, sample, now: float) -> list[Action]:
        """Continuous per-tick input: latest FrameSample (or None). Aim + face timers."""
        out: list[Action] = []
        face_ok = bool(sample is not None and sample.face_ok)

        # ---- face presence bookkeeping (P0) ----
        if face_ok:
            if self._face_bad_since is not None:
                self._face_bad_since = None
            if self._face_good_since is None:
                self._face_good_since = now
            if self._clean_since is None:
                self._clean_since = now                   # start of clean streak
        else:
            if self._face_good_since is not None:
                self._face_good_since = None
            if self._face_bad_since is None:
                self._face_bad_since = now

        # leave SAFE_IDLE only after continuous face for face_present_s
        if self.state == SAFE_IDLE:
            if self._face_good_since is not None and \
                    now - self._face_good_since >= self.face_present_s:
                self._transition(self._prev_mode, "face regained", now)
                out.append(Action("mode", self._prev_mode))
            return out

        # enter SAFE_IDLE on sustained face loss - panic FIRST
        if self._face_bad_since is not None and \
                now - self._face_bad_since >= self.face_lost_s and \
                self.state not in (SAFE_IDLE,):
            prev = self.state if self.state.startswith("ACTIVE") else self._prev_mode
            self._transition(SAFE_IDLE, "face lost", now)
            out += [Action("panic",), Action("mode", SAFE_IDLE),
                    Action("notice", "face lost - all released")]
            self._prev_mode = prev
            return out

        # ---- ACTIVE modes only below ----
        if not self.state.startswith("ACTIVE"):
            return out

        # CONVERSE_LOCK mute is handled in handle_event; here we just don't aim.
        if sample is None or not sample.face_ok:
            # brief dropouts: esports degrades to GLASSES_FALLBACK, aim zeroed
            if self.state == ACTIVE_ESPORTS:
                self._track_degraded(now, out)
            out.append(Action("axis", "right_x", 0.0))
            out.append(Action("axis", "right_y", 0.0))
            return out

        if self.substate == GLASSES_FALLBACK and self._clean_since is not None and \
                now - self._clean_since >= self.glasses_fallback_s:
            self.substate = FULL_TRACKING
            self._transition(self.state, "tracking restored", now)
            out.append(Action("notice", "full tracking"))

        # ---- aim: gaze -> right stick (esports) ----
        if self.state == ACTIVE_ESPORTS and self.substate == FULL_TRACKING:
            gx = self._curve(sample.gaze_x - self._calib_offset[0])
            gy = self._curve(sample.gaze_y - self._calib_offset[1])
            out.append(Action("axis", "right_x", gx))
            out.append(Action("axis", "right_y", gy))
        elif self.state == ACTIVE_WORKSTATION:
            # dwell events already fire via handle_event("dwell"); continuous gaze
            # drives the HUD predictive highlight only (no OS output here)
            out.append(Action("predict", "gaze",
                              sample.gaze_x, "",
                              (sample.gaze_x, sample.gaze_y)))
        return out

    def tick(self, now: float) -> list[Action]:
        """Timer-driven transitions called every loop iteration."""
        out: list[Action] = []
        # calibrating expiry
        if self.state == CALIBRATING and self._cal_end is not None and now >= self._cal_end:
            self._transition(self._prev_mode, "calibration done", now)
            out.append(Action("mode", self._prev_mode))
            self._cal_end = None
        # latch expiry
        if self.state == LATCHED and self._latch_end is not None and now >= self._latch_end:
            self._transition(self._prev_mode, "hold expired", now)
            out += [Action("mode", self._prev_mode), Action("release_keys")]
            self._latch_end = None
        return out

    def _track_degraded(self, now: float, out: list) -> None:
        self._clean_since = None                            # clean streak broken
        if self._degraded_since is None:
            self._degraded_since = now
        elif self.substate == FULL_TRACKING and \
                now - self._degraded_since >= self.glasses_fallback_s:
            self.substate = GLASSES_FALLBACK
            self._degraded_since = None                     # re-arm for next episode
            self._transition(self.state, "degraded -> glasses fallback", now)
            out.append(Action("notice", "glasses fallback"))
            out.append(Action("axis", "right_x", 0.0))
            out.append(Action("axis", "right_y", 0.0))

    def _curve(self, x: float) -> float:
        x = max(-1.0, min(1.0, x))
        if self.curve == "cubic":
            return x ** 3
        return x

    def _transition(self, new_state: str, reason: str, now: float) -> None:
        old = self.state
        self.state = new_state
        log.info("FSM %s -> %s (%s)", old, new_state, reason)
        try:
            self.on_transition(old, new_state, now)
        except Exception:
            pass

    def snapshot(self) -> dict:
        return {"state": self.state, "substate": self.substate,
                "prev_mode": self._prev_mode, "predict_slot": self.predict_slot}
