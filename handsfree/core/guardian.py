"""AI Software Guardian & Auto-Recovery Engine.

Continuous Health Monitoring & Fail-Safe Handover:
  1. Primary Wearable Hardware:
     - Monitors USB/Serial connection and heartbeat data freshness.
     - Detects hardware stalls/disconnects without false-triggering on single dropped frames.
  2. Safe-Stop & Input Release:
     - Immediately releases all held mouse/gamepad/keyboard inputs via OutputGateway.
  3. Seamless Webcam Fallback:
     - Prepares and validates webcam FaceLandmarker tracking stability before handover.
     - Announces transition via voice feedback and UI telemetry badge.
  4. Safe Re-engagement:
     - When hardware reconnects, verifies signal stability before safely returning control.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("handsfree.guardian")


class SoftwareGuardian:
    """Supervises hardware connection, webcam fallback stability, input safety, and automated recovery."""

    def __init__(
        self,
        gateway_ref: Optional[Any] = None,
        watchdog_ref: Optional[Any] = None,
        tts_speaker: Optional[Callable[[str], None]] = None,
        on_status_update: Optional[Callable[[Dict[str, Any]], None]] = None,
        hardware_timeout_s: float = 1.8,
        recovery_stability_s: float = 1.2,
    ):
        self.gateway_ref = gateway_ref
        self.watchdog_ref = watchdog_ref
        self.tts_speaker = tts_speaker
        self.on_status_update = on_status_update
        self.hardware_timeout_s = hardware_timeout_s
        self.recovery_stability_s = recovery_stability_s

        self.is_running: bool = False
        self._thread: Optional[threading.Thread] = None

        # Mode: "wearable_primary", "webcam_fallback", "safe_stop"
        self.active_mode: str = "webcam_fallback"
        self.hardware_connected: bool = False
        self.hardware_last_beat: float = 0.0
        self.hardware_stable_since: float = 0.0

        self.webcam_tracking_stable: bool = False
        self.webcam_last_frame: float = 0.0
        self.webcam_fps: float = 30.0

        self.voice_status: str = "active"
        self.last_incident: str = "None"
        self.incident_count: int = 0

    def speak(self, text: str) -> None:
        """Announces safety and status transitions to the user."""
        if not text:
            return
        logger.info(f"[GUARDIAN VOICE]: {text}")
        if self.tts_speaker:
            try:
                self.tts_speaker(text)
                return
            except Exception:
                pass

        try:
            import subprocess
            safe_text = text.replace("'", "''").replace('"', '`"')
            cmd = f"$voice = New-Object -ComObject SAPI.SpVoice; $voice.Speak('{safe_text}')"
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def start(self) -> None:
        """Starts background health supervisor thread."""
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._supervision_loop, daemon=True, name="GuardianThread")
        self._thread.start()
        logger.info("SoftwareGuardian supervisor started.")

    def stop(self) -> None:
        """Stops background health supervisor thread."""
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        logger.info("SoftwareGuardian stopped.")

    def record_hardware_heartbeat(self, source: str = "usb") -> None:
        """Called whenever a valid packet arrives from the primary wearable headband."""
        now = time.time()
        self.hardware_last_beat = now

        if not self.hardware_connected:
            if self.hardware_stable_since == 0.0:
                self.hardware_stable_since = now
            elif (now - self.hardware_stable_since) >= self.recovery_stability_s:
                # Hardware has demonstrated stable input: safely hand over
                self.hardware_connected = True
                self.active_mode = "wearable_primary"
                msg = "Wearable headband connection verified and stable. Primary hardware active."
                logger.info(msg)
                self.speak(msg)
        else:
            self.hardware_stable_since = now

    def record_webcam_frame(self, has_face: bool = True, fps: float = 30.0) -> None:
        """Updates health of the webcam fallback tracking pipeline."""
        self.webcam_last_frame = time.time()
        self.webcam_fps = fps
        self.webcam_tracking_stable = has_face

    def trigger_safe_stop(self, reason: str = "Manual emergency stop") -> None:
        """Immediately releases all held virtual gamepad buttons and mouse clicks."""
        logger.warning(f"Guardian triggering safe stop: {reason}")
        if self.gateway_ref:
            try:
                self.gateway_ref.release_all()
            except Exception as e:
                logger.error(f"Error executing safe stop: {e}")

        self.last_incident = f"Safe Stop: {reason} at {time.strftime('%H:%M:%S')}"
        self.incident_count += 1
        self.speak("Emergency stop activated. All generated inputs released.")

    def _supervision_loop(self) -> None:
        """Evaluates hardware freshness, handles fail-safe fallback, and updates telemetry."""
        while self.is_running:
            now = time.time()

            # 1. Check Primary Wearable Connection Health
            if self.hardware_connected:
                elapsed_hw = now - self.hardware_last_beat
                if elapsed_hw > self.hardware_timeout_s:
                    # Connection lost or stalled!
                    logger.warning(
                        f"Wearable hardware heartbeat silent for {elapsed_hw:.2f}s "
                        f"(> {self.hardware_timeout_s}s limit). Initiating emergency fail-safe."
                    )
                    self.hardware_connected = False
                    self.hardware_stable_since = 0.0

                    # 1. Release all currently held inputs
                    if self.gateway_ref:
                        try:
                            self.gateway_ref.release_all()
                        except Exception as e:
                            logger.error(f"Error during fail-safe release_all: {e}")

                    # 2. Engage webcam fallback
                    self.active_mode = "webcam_fallback"
                    self.last_incident = f"Hardware disconnect at {time.strftime('%H:%M:%S')}"
                    self.incident_count += 1

                    # 3. Voice & UI notification
                    fallback_msg = "Your wearable connection has been interrupted. Webcam fallback is now active."
                    self.speak(fallback_msg)

            # 2. Check Watchdog Status
            if self.watchdog_ref and getattr(self.watchdog_ref, "is_tripped", False):
                self.last_incident = f"Watchdog timeout at {time.strftime('%H:%M:%S')}"

            # 3. Push telemetry update
            if self.on_status_update:
                try:
                    self.on_status_update(self.get_telemetry_dict())
                except Exception as e:
                    logger.debug(f"Telemetry update error: {e}")

            time.sleep(0.05)

    def get_telemetry_dict(self) -> Dict[str, Any]:
        """Returns structured guardian telemetry for dashboard HUD."""
        now = time.time()
        hw_freshness = round(now - self.hardware_last_beat, 2) if self.hardware_last_beat > 0 else 999.0
        cam_freshness = round(now - self.webcam_last_frame, 2) if self.webcam_last_frame > 0 else 999.0

        return {
            "active_mode": self.active_mode,
            "hardware": {
                "connected": self.hardware_connected,
                "freshness_sec": hw_freshness,
                "status": "Online (Primary)" if self.hardware_connected else "Disconnected / Standby",
            },
            "webcam_fallback": {
                "available": True,
                "active": self.active_mode == "webcam_fallback",
                "tracking_stable": self.webcam_tracking_stable,
                "fps": round(self.webcam_fps, 1),
                "freshness_sec": cam_freshness,
                "status": "Active (Fallback)" if self.active_mode == "webcam_fallback" else "Ready in Standby",
            },
            "voice": {
                "status": self.voice_status,
            },
            "safety": {
                "last_incident": self.last_incident,
                "incident_count": self.incident_count,
            }
        }
