"""Integration tests for the handsfree/ bridge (Binore).

Proves the teammates' REAL modules (handsfree.adapters.*, core.fastpath/slowpath/mapping,
profiles/*.json) run through main.py's poll()/process()/handle() contracts with no name
mismatches. No camera, mic, vgamepad or mediapipe needed: the adapters degrade to mock/passive
modes and the gateway uses RecordingBackend.
"""
import sys
import time

import main as app
from config import parse_config
from core.events import Intent, SignalEvent
from gateway import Gateway, RecordingBackend
from handsfree.core.events import SignalEvent as HFEvent
from handsfree.core.events import SignalType
from state_machine import SMConfig, State, StateMachine

DT = 1 / 60


def hf(st, name, value):
    return HFEvent(signal_type=st, name=name, value=value, timestamp=time.time())


# ----------------------------------------------------------------- signal translation
def test_pose_becomes_face_and_head_events():
    out = app._hf_events(hf(SignalType.HEAD_POSE, "pose",
                            {"yaw": 12.0, "pitch": -4.0, "roll": 1.5}), 42.0, "webcam")
    by_name = {e.name: e for e in out}
    assert by_name["face"].value == 1.0 and by_name["face"].src == "webcam"
    assert by_name["head_yaw"].value == 12.0 and by_name["head_yaw"].t == 42.0
    assert by_name["head_pitch"].value == -4.0 and by_name["head_roll"].value == 1.5


def test_blendshape_dict_becomes_one_event_per_key():
    out = app._hf_events(hf(SignalType.BLENDSHAPE, "blendshapes",
                            {"jawOpen": 0.8, "browInnerUp": 0.1, "junk": "x"}), 1.0, "webcam")
    names = {e.name: e.value for e in out}
    assert names == {"jawOpen": 0.8, "browInnerUp": 0.1}        # non-numeric dropped


def test_audio_pop_and_speech():
    pop = app._hf_events(hf(SignalType.AUDIO_POP, "pop", 1.0), 1.0, "audio")
    assert [(e.name, e.src) for e in pop] == [("pop", "mic")]
    speech = app._hf_events(hf(SignalType.AUDIO_SPEECH, "speech_command", "freeze"), 1.0, "audio")
    assert speech[0].name == "speech_command" and speech[0].value == 1.0   # text cannot cross


def test_switch_f12_becomes_kill_and_other_keys_pass_through():
    kill = app._hf_events(hf(SignalType.SWITCH, "f12", 1.0), 1.0, "switch")
    assert [(e.name, e.value) for e in kill] == [("kill", 1.0)]
    assert app._hf_events(hf(SignalType.SWITCH, "f12", 0.0), 1.0, "switch") == []
    esc = app._hf_events(hf(SignalType.SWITCH, "escape", 1.0), 1.0, "switch")
    assert esc[0].name == "escape" and esc[0].src == "switch"


# ----------------------------------------------------------------- producer bridge
def _producer():
    return app._HandsfreeProducer()


def _ticks(p, events, start=100.0, n=20):
    """Feed the same events for n ticks (slowpath debounce needs ~150 ms sustained)."""
    out: list = []
    for i in range(n):
        out.extend(p.process([SignalEvent(e.src, e.name, e.value, start + i * DT)
                              for e in events], start + i * DT))
    return out


def test_producer_emits_stick_and_hold_for_real_pipeline():
    p = _producer()
    out = _ticks(p, [SignalEvent("webcam", "head_yaw", 10.0, 0.0),
                     SignalEvent("webcam", "head_pitch", 0.0, 0.0),
                     SignalEvent("webcam", "jawOpen", 0.9, 0.0)])
    kinds = {(i.kind, i.name) for i in out}
    assert ("stick", "head") in kinds
    assert ("hold", "jawOpen") in kinds


def test_producer_releases_gesture_when_blendshape_drops():
    p = _producer()
    _ticks(p, [SignalEvent("webcam", "jawOpen", 0.9, 0.0)], n=20)
    out = _ticks(p, [SignalEvent("webcam", "jawOpen", 0.1, 0.0)], start=200.0, n=20)
    assert Intent("release", "jawOpen") in out
    out2 = p.process([], 300.0)
    assert not any(i.name == "jawOpen" for i in out2)           # no stale hold refresh


def test_producer_speech_activity_reaches_slowpath():
    p = _producer()
    p.process([SignalEvent("mic", "speech_active", 1.0, 0.0)], 0.0)
    assert p.slowpath.speech_active is True


def test_producer_switch_key_becomes_hold_and_release():
    p = _producer()
    out = p.process([SignalEvent("switch", "escape", 1.0, 0.0)], 0.0)
    assert any(i.kind == "hold" and i.name == "escape" for i in out)
    out = p.process([SignalEvent("switch", "escape", 0.0, 0.05)], 0.05)
    assert Intent("release", "escape") in out


# ----------------------------------------------------------------- mapper bridge
def _mapper(profile="supertuxkart"):
    return app._HandsfreeMapper(profile)


def test_handsfree_mapper_super_tux_kart_jaw_open_is_button_a():
    m = _mapper("supertuxkart")
    cmds = m.handle(Intent("hold", "jawOpen", ms=0), 0.0)
    assert any(c.kind == "button" and c.name == "A" and c.pressed for c in cmds)
    cmds = m.handle(Intent("release", "jawOpen"), 0.01)
    assert any(c.kind == "button" and c.name == "A" and c.pressed is False for c in cmds)


def test_handsfree_mapper_cancel_all_releases_pressed_and_zeroes_axes():
    m = _mapper()
    m.handle(Intent("hold", "jawOpen", ms=0), 0.0)
    m.handle(Intent("stick", "head", x=0.5, y=-0.5), 0.01)
    out = m.handle(Intent("cancel_all"), 0.02)
    kinds = {(c.kind, c.name, c.pressed if c.pressed is not None else c.value)
             for c in out}
    assert ("button", "A", False) in kinds
    assert all(not c.pressed for c in out if c.kind in ("key", "button"))
    assert any(c.kind == "axis" and c.value == 0.0 for c in out)


def test_handsfree_mapper_unmapped_action_is_silent_not_fatal():
    m = _mapper()
    assert m.handle(Intent("hold", "no_such_gesture", ms=0), 0.0) == []


def test_handsfree_mapper_layer_switch_releases():
    m = _mapper()
    m.handle(Intent("hold", "jawOpen", ms=0), 0.0)
    out = m.handle(Intent("layer", "shift"), 0.01)
    assert any(c.kind == "button" and c.pressed is False for c in out)


# ----------------------------------------------------------------- end to end
def _engine(profile="supertuxkart"):
    backend = RecordingBackend()
    gw = Gateway(backend)
    sm = StateMachine(SMConfig())
    eng = app.Engine(sm, _mapper(profile), gw, [_producer()])
    return eng, backend, sm, gw


def _run_jaw(eng, t, ticks=20):
    for i in range(ticks):
        eng.step([SignalEvent("webcam", "jawOpen", 0.9, t + i * DT)], t + i * DT)
    return t + ticks * DT


def test_end_to_end_jaw_open_fires_and_face_loss_releases():
    eng, backend, sm, gw = _engine()
    t = _run_jaw(eng, 100.0)
    assert sm.state == State.LATCHED and gw.held
    eng.step([SignalEvent("webcam", "face", 0.0, t)], t)
    assert sm.state == State.SAFE_IDLE and not gw.held


def test_end_to_end_pop_while_latched_cancels_in_one_tick():
    eng, backend, sm, gw = _engine()
    t = _run_jaw(eng, 100.0)
    assert gw.held
    eng.step([SignalEvent("mic", "pop", 1.0, t)], t)
    assert not gw.held and sm.state == State.ACTIVE


def test_handsfree_source_webcam_mock_loop_bridges(tmp_path):
    """The REAL WebcamAdapter (mock mode, no mediapipe here) through _HFSource.poll()."""
    src = app._HFSource("webcam")
    try:
        deadline = time.time() + 2.0
        got = []
        while time.time() < deadline and not got:
            got = src.poll(time.perf_counter())
            if not got:
                time.sleep(0.02)
        names = {e.name for e in got}
        assert "face" in names and "head_yaw" in names
        assert all(e.t > 0 for e in got)
    finally:
        src.close()


def test_handsfree_source_switch_and_audio_start_clean():
    for kind in ("audio", "switch"):                     # passive/mock modes without deps
        src = app._HFSource(kind)
        src.close()
        assert src.poll(0.0) == []


# ----------------------------------------------------------------- config additions
def test_config_watchdog_and_cancel_events():
    cfg, warns = parse_config({"watchdog_ms": 300,
                               "cancel_events": ["pop", "bogus", "hard_blink"]})
    assert cfg.watchdog_ms == 300
    assert cfg.cancel_events == ("pop", "hard_blink")
    assert any("bogus" in w for w in warns)


def test_config_cancel_events_clamped_to_known_names():
    cfg, _ = parse_config({"cancel_events": "pop"})
    assert cfg.cancel_events == ("pop", "hard_blink", "voice_cancel", "voice_stop")


# ----------------------------------------------------------------- output stays SendInput
def test_gateway_never_imports_vgamepad():
    assert "vgamepad" not in sys.modules
    import gateway
    assert "vgamepad" not in gateway.__dict__ and "pydirectinput" not in gateway.__dict__


# ----------------------------------------------------------------- mock input must be impossible to miss
def test_hf_source_flags_mock_when_mediapipe_missing(monkeypatch):
    import handsfree.adapters.webcam as w
    monkeypatch.setattr(w, "HAS_MEDIAPIPE", False)
    src = app._HFSource("webcam")
    try:
        assert src.is_mock is True and "mediapipe" in src.mock_reason
    finally:
        src.close()


def test_hf_source_flags_mock_when_camera_cannot_open(monkeypatch):
    try:
        import cv2
    except ImportError:
        import types
        cv2 = types.ModuleType("cv2")
        cv2.CAP_PROP_FRAME_WIDTH = 3
        cv2.CAP_PROP_FRAME_HEIGHT = 4
        monkeypatch.setitem(sys.modules, "cv2", cv2)

    import handsfree.adapters.webcam as w
    monkeypatch.setattr(w, "cv2", cv2, raising=False)
    monkeypatch.setattr(w, "HAS_CV2", True)
    monkeypatch.setattr(w, "HAS_MEDIAPIPE", True)

    class _DeadCap:
        def isOpened(self):
            return False

        def set(self, *a):
            return False

        def read(self):
            return False, None

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoCapture", lambda *a, **k: _DeadCap(), raising=False)
    src = app._HFSource("webcam")
    try:
        assert src.is_mock is True and "camera" in src.mock_reason
    finally:
        src.close()


def test_hf_source_flags_mock_when_audio_and_switch_deps_missing(monkeypatch):
    import handsfree.adapters.audio as au
    import handsfree.adapters.switch as sw
    monkeypatch.setattr(au, "HAS_PYAUDIO", False)
    monkeypatch.setattr(sw, "HAS_KEYBOARD", False)
    audio, switch = app._HFSource("audio"), app._HFSource("switch")
    try:
        assert audio.is_mock is True and "pyaudio" in audio.mock_reason
        assert switch.is_mock is True and "keyboard" in switch.mock_reason
    finally:
        audio.close()
        switch.close()


def test_engine_status_exposes_mock_sources():
    eng, *_ = _engine()
    eng.mock_sources = (("webcam", "missing mediapipe"),)
    assert eng.status()["mock_sources"] == ["webcam: missing mediapipe"]
    eng.mock_sources = ()
    assert eng.status()["mock_sources"] == []


def test_tray_tooltip_warns_on_mock():
    import tray
    assert "MOCK INPUT" in tray.tooltip({"state": "ACTIVE", "mock_sources": ["webcam: x"]})
    assert "MOCK INPUT" not in tray.tooltip({"state": "ACTIVE"})


def test_replay_source_is_flagged_as_simulated(tmp_path):
    from adapters.replay import ReplaySource
    f = tmp_path / "s.jsonl"
    f.write_text('{"name": "face", "value": 1.0, "t": 0.0}\n', encoding="utf-8")
    src = ReplaySource(str(f))
    assert src.is_mock is True and src.mock_reason == "replay file"
