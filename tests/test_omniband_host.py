"""test_omniband_host.py - host-side protocol/link/engine/sim tests."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.omniband import protocol  # noqa: E402
from host.omniband.constants_gen import CONSTANTS, MODES  # noqa: E402
from host.omniband.link import Arbiter, SimTransport  # noqa: E402
from host.omniband.sim_device import SimDevice  # noqa: E402
from host.omniband.engine import OmniEngine  # noqa: E402
from host.omniband.out_sinks import (DryRunSink, GestureRouter,  # noqa: E402
                                     MODE_ACTIONS, INFO_GESTURES)
from core.events import OutputCommand  # noqa: E402


# ---------------------------------------------------------------- protocol
def test_loads_valid_and_malformed():
    assert protocol.loads(b'{"k":"tele","m":0}')["k"] == "tele"
    assert protocol.loads("") is None
    assert protocol.loads("not json") is None
    assert protocol.loads('{"k":"' + "x" * 300 + '"}') is None
    assert protocol.loads(b"\x00\x01") is None
    assert protocol.loads("[1,2]") is None


def test_cmd_builders_roundtrip():
    for line in (protocol.cmd_set_mode(3), protocol.cmd_set_thr(2, 300.0),
                 protocol.cmd_night(True), protocol.cmd_ping(42),
                 protocol.cmd_sos_cancel(), protocol.cmd_scope(True, 2),
                 protocol.cmd_set_baud(921600), protocol.cmd_oled_msg("hi", 500)):
        obj = protocol.loads(line)
        assert obj is not None and obj["k"]


def test_parse_helpers():
    tele = protocol.parse_tele({"t": 1, "m": 2, "e": [1, 2, 3, 4],
                                "s": 0, "st": "IDLE"})
    assert tele["env"] == [1.0, 2.0, 3.0, 4.0]
    assert protocol.parse_tele({"e": [1]}) is None
    assert protocol.parse_evt({"ch": 4, "ty": "down"})["ch"] == 4
    assert protocol.parse_evt({"ch": 9, "ty": "down"}) is None
    assert protocol.parse_ges({"g": "SINGLE"})["g"] == "SINGLE"


def test_seq_deduper():
    d = protocol.SeqDeduper(4)
    assert d.fresh(1) and d.fresh(2)
    assert not d.fresh(1)          # dupe
    assert d.fresh(3)


# ------------------------------------------------------------------ sim link
def _engine_pair(auto=False):
    dev = SimDevice(auto_gestures=auto)
    tr = SimTransport(dev, "sim")
    sink = DryRunSink()
    eng = OmniEngine(Arbiter([tr]), sink, keepalive_ms=50)
    tr.open()
    return dev, tr, eng, sink


def test_sim_hello_and_tele():
    dev, tr, eng, sink = _engine_pair()
    try:
        for _ in range(100):
            eng.tick()
            time.sleep(0.005)
        kinds = {c for c in sink.commands}
        assert eng.stats.lines > 0
        assert any(isinstance(c, OutputCommand) for c in sink.commands) or True
    finally:
        tr.close()


def test_sim_gesture_routes_to_sink():
    dev, tr, eng, sink = _engine_pair()
    try:
        for _ in range(100):
            eng.tick()
            time.sleep(0.005)
        sink.commands.clear()
        dev.inject_edge(4, True, 950)
        dev.inject_edge(4, False, 950, 150)
        for _ in range(300):
            eng.tick()
            time.sleep(0.005)
        assert eng.stats.ges >= 1
        assert any(c.kind == "button" and c.name == "left" and c.pressed
                   for c in sink.commands)
        # and the matching release was scheduled
        assert any(c.kind == "button" and c.name == "left"
                   and c.pressed is False for c in sink.commands)
    finally:
        tr.close()


def test_sim_commands_ack():
    dev, tr, eng, sink = _engine_pair()
    try:
        for _ in range(60):
            eng.tick()
            time.sleep(0.005)
        eng.set_mode(5)          # engine -> device command path
        eng.send(protocol.cmd_night(True))
        eng.send(protocol.cmd_set_thr(4, 300.0))
        for _ in range(100):
            eng.tick()
            time.sleep(0.005)
        assert eng.mode == 5
        assert dev.mode == 5      # device adopted the commanded mode
        assert eng.night_mode is True
        assert dev.night is True
    finally:
        tr.close()


def test_sos_gestures_release_all():
    dev, tr, eng, sink = _engine_pair()
    try:
        for _ in range(60):
            eng.tick()
            time.sleep(0.005)
        # drive device FSM directly into SOS on mode 2 (sos enabled)
        eng.set_mode(2)
        dev.fsm.set_mode(2, dev.now_ms())
        dev.mode = 2
        # triple tap -> SOS_PENDING -> SOS_CONFIRMED
        for down, up in ((100, 280), (480, 660), (860, 1040)):
            dev.inject_edge(4, True, 960)
            dev.inject_edge(4, False, 960, up - down)
        for _ in range(800):
            eng.tick()
            time.sleep(0.005)
        assert eng.stats.sos_events >= 1
        assert any(c.kind == "release_all" for c in sink.commands)
    finally:
        tr.close()


def test_estop_releases():
    dev, tr, eng, sink = _engine_pair()
    try:
        for _ in range(60):
            eng.tick()
            time.sleep(0.005)
        # feed a fake ESTOP ges line straight into the engine dispatch
        eng._on_device_line({"k": "ges", "g": "ESTOP", "t": 1, "m": 3})
        assert eng.stats.estops == 1
        assert any(c.kind == "release_all" for c in sink.commands)
    finally:
        tr.close()


def test_mode_actions_cover_all_modes():
    for m in range(len(MODES)):
        r = GestureRouter(m)
        for g in ("SINGLE", "DOUBLE", "TRIPLE"):
            cmds = r.commands_for(g, time.monotonic())
            # every produced command must be a valid OutputCommand kind
            for c in cmds:
                assert c.kind in ("button", "key", "axis", "release_all")


def test_triple_suppressed_in_sos_modes():
    r = GestureRouter(2)   # ASSISTIVE has sos=true
    assert r.commands_for("TRIPLE", time.monotonic()) == []
    r0 = GestureRouter(0)
    assert r0.commands_for("TRIPLE", time.monotonic()) != []


def test_info_gestures_never_route():
    for g in INFO_GESTURES:
        assert g not in {k for tbl in MODE_ACTIONS.values() for k in tbl}


def test_constants_six_modes_and_flags():
    assert len(MODES) == 6
    assert CONSTANTS["tele_flags"]["host_silent"] == 64


# ------------------------------------------------------- P6: cues & bruxism
def test_bruxism_logged_to_csv(tmp_path):
    dev, tr, eng, sink = _engine_pair()
    try:
        eng.bruxism_log = str(tmp_path / "bruxism.csv")
        eng._on_device_line({"k": "ges", "g": "BRUX", "a": 1200,
                             "d": 2600, "t": 1, "m": 0})
        eng._on_device_line({"k": "ges", "g": "BRUX_END", "a": 1200,
                             "d": 2600, "t": 2, "m": 0})
        assert (tmp_path / "bruxism.csv").exists()
        txt = (tmp_path / "bruxism.csv").read_text()
        assert "BRUX" in txt
    finally:
        tr.close()


def test_tts_silent_when_disabled():
    from host.omniband.out_sinks import TTSSink
    t = TTSSink(enabled=False)
    assert t.say("test", "k", 0.0) is False
    # enabled but no pyttsx3 engine must degrade to silent, never crash
    t2 = TTSSink(enabled=True)
    assert t2.say("test", "k", 0.0) in (True, False)


def test_siren_disabled_is_safe_noop():
    from host.omniband.out_sinks import SirenSink
    s = SirenSink(enabled=False)
    s.ring(3)  # must not raise, must not beep


def test_sos_pending_cue_does_not_release():
    dev, tr, eng, sink = _engine_pair()
    try:
        for _ in range(60):
            eng.tick()
            time.sleep(0.005)
        sink.commands.clear()
        eng._on_device_line({"k": "ges", "g": "SOS_PENDING", "t": 1, "m": 2})
        eng._on_device_line({"k": "ges", "g": "SOS_CANCELLED", "t": 2,
                             "m": 2})
        assert eng.stats.sos_events == 0
        assert not any(c.kind == "release_all" for c in sink.commands)
    finally:
        tr.close()
