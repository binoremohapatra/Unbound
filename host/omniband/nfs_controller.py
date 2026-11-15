"""nfs_controller.py - DirectInput Racing Controller for Need for Speed: Most Wanted.

Implements:
1. Auto-Gas (W + UP held automatically; head tilted backward switches to S + DOWN brake).
2. Proportional PWM Steering (no oversteer):
   - Center Deadzone (-10°..+10°): Straight (no keys)
   - Moderate Tilt (10°..22°): Pulsed 45ms ON / 35ms OFF for smooth lane changes
   - Full Tilt (>22°): Continuous hold for sharp hairpins
3. 3-Piezo Muscle Triggers (CH1=GPIO32, CH2=GPIO34, CH3=GPIO33):
   - Jaw Hold (>350ms on CH1/CH2): Hold NOS (X + LSHIFT + LALT)
   - Single Jaw Clench (<350ms on CH1/CH2): Tap Handbrake Drift (SPACE for 180ms)
   - Forehead Eyebrow Raise (CH3 spike): Tap Speedbreaker (G for 120ms)
4. Instant 1-click Gyro Re-centering & Emergency Release.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, List, Optional

log = logging.getLogger("omniband.nfs")


class NFSRacingController:
    """Stateful DirectInput Racing Controller for Need for Speed: Most Wanted."""

    def __init__(
        self,
        gateway: Any,
        on_telemetry: Optional[Callable[[bool, bool, bool, bool, bool, bool], None]] = None,
    ) -> None:
        self.gw = gateway
        self.on_telemetry = on_telemetry  # (gas, brake, steer_left, steer_right, nos, drift)
        self._lock = threading.Lock()

        self.enabled = False
        self.pitch_center: float = 0.0
        self.roll_center: float = 0.0
        self.current_pitch: float = 0.0
        self.current_roll: float = 0.0

        # Output states
        self.gas_active: bool = False
        self.brake_active: bool = False
        self.steer_state: str = "center"  # "center", "pwm_left", "full_left", "pwm_right", "full_right"
        self.steer_left_active: bool = False
        self.steer_right_active: bool = False
        self.nos_active: bool = False
        self.handbrake_active: bool = False
        self.speedbreaker_active: bool = False
        self.piezo_triggers_enabled: bool = False  # Piezo = Visual Telemetry Only by default

        # Piezo edge tracking
        self._jaw_down: bool = False
        self._jaw_press_t: float = 0.0
        self._brow_down: bool = False

        # Pulse deadline timestamps
        self._handbrake_until: float = 0.0
        self._speedbreaker_until: float = 0.0

    def enable(self) -> None:
        """Activate NFS Racing mode: auto-gas on, center gyro."""
        with self._lock:
            self.enabled = True
            self.zero_gyro()
            self.brake_active = False
            self.gas_active = True
            self.gw.set_nfs_action("throttle", True)
            self._notify_telemetry()
        log.info("🏎️ NFS Racing Controller ENABLED (Auto-Gas ON)")

    def disable(self) -> None:
        """Deactivate NFS Racing mode: release all driving keys."""
        with self._lock:
            self.enabled = False
            self.gas_active = False
            self.brake_active = False
            self.steer_state = "center"
            self.steer_left_active = False
            self.steer_right_active = False
            self.nos_active = False
            self.handbrake_active = False
            self.speedbreaker_active = False
            self._jaw_down = False
            self._brow_down = False
            self.gw.release_all_keys()
            self._notify_telemetry()
        log.info("🖥️ NFS Racing Controller DISABLED (Keys released)")

    def zero_gyro(self) -> None:
        """Re-center pitch and roll angles to current head position."""
        self.pitch_center = self.current_pitch
        self.roll_center = self.current_roll
        log.info("🎯 Gyro Zeroed: PitchCenter=%.1f°, RollCenter=%.1f°", self.pitch_center, self.roll_center)

    def feed_imu(self, dx: float, dy: float, pitch: float, roll: float, now: Optional[float] = None) -> None:
        """Update head posture and evaluate auto-gas, brake, and steering zones."""
        if not self.enabled:
            return
        if now is None:
            now = time.perf_counter()

        with self._lock:
            self.current_pitch = pitch
            self.current_roll = roll

            rel_pitch = pitch - self.pitch_center
            rel_roll = roll - self.roll_center

            # 1. Auto-Gas vs Brake:
            # Tilting head backward (Pitch > 10.0°) releases gas and holds brake/reverse.
            if rel_pitch > 10.0:
                if self.gas_active:
                    self.gas_active = False
                    self.gw.set_nfs_action("throttle", False)
                if not self.brake_active:
                    self.brake_active = True
                    self.gw.set_nfs_action("brake", True)
            else:
                # Neutral / forward posture: Auto-Gas engaged
                if self.brake_active:
                    self.brake_active = False
                    self.gw.set_nfs_action("brake", False)
                if not self.gas_active:
                    self.gas_active = True
                    self.gw.set_nfs_action("throttle", True)

            # 2. Proportional Steering Zones
            if -10.0 <= rel_roll <= 10.0:
                self.steer_state = "center"
            elif -22.0 <= rel_roll < -10.0:
                self.steer_state = "pwm_left"
            elif rel_roll < -22.0:
                self.steer_state = "full_left"
            elif 10.0 < rel_roll <= 22.0:
                self.steer_state = "pwm_right"
            elif rel_roll > 22.0:
                self.steer_state = "full_right"

            self._tick_locked(now)

    def feed_somatic(self, channels: List[float], now: Optional[float] = None) -> None:
        """Process 3 piezo channels (CH1/CH2 temples, CH3 forehead)."""
        if not self.enabled:
            return
        if not getattr(self, "piezo_triggers_enabled", False):
            # Piezo is VISUAL TELEMETRY ONLY. Disconnected from game actions.
            return
        if now is None:
            now = time.perf_counter()
        if len(channels) < 3:
            return

        c = [float(x) / 4095.0 if float(x) > 1.0 else float(x) for x in channels]
        ch1, ch2, ch3 = c[0], c[1], c[2]

        with self._lock:
            # 1. Jaw Clench (CH1 or CH2 temples)
            jaw_high = (ch1 > 0.25) or (ch2 > 0.25)

            if jaw_high and not self._jaw_down:
                self._jaw_down = True
                self._jaw_press_t = now

            elif jaw_high and self._jaw_down:
                dur = now - self._jaw_press_t
                if dur >= 0.350 and not self.nos_active:
                    # Jaw Hold > 350ms -> Hold NOS (X + LSHIFT + LALT)
                    self.nos_active = True
                    self.gw.set_nfs_action("nos", True)

            elif not jaw_high and self._jaw_down:
                self._jaw_down = False
                dur = now - self._jaw_press_t

                if self.nos_active:
                    self.nos_active = False
                    self.gw.set_nfs_action("nos", False)
                else:
                    # Single Quick Clench < 350ms -> Handbrake Drift (SPACE for 180ms)
                    self.handbrake_active = True
                    self._handbrake_until = now + 0.180
                    self.gw.set_nfs_action("handbrake", True)

            # 2. Forehead Eyebrow Raise (CH3 spike)
            brow_high = (ch3 > 0.28)
            if brow_high and not self._brow_down:
                self._brow_down = True
                self.speedbreaker_active = True
                self._speedbreaker_until = now + 0.120
                self.gw.set_nfs_action("speedbreaker", True)
            elif not brow_high and self._brow_down:
                self._brow_down = False

            self._tick_locked(now)

    def tick(self, now: Optional[float] = None) -> None:
        """Periodic 16ms tick for PWM pulsing and timer expiry."""
        if not self.enabled:
            return
        if now is None:
            now = time.perf_counter()
        with self._lock:
            self._tick_locked(now)

    def _tick_locked(self, now: float) -> None:
        # Check pulse expirations
        if self.handbrake_active and now >= self._handbrake_until:
            self.handbrake_active = False
            self.gw.set_nfs_action("handbrake", False)

        if self.speedbreaker_active and now >= self._speedbreaker_until:
            self.speedbreaker_active = False
            self.gw.set_nfs_action("speedbreaker", False)

        # Proportional PWM steering
        if self.steer_state == "center":
            if self.steer_left_active:
                self.steer_left_active = False
                self.gw.set_nfs_action("steer_left", False)
            if self.steer_right_active:
                self.steer_right_active = False
                self.gw.set_nfs_action("steer_right", False)

        elif self.steer_state == "full_left":
            if not self.steer_left_active:
                self.steer_left_active = True
                self.gw.set_nfs_action("steer_left", True)
            if self.steer_right_active:
                self.steer_right_active = False
                self.gw.set_nfs_action("steer_right", False)

        elif self.steer_state == "full_right":
            if not self.steer_right_active:
                self.steer_right_active = True
                self.gw.set_nfs_action("steer_right", True)
            if self.steer_left_active:
                self.steer_left_active = False
                self.gw.set_nfs_action("steer_left", False)

        elif self.steer_state == "pwm_left":
            # 45ms ON / 35ms OFF
            phase = int(now * 1000) % 80
            want_on = (phase < 45)
            if want_on != self.steer_left_active:
                self.steer_left_active = want_on
                self.gw.set_nfs_action("steer_left", want_on)
            if self.steer_right_active:
                self.steer_right_active = False
                self.gw.set_nfs_action("steer_right", False)

        elif self.steer_state == "pwm_right":
            # 45ms ON / 35ms OFF
            phase = int(now * 1000) % 80
            want_on = (phase < 45)
            if want_on != self.steer_right_active:
                self.steer_right_active = want_on
                self.gw.set_nfs_action("steer_right", want_on)
            if self.steer_left_active:
                self.steer_left_active = False
                self.gw.set_nfs_action("steer_left", False)

        self._notify_telemetry()

    def _notify_telemetry(self) -> None:
        if self.on_telemetry:
            try:
                self.on_telemetry(
                    self.gas_active,
                    self.brake_active,
                    self.steer_left_active,
                    self.steer_right_active,
                    self.nos_active,
                    self.handbrake_active,
                )
            except Exception as e:
                log.debug("on_telemetry exception: %s", e)
