"""App-layer tests (Binore): single instance, elevation, config + hot reload, logging, crash hooks.

Pure Python. Windows-only branches are exercised through fakes and decision tables, so a pass here
proves the LOGIC, not the real Win32 calls (those still need the manual Windows checklist).
"""
import json
import logging
import os
import sys
import threading
import uuid
from pathlib import Path

import pytest

import config as cfgmod
import elevation as el
import logsetup
import single_instance as si
from config import (
    AppConfig,
    ConfigError,
    ConfigWatcher,
    effective_axis_map,
    parse_config,
)
from core.events import OutputCommand
from gateway import AXIS_MAP, Gateway, RecordingBackend


# ================================================================== single instance
@pytest.mark.parametrize("handle,err,want", [
    (0x10, 0, si.OWNER),
    (0x10, si.ERROR_ALREADY_EXISTS, si.DUPLICATE),
    (None, si.ERROR_ACCESS_DENIED, si.DUPLICATE),      # created by an elevated copy
    (0, si.ERROR_ACCESS_DENIED, si.DUPLICATE),
    (None, 87, si.UNKNOWN),
    (0, 0, si.UNKNOWN),
])
def test_mutex_decision_table(handle, err, want):
    assert si.classify_mutex_result(handle, err) == want


def _fake_windows(inst, handle, err):
    closed = []
    inst._create_mutex = lambda: (handle, err)
    inst._close_handle = closed.append
    return closed


def test_windows_path_first_instance_keeps_handle():
    inst = si.SingleInstance("t")
    closed = _fake_windows(inst, 0x99, 0)
    assert inst._acquire_windows() is True
    assert inst._handle == 0x99 and not closed


def test_windows_path_duplicate_closes_its_reference():
    inst = si.SingleInstance("t")
    closed = _fake_windows(inst, 0x99, si.ERROR_ALREADY_EXISTS)
    assert inst._acquire_windows() is False
    assert closed == [0x99] and inst._handle is None


def test_windows_path_elevated_owner_counts_as_duplicate():
    inst = si.SingleInstance("t")
    _fake_windows(inst, None, si.ERROR_ACCESS_DENIED)
    assert inst._acquire_windows() is False


def test_windows_path_unknown_error_fails_open(caplog):
    inst = si.SingleInstance("t")
    _fake_windows(inst, None, 87)
    with caplog.at_level(logging.WARNING):
        assert inst._acquire_windows() is True
    assert "starting anyway" in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="exercises the flock fallback")
def test_posix_second_copy_refused_until_first_releases():
    name = f"omnitest-{uuid.uuid4().hex}"
    a, b = si.SingleInstance(name), si.SingleInstance(name)
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


@pytest.mark.skipif(os.name == "nt", reason="exercises the flock fallback")
def test_context_manager_releases():
    name = f"omnitest-{uuid.uuid4().hex}"
    with si.SingleInstance(name) as a:
        assert a.owned
        assert si.SingleInstance(name).acquire() is False
    assert si.SingleInstance(name).acquire() is True


# ================================================================== elevation
def test_build_relaunch_frozen_exe():
    file, params = el.build_relaunch(["--profile", "gta v", "--elevate"], frozen=True,
                                     executable=r"C:\Omni\OmniControl.exe", script="ignored")
    assert file == r"C:\Omni\OmniControl.exe"
    assert params == '--profile "gta v" --elevated'          # quoted, --elevate swapped for --elevated


def test_build_relaunch_from_source_passes_script_first():
    file, params = el.build_relaunch(["--elevate", "--dry-run"], frozen=False,
                                     executable="python.exe", script=r"C:\src\main.py")
    assert file == "python.exe"
    assert params == r"C:\src\main.py --dry-run --elevated"


def test_build_relaunch_never_duplicates_marker():
    _, params = el.build_relaunch(["--elevated", "--elevate"], frozen=True, executable="x.exe",
                                  script="")
    assert params.split().count("--elevated") == 1


@pytest.mark.parametrize("open_err,tok_err,elev,want", [
    (0, 0, True, el.ELEVATED),
    (0, 0, False, el.NORMAL),
    (0, 0, None, el.UNKNOWN),
    (el.ERROR_ACCESS_DENIED, 0, None, el.LIKELY_ELEVATED),
    (0, el.ERROR_ACCESS_DENIED, None, el.LIKELY_ELEVATED),
    (87, 0, None, el.UNKNOWN),
    (el.UNKNOWN_ERR, 0, None, el.UNKNOWN),
    (0, 6, None, el.UNKNOWN),
])
def test_classify_probe_table(open_err, tok_err, elev, want):
    assert el.classify_probe(open_err, tok_err, elev) == want


@pytest.mark.parametrize("admin,state,blocked", [
    (False, el.ELEVATED, True), (False, el.LIKELY_ELEVATED, True),
    (False, el.NORMAL, False), (False, el.UNKNOWN, False),
    (True, el.ELEVATED, False), (True, el.LIKELY_ELEVATED, False),
])
def test_uipi_blocked_truth_table(admin, state, blocked):
    assert el.uipi_blocked(admin, state) is blocked


def test_monitor_probes_once_per_pid_and_flags_blocked_input():
    calls = []

    def probe(pid):
        calls.append(pid)
        return el.ELEVATED if pid == 42 else el.NORMAL

    mon = el.ElevationMonitor(self_admin=False, probe=probe)
    assert mon.update(0) is None                              # unknown foreground: ignored
    first = mon.update(7)
    assert first.state == el.NORMAL and first.blocked is False
    assert mon.update(7) is None                              # same pid: no probe, no report
    game = mon.update(42)
    assert game.blocked is True and mon.info is game
    assert mon.update(42) is None
    assert calls == [7, 42]


def test_monitor_admin_copy_is_never_blocked():
    mon = el.ElevationMonitor(self_admin=True, probe=lambda pid: el.ELEVATED)
    assert mon.update(5).blocked is False


def test_relaunch_is_a_noop_off_windows():
    if os.name != "nt":
        assert el.relaunch_as_admin(["--elevate"]) is False
    assert isinstance(el.is_admin(), bool)


# ================================================================== config: locations
def test_omnicontrol_home_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNICONTROL_HOME", str(tmp_path / "x"))
    assert cfgmod.config_dir() == tmp_path / "x"
    assert cfgmod.log_dir() == tmp_path / "x" / "logs"
    assert cfgmod.config_path() == tmp_path / "x" / "config.json"


def test_resource_dir_is_the_source_folder_when_not_frozen():
    assert (cfgmod.resource_dir() / "config.py").exists()


# ================================================================== config: parsing
def test_default_config_text_round_trips_to_defaults():
    cfg, warns = parse_config(json.loads(cfgmod.default_config_text()))
    assert cfg == AppConfig() and warns == []


def test_numbers_are_clamped_with_warnings():
    cfg, warns = parse_config({"hz": 1000, "face_regain_ms": 0, "max_hold_ms": 999999})
    assert cfg.hz == 240.0
    assert cfg.face_regain_ms == 100                          # the dwell can never be switched off
    assert cfg.max_hold_ms == cfgmod.HARD_MAX_HOLD_MS         # a hold can never outlive 10 s
    assert len(warns) == 3


def test_default_hold_cannot_exceed_max_hold():
    cfg, warns = parse_config({"default_hold_ms": 9000, "max_hold_ms": 2000})
    assert cfg.default_hold_ms == 2000 and cfg.max_hold_ms == 2000
    assert any("default_hold_ms" in w for w in warns)


@pytest.mark.parametrize("bad", [True, "60", None, [1], float("nan"), float("inf")])
def test_bad_numbers_keep_previous_value(bad):
    base = AppConfig(hz=90.0)
    cfg, warns = parse_config({"hz": bad}, base)
    assert cfg.hz == 90.0 and warns


@pytest.mark.parametrize("name", ["../evil", "a/b", "a\\b", "", ".hidden", "x" * 65, 5, None])
def test_profile_name_cannot_escape_the_profiles_folder(name):
    cfg, warns = parse_config({"profile": name})
    assert cfg.profile == "default" and warns


def test_good_profile_and_log_level():
    cfg, warns = parse_config({"profile": "gta_v-1.2", "log_level": "debug"})
    assert (cfg.profile, cfg.log_level, warns) == ("gta_v-1.2", "DEBUG", [])
    assert parse_config({"log_level": "chatty"})[0].log_level == "INFO"


def test_unknown_keys_warn_but_underscore_keys_are_notes():
    cfg, warns = parse_config({"_comment": "hi", "speeed": 1})
    assert cfg == AppConfig()
    assert len(warns) == 1 and "speeed" in warns[0]


def test_top_level_must_be_an_object():
    with pytest.raises(ConfigError):
        parse_config([1, 2, 3])


def test_button_map_validated_against_real_scancodes():
    cfg, warns = parse_config({"button_map": {
        "A": ["key", "space"], "B": ["mouse", "right"],
        "X": ["key", "nosuchkey"], "Y": ["gamepad", "a"], "LB": "q", "": ["key", "q"]}})
    assert cfg.button_map == {"A": ("key", "space"), "B": ("mouse", "right")}
    assert len(warns) == 4


def test_axis_tuning_is_per_axis_type():
    cfg, warns = parse_config({"axis_tuning": {
        "right_x": {"speed": 1200, "invert": True, "threshold": 0.5},   # threshold is a keys-axis field
        "left_x": {"threshold": 0.4},
        "nope": {"speed": 5}, "right_y": {"speed": 99999}}})
    assert cfg.axis_tuning["right_x"] == {"speed": 1200.0, "invert": True}
    assert cfg.axis_tuning["left_x"] == {"threshold": 0.4}
    assert cfg.axis_tuning["right_y"] == {"speed": 5000.0}              # clamped
    assert "nope" not in cfg.axis_tuning and len(warns) == 3


def test_effective_axis_map_merges_without_mutating_defaults():
    before = json.dumps(AXIS_MAP, sort_keys=True)
    cfg, _ = parse_config({"axis_tuning": {"right_x": {"speed": 1500}}})
    eff = effective_axis_map(cfg)
    assert eff["right_x"]["speed"] == 1500.0 and eff["right_x"]["axis"] == "x"
    assert eff["right_y"] == AXIS_MAP["right_y"]
    assert json.dumps(AXIS_MAP, sort_keys=True) == before


# ================================================================== config: watcher / hot reload
def bump(path: Path, n: int = 1) -> None:
    """Force a visibly newer mtime so tests never depend on filesystem timestamp granularity."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + n * 1_000_000_000))


def test_first_run_creates_default_file_and_loads_defaults():
    w = ConfigWatcher()
    assert w.load() == AppConfig()
    assert cfgmod.config_path().exists()
    assert not list(cfgmod.config_dir().glob("*.tmp"))


def test_hot_reload_applies_a_valid_edit():
    w = ConfigWatcher()
    w.load()
    cfgmod.write_atomic(w.path, json.dumps({"profile": "cyberpunk", "default_hold_ms": 900}))
    bump(w.path)
    new = w.poll(10.0)
    assert new.profile == "cyberpunk" and new.default_hold_ms == 900
    assert w.poll(11.0) is None                                # nothing changed since


def test_polling_is_rate_limited():
    w = ConfigWatcher(poll_s=0.5)
    w.load()
    assert w.poll(10.0) is None
    cfgmod.write_atomic(w.path, json.dumps({"profile": "a"}))
    bump(w.path)
    assert w.poll(10.2) is None                                # too soon: not even a stat()
    assert w.poll(10.6).profile == "a"


def test_broken_json_keeps_last_good_config_and_logs_once(caplog):
    w = ConfigWatcher()
    cfgmod.write_atomic(w.path, json.dumps({"profile": "good"}))
    assert w.load().profile == "good"
    w.path.write_text("{ this is not json", encoding="utf-8")
    bump(w.path)
    with caplog.at_level(logging.ERROR):
        assert w.poll(10.0) is None
        assert w.poll(11.0) is None                            # same bad file: no second read/log
        assert w.poll(12.0) is None
    assert w.current.profile == "good"
    assert caplog.text.count("unusable") == 1
    cfgmod.write_atomic(w.path, json.dumps({"profile": "fixed"}))   # the user saves a fix
    bump(w.path, 2)
    assert w.poll(13.0).profile == "fixed"


def test_half_written_file_is_retried_when_it_completes():
    w = ConfigWatcher()
    w.load()
    full = json.dumps({"profile": "done", "hz": 75})
    w.path.write_text(full[:12], encoding="utf-8")             # editor mid-save
    bump(w.path)
    assert w.poll(10.0) is None
    w.path.write_text(full, encoding="utf-8")
    bump(w.path, 2)
    new = w.poll(11.0)
    assert new.profile == "done" and new.hz == 75.0


def test_deleted_file_keeps_running_with_current_settings():
    w = ConfigWatcher()
    cfgmod.write_atomic(w.path, json.dumps({"profile": "keep"}))
    w.load()
    w.path.unlink()
    assert w.poll(10.0) is None and w.current.profile == "keep"


def test_rewriting_identical_settings_is_not_a_change():
    w = ConfigWatcher()
    w.load()
    w.path.write_text(json.dumps({"profile": "default"}), encoding="utf-8")   # same effective config
    bump(w.path)
    assert w.poll(10.0) is None


def test_utf8_bom_from_notepad_is_tolerated():
    w = ConfigWatcher()
    w.path.parent.mkdir(parents=True, exist_ok=True)
    w.path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"profile": "bom"}).encode())
    assert w.load().profile == "bom"


def test_invalid_field_in_a_reload_keeps_the_previous_value_not_the_default():
    w = ConfigWatcher()
    cfgmod.write_atomic(w.path, json.dumps({"hz": 90}))
    w.load()
    cfgmod.write_atomic(w.path, json.dumps({"hz": "fast", "profile": "p2"}))
    bump(w.path)
    new = w.poll(10.0)
    assert new.hz == 90.0 and new.profile == "p2"


# ================================================================== gateway.update_maps (hot reload)
def test_update_maps_releases_everything_before_swapping_bindings():
    b = RecordingBackend()
    gw = Gateway(b)
    gw.apply([OutputCommand("key", "w", pressed=True), OutputCommand("button", "RT", pressed=True)])
    gw.flush(0.0)
    assert b.keys == {"w"} and b.mouse == {"left"}
    gw.update_maps(button_map={"A": ("key", "x")})
    assert not b.keys and not b.mouse and not gw.held        # nothing survives a remap
    gw.apply([OutputCommand("button", "A", pressed=True)])
    gw.flush(0.1)
    assert b.keys == {"x"}                                    # the NEW binding is live


def test_update_maps_applies_axis_tuning():
    b = RecordingBackend()
    gw = Gateway(b)
    cfg, _ = parse_config({"axis_tuning": {"right_x": {"speed": 1800}}})
    gw.update_maps(axis_map=effective_axis_map(cfg))
    gw.apply([OutputCommand("axis", "right_x", value=1.0)])
    gw.flush(0.0)
    gw.flush(0.05)
    assert b.total_dx == 90                                   # 1800 px/s * 0.05 s


# ================================================================== logging
def test_ring_handler_keeps_only_the_last_n_lines():
    ring = logsetup.RingHandler(size=3)
    ring.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("ring-test")
    lg.propagate = False
    lg.addHandler(ring)
    for i in range(10):
        lg.warning("m%d", i)
    assert ring.dump() == ["m7", "m8", "m9"]


def test_setup_logging_writes_a_file_and_is_idempotent(tmp_path):
    d = tmp_path / "logs"
    try:
        logsetup.setup_logging(d, "INFO", console=False)
        logsetup.setup_logging(d, "INFO", console=False)       # again: must not double up
        tagged = [h for h in logging.getLogger().handlers if getattr(h, "_omnicontrol_handler", False)]
        assert len(tagged) == 2                                 # ring + file, not 4
        logging.getLogger("omnicontrol.t").info("hello log")
    finally:
        logsetup.teardown_logging()
    assert "hello log" in (d / "omnicontrol.log").read_text(encoding="utf-8")
    assert not [h for h in logging.getLogger().handlers if getattr(h, "_omnicontrol_handler", False)]


def test_log_file_rotates(tmp_path):
    d = tmp_path / "logs"
    try:
        logsetup.setup_logging(d, "INFO", console=False, max_bytes=400, backups=2)
        for i in range(60):
            logging.getLogger("omnicontrol.rot").info("line %03d %s", i, "x" * 30)
    finally:
        logsetup.teardown_logging()
    names = sorted(p.name for p in d.iterdir())
    assert "omnicontrol.log" in names and "omnicontrol.log.1" in names
    assert len(names) <= 3                                      # live file + at most 2 backups


def test_unwritable_log_folder_does_not_raise(tmp_path):
    blocker = tmp_path / "iam_a_file"
    blocker.write_text("x")
    try:
        ring = logsetup.setup_logging(blocker / "logs", "INFO", console=False)
        logging.getLogger("omnicontrol.t").warning("still alive")
        assert any("still alive" in line for line in ring.dump())
    finally:
        logsetup.teardown_logging()


def test_set_level_changes_root_level():
    root = logging.getLogger()
    old = root.level
    try:
        logsetup.set_level("ERROR")
        assert root.level == logging.ERROR
    finally:
        root.setLevel(old)


# ================================================================== crash hooks
@pytest.fixture
def quiet_hooks(monkeypatch):
    """Replace the 'previous' hooks with no-ops so crash tests do not spray stderr."""
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    monkeypatch.setattr(threading, "excepthook", lambda a: None)


def _boom():
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        return sys.exc_info()


def test_crash_releases_keys_BEFORE_writing_the_report(tmp_path, quiet_hooks):
    crash_dir = tmp_path / "crashes"
    seen = {}

    def release(reason):
        seen["reason"] = reason
        seen["files_at_release_time"] = list(crash_dir.glob("crash-*.txt")) if crash_dir.exists() else []

    ring = logsetup.RingHandler()
    ring.setFormatter(logging.Formatter("%(message)s"))
    uninstall = logsetup.install_crash_handlers(release, crash_dir, ring, use_faulthandler=False)
    try:
        logging.getLogger("omnicontrol.t").addHandler(ring)
        logging.getLogger("omnicontrol.t").warning("breadcrumb before crash")
        sys.excepthook(*_boom())
    finally:
        uninstall()
    assert seen["reason"] == "crash in main thread"
    assert seen["files_at_release_time"] == []                  # no report existed yet: keys came first
    report = next(crash_dir.glob("crash-*.txt")).read_text(encoding="utf-8")
    assert "RuntimeError: kaboom" in report and "breadcrumb before crash" in report
    assert "OmniControl" in report and "where:    main thread" in report


def test_crash_in_worker_thread_releases_keys(tmp_path, quiet_hooks):
    reasons = []
    uninstall = logsetup.install_crash_handlers(reasons.append, tmp_path / "c", None,
                                                use_faulthandler=False)
    try:
        t = threading.Thread(target=lambda: 1 / 0, name="webcam-reader")
        t.start()
        t.join()
    finally:
        uninstall()
    assert reasons == ["crash in thread webcam-reader"]
    assert "ZeroDivisionError" in next((tmp_path / "c").glob("crash-*.txt")).read_text()


def test_ctrl_c_releases_but_is_not_reported_as_a_crash(tmp_path, quiet_hooks):
    reasons = []
    uninstall = logsetup.install_crash_handlers(reasons.append, tmp_path / "c", None,
                                                use_faulthandler=False)
    try:
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            sys.excepthook(*sys.exc_info())
    finally:
        uninstall()
    assert reasons and not list((tmp_path / "c").glob("crash-*.txt"))


def test_thread_systemexit_is_ignored_like_python_does(tmp_path, quiet_hooks):
    reasons = []
    uninstall = logsetup.install_crash_handlers(reasons.append, tmp_path / "c", None,
                                                use_faulthandler=False)
    try:
        t = threading.Thread(target=lambda: sys.exit(0))
        t.start()
        t.join()
    finally:
        uninstall()
    assert reasons == []


def test_a_failing_release_does_not_stop_the_report(tmp_path, quiet_hooks):
    def bad_release(reason):
        raise OSError("input driver gone")

    uninstall = logsetup.install_crash_handlers(bad_release, tmp_path / "c", None,
                                                use_faulthandler=False)
    try:
        sys.excepthook(*_boom())
    finally:
        uninstall()
    assert list((tmp_path / "c").glob("crash-*.txt"))


def test_uninstall_restores_the_previous_hooks(tmp_path, quiet_hooks):
    before = (sys.excepthook, threading.excepthook)
    uninstall = logsetup.install_crash_handlers(lambda r: None, tmp_path / "c", None,
                                                use_faulthandler=False)
    assert (sys.excepthook, threading.excepthook) != before
    uninstall()
    assert (sys.excepthook, threading.excepthook) == before


def test_old_crash_reports_are_pruned(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    for i in range(logsetup.KEEP_CRASH_REPORTS + 7):
        (d / f"crash-20200101-0000{i:02d}-000.txt").write_text("old")
    logsetup.write_crash_report(d, *_boom(), "test")
    assert len(list(d.glob("crash-*.txt"))) == logsetup.KEEP_CRASH_REPORTS


def test_faulthandler_writes_to_a_real_file_not_stderr(tmp_path, quiet_hooks):
    import faulthandler
    was_on = faulthandler.is_enabled()
    uninstall = logsetup.install_crash_handlers(lambda r: None, tmp_path / "c", None,
                                                use_faulthandler=True)
    try:
        assert faulthandler.is_enabled()
        assert "started" in (tmp_path / "c" / "fatal.log").read_text(encoding="utf-8")
    finally:
        uninstall()
        if was_on:
            faulthandler.enable()


# ================================================================== the Notepad demo sample really behaves as documented
def test_notepad_demo_sample_types_then_stops_instantly_on_face_loss(tmp_path):
    """samples/notepad_demo.jsonl is what the Windows checklist tells a human to watch.
    Replay it through the real engine and check the key timeline the checklist promises."""
    from adapters.replay import ReplaySource
    from main import BasicMapper, DefaultProducer, Engine
    from state_machine import SMConfig, StateMachine

    sample = Path(__file__).resolve().parent.parent / "samples" / "notepad_demo.jsonl"
    backend = RecordingBackend()
    gw = Gateway(backend, button_map={"RT": ("key", "a")})
    eng = Engine(StateMachine(SMConfig()), BasicMapper(), gw, [DefaultProducer()])
    rs = ReplaySource(str(sample), speed=1.0)
    timeline, t0, t = [], 1000.0, 1000.0
    last = frozenset()
    while t < t0 + 15.0:
        eng.step(rs.poll(t), t)
        if gw.held != last:
            timeline.append((round(t - t0, 1), "DOWN" if "a" in gw.held else "UP"))
            last = gw.held
        t += 1 / 60
    downs = [s for s, d in timeline if d == "DOWN"]
    ups = [s for s, d in timeline if d == "UP"]
    assert len(downs) == 2 and len(ups) == 2
    assert 6.0 <= downs[0] <= 6.1                              # jaw opens: 'a' starts
    assert 7.2 <= ups[0] <= 7.3                                # face lost at 7.2 s: released at once
    assert 10.1 <= downs[1] <= 10.2                            # face back + jaw again
    assert 11.8 <= ups[1] <= 12.0                              # timed hold expires by itself (1.8 s)
    assert not gw.held
