"""Handsfree: Hands-free multimodal game & assistive controller.

Wires together input adapters, fastpath/slowpath pipelines, watchdog, state machine,
hardware output gateway, and telemetry dashboard in a safe, multithreaded architecture.
"""

import argparse
import logging
from pathlib import Path
import queue
import signal
import sys

# Ensure project root (c:\Unbound) is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from handsfree.core.events import SignalEvent, SignalType, Intent
from handsfree.core.calibration import CalibrationManager
from handsfree.core.fastpath import FastPath
from handsfree.core.slowpath import SlowPath
from handsfree.core.state_machine import StateMachine, SystemState
from handsfree.core.mapping import ProfileMapper
from handsfree.core.gateway import OutputGateway
from handsfree.core.watchdog import Watchdog
from handsfree.adapters.webcam import WebcamAdapter
from handsfree.adapters.audio import AudioAdapter
from handsfree.adapters.switch import SwitchAdapter
from handsfree.adapters.replay import ReplayAdapter
from handsfree.core.cursor_tracker import CursorTracker
from handsfree.dashboard.server import DashboardServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("handsfree.main")


class HandsfreeEngine:
    """Central engine wiring threads, adapters, processing pipelines, and safety mechanisms."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.is_running = False
        self.event_queue: queue.Queue[SignalEvent] = queue.Queue(maxsize=1000)

        # 1. Output Gateway (vgamepad / pydirectinput)
        self.gateway = OutputGateway(dry_run=args.dry_run)

        # 2. Safety Watchdog
        self.watchdog = Watchdog(
            gateway=self.gateway,
            timeout_seconds=args.watchdog_timeout,
            on_timeout=self._handle_watchdog_timeout,
        )

        # 3. State Machine & Calibration
        self.state_machine = StateMachine(initial_state=SystemState.ACTIVE)
        self.calibration = CalibrationManager()

        # 4. Pipelines
        self.fastpath = FastPath(calibration=self.calibration)
        self.slowpath = SlowPath(calibration=self.calibration)

        # 5. Profile Mapper
        profile_path = Path(args.profile)
        if not profile_path.exists():
            default_p = Path(__file__).parent / "profiles" / "default.json"
            logger.warning(f"Profile {profile_path} not found; falling back to {default_p}")
            profile_path = default_p
        self.mapper = ProfileMapper.from_file(profile_path)
        logger.info(f"Loaded controller profile: '{self.mapper.name}'")

        # 6. Dashboard Server & Head Cursor Tracker
        self.dashboard = None
        if not args.no_dashboard:
            tracker = CursorTracker(
                angle_range_x=args.angle_range_x,
                angle_range_y=args.angle_range_y,
                deadzone_deg=args.deadzone,
                invert_x=args.invert_x,
                auto_calibrate=True,
                enable_blink=args.enable_blink_click,
            )
            self.dashboard = DashboardServer(
                host=args.dashboard_host,
                port=args.dashboard_port,
                cursor_tracker=tracker,
            )

        # 7. Adapters
        self.adapters = []
        self._init_adapters()

    def _init_adapters(self) -> None:
        def on_signal(event: SignalEvent):
            try:
                self.event_queue.put_nowait(event)
            except queue.Full:
                pass

        def on_heartbeat():
            if self.state_machine.current_state == SystemState.PAUSED:
                self.state_machine.transition_to(SystemState.ACTIVE)
                if self.dashboard:
                    self.dashboard.update_telemetry("state", "active")
            self.watchdog.beat()

        if self.args.replay:
            logger.info(f"Replay mode active with: {self.args.replay}")
            replay_adapter = ReplayAdapter(
                recording_path=Path(self.args.replay),
                on_event=on_signal,
                on_heartbeat=on_heartbeat,
                loop=True,
            )
            self.adapters.append(replay_adapter)
        else:
            webcam = WebcamAdapter(
                camera_id=self.args.camera,
                on_event=on_signal,
                on_heartbeat=on_heartbeat,
            )
            self.adapters.append(webcam)

            def on_speech_flag(active: bool):
                self.slowpath.set_speech_active(active)
                if self.dashboard:
                    self.dashboard.update_telemetry("audio", {"speech": active, "pop": False})

            audio = AudioAdapter(
                on_event=on_signal,
                on_speech_activity=on_speech_flag,
            )
            self.adapters.append(audio)

            switch = SwitchAdapter(on_event=on_signal)
            self.adapters.append(switch)

    def _handle_watchdog_timeout(self) -> None:
        logger.warning("Safety watchdog triggered! Resetting system state to PAUSED.")
        self.state_machine.transition_to(SystemState.PAUSED)
        if self.dashboard:
            self.dashboard.update_telemetry("state", "paused")
            self.dashboard.update_telemetry("pressed", [])

    def start(self) -> None:
        self.is_running = True
        logger.info("Starting Handsfree controller...")

        self.watchdog.start()

        if self.dashboard:
            self.dashboard.start()

        for adapter in self.adapters:
            adapter.start()

        logger.info("Engine running. Press Ctrl+C to terminate.")

        try:
            self._process_loop()
        except KeyboardInterrupt:
            logger.info("Interrupt received.")
        finally:
            self.stop()

    def stop(self) -> None:
        logger.info("Shutting down Handsfree...")
        self.is_running = False

        for adapter in self.adapters:
            try:
                adapter.stop()
            except Exception as e:
                logger.error(f"Error stopping adapter: {e}")

        self.watchdog.stop()
        self.gateway.release_all()
        logger.info("Safe release executed. Shutdown complete.")

    def _process_loop(self) -> None:
        while self.is_running:
            try:
                event = self.event_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            intents = []

            if event.signal_type == SignalType.HEAD_POSE:
                val = event.value
                yaw = val.get("yaw", 0.0)
                pitch = val.get("pitch", 0.0)
                roll = val.get("roll", 0.0)
                intents.extend(self.fastpath.process_pose(yaw, pitch, roll, event.timestamp))

                if self.dashboard:
                    self.dashboard.update_telemetry("pose", {"yaw": yaw, "pitch": pitch, "roll": roll})
                    if hasattr(self.dashboard, "cursor_tracker"):
                        tracker = self.dashboard.cursor_tracker
                        dx, dy, (cx, cy), dwell_status = tracker.process_head_pose(
                            yaw, pitch, event.timestamp
                        )
                        norm_x = cx / max(1.0, float(tracker.screen_width))
                        norm_y = cy / max(1.0, float(tracker.screen_height))

                        # Move physical Windows cursor on non-dry-run
                        import os
                        if not self.args.dry_run and os.name == "nt":
                            try:
                                import ctypes
                                ctypes.windll.user32.SetCursorPos(int(cx), int(cy))
                            except Exception:
                                pass

                        self.dashboard.update_telemetry("cursor", {
                            "x": cx, "y": cy, "dx": dx, "dy": dy,
                            "norm_x": norm_x, "norm_y": norm_y,
                        })
                        if dwell_status:
                            self.dashboard.update_telemetry("dwell", {
                                "state": dwell_status.state.value,
                                "progress": dwell_status.progress,
                                "action": dwell_status.action.value,
                                "dragging": dwell_status.is_dragging,
                            })
                            if dwell_status.state.value == "triggered" and not self.args.dry_run and os.name == "nt":
                                try:
                                    import ctypes
                                    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                                    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
                                except Exception:
                                    pass

            elif event.signal_type == SignalType.BLENDSHAPE:
                blendshapes = event.value
                intents.extend(self.slowpath.process_blendshapes(blendshapes, event.timestamp))
                if self.dashboard:
                    self.dashboard.update_telemetry("blendshapes", blendshapes)

                # Double-blink left-click detection
                blink_l = blendshapes.get("eyeBlinkLeft", 0.0)
                blink_r = blendshapes.get("eyeBlinkRight", 0.0)
                avg_blink = (blink_l + blink_r) / 2.0

                if self.dashboard and hasattr(self.dashboard, "cursor_tracker"):
                    tracker = self.dashboard.cursor_tracker
                    if tracker.blink_detector and tracker.blink_detector.update(avg_blink, event.timestamp):
                        logger.info("Double-blink detected! Dispatching Windows Left Click.")
                        import os
                        if not self.args.dry_run and os.name == "nt":
                            try:
                                import ctypes
                                ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
                                ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
                            except Exception as e:
                                logger.error(f"Error dispatching mouse click: {e}")
                        self.dashboard.update_telemetry("blink_click", {"timestamp": event.timestamp})

            elif event.signal_type == SignalType.AUDIO_POP:
                intents.append(Intent(action="pop", value=1.0, source="audio"))
                if self.dashboard:
                    self.dashboard.update_telemetry("audio", {"pop": True, "speech": self.slowpath.speech_active})

            elif event.signal_type == SignalType.SWITCH:
                intents.append(Intent(action=event.name, value=event.value, source="switch"))

            for intent in intents:
                filtered = self.state_machine.filter_intent(intent)
                if not filtered:
                    continue

                commands = self.mapper.map_intent(filtered)
                for cmd in commands:
                    self.gateway.dispatch(cmd)

                if self.dashboard and filtered.action == "stick_x":
                    stick_data = self.dashboard.latest_telemetry.get("stick", {"x": 0.0, "y": 0.0})
                    stick_data["x"] = filtered.value
                    self.dashboard.update_telemetry("stick", stick_data)
                elif self.dashboard and filtered.action == "stick_y":
                    stick_data = self.dashboard.latest_telemetry.get("stick", {"x": 0.0, "y": 0.0})
                    stick_data["y"] = filtered.value
                    self.dashboard.update_telemetry("stick", stick_data)

            if self.dashboard:
                self.dashboard.update_telemetry("pressed", list(self.gateway.pressed_buttons))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Handsfree Assistive Game Controller")
    default_profile = str(Path(__file__).parent / "profiles" / "default.json")
    parser.add_argument("--profile", type=str, default=default_profile, help="Path to profile JSON")
    parser.add_argument("--camera", type=int, default=0, help="Camera device index")
    parser.add_argument("--replay", type=str, default=None, help="Path to landmark stream recording .jsonl")
    parser.add_argument("--dry-run", action="store_true", help="Run without dispatching real hardware inputs")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable web HUD dashboard server")
    parser.add_argument("--dashboard-host", type=str, default="127.0.0.1", help="Dashboard host IP")
    parser.add_argument("--dashboard-port", type=int, default=8080, help="Dashboard port")
    parser.add_argument("--watchdog-timeout", type=float, default=0.75, help="Safety heartbeat timeout in seconds")
    parser.add_argument("--angle-range-x", type=float, default=12.0, help="Head yaw degrees for full screen width (smaller=more sensitive)")
    parser.add_argument("--angle-range-y", type=float, default=9.0, help="Head pitch degrees for full screen height (smaller=more sensitive)")
    parser.add_argument("--deadzone", type=float, default=2.0, help="Head pose deadzone in degrees (suppress micro-jitter)")
    parser.add_argument("--invert-x", action=argparse.BooleanOptionalAction, default=True, help="Invert X axis for mirror webcam (default: True)")
    parser.add_argument("--enable-blink-click", action=argparse.BooleanOptionalAction, default=True, help="Enable double-blink left click (default: True)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    engine = HandsfreeEngine(args)

    def handle_sig(signum, frame):
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    engine.start()


if __name__ == "__main__":
    main()

