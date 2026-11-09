"""main() and Engine.apply_config integration tests (Binore). Dry-run only: no real input,
no camera. They prove the wiring; the real Win32 side still needs the Windows checklist."""
import json
import logging
import signal
import sys
import threading
import time
import uuid

import pytest

import config as cfgmod
import elevation
import main as app
import single_instance
from config import AppConfig, parse_config
from core.events import Intent, SignalEvent
from gateway import Gateway, RecordingBackend
from state_machine import SMConfig, State, StateMachine

DT = 1 / 60


@pytest.fixture(autouse=True)
def _process_state(monkeypatch):
    """main() installs signal handlers and a lock; keep each test's changes from leaking."""
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(app, "SINGLE_INSTANCE_NAME", f"omnitest-{uuid.uuid4().hex}")
    yield
    for s, h in old.items():
        signal.signal(s, h)


def write_session(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.fixture
def session(tmp_path):
    p = tmp_path / "s.jsonl"
    write_session(p, [
        {"src": "webcam", "name": "face", "value": 1.0, "t": 10.00},
        {"src": "webcam", "name": "jaw_open", "value": 0.9, "t": 10.05},
        {"src": "webcam", "name": "face", "value": 0.0, "t": 10.30},
    ])
    return p


def make_engine(cfg=None, **kw):
    b = RecordingBackend()
    gw = Gateway(b)
    sm = StateMachine(SMConfig(**kw))
    return app.Engine(sm, app.BasicMapper(), gw, [app.DefaultProducer()], cfg=cfg), b, sm, gw


def hold_fire(eng, t=100.0):
    for i in range(3):
        eng.step([SignalEvent("webcam", "jaw_open", 0.9, t + i * DT)] if i == 0 else [], t + i * DT)
    return t + 3 * DT


# ================================================================== main() wiring
def test_replay_run_exits_clean_and_leaves_no_global_state(session):
    hook_before, root_handlers = sys.excepthook, list(logging.getLogger().handlers)
    assert app.main(["--replay", str(session), "--dry-run", "--speed", "8"]) == 0
    assert sys.excepthook is hook_before                      # crash hook uninstalled
    assert list(logging.getLogger().handlers) == root_handlers  # our log handlers removed
    log_text = (cfgmod.log_dir() / "omnicontrol.log").read_text(encoding="utf-8")
    assert "starting" in log_text and "SAFE_IDLE" in log_text  # the face loss really happened
    assert (cfgmod.log_dir() / "fatal.log").exists()


def test_first_run_creates_config_and_safe_mode_does_not(session):
    app.main(["--safe", "--replay", str(session), "--dry-run", "--speed", "8"])
    assert not cfgmod.config_path().exists()                   # safe mode never touches the file
    app.main(["--replay", str(session), "--dry-run", "--speed", "8"])
    assert cfgmod.config_path().exists()


def test_safe_mode_ignores_a_hostile_config_and_caps_holds(session):
    cfgmod.write_atomic(cfgmod.config_path(), json.dumps({"max_hold_ms": 10000, "profile": "x"}))
    app.main(["--safe", "--replay", str(session), "--dry-run", "--speed", "8"])
    text = (cfgmod.log_dir() / "omnicontrol.log").read_text(encoding="utf-8")
    assert "SAFE MODE" in text and "3000 ms" in text
    assert app.SAFE_CONFIG.max_hold_ms == 3000 and app.SAFE_CONFIG.face_regain_ms == 500


def test_duplicate_instance_is_refused_with_exit_2():
    holder = single_instance.SingleInstance(app.SINGLE_INSTANCE_NAME)
    assert holder.acquire()
    try:
        assert app.main(["--dry-run"]) == 2
    finally:
        holder.release()
    assert app.main(["--dry-run"]) == 1                        # lock free: gets as far as "no sources"


def test_allow_multiple_skips_the_lock():
    holder = single_instance.SingleInstance(app.SINGLE_INSTANCE_NAME)
    holder.acquire()
    try:
        assert app.main(["--dry-run", "--allow-multiple"]) == 1
    finally:
        holder.release()


def test_replay_is_exempt_from_the_single_instance_lock(session):
    holder = single_instance.SingleInstance(app.SINGLE_INSTANCE_NAME)
    holder.acquire()
    try:
        assert app.main(["--replay", str(session), "--dry-run", "--speed", "8"]) == 0
    finally:
        holder.release()


def test_lock_is_taken_during_a_run_and_released_after_it():
    seen = {}

    def spy_sources(*a, **k):               # called while main() is running and holds the lock
        probe = single_instance.SingleInstance(app.SINGLE_INSTANCE_NAME)
        seen["free_during_run"] = probe.acquire()
        probe.release()
        return []                            # no sources: main returns 1 right after
    app._load_live_sources, orig = spy_sources, app._load_live_sources
    try:
        assert app.main(["--dry-run"]) == 1
    finally:
        app._load_live_sources = orig
    assert seen["free_during_run"] is False  # we really held the lock while running
    again = single_instance.SingleInstance(app.SINGLE_INSTANCE_NAME)
    assert again.acquire()                   # and let go of it afterwards
    again.release()


def test_doctor_flag_returns_before_anything_else_starts(capsys):
    assert app.main(["--doctor", "--quick", "--allow-mock"]) == 0
    assert "doctor" in capsys.readouterr().out
    assert not cfgmod.config_path().exists()                   # no config, no logging, no engine
    # without --allow-mock the same run FAILs (exit 1) on a machine that would feed mock input
    code = app.main(["--doctor", "--quick"])
    assert code in (0, 1)
    assert not cfgmod.config_path().exists()


def test_cli_profile_and_verbose_beat_the_config_file():
    base = parse_config({"profile": "from_file", "log_level": "ERROR"})[0]
    a = app.build_parser().parse_args(["--profile", "cli", "-v"])
    pinned = app._pin(base, a)
    assert (pinned.profile, pinned.log_level) == ("cli", "DEBUG")
    plain = app._pin(base, app.build_parser().parse_args([]))
    assert plain == base


def test_hot_reload_happens_while_main_is_running(tmp_path):
    p = tmp_path / "long.jsonl"
    write_session(p, [{"src": "webcam", "name": "face", "value": 1.0, "t": 10.0},
                      {"src": "webcam", "name": "face", "value": 1.0, "t": 11.6}])   # ~1.6 s run

    def edit_config_soon():
        time.sleep(0.5)
        cfgmod.write_atomic(cfgmod.config_path(), json.dumps({"default_hold_ms": 700}))
    threading.Thread(target=edit_config_soon, daemon=True).start()
    assert app.main(["--replay", str(p), "--dry-run"]) == 0
    assert "config reloaded" in (cfgmod.log_dir() / "omnicontrol.log").read_text(encoding="utf-8")


# ================================================================== --elevate
def test_elevate_relaunches_and_exits_without_claiming_the_lock(monkeypatch):
    calls = []
    monkeypatch.setattr(elevation, "is_admin", lambda: False)
    monkeypatch.setattr(elevation, "relaunch_as_admin", lambda args: calls.append(list(args)) or True)
    assert app.main(["--elevate", "--dry-run"]) == 0
    assert calls == [["--elevate", "--dry-run"]]
    assert single_instance.SingleInstance(app.SINGLE_INSTANCE_NAME).acquire()   # lock untouched


def test_declined_uac_keeps_running_normally(monkeypatch):
    monkeypatch.setattr(elevation, "is_admin", lambda: False)
    monkeypatch.setattr(elevation, "relaunch_as_admin", lambda args: False)
    assert app.main(["--elevate", "--dry-run"]) == 1           # carried on to "no sources"


def test_relaunched_copy_never_asks_again(monkeypatch):
    monkeypatch.setattr(elevation, "is_admin", lambda: False)
    monkeypatch.setattr(elevation, "relaunch_as_admin",
                        lambda args: pytest.fail("must not loop on --elevated"))
    assert app.main(["--elevate", "--elevated", "--dry-run"]) == 1


def test_already_admin_does_not_relaunch(monkeypatch):
    monkeypatch.setattr(elevation, "is_admin", lambda: True)
    monkeypatch.setattr(elevation, "relaunch_as_admin",
                        lambda args: pytest.fail("already elevated"))
    assert app.main(["--elevate", "--dry-run"]) == 1


# ================================================================== Engine.apply_config
def test_rebind_cancels_a_running_hold_through_every_layer():
    eng, backend, sm, gw = make_engine()
    hold_fire(eng)
    assert sm.state == State.LATCHED and backend.mouse == {"left"}
    new, _ = parse_config({"button_map": {"RT": ["key", "x"]}})
    eng.apply_config(new, 101.0)
    assert sm.state == State.ACTIVE and not gw.held and not backend.mouse
    assert eng.mapper._held == set()                           # the mapper forgot the hold too
    eng.step([SignalEvent("webcam", "jaw_open", 0.1, 101.1)], 101.1)   # jaw closes
    eng.step([SignalEvent("webcam", "jaw_open", 0.9, 101.2)], 101.2)
    assert backend.keys == {"x"}                               # new binding live


def test_timing_only_change_does_not_disturb_a_hold():
    eng, backend, sm, gw = make_engine()
    hold_fire(eng)
    new, _ = parse_config({"default_hold_ms": 900, "log_level": "DEBUG", "face_regain_ms": 400})
    eng.apply_config(new, 101.0)
    assert sm.state == State.LATCHED and backend.mouse == {"left"}   # still held
    assert (sm.cfg.default_hold_ms, sm.cfg.face_regain_ms) == (900, 400)
    assert eng.cfg == new


def test_max_hold_change_applies_to_the_next_hold():
    eng, *_ = make_engine()
    new, _ = parse_config({"max_hold_ms": 1000, "default_hold_ms": 800})
    eng.apply_config(new, 0.0)
    out = eng.sm.gate([Intent("hold", "fire", ms=60000)], 5.0)
    assert out[0].ms == 1000


def test_profile_swap_uses_the_factory_and_keeps_old_mapper_on_failure():
    eng, *_ = make_engine()
    old_mapper = eng.mapper
    new_mapper = app.BasicMapper()
    new, _ = parse_config({"profile": "cyberpunk"})
    eng.apply_config(new, 0.0, lambda name: new_mapper if name == "cyberpunk" else None)
    assert eng.mapper is new_mapper
    broken, _ = parse_config({"profile": "broken"})
    eng.apply_config(broken, 1.0, lambda name: None)
    assert eng.mapper is new_mapper and eng.cfg.profile == "broken"
    assert old_mapper is not new_mapper


def test_apply_config_with_identical_bindings_never_releases(monkeypatch):
    eng, backend, sm, gw = make_engine()
    hold_fire(eng)
    monkeypatch.setattr(gw, "release_all", lambda why="": pytest.fail("must not release"))
    eng.apply_config(AppConfig(log_level="WARNING"), 101.0)
    assert sm.state == State.LATCHED


def test_status_exposes_the_uipi_flag_for_the_dashboard():
    eng, *_ = make_engine()
    s = eng.status()
    assert s["input_blocked"] is False and s["foreground"] == ""
    eng.input_blocked, eng.foreground = True, "game.exe"
    assert eng.status()["input_blocked"] is True


def test_original_engine_constructor_still_works_without_cfg():
    eng, *_ = make_engine()
    assert eng.cfg == AppConfig()
