"""Tray tests (Binore). Pure logic, controller, and the Tray wrapper against a FAKE pystray.
The real pystray Win32 backend is not exercised here (needs the Windows checklist)."""
import json
import logging
import signal
import sys
import threading
import uuid

import pytest

import config as cfgmod
import elevation
import main as app
import single_instance
import tray
from core.events import SignalEvent
from gateway import Gateway, RecordingBackend
from state_machine import SMConfig, State, StateMachine
from tray import (
    MenuEntry,
    Tray,
    TrayController,
    doctor_command,
    draw_icon,
    menu_spec,
    tooltip,
)


# ================================================================== tooltip + menu (pure)
def test_tooltip_shows_state_holds_and_blocked_input():
    assert tooltip({"state": "ACTIVE"}) == "OmniControl: ACTIVE"
    t = tooltip({"state": "LATCHED", "latched": ("fire", "cruise"), "input_blocked": True})
    assert "LATCHED" in t and "holding fire, cruise" in t and "restart as admin" in t


def test_tooltip_never_exceeds_the_windows_limit():
    assert len(tooltip({"state": "LATCHED", "latched": tuple("x" * 40 for _ in range(10))})) == 127
    assert tooltip({}) == "OmniControl: starting"


def labels(entries):
    return [e.label for e in entries if e.action != "-"]


def test_menu_freeze_becomes_resume_when_locked():
    assert "Freeze" in labels(menu_spec({"state": "ACTIVE", "locked": False}, True))
    spec = menu_spec({"state": "CONVERSE_LOCK", "locked": True}, True)
    assert "Resume" in labels(spec) and "Freeze" not in labels(spec)


def test_menu_offers_elevation_only_to_a_normal_user():
    assert not any(e.action == "elevate" for e in menu_spec({}, is_admin=True))
    normal = [e for e in menu_spec({}, is_admin=False) if e.action == "elevate"]
    assert len(normal) == 1 and "needed" not in normal[0].label
    blocked = [e for e in menu_spec({"input_blocked": True}, False) if e.action == "elevate"]
    assert "needed for this app" in blocked[0].label


def test_menu_layout_status_disabled_quit_last_separators_between_groups():
    spec = menu_spec({"state": "ACTIVE"}, False)
    assert spec[0] == MenuEntry("status", "State: ACTIVE", False)
    assert spec[-1].action == "quit"
    assert [e.action for e in spec].count("-") == 2
    assert all(e.enabled for e in spec[1:] if e.action != "-")


def test_doctor_command_source_and_frozen(monkeypatch):
    cmd = doctor_command()
    assert cmd[0] == sys.executable and cmd[1].endswith("main.py") and cmd[-2:] == ["--doctor", "--quick"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert doctor_command() == [sys.executable, "--doctor", "--quick"]


# ================================================================== icon drawing
def test_every_state_has_a_distinct_icon():
    imgs = {s: draw_icon(s, 32) for s in ("ACTIVE", "LATCHED", "CONVERSE_LOCK", "SAFE_IDLE")}
    assert all(i.size == (32, 32) and i.mode == "RGBA" for i in imgs.values())
    assert len({i.tobytes() for i in imgs.values()}) == 4


def test_shape_differs_even_with_colour_removed():
    """Colour-blind users: the white glyph alone must tell the states apart."""
    def glyph_mask(state):
        raw = draw_icon(state, 32).tobytes()
        return bytes(1 if (raw[i] > 240 and raw[i + 1] > 240 and raw[i + 2] > 240 and raw[i + 3] > 200)
                     else 0 for i in range(0, len(raw), 4))
    masks = {s: glyph_mask(s) for s in ("ACTIVE", "LATCHED", "CONVERSE_LOCK", "SAFE_IDLE")}
    assert len(set(masks.values())) == 4


def test_icon_is_a_disc_with_the_state_colour_and_a_blocked_badge():
    im = draw_icon("SAFE_IDLE", 64)
    assert im.getpixel((0, 0))[3] == 0                           # corner transparent: round icon
    assert im.getpixel((8, 32))[:3] == (200, 45, 45)             # disc edge in the state colour
    assert draw_icon("ACTIVE", 64, blocked=True).tobytes() != draw_icon("ACTIVE", 64).tobytes()
    assert draw_icon("nonsense", 16).size == (16, 16)            # unknown state: grey fallback


# ================================================================== controller
def controller(**kw):
    opened, ran = [], []
    kw.setdefault("open_path", opened.append)
    kw.setdefault("run_doctor", lambda: ran.append(1))
    kw.setdefault("now", lambda: 42.0)
    return TrayController(cfgmod.config_path(), cfgmod.log_dir(), **kw), opened, ran


def test_toggle_freeze_sends_the_same_event_as_the_f12_kill_switch():
    ctl, *_ = controller()
    ctl.handle("toggle_freeze")
    [e] = ctl.drain()
    assert (e.src, e.name, e.value, e.t) == ("switch", "kill", 1.0, 42.0)
    ctl.push_status({"locked": True})
    ctl.handle("toggle_freeze")
    assert [e.name for e in ctl.drain()] == ["voice_resume"]
    assert ctl.drain() == []


def test_quit_and_elevate_only_set_flags_for_the_main_loop():
    ctl, *_ = controller()
    assert not ctl.quit_requested.is_set() and not ctl.elevate_requested.is_set()
    ctl.handle("quit")
    ctl.handle("elevate")
    assert ctl.quit_requested.is_set() and ctl.elevate_requested.is_set()
    assert ctl.drain() == []                                     # no engine events from these


def test_open_and_doctor_actions_call_the_injected_helpers():
    ctl, opened, ran = controller()
    ctl.handle("open_config")
    ctl.handle("open_logs")
    ctl.handle("doctor")
    assert opened == [cfgmod.config_path(), cfgmod.log_dir()] and ran == [1]


def test_a_failing_action_never_raises_into_the_tray_thread(caplog):
    def broken(path):
        raise OSError("no default app")
    ctl, *_ = controller(open_path=broken)
    with caplog.at_level(logging.ERROR):
        ctl.handle("open_config")                                # must not raise
    assert "tray action 'open_config' failed" in caplog.text
    ctl.handle("no_such_action")                                 # logged, not fatal


def test_events_from_many_threads_all_arrive():
    ctl, *_ = controller()
    ts = [threading.Thread(target=lambda: [ctl.handle("toggle_freeze") for _ in range(50)])
          for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ctl.drain()) == 200


def test_tray_freeze_really_locks_the_engine_and_releases_keys():
    gw = Gateway(RecordingBackend())
    sm = StateMachine(SMConfig())
    eng = app.Engine(sm, app.BasicMapper(), gw, [app.DefaultProducer()])
    eng.step([SignalEvent("webcam", "jaw_open", 0.9, 1.0)], 1.0)
    assert sm.state == State.LATCHED and gw.held
    ctl, *_ = controller()
    ctl.handle("toggle_freeze")
    eng.step(ctl.drain(), 1.1)
    assert sm.state == State.CONVERSE_LOCK and not gw.held       # latch cancelled, nothing pressed
    ctl.push_status(eng.status())
    ctl.handle("toggle_freeze")
    eng.step(ctl.drain(), 1.2)
    assert sm.state == State.ACTIVE


# ================================================================== Tray wrapper vs a fake pystray
class FakeItem:
    def __init__(self, text, action, enabled=True):
        self.text, self.action, self.enabled = text, action, enabled


class FakeIcon:
    def __init__(self, name, icon, title, menu):
        self.name, self.title, self.menu = name, title, menu
        self._icon, self.redraws, self.menu_updates = icon, 0, 0
        self.started = self.stopped = False
        self.fail = False

    @property
    def icon(self):
        return self._icon

    @icon.setter
    def icon(self, value):
        if self.fail:
            raise RuntimeError("backend gone")
        self._icon = value
        self.redraws += 1

    def update_menu(self):
        self.menu_updates += 1

    def run_detached(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakePystray:
    class Menu:
        SEPARATOR = object()

        def __init__(self, items):
            self.items = items

    MenuItem, Icon = FakeItem, FakeIcon


def make_tray(is_admin=False):
    ctl, *_ = controller()
    return Tray(ctl, is_admin, FakePystray), ctl


def test_menu_is_built_fresh_each_time_from_the_latest_status():
    t, ctl = make_tray()
    first = list(t._icon.menu.items())
    assert [i.text for i in first if isinstance(i, FakeItem)][:2] == ["State: starting", "Freeze"]
    assert FakePystray.Menu.SEPARATOR in first
    ctl.push_status({"state": "CONVERSE_LOCK", "locked": True})
    texts = [i.text for i in t._icon.menu.items() if isinstance(i, FakeItem)]
    assert "Resume" in texts and "State: CONVERSE_LOCK" in texts


def test_clicking_menu_items_reaches_the_controller():
    t, ctl = make_tray()
    items = {i.text: i for i in t._icon.menu.items() if isinstance(i, FakeItem)}
    assert items["State: starting"].enabled is False
    items["Quit OmniControl"].action(None, None)
    assert ctl.quit_requested.is_set()
    items["Freeze"].action(None, None)
    assert [e.name for e in ctl.drain()] == ["kill"]


def test_update_redraws_only_when_something_visible_changed():
    t, _ = make_tray()
    s = {"state": "ACTIVE", "locked": False, "latched": ()}
    t.update(s)
    t.update(dict(s, tick_ms_p50=0.5))                           # telemetry noise: no redraw
    t.update(dict(s))
    assert t._icon.redraws == 1 and t._icon.menu_updates == 1
    t.update(dict(s, state="LATCHED", latched=("fire",)))
    assert t._icon.redraws == 2 and "holding fire" in t._icon.title
    t.update(dict(s, state="LATCHED", latched=("fire",), input_blocked=True))
    assert t._icon.redraws == 3 and "INPUT BLOCKED" in t._icon.title


def test_update_hands_the_status_to_the_controller_even_without_a_redraw():
    t, ctl = make_tray()
    t.update({"state": "ACTIVE", "locked": True})
    assert ctl.status["locked"] is True                          # freeze/resume reads this


def test_a_broken_tray_backend_cannot_hurt_the_engine(caplog):
    t, _ = make_tray()
    t._icon.fail = True
    with caplog.at_level(logging.ERROR):
        t.update({"state": "ACTIVE"})                            # must not raise
        t.update({"state": "SAFE_IDLE"})                         # frozen: no second attempt
    assert caplog.text.count("tray update failed") == 1


def test_start_and_stop_delegate_and_stop_never_raises():
    t, _ = make_tray()
    t.start()
    assert t._icon.started
    t.stop()
    assert t._icon.stopped

    def boom():
        raise RuntimeError("x")
    t._icon.stop = boom
    t.stop()                                                     # swallowed


def test_create_tray_degrades_gracefully(monkeypatch):
    ctl, *_ = controller()
    monkeypatch.setitem(sys.modules, "pystray", None)            # import raises ImportError
    assert tray.create_tray(ctl, True) is None
    monkeypatch.setitem(sys.modules, "pystray", FakePystray)
    assert isinstance(tray.create_tray(ctl, True), Tray)

    class Exploding(FakePystray):
        class Icon:
            def __init__(self, *a):
                raise OSError("no display")
    monkeypatch.setitem(sys.modules, "pystray", Exploding)
    assert tray.create_tray(ctl, True) is None


# ================================================================== inside main()'s loop
@pytest.fixture
def loop_env(monkeypatch, tmp_path):
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(app, "SINGLE_INSTANCE_NAME", f"omnitest-{uuid.uuid4().hex}")
    monkeypatch.setattr(app, "_tray_wanted", lambda a: True)
    long_run = tmp_path / "long.jsonl"                           # would run 30 s if nobody quits it
    long_run.write_text("\n".join(json.dumps(
        {"src": "webcam", "name": "face", "value": 1.0, "t": t}) for t in (10.0, 40.0)) + "\n")
    yield long_run
    for s, h in old.items():
        signal.signal(s, h)


class FakeLoopTray:
    """Stands in for Tray inside main(): records what the loop does and clicks on a timer."""
    instances = []

    def __init__(self, ctl, script):
        self.ctl, self.script, self.updates, self.started, self.stopped = ctl, script, [], False, False
        FakeLoopTray.instances.append(self)

    def start(self):
        self.started = True
        for delay, action in self.script:
            threading.Timer(delay, self.ctl.handle, args=(action,)).start()

    def update(self, status):
        self.ctl.push_status(status)
        self.updates.append(status)

    def stop(self):
        self.stopped = True


def install_fake_tray(monkeypatch, script):
    FakeLoopTray.instances.clear()
    monkeypatch.setattr(app, "create_tray", lambda ctl, is_admin: FakeLoopTray(ctl, script))


def run_log():
    return (cfgmod.log_dir() / "omnicontrol.log").read_text(encoding="utf-8")


def test_quit_from_the_tray_ends_the_run_cleanly(monkeypatch, loop_env):
    install_fake_tray(monkeypatch, [(0.4, "quit")])
    assert app.main(["--replay", str(loop_env), "--dry-run"]) == 0
    t = FakeLoopTray.instances[0]
    assert t.started and t.stopped and t.updates and "state" in t.updates[0]
    assert "quit requested from the tray" in run_log()


def test_freeze_from_the_tray_locks_the_engine_in_a_live_loop(monkeypatch, loop_env):
    install_fake_tray(monkeypatch, [(0.3, "toggle_freeze"), (0.8, "quit")])
    assert app.main(["--replay", str(loop_env), "--dry-run"]) == 0
    assert "STATE CONVERSE_LOCK" in run_log()
    assert any(u.get("locked") for u in FakeLoopTray.instances[0].updates)   # tray saw it too


def test_resume_from_the_tray_unlocks_again(monkeypatch, loop_env):
    install_fake_tray(monkeypatch, [(0.3, "toggle_freeze"), (0.7, "toggle_freeze"), (1.2, "quit")])
    app.main(["--replay", str(loop_env), "--dry-run"])
    text = run_log()
    assert text.index("STATE CONVERSE_LOCK") < text.index("STATE ACTIVE (voice_resume)")


def test_run_works_when_no_tray_can_be_created(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_tray_wanted", lambda a: True)
    monkeypatch.setattr(app, "create_tray", lambda ctl, is_admin: None)
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"src": "webcam", "name": "face", "value": 1.0, "t": 1.0}) + "\n")
    assert app.main(["--replay", str(p), "--dry-run", "--speed", "8"]) == 0


def test_tray_elevate_in_the_loop_relaunches_then_exits(monkeypatch, loop_env):
    calls = []
    monkeypatch.setattr(elevation, "relaunch_as_admin", lambda args: calls.append(list(args)) or True)
    install_fake_tray(monkeypatch, [(0.3, "elevate")])
    assert app.main(["--replay", str(loop_env), "--dry-run"]) == 0
    assert len(calls) == 1 and "--dry-run" in calls[0]


# ================================================================== _elevate_from_tray lock handling
def test_elevate_releases_the_lock_first_so_the_new_copy_can_take_it(monkeypatch):
    name = f"omnitest-{uuid.uuid4().hex}"
    inst = single_instance.SingleInstance(name)
    assert inst.acquire()
    seen = {}

    def fake_relaunch(args):
        probe = single_instance.SingleInstance(name)
        seen["lock_free_at_relaunch"] = probe.acquire()          # what the new copy would see
        probe.release()
        return True
    monkeypatch.setattr(elevation, "relaunch_as_admin", fake_relaunch)
    assert app._elevate_from_tray(inst, ["--x"]) is True
    assert seen["lock_free_at_relaunch"] is True


def test_declined_uac_takes_the_lock_back(monkeypatch):
    name = f"omnitest-{uuid.uuid4().hex}"
    inst = single_instance.SingleInstance(name)
    assert inst.acquire()
    monkeypatch.setattr(elevation, "relaunch_as_admin", lambda args: False)
    assert app._elevate_from_tray(inst, []) is False
    assert inst.owned and single_instance.SingleInstance(name).acquire() is False
    inst.release()


def test_replay_run_never_grabs_a_lock_it_did_not_have(monkeypatch):
    name = f"omnitest-{uuid.uuid4().hex}"
    inst = single_instance.SingleInstance(name)                  # never acquired (replay mode)
    monkeypatch.setattr(elevation, "relaunch_as_admin", lambda args: False)
    app._elevate_from_tray(inst, [])
    assert not inst.owned and single_instance.SingleInstance(name).acquire() is True


# ================================================================== --tray demo mode (replay + tray, no camera)
def parse(*argv):
    return app.build_parser().parse_args(list(argv))


def test_tray_wanted_truth_table(monkeypatch):
    monkeypatch.setattr(app.os, "name", "nt")
    assert app._tray_wanted(parse()) is True                          # live session
    assert app._tray_wanted(parse("--no-tray")) is False
    assert app._tray_wanted(parse("--replay", "x")) is False          # plain replay: no tray
    assert app._tray_wanted(parse("--replay", "x", "--tray")) is True # demo mode
    assert app._tray_wanted(parse("--replay", "x", "--tray", "--no-tray")) is False
    monkeypatch.setattr(app.os, "name", "posix")
    assert app._tray_wanted(parse("--tray")) is False                 # never off Windows


def test_lock_policy():
    assert app._needs_lock(parse()) is True
    assert app._needs_lock(parse("--replay", "x")) is False
    assert app._needs_lock(parse("--replay", "x", "--tray")) is True
    assert app._needs_lock(parse("--allow-multiple")) is False
    assert app._needs_lock(parse("--replay", "x", "--tray", "--allow-multiple")) is False


def test_demo_mode_keeps_running_after_the_replay_ends_until_quit(monkeypatch, tmp_path):
    p = tmp_path / "short.jsonl"
    p.write_text(json.dumps({"src": "webcam", "name": "face", "value": 1.0, "t": 1.0}) + "\n")
    monkeypatch.setattr(app, "SINGLE_INSTANCE_NAME", f"omnitest-{uuid.uuid4().hex}")
    monkeypatch.setattr(app, "_tray_wanted", lambda a: True)
    install_fake_tray(monkeypatch, [(1.0, "quit")])                   # replay is long over by then
    t0 = __import__("time").monotonic()
    assert app.main(["--replay", str(p), "--dry-run", "--tray"]) == 0
    assert __import__("time").monotonic() - t0 >= 0.9                 # it waited for the Quit click
    assert "quit requested from the tray" in run_log()


def test_demo_mode_without_a_tray_does_not_hang(monkeypatch, tmp_path):
    p = tmp_path / "short.jsonl"
    p.write_text(json.dumps({"src": "webcam", "name": "face", "value": 1.0, "t": 1.0}) + "\n")
    monkeypatch.setattr(app, "SINGLE_INSTANCE_NAME", f"omnitest-{uuid.uuid4().hex}")
    monkeypatch.setattr(app, "_tray_wanted", lambda a: True)
    monkeypatch.setattr(app, "create_tray", lambda ctl, is_admin: None)   # pystray missing
    assert app.main(["--replay", str(p), "--dry-run", "--tray", "--speed", "8"]) == 0


def test_demo_mode_takes_the_single_instance_lock(monkeypatch, tmp_path):
    p = tmp_path / "short.jsonl"
    p.write_text(json.dumps({"src": "webcam", "name": "face", "value": 1.0, "t": 1.0}) + "\n")
    monkeypatch.setattr(app, "SINGLE_INSTANCE_NAME", f"omnitest-{uuid.uuid4().hex}")
    holder = single_instance.SingleInstance(app.SINGLE_INSTANCE_NAME)
    assert holder.acquire()
    try:
        assert app.main(["--replay", str(p), "--dry-run", "--tray"]) == 2
    finally:
        holder.release()
