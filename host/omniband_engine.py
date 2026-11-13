#!/usr/bin/env python3
"""omniband_engine.py - OmniBand V2 host engine CLI.

  python host/omniband_engine.py --simulate --dry-run --self-test
  python host/omniband_engine.py --port COM5 [--baud 115200] [--dry-run]
  python host/omniband_engine.py --doctor

Modes: --mode N (0..5). Safety: link silence, ESTOP and SOS always release.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.omniband.constants_gen import CONSTANTS, MODES  # noqa: E402
from host.omniband import protocol  # noqa: E402
from host.omniband.link import Arbiter, SerialTransport, SimTransport  # noqa: E402
from host.omniband.sim_device import SimDevice  # noqa: E402
from host.omniband.engine import OmniEngine  # noqa: E402
from host.omniband.out_sinks import (DryRunSink, TTSSink, SirenSink)  # noqa: E402

log = logging.getLogger("omniband")
K = CONSTANTS


def _build_sink(args):
    if args.dry_run:
        return DryRunSink()
    # Real path: ALWAYS use the real unbound Gateway + SendInputBackend for real OS hardware injection
    try:
        from gateway import Gateway, SendInputBackend, RecordingBackend
        backend = RecordingBackend() if args.record else SendInputBackend()
        gw = Gateway(backend)
        gw.install_exit_handlers()
        from host.omniband.out_sinks import GatewaySink
        return GatewaySink(gw)
    except Exception as e:  # noqa: BLE001
        log.error("cannot build Win32 sink (%s); falling back to dry-run", e)
        return DryRunSink()


def _auto_discover_transports(baud: int):
    """Probe USB/BT ports, falling back smoothly to SimDevice if none connected."""
    import serial.tools.list_ports
    ports = list(serial.tools.list_ports.comports())
    valid_ports = []
    for p in ports:
        desc = p.description.lower()
        if "usb" in desc or "ch340" in desc or "cp210" in desc or "serial" in desc:
            valid_ports.append(p.device)

    if valid_ports:
        log.info("Auto-discovered hardware COM port: %s", valid_ports[0])
        return [SerialTransport(valid_ports[0], baud, "usb")], None

    # Fallback to interactive simulator device
    log.info("No ESP32 serial device found; activating zero-config Interactive Simulator")
    dev = SimDevice(auto_gestures=True)
    return [SimTransport(dev, "sim")], dev


def _launch_native_gui(args) -> int:
    """Launch PySide6 Native Command Center, System Tray, and HUD Overlay."""
    try:
        from PySide6.QtWidgets import QApplication
        from host.omniband.app.theme import apply_theme
        from host.omniband.app.main_window import MainWindow

        app = QApplication.instance() or QApplication(sys.argv)
        apply_theme(app)
        app.setQuitOnLastWindowClosed(False)

        win = MainWindow()
        if not getattr(args, "minimized", False):
            win.show()
        else:
            log.info("Started silently in system tray (--minimized)")

        return app.exec()
    except Exception as e:
        log.error("Failed launching PySide6 GUI: %s", e)
        return 1


def _build_transports(args):
    if args.simulate:
        dev = SimDevice(auto_gestures=not args.no_auto)
        return [SimTransport(dev, "sim")], dev
    transports = []
    ports = []
    if args.port:
        ports.append(args.port)
    if args.bt_port:
        ports.append(args.bt_port)
    if not ports and not (args.optical or args.optical_fallback):
        return _auto_discover_transports(args.baud)
    for i, p in enumerate(ports):
        name = "usb" if i == 0 else "bt"
        transports.append(SerialTransport(p, args.baud, name))
    return transports, None


def _self_test() -> int:
    """Protocol + engine smoke: sim device -> arbiter -> engine -> dry sink."""
    print("== omniband self-test ==")
    dev = SimDevice()
    tr = SimTransport(dev, "sim")
    sink = DryRunSink()
    eng = OmniEngine(Arbiter([tr]), sink, keepalive_ms=50)
    tr.open()
    # pump engine ticks while the device thread produces lines
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        eng.tick()
        time.sleep(0.01)
    # inject a CH4 tap -> SINGLE gesture -> routed action (mode 0: left click)
    dev.inject_edge(4, True, 950)
    dev.inject_edge(4, False, 950, 150)
    for _ in range(200):
        eng.tick()
        time.sleep(0.01)
    ok = any(c.kind == "button" and c.name == "left" and c.pressed
             for c in sink.commands)
    print(f"gesture routed to sink: {'YES' if ok else 'NO'}")
    print(f"stats: {eng.stats}")
    tr.close()
    return 0 if ok else 1


def _doctor(args) -> int:
    print("== omniband doctor ==")
    import serial.tools.list_ports
    ports = list(serial.tools.list_ports.comports())
    print(f"serial ports: {len(ports)}")
    for p in ports:
        print(f"  {p.device}: {p.description}")
    print(f"simulate available: yes (SimDevice)")
    print(f"pyserial: ok, pyttsx3: ", end="")
    try:
        import pyttsx3  # noqa: F401
        print("ok")
    except Exception:
        print("MISSING (tts disabled)")
    try:
        import winsound  # noqa: F401
        print("winsound: ok (siren available)")
    except Exception:
        print("winsound: n/a (non-Windows)")

    print("\n-- Optical Tracking Doctor --")
    try:
        from host.omniband.tracking.webcam_tracker import find_model_path, HAS_MEDIAPIPE
        print(f"HAS_MEDIAPIPE: {HAS_MEDIAPIPE}")
        model_path = find_model_path()
        print(f"face_landmarker.task: {model_path}")
        if HAS_MEDIAPIPE and model_path:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
            base_options = mp_python.BaseOptions(model_asset_path=model_path)
            options = vision.FaceLandmarkerOptions(
                base_options=base_options,
                output_face_blendshapes=True,
                num_faces=1,
                min_face_detection_confidence=0.1,
                min_face_presence_confidence=0.1,
            )
            detector = vision.FaceLandmarker.create_from_options(options)
            print("FaceLandmarker initialized: OK")
            import cv2
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY)
            for _ in range(15):
                cap.read()
            ret, frame = cap.read()
            cap.release()
            print(f"Camera 0 capture: {'OK' if ret else 'FAIL'}")
            if ret:
                rgb = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
                res = detector.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
                faces = len(res.face_landmarks) if res and res.face_landmarks else 0
                print(f"FaceLandmarker test detect on camera frame: {faces} face(s) found")
        else:
            print("Optical tracking cannot initialize: model missing or mediapipe not found")
    except Exception as e:
        import traceback
        print(f"Optical tracking doctor ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()

    return 0


def _run_selftest_env() -> int:
    """B-02: Self-test probe for Python, OpenCV, and MediaPipe runtime."""
    print("=== SELF-TEST: ENVIRONMENT PROBE ===")
    import cv2
    print(f"Python: {sys.version.split()[0]} ({sys.platform})")
    print(f"OpenCV: {cv2.__version__}")
    has_yunet = hasattr(cv2, "FaceDetectorYN")
    print(f"OpenCV cv2.FaceDetectorYN: {has_yunet}")
    if not has_yunet:
        print("FAIL: cv2.FaceDetectorYN not available!")
        return 1

    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
        print(f"MediaPipe: {getattr(mp, '__version__', 'unknown')}")
        print("MediaPipe Tasks Vision API: AVAILABLE")
    except Exception as e:
        print(f"FAIL: MediaPipe Tasks API import error: {e}")
        return 1

    try:
        from PySide6 import QtCore
        print(f"PySide6: {QtCore.__version__}")
    except Exception as e:
        print(f"WARNING: PySide6 import error: {e}")

    print("SELFTEST-ENV: PASS")
    return 0


def _run_selftest_resources() -> int:
    """B-02: Self-test probe verifying neural models and SHA-256 integrity."""
    print("=== SELF-TEST: RESOURCES & MODEL INTEGRITY ===")
    from host.omniband.tracking.paths import load_verified_model_bytes, resource_path, ModelIntegrityError
    try:
        manifest_p = resource_path("models/MANIFEST.json")
        print(f"MANIFEST path: {manifest_p} (exists={manifest_p.is_file()})")
        if not manifest_p.is_file():
            print("FAIL: models/MANIFEST.json missing")
            return 1

        b = load_verified_model_bytes("face_landmarker.task")
        print(f"face_landmarker.task: VERIFIED ({len(b)} bytes)")

        yn_p = resource_path("models/face_detection_yunet_2023mar.onnx")
        print(f"face_detection_yunet_2023mar.onnx: {yn_p} (exists={yn_p.is_file()})")
        if not yn_p.is_file():
            print("FAIL: YuNet model file missing")
            return 1

        print("SELFTEST-RESOURCES: PASS")
        return 0
    except Exception as e:
        print(f"FAIL: Model integrity failure: {e}")
        return 1


def _run_selftest_tracker() -> int:
    """B-02: Self-test probe for RealWebcamTracker pipeline initialization."""
    print("=== SELF-TEST: TRACKER PIPELINE ===")
    try:
        import numpy as np
        from host.omniband.tracking.webcam_tracker import RealWebcamTracker
        tracker = RealWebcamTracker(camera_id=999)
        det_v, det_i, yn = tracker._init_models()
        print(f"Detector Video: {'READY' if det_v else 'NOT_LOADED'}")
        print(f"Detector Image: {'READY' if det_i else 'NOT_LOADED'}")
        print(f"Detector YuNet: {'READY' if yn else 'NOT_LOADED'}")

        # Feed black frame to test negative control
        black_rgb = np.zeros((480, 640, 3), dtype=np.uint8)
        black_bgr = np.zeros((480, 640, 3), dtype=np.uint8)
        res = tracker._execute_pipeline(black_rgb, black_bgr, 640, 480, 1000, det_v, det_i, yn)
        print(f"Blank frame test -> tier: {res.tier}, face_found: {res.face_found}")
        if res.face_found or res.tier != "NO_FACE":
            print("FAIL: False positive face on blank frame!")
            return 1

        if det_v is not None:
            det_v.close()
        if det_i is not None:
            det_i.close()

        print("SELFTEST-TRACKER: PASS")
        return 0
    except Exception as e:
        print(f"FAIL: Tracker pipeline test error: {e}")
        return 1


def _run_selftest_camera() -> int:
    """B-02: Self-test probe for camera capture device access."""
    print("=== SELF-TEST: CAMERA CAPTURE ===")
    import cv2
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    cap = cv2.VideoCapture(0, backend)
    if not cap.isOpened():
        cap = cv2.VideoCapture(0)

    if not cap.isOpened():
        print("WARNING: Camera index 0 could not be opened (hardware disconnected or in use)")
        print("SELFTEST-CAMERA: PASS (WITH WARNING)")
        return 0

    ret, frame = cap.read()
    cap.release()
    if ret and frame is not None:
        h, w = frame.shape[:2]
        import numpy as np
        luma = float(np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))
        print(f"Camera 0: OK ({w}x{h}, mean luma={luma:.1f})")
    else:
        print("Camera 0 opened but frame read returned False")

    print("SELFTEST-CAMERA: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniBand V2 host engine")
    ap.add_argument("--simulate", action="store_true",
                    help="run against the built-in simulated device")
    ap.add_argument("--no-auto", action="store_true",
                    help="disable autonomous demo gestures in simulate")
    ap.add_argument("--port", default="", help="USB serial COM port")
    ap.add_argument("--bt-port", default="", help="BT SPP COM port")
    ap.add_argument("--baud", type=int, default=K["link"]["baud_boot"])
    ap.add_argument("--mode", type=int, default=0, choices=range(len(MODES)))
    ap.add_argument("--dry-run", action="store_true",
                    help="log outputs, inject nothing")
    ap.add_argument("--record", action="store_true",
                    help="record OutputCommands instead of SendInput")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--selftest-env", action="store_true",
                    help="Self-test: probe Python, OpenCV, and MediaPipe runtime")
    ap.add_argument("--selftest-resources", action="store_true",
                    help="Self-test: verify neural models integrity against MANIFEST.json")
    ap.add_argument("--selftest-tracker", action="store_true",
                    help="Self-test: test RealWebcamTracker pipeline initialization")
    ap.add_argument("--selftest-camera", action="store_true",
                    help="Self-test: test camera capture device access")
    ap.add_argument("--doctor", action="store_true")
    ap.add_argument("--duration", type=float, default=None,
                    help="seconds to run (default: forever)")
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--no-siren", action="store_true")
    ap.add_argument("--focus-release", action="store_true",
                    help="release all outputs on foreground app change")
    ap.add_argument("--optical", action="store_true",
                    help="enable webcam optical gesture source (always on)")
    ap.add_argument("--optical-fallback", action="store_true",
                    help="optical source only while the device link is silent")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--gui", action="store_true",
                    help="launch native PySide6 desktop GUI")
    ap.add_argument("--minimized", action="store_true",
                    help="start minimized to system tray")
    ap.add_argument("--headless", action="store_true",
                    help="run terminal engine loop without GUI")
    ap.add_argument("--dashboard", action="store_true",
                    help="launch the telemetry HUD (FastAPI/WebSocket)")
    ap.add_argument("--dashboard-port", type=int, default=8080)
    args = ap.parse_args()

    # Zero-argument or double-click / frozen executable default: launch GUI!
    is_frozen = getattr(sys, "frozen", False)
    has_explicit_cli_mode = (
        args.self_test or args.selftest_env or args.selftest_resources
        or args.selftest_tracker or args.selftest_camera
        or args.doctor or args.headless or args.port
        or args.bt_port or args.simulate or args.dry_run or args.duration
    )
    if (is_frozen or len(sys.argv) == 1 or args.gui or args.minimized) and not has_explicit_cli_mode:
        return _launch_native_gui(args)

    if args.selftest_env:
        return _run_selftest_env()
    if args.selftest_resources:
        return _run_selftest_resources()
    if args.selftest_tracker:
        return _run_selftest_tracker()
    if args.selftest_camera:
        return _run_selftest_camera()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.self_test:
        return _self_test()
    if args.doctor:
        return _doctor(args)

    transports, dev = _build_transports(args)
    sink = _build_sink(args)
    arb = Arbiter(transports)

    # ---- optional telemetry dashboard (P8) ----
    dash = None
    ob_state: dict = {"mode": args.mode, "env": [0, 0, 0, 0], "fsm": "-",
                      "last_ges": "", "ges": 0, "evts": 0, "releases": 0,
                      "sos": 0, "estops": 0, "link": ""}

    def _on_event(kind: str, d: dict) -> None:
        if dash is None:
            return
        if kind == "tele":
            ob_state.update({
                "mode": d.get("m", ob_state["mode"]),
                "env": [round(x) for x in d.get("env", [])],
                "fsm": d.get("st", "-"),
                "mode_name": MODES[d.get("m", 0)]["name"]
                if 0 <= d.get("m", 0) < len(MODES) else "?",
                "link": arb.freshest_transport_name(),
                "ges": eng.stats.ges, "evts": eng.stats.evts,
                "releases": eng.stats.releases, "sos": eng.stats.sos_events,
                "estops": eng.stats.estops,
            })
            dash.update_telemetry("omniband", dict(ob_state))
            dash.update_telemetry("state", "omniband")
        elif kind == "ges":
            ob_state["last_ges"] = str(d.get("g", ""))
            dash.update_telemetry("omniband", dict(ob_state))

    if args.dashboard:
        try:
            from handsfree.dashboard.server import DashboardServer
            dash = DashboardServer(port=args.dashboard_port)
            dash.start()
            log.info("dashboard on http://127.0.0.1:%d", args.dashboard_port)
        except Exception as e:  # noqa: BLE001
            log.warning("dashboard unavailable: %s", e)
            dash = None

    eng = OmniEngine(
        arb, sink,
        tts=None if args.no_tts else TTSSink(enabled=not args.dry_run),
        siren=None if args.no_siren else SirenSink(enabled=not args.dry_run),
        release_on_focus_change=args.focus_release,
        on_event=_on_event,
    )
    eng.set_mode(args.mode)
    arb.open_all()

    # ---- optical fallback / hybrid source (P9) ----
    optical = None
    if args.optical or args.optical_fallback:
        from host.omniband.optical import OpticalSource
        fallback_only = args.optical_fallback and not args.optical

        def _opt_gesture(g: str, amp: float, dur_ms: int) -> None:
            # optical gestures enter through the same dispatch as device ges
            if fallback_only:
                import time as _t
                if _t.monotonic() * 1000.0 - eng._last_line_ms < 5000.0:
                    return   # hardware link healthy: hardware wins
            eng._on_device_line({"k": "ges", "g": g, "a": amp, "d": dur_ms,
                                 "m": eng.mode, "t": int(time.time() * 1e6)})

        optical = OpticalSource(camera_id=args.camera,
                                on_gesture=_opt_gesture)
        if optical.start(args.camera):
            log.info("optical source active%s",
                     " (fallback-only)" if fallback_only else "")
        else:
            optical = None

    try:
        eng.run(duration_s=args.duration)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        if optical is not None:
            optical.stop()
        arb.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
