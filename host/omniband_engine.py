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
    if args.dry_run or args.simulate:
        return DryRunSink()
    # real path: reuse the existing unbound Gateway + SendInput backend
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
    if not ports:
        log.error("no transport: pass --simulate or --port COMx")
        sys.exit(2)
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
    ap.add_argument("--dashboard", action="store_true",
                    help="launch the telemetry HUD (FastAPI/WebSocket)")
    ap.add_argument("--dashboard-port", type=int, default=8080)
    args = ap.parse_args()

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
        eng.run(duration_s=args.duration or (30.0 if args.simulate else None))
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        if optical is not None:
            optical.stop()
        arb.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
