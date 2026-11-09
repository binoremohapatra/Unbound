"""Doctor tests (Binore). Probes are injected, so no camera, mic or registry is needed.
Windows-only checks must SKIP cleanly elsewhere; their real behaviour needs the Windows checklist."""
import importlib.util
import json
import os

import pytest

import doctor
from doctor import FAIL, OK, SKIP, WARN, Check


def by_name(checks):
    return {c.name: c for c in checks}


# ---------------------------------------------------------------- whole run
def test_quick_run_returns_checks_and_never_raises():
    checks = doctor.run_all(slow=False)
    names = [c.name for c in checks]
    assert {"python", "platform", "elevation", "folders", "dependencies"} <= set(names)
    assert all(c.status in (OK, WARN, FAIL, SKIP) and c.detail for c in checks)
    assert "camera" not in names and "loop jitter" not in names       # slow ones left out


@pytest.mark.skipif(os.name == "nt", reason="asserts the non-Windows SKIP path")
def test_windows_only_checks_skip_cleanly_elsewhere():
    got = by_name(doctor.run_all(slow=False))
    for name in ("sendinput", "display", "pointer precision", "webcam privacy", "microphone privacy"):
        assert got[name].status == SKIP, name
    assert got["platform"].status == WARN                             # dev machine, not the target


def test_a_crashing_check_becomes_a_fail_line_not_a_crash(monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(doctor, "check_python", boom)
    got = by_name(doctor.run_all(slow=False))
    assert got["python"].status == FAIL and "probe exploded" in got["python"].detail
    assert "folders" in got                                           # later checks still ran


def test_exit_code_only_fails_on_FAIL():
    assert doctor.exit_code([Check("a", OK, "x"), Check("b", WARN, "x"), Check("c", SKIP, "x")]) == 0
    assert doctor.exit_code([Check("a", OK, "x"), Check("b", FAIL, "x")]) == 1
    assert doctor.exit_code([]) == 0


def test_report_shows_fix_only_for_warn_and_fail_and_counts():
    text = doctor.format_report([
        Check("a", OK, "fine", fix="never shown"), Check("b", WARN, "meh", fix="do this"),
        Check("c", FAIL, "bad", fix="do that"), Check("d", SKIP, "n/a", fix="never shown")])
    assert "never shown" not in text
    assert "fix: do this" in text and "fix: do that" in text
    assert "1 ok, 1 warnings, 1 failures, 1 skipped" in text


# ---------------------------------------------------------------- privacy (registry injected)
def fake_registry(**denied_at):
    """denied_at keys: hklm / hkcu / nonpackaged -> value string."""
    def reader(hive, path, value):
        if hive == "HKLM":
            return denied_at.get("hklm")
        return denied_at.get("nonpackaged") if path.endswith("NonPackaged") else denied_at.get("hkcu")
    return reader


def test_privacy_ok_when_allowed_or_never_configured():
    assert doctor.check_privacy("webcam", fake_registry()).status == OK
    assert doctor.check_privacy("webcam", fake_registry(hklm="Allow", hkcu="Allow")).status == OK


@pytest.mark.parametrize("where,label", [("hklm", "system-wide"), ("hkcu", "your account"),
                                         ("nonpackaged", "desktop apps")])
def test_privacy_fails_when_any_switch_says_deny(where, label):
    c = doctor.check_privacy("webcam", fake_registry(**{where: "Deny"}))
    assert c.status == FAIL and label in c.detail
    assert "Let desktop apps access your camera" in c.fix


def test_privacy_deny_is_case_insensitive_and_mic_has_its_own_wording():
    c = doctor.check_privacy("microphone", fake_registry(hkcu="DENY"))
    assert c.status == FAIL and "Microphone" in c.detail and "microphone" in c.fix


def test_privacy_queries_the_right_registry_paths():
    seen = []

    def reader(hive, path, value):
        seen.append((hive, path, value))
        return None
    doctor.check_privacy("webcam", reader)
    assert ("HKLM", doctor._CONSENT.format("webcam"), "Value") in seen
    assert ("HKCU", doctor._CONSENT.format("webcam") + r"\NonPackaged", "Value") in seen


# ---------------------------------------------------------------- devices (probes injected)
def test_camera_outcomes():
    assert doctor.check_camera(lambda: None).status == FAIL
    assert doctor.check_camera(lambda: {"frames": 0, "fps": 0.0, "size": (640, 480)}).status == FAIL
    low = doctor.check_camera(lambda: {"frames": 15, "fps": 9.0, "size": (640, 480)})
    assert low.status == WARN and "lighting" in low.fix
    good = doctor.check_camera(lambda: {"frames": 15, "fps": 30.0, "size": (1280, 720)})
    assert good.status == OK and "1280x720" in good.detail


def test_camera_skips_when_opencv_missing(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert doctor.check_camera().status == SKIP


def test_microphone_outcomes(monkeypatch):
    assert doctor.check_microphone(lambda: []).status == FAIL
    ok = doctor.check_microphone(lambda: ["USB Mic", "Array"])
    assert ok.status == OK and "USB Mic" in ok.detail

    def broken():
        raise OSError("PortAudio error")
    assert doctor.check_microphone(broken).status == FAIL
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert doctor.check_microphone().status == SKIP


def test_dependencies_warn_lists_the_pip_command(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    c = doctor.check_dependencies()
    assert c.status == WARN and c.fix.startswith("pip install ") and "opencv-python" in c.fix
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert doctor.check_dependencies().status == OK


# ---------------------------------------------------------------- folders / timing / python
def test_folders_ok_and_fail(tmp_path, monkeypatch):
    assert doctor.check_folders().status == OK
    blocker = tmp_path / "file_not_dir"
    blocker.write_text("x")
    monkeypatch.setenv("OMNICONTROL_HOME", str(blocker / "sub"))
    c = doctor.check_folders()
    assert c.status == FAIL and "OMNICONTROL_HOME" in c.fix


def test_loop_jitter_reports_percentiles():
    c = doctor.check_loop_jitter(seconds=0.25)
    assert c.status in (OK, WARN, FAIL)           # a shared CI box may be noisy; we test the report
    assert "p99 jitter" in c.detail and "worst" in c.detail and "overruns" in c.detail


def test_python_check_mentions_version_and_bits():
    c = doctor.check_python()
    assert "bit" in c.detail and c.status in (OK, WARN)


# ---------------------------------------------------------------- mock input check
def test_mock_sources_all_real_is_ok(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    c = doctor.check_mock_sources(camera_probe=lambda: True)
    assert c.status == OK and "real hardware" in c.detail


def test_mock_sources_missing_deps_fail_unless_allow_mock(monkeypatch):
    def spec(name):
        return None if name in ("mediapipe", "pyaudio", "keyboard") else object()
    monkeypatch.setattr(importlib.util, "find_spec", spec)
    c = doctor.check_mock_sources(camera_probe=lambda: True)
    assert c.status == FAIL and "MOCK INPUT" in c.detail
    assert "webcam" in c.detail and "audio" in c.detail and "switch" in c.detail
    w = doctor.check_mock_sources(allow_mock=True, camera_probe=lambda: True)
    assert w.status == WARN and "allow-mock" in w.detail


def test_mock_sources_no_camera_is_mock_even_with_deps(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    c = doctor.check_mock_sources(camera_probe=lambda: False)
    assert c.status == FAIL and "no camera" in c.detail


def test_run_all_includes_mock_input_check():
    names = [c.name for c in doctor.run_all(slow=False)]
    assert "mock input" in names


# ---------------------------------------------------------------- CLI entry
def test_run_cli_prints_and_saves_report(capsys):
    code = doctor.run_cli(slow=False, allow_mock=True)
    out = capsys.readouterr().out
    assert code == 0 and "OmniControl" in out and "doctor" in out
    saved = (doctor.config.log_dir() / "doctor-report.txt").read_text(encoding="utf-8")
    assert "folders" in saved


def test_run_cli_json_mode_is_valid_json(capsys):
    doctor.run_cli(as_json=True, slow=False)
    rows = json.loads(capsys.readouterr().out)
    assert rows and {"name", "status", "detail", "fix"} <= set(rows[0])


@pytest.mark.skipif(os.name == "nt", reason="non-Windows no-ops")
def test_console_helpers_are_noops_off_windows():
    assert doctor.attach_parent_console() is False
    doctor.show_message_box("t", "x")             # must not raise


# ---------------------------------------------------------------- jitter maths
def test_jitter_stats_perfect_grid_is_zero():
    stamps = [100.0 + i / 60 for i in range(90)]
    p99, worst = doctor.jitter_stats(stamps, 60.0)
    assert p99 < 1e-6 and worst < 1e-6


def test_one_late_frame_counts_once_not_twice():
    stamps = [100.0 + i / 60 for i in range(90)]
    stamps[40] += 0.003                                    # a single frame lands 3 ms late
    p99, worst = doctor.jitter_stats(stamps, 60.0)
    assert worst == pytest.approx(3.0, abs=1e-6)
    assert p99 < 0.1                                       # NOT 3.0: p99 is no longer glued to the worst
    errs_over_1ms = sum(1 for i, t in enumerate(stamps) if abs(t - (100.0 + i / 60)) * 1000 > 1.0)
    assert errs_over_1ms == 1


def test_steady_offset_is_not_jitter():
    stamps = [250.0 + i / 60 for i in range(90)]           # starts at any time; only spacing matters
    assert doctor.jitter_stats(stamps, 60.0)[1] < 1e-6


def test_overrun_forces_at_least_a_warning(monkeypatch):
    import watchdog

    class Stuttering(watchdog.FrameClock):
        def wait(self):
            t = super().wait()
            self.overruns = 1
            return t
    monkeypatch.setattr(watchdog, "FrameClock", Stuttering)
    assert doctor.check_loop_jitter(seconds=0.1).status in (WARN, FAIL)


# ---------------------------------------------------------------- running-instance check
@pytest.mark.skipif(os.name == "nt", reason="uses the flock fallback")
def test_running_instance_warns_when_another_copy_holds_the_lock():
    import uuid

    import single_instance
    name = f"omnitest-{uuid.uuid4().hex}"
    assert doctor.check_running_instance(name).status == OK
    holder = single_instance.SingleInstance(name)
    assert holder.acquire()
    try:
        c = doctor.check_running_instance(name)
        assert c.status == WARN and "camera" in c.fix
    finally:
        holder.release()


def test_message_box_is_skipped_when_no_gui_is_requested(monkeypatch):
    """On Windows a blocking dialog would hang the build smoke test; the env var must prevent it."""
    calls = []
    fake_user32 = type("U", (), {"MessageBoxW": staticmethod(lambda *a: calls.append(a))})
    monkeypatch.setattr(doctor.os, "name", "nt")
    monkeypatch.setattr(doctor.ctypes, "windll", type("W", (), {"user32": fake_user32}), raising=False)
    monkeypatch.setenv("OMNICONTROL_NO_GUI", "1")
    doctor.show_message_box("t", "x")
    assert calls == []
    monkeypatch.delenv("OMNICONTROL_NO_GUI")
    doctor.show_message_box("t", "x")
    assert len(calls) == 1


def test_frozen_build_never_tells_the_user_to_pip_install_into_an_exe(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(doctor.sys, "frozen", True, raising=False)
    c = doctor.check_dependencies()
    assert c.status == WARN and "rebuild" in c.fix and not c.fix.startswith("pip install")
    monkeypatch.delattr(doctor.sys, "frozen")
    assert doctor.check_dependencies().fix.startswith("pip install ")
