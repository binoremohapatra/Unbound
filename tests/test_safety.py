"""Safety tests (Binore). Pure Python: no Windows, no camera, no mic needed.

The four rules the demo must never break:
  (a) face loss releases every key on the first tick (spec limit: 300 ms)
  (b) a deliberate blink / tongue click cancels an active latch within one tick
  (c) CONVERSE_LOCK blocks gesture intents but never traps a release
  (d) a window switch releases everything
"""
import ctypes
import json
import threading
import time

import pytest

from adapters.replay import Recorder, ReplaySource
from core.events import Intent, OutputCommand, SignalEvent
from gateway import (
    INPUT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    MOUSEEVENTF_MOVE,
    Gateway,
    RecordingBackend,
    build_key_input,
    build_mouse_button_input,
    build_mouse_move_input,
)
from main import BasicMapper, DefaultProducer, Engine
from state_machine import SMConfig, State, StateMachine
from watchdog import LatencyLog, Watchdog

DT = 1 / 60


def ev(name, value=1.0, t=0.0, src="webcam"):
    return SignalEvent(src, name, value, t)


def make_engine(**kw):
    backend = RecordingBackend()
    gw = Gateway(backend)
    sm = StateMachine(SMConfig(**kw))
    return Engine(sm, BasicMapper(), gw, [DefaultProducer()]), backend, sm, gw


def run(engine, events_by_tick, ticks, t0=100.0):
    """Drive the engine one 60 Hz tick at a time. events_by_tick: {tick_index: [events]}."""
    for i in range(ticks):
        now = t0 + i * DT
        evs = [SignalEvent(e.src, e.name, e.value, now, e.conf) for e in events_by_tick.get(i, [])]
        engine.step(evs, now)
    return t0 + ticks * DT


# ----------------------------------------------------------------- struct / scancode layout
def test_input_struct_is_40_bytes_on_x64():
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(INPUT) == 40


def test_key_input_uses_scancodes_and_extended_flag():
    w = build_key_input("w", True).u.ki
    assert w.wScan == 0x11 and w.wVk == 0 and w.dwFlags == KEYEVENTF_SCANCODE
    up = build_key_input("w", False).u.ki
    assert up.dwFlags == KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP
    arrow = build_key_input("left", True).u.ki
    assert arrow.dwFlags & KEYEVENTF_EXTENDEDKEY


def test_mouse_inputs():
    m = build_mouse_move_input(-5, 7).u.mi
    assert (m.dx, m.dy, m.dwFlags) == (-5, 7, MOUSEEVENTF_MOVE)
    assert build_mouse_button_input("left", True).u.mi.dwFlags == 0x0002
    assert build_mouse_button_input("left", False).u.mi.dwFlags == 0x0004


# ----------------------------------------------------------------- (a) face loss
def test_face_loss_releases_everything_on_first_tick():
    eng, backend, sm, gw = make_engine()
    run(eng, {0: [ev("jaw_open", 0.9)]}, 3)
    assert backend.mouse == {"left"}                           # fire (RT) really is held
    assert sm.state == State.LATCHED
    run(eng, {0: [ev("face", 0.0)]}, 1, t0=101.0)
    assert not backend.keys and not backend.mouse and not gw.held
    assert sm.state == State.SAFE_IDLE


def test_safe_idle_blocks_input_until_face_back_300ms():
    eng, backend, sm, gw = make_engine()
    run(eng, {0: [ev("face", 0.0)]}, 1)
    run(eng, {0: [ev("jaw_open", 0.9)]}, 2, t0=100.1)           # blocked
    assert not gw.held and sm.dropped >= 1
    t = 101.0
    eng.step([SignalEvent("webcam", "face", 1.0, t)], t)
    eng.step([], t + 0.2)
    assert sm.state == State.SAFE_IDLE                         # 200 ms: still waiting
    eng.step([], t + 0.31)
    assert sm.state == State.ACTIVE


def test_face_flicker_resets_regain_timer():
    sm = StateMachine()
    sm.on_event(ev("face", 0.0), 0.0)
    sm.on_event(ev("face", 1.0), 1.0)
    sm.on_event(ev("face", 0.0), 1.2)                          # lost again
    sm.on_event(ev("face", 1.0), 1.25)
    sm.tick(1.5)                                               # only 250 ms of continuous face
    assert sm.state == State.SAFE_IDLE
    sm.tick(1.56)
    assert sm.state == State.ACTIVE


# ----------------------------------------------------------------- latch expiry / caps
def test_hold_expires_and_releases():
    eng, backend, sm, gw = make_engine()
    t = run(eng, {0: [ev("jaw_open", 0.9)]}, 1)
    assert sm.state == State.LATCHED
    eng.step([], t + 1.7)
    assert sm.state == State.LATCHED
    eng.step([], t + 1.85)                                     # past 1800 ms
    assert sm.state == State.ACTIVE and not gw.held


def test_hold_is_capped_at_max_hold_ms():
    sm = StateMachine(SMConfig(max_hold_ms=3000))
    out = sm.gate([Intent("hold", "fire", ms=60000)], 10.0)
    assert out[0].ms == 3000
    assert sm.tick(13.01) == [Intent("release", "fire")]


def test_hold_with_zero_ms_uses_default():
    sm = StateMachine(SMConfig(default_hold_ms=1800))
    assert sm.gate([Intent("hold", "fire", ms=0)], 0.0)[0].ms == 1800


# ----------------------------------------------------------------- (b) instant cancel
@pytest.mark.parametrize("name", ["hard_blink", "pop", "voice_cancel", "voice_stop"])
def test_cancel_signals_release_latch_in_one_tick(name):
    eng, backend, sm, gw = make_engine()
    run(eng, {0: [ev("jaw_open", 0.9)]}, 2)
    assert gw.held
    run(eng, {0: [ev(name)]}, 1, t0=100.5)
    assert not gw.held and sm.state == State.ACTIVE


def test_cancel_event_is_consumed_only_while_latched():
    sm = StateMachine()
    _, consumed = sm.on_event(ev("pop"), 0.0)
    assert consumed is False                                   # nothing held: producers may use it
    sm.gate([Intent("hold", "fire", ms=1000)], 0.0)
    out, consumed = sm.on_event(ev("pop"), 0.1)
    assert consumed is True and out == [Intent("cancel_all")]  # empties mapper-held toggles too


def test_cancel_events_list_is_configurable():
    sm = StateMachine(SMConfig(cancel_events=("pop",)))
    sm.gate([Intent("hold", "fire", ms=1000)], 0.0)
    out, consumed = sm.on_event(ev("voice_stop"), 0.1)          # removed from the list
    assert not consumed and out == [] and sm.state == State.LATCHED
    out, consumed = sm.on_event(ev("pop"), 0.2)
    assert consumed and sm.state == State.ACTIVE


def test_latch_cancel_also_releases_non_latch_toggles():
    """Regression (fuzz seed 153/192): pop/voice_stop used to release only sm-tracked latches,
    so a mapper-held action toggle (interact) survived the cancel."""
    eng, backend, sm, gw = make_engine()
    run(eng, {0: [ev("brow_raise", 0.9)]}, 1)                     # interact toggle ON (key e)
    assert backend.keys == {"e"}
    run(eng, {0: [ev("jaw_open", 0.9)]}, 2, t0=100.1)             # fire latch
    assert gw.held
    run(eng, {0: [ev("pop")]}, 1, t0=100.5)
    assert not gw.held and not backend.keys and not backend.mouse


def test_context_change_tick_emits_no_new_holds():
    """Regression (fuzz seed 27): on a cancel tick the producers are skipped, so a gesture
    already in progress cannot re-press on the same tick the release went out."""
    eng, backend, sm, gw = make_engine()
    eng.step([], 100.0, context="game.exe")
    run(eng, {0: [ev("jaw_open", 0.9)]}, 2, t0=100.1)             # fire latch
    assert gw.held
    eng.step([ev("jaw_open", 0.9, 100.2)], 100.2, context="chrome.exe")   # Alt+Tab mid-hold
    assert not gw.held


def test_cancel_keeps_head_aim_alive():
    eng, backend, sm, gw = make_engine()
    run(eng, {0: [ev("head_yaw", 20.0), ev("jaw_open", 0.9)]}, 2)
    run(eng, {0: [ev("pop")]}, 1, t0=100.5)
    assert not gw.held
    before = backend.total_dx
    run(eng, {}, 6, t0=100.6)
    assert backend.total_dx > before                           # camera still turning


# ----------------------------------------------------------------- (c) CONVERSE_LOCK
def test_freeze_cancels_latch_then_locks_and_blocks_gestures():
    eng, backend, sm, gw = make_engine()
    run(eng, {0: [ev("jaw_open", 0.9)]}, 2)
    assert gw.held
    run(eng, {0: [ev("voice_freeze", src="mic")]}, 1, t0=100.5)
    assert sm.state == State.CONVERSE_LOCK and not gw.held
    run(eng, {0: [ev("jaw_open", 0.1)]}, 1, t0=100.6)          # reset hysteresis
    run(eng, {0: [ev("jaw_open", 0.9)], 1: [ev("head_yaw", 20.0)]}, 5, t0=100.7)
    assert not gw.held and backend.total_dx == 0               # talking/laughing does nothing
    assert sm.dropped >= 1


def test_resume_unlocks():
    eng, backend, sm, gw = make_engine()
    run(eng, {0: [ev("voice_freeze", src="mic")]}, 1)
    run(eng, {0: [ev("voice_resume", src="mic")]}, 1, t0=101)
    assert sm.state == State.ACTIVE
    run(eng, {0: [ev("jaw_open", 0.9)]}, 2, t0=102)
    assert gw.held


def test_f12_kill_switch_locks_and_releases():
    eng, backend, sm, gw = make_engine()
    run(eng, {0: [ev("jaw_open", 0.9)]}, 2)
    run(eng, {0: [ev("kill", src="switch")]}, 1, t0=101)
    assert sm.state == State.CONVERSE_LOCK and not gw.held


def test_release_and_cancel_all_pass_the_gate_while_locked():
    sm = StateMachine()
    sm.on_event(ev("voice_freeze"), 0.0)
    out = sm.gate([Intent("release", "fire"), Intent("cancel_all"), Intent("action", "interact")], 1.0)
    assert [i.kind for i in out] == ["release", "cancel_all"]  # latch is never trapped


def test_pause_intent_from_upstream_locks_the_machine():
    sm = StateMachine()
    out = sm.gate([Intent("pause")], 0.0)
    assert sm.state == State.CONVERSE_LOCK
    assert [i.kind for i in out] == ["cancel_all", "pause"]


def test_face_loss_while_locked_stays_locked_after_return():
    sm = StateMachine()
    sm.on_event(ev("voice_freeze"), 0.0)
    sm.on_event(ev("face", 0.0), 1.0)
    sm.on_event(ev("face", 1.0), 2.0)
    sm.tick(2.4)
    assert sm.state == State.CONVERSE_LOCK


# ----------------------------------------------------------------- (d) context change
def test_window_switch_releases_everything():
    eng, backend, sm, gw = make_engine()
    eng.step([], 100.0, context="gta5.exe")
    t = run(eng, {0: [ev("jaw_open", 0.9)]}, 2)
    assert gw.held
    eng.step([], t, context="chrome.exe")                      # Alt+Tab
    assert not gw.held and sm.state == State.ACTIVE


def test_same_context_does_not_release():
    eng, backend, sm, gw = make_engine()
    eng.step([], 100.0, context="gta5.exe")
    run(eng, {0: [ev("jaw_open", 0.9)]}, 2)
    eng.step([], 100.1, context="gta5.exe")
    assert gw.held


# ----------------------------------------------------------------- cruise / persistent
def test_cruise_toggle_persists_then_stop_cancels():
    sm = StateMachine()
    sm.gate([Intent("action", "cruise")], 0.0)
    assert sm.state == State.LATCHED
    assert sm.tick(60.0) == []                                 # no expiry
    out, consumed = sm.on_event(ev("voice_stop"), 61.0)
    assert consumed and out == [Intent("cancel_all")] and sm.state == State.ACTIVE


def test_cruise_toggle_off_clears_tracking():
    sm = StateMachine()
    sm.gate([Intent("action", "cruise")], 0.0)
    sm.gate([Intent("action", "cruise")], 1.0)
    assert sm.state == State.ACTIVE


def test_layer_switch_clears_latches():
    sm = StateMachine()
    sm.gate([Intent("hold", "fire", ms=5000)], 0.0)
    sm.gate([Intent("layer", "driving")], 0.1)
    assert sm.state == State.ACTIVE


# ----------------------------------------------------------------- gateway
def test_gateway_never_leaves_a_key_down_after_release_all():
    b = RecordingBackend()
    gw = Gateway(b)
    gw.apply([OutputCommand("key", "w", pressed=True), OutputCommand("button", "RT", pressed=True),
              OutputCommand("axis", "left_x", value=1.0)])
    gw.flush(1.0)
    assert b.keys == {"w", "d"} and b.mouse == {"left"}
    gw.apply([OutputCommand("release_all", "")])
    assert not b.keys and not b.mouse and not gw.held


def test_gateway_releases_before_pressing():
    b = RecordingBackend()
    gw = Gateway(b)
    gw.apply([OutputCommand("key", "a", pressed=True)])
    gw.flush(1.0)
    b.log.clear()
    gw.apply([OutputCommand("key", "a", pressed=False), OutputCommand("key", "d", pressed=True)])
    gw.flush(1.1)
    assert b.log == [("key", "a", False), ("key", "d", True)]


def test_gateway_is_idempotent_no_double_press():
    b = RecordingBackend()
    gw = Gateway(b)
    for _ in range(5):
        gw.apply([OutputCommand("key", "w", pressed=True)])
        gw.flush(1.0)
    assert b.log.count(("key", "w", True)) == 1


def test_axis_key_threshold_has_hysteresis():
    b = RecordingBackend()
    gw = Gateway(b)
    for v, expect in ((0.2, False), (0.4, True), (0.3, True), (0.15, False)):
        gw.apply([OutputCommand("axis", "left_y", value=v)])
        gw.flush(1.0)
        assert ("w" in b.keys) is expect, v


def test_mouse_axis_accumulates_subpixels_and_is_dt_based():
    b = RecordingBackend()
    gw = Gateway(b)
    gw.apply([OutputCommand("axis", "right_x", value=0.01)])    # 0.01*900 = 9 px/s
    t = 0.0
    gw.flush(t)
    for _ in range(60):                                         # one second at 60 Hz
        t += DT
        gw.flush(t)
    assert 8 <= b.total_dx <= 9                                 # nothing lost to rounding


def test_right_y_up_positive_moves_mouse_up():
    b = RecordingBackend()
    gw = Gateway(b)
    gw.apply([OutputCommand("axis", "right_y", value=1.0)])
    gw.flush(0.0)
    gw.flush(0.1)
    assert b.total_dy < 0


def test_release_all_is_thread_safe_from_watchdog_thread():
    b = RecordingBackend()
    gw = Gateway(b)
    gw.apply([OutputCommand("key", "w", pressed=True)])
    gw.flush(0.0)
    th = threading.Thread(target=gw.release_all, args=("test",))
    th.start()
    th.join()
    assert not b.keys


# ----------------------------------------------------------------- watchdog
def test_watchdog_trips_when_loop_stalls_and_releases_keys():
    b = RecordingBackend()
    gw = Gateway(b)
    gw.apply([OutputCommand("key", "w", pressed=True)])
    gw.flush(0.0)
    wd = Watchdog()
    wd.add_channel("loop", 250, lambda n: gw.release_all("deadman"))
    wd.beat("loop")
    assert wd.check(time.perf_counter() + 0.1) == []
    assert wd.check(time.perf_counter() + 0.5) == ["loop"]
    assert not b.keys
    assert wd.check(time.perf_counter() + 0.6) == []            # fires once, not every check
    wd.beat("loop")                                             # recovery re-arms
    assert wd.check(time.perf_counter() + 1.0) == ["loop"]


def test_watchdog_unarmed_channel_never_trips():
    wd = Watchdog()
    wd.add_channel("webcam", 500, lambda n: None)
    assert wd.check(time.perf_counter() + 100) == []


def test_watchdog_real_thread_releases_stuck_key():
    b = RecordingBackend()
    gw = Gateway(b)
    gw.apply([OutputCommand("key", "w", pressed=True)])
    gw.flush(0.0)
    wd = Watchdog(check_ms=10)
    wd.add_channel("loop", 60, lambda n: gw.release_all("deadman"))
    wd.beat("loop")
    wd.start()
    time.sleep(0.3)                                             # main loop "froze"
    wd.stop()
    assert not b.keys


# ----------------------------------------------------------------- latency log
def test_latency_percentiles():
    lg = LatencyLog()
    for i in range(1, 101):
        lg.record(float(i), 0.5, float(i))
    s = lg.summary()
    assert s["ev_ms_max"] == 100.0
    assert 49 <= s["ev_ms_p50"] <= 52 and 93 <= s["ev_ms_p95"] <= 96


def test_latency_jsonl_written(tmp_path):
    p = tmp_path / "lat.jsonl"
    lg = LatencyLog(jsonl_path=str(p), every_s=1.0)
    for i in range(130):
        lg.record(i * DT, 0.4, 1.0)
    lg.close()
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    assert rows and "fps" in rows[0] and "ev_ms_p95" in rows[0]


# ----------------------------------------------------------------- replay end to end
def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_replay_face_loss_mid_hold_ends_with_zero_keys(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [
        {"src": "webcam", "name": "face", "value": 1.0, "t": 10.00},
        {"src": "webcam", "name": "jaw_open", "value": 0.9, "t": 10.05},
        {"src": "webcam", "name": "face", "value": 0.0, "t": 10.50},
    ])
    rs = ReplaySource(str(p), speed=1.0)
    eng, backend, sm, gw = make_engine()
    t, held_mid, end = 500.0, False, 500.0 + 1.0
    while t < end:
        eng.step(rs.poll(t), t)
        if 500.2 < t < 500.4 and gw.held:
            held_mid = True
        t += DT
    assert held_mid, "fire should have been held before the face vanished"
    assert rs.finished and not gw.held and not backend.mouse


def test_replay_speed_and_roundtrip(tmp_path):
    p = tmp_path / "r.jsonl"
    rec = Recorder(str(p))
    for i in range(3):
        rec.write(SignalEvent("webcam", "head_yaw", float(i), 50.0 + i))
    rec.close()
    rs = ReplaySource(str(p), speed=2.0)
    assert rs.duration_s == 1.0
    got = rs.poll(0.0) + rs.poll(0.51) + rs.poll(1.01)
    assert [e.value for e in got] == [0.0, 1.0, 2.0]
    assert all(e.src == "replay" for e in got)


def test_replay_rejects_bad_line(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"name": "x"}\n')
    with pytest.raises(ValueError):
        ReplaySource(str(p))


# ----------------------------------------------------------------- resilience
def test_broken_producer_does_not_kill_the_tick():
    class Boom:
        def process(self, events, now):
            raise RuntimeError("producer bug")

    backend = RecordingBackend()
    eng = Engine(StateMachine(), BasicMapper(), Gateway(backend), [Boom(), DefaultProducer()])
    eng.step([ev("jaw_open", 0.9, 1.0)], 1.0)
    assert backend.mouse                                        # healthy producer still worked


def test_runaway_producer_trips_rate_limiter_and_locks():
    class Runaway:
        def process(self, events, now):
            return [Intent("action", f"spam{i}") for i in range(200)]

    eng, backend, sm, gw = make_engine()
    eng.producers = [Runaway(), DefaultProducer()]
    t = 0.0
    for i in range(30):                               # 200*30 = 6000 intents >> 600/s limit
        eng.step([], t + i * DT)
    assert sm.state == State.CONVERSE_LOCK and eng.rate_trips >= 1
    assert not gw.held
    eng.step([], t + 31 * DT)
    assert sm.state == State.CONVERSE_LOCK           # stays locked until an explicit resume


def test_listener_exception_does_not_break_state_machine():
    sm = StateMachine()
    sm.subscribe(lambda s: 1 / 0)
    sm.on_event(ev("face", 0.0), 0.0)
    assert sm.state == State.SAFE_IDLE


def test_status_snapshot_for_dashboard():
    eng, backend, sm, gw = make_engine()
    run(eng, {0: [ev("jaw_open", 0.9)]}, 2)
    s = eng.status()
    assert s["state"] == "LATCHED" and s["latched"] == ("fire",) and "fps" in s
