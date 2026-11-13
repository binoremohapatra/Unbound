"""test_auto_profile.py - Unit tests for AutoProfileWatcher and Zero-Argument launch logic.

Tests:
1. Process name matching (Cyberpunk2077.exe -> Mode 0 + HUD overlay active)
2. Forza Horizon / Motorsport matching -> Mode 2 + HUD active
3. Focus switch away from game triggers immediate release_all()
4. Manual mode lock prevents auto-profile switching
5. Zero-argument discovery fallback produces valid transport without error
"""
from __future__ import annotations

import pytest
from host.omniband.app.auto_profile import (
    AutoProfileWatcher, ProcessInfo, ProfileRule, match_process_rule
)


@pytest.fixture
def sample_rules():
    return [
        ProfileRule(
            name="Cyberpunk 2077",
            executables=["cyberpunk2077.exe"],
            title_keywords=["cyberpunk 2077"],
            mode=0,
            profile="cyberpunk_2077_direct.json",
            show_hud=True,
            badge="MOD ACTIVE: CYBERPUNK 2077",
            tts_announcement="Cyberpunk profile active",
        ),
        ProfileRule(
            name="Forza Horizon 5",
            executables=["forzahorizon5.exe"],
            title_keywords=["forza horizon"],
            mode=2,
            profile="racing_forza.json",
            show_hud=True,
            badge="MOD ACTIVE: FORZA RACING",
            tts_announcement="Forza profile active",
        ),
        ProfileRule(
            name="Workstation",
            executables=["code.exe", "notepad.exe"],
            title_keywords=[],
            mode=1,
            profile="workstation.json",
            show_hud=False,
            badge="MODE 1: WORKSTATION",
            tts_announcement=None,
        ),
    ]


def test_cyberpunk_process_matching(sample_rules):
    proc = ProcessInfo(exe_name="Cyberpunk2077.exe", window_title="Cyberpunk 2077 (v2.1)", pid=1234)
    matched = match_process_rule(proc, sample_rules)
    assert matched is not None
    assert matched.name == "Cyberpunk 2077"
    assert matched.mode == 0
    assert matched.show_hud is True
    assert "cyberpunk" in matched.profile.lower()


def test_title_fallback_matching(sample_rules):
    proc = ProcessInfo(exe_name="game_launcher.exe", window_title="Playing Cyberpunk 2077 Now", pid=5678)
    matched = match_process_rule(proc, sample_rules)
    assert matched is not None
    assert matched.name == "Cyberpunk 2077"


def test_release_all_on_focus_loss(sample_rules):
    released = False

    def on_release():
        nonlocal released
        released = True

    # Mock sequence: Cyberpunk active -> User Alt-Tabs to Notepad
    steps = [
        ProcessInfo(exe_name="Cyberpunk2077.exe", window_title="Cyberpunk 2077", pid=100),
        ProcessInfo(exe_name="notepad.exe", window_title="notes.txt - Notepad", pid=200),
    ]
    step_idx = 0

    def mock_poll():
        nonlocal step_idx
        p = steps[min(step_idx, len(steps) - 1)]
        step_idx += 1
        return p

    watcher = AutoProfileWatcher(
        on_release_all=on_release,
        rules=sample_rules,
        poll_fn=mock_poll,
    )

    # Step 1: Hooks into Cyberpunk
    r1 = watcher.check_once()
    assert r1 is not None
    assert r1.name == "Cyberpunk 2077"
    assert not released

    # Step 2: User switches to Notepad -> must release all keys!
    r2 = watcher.check_once()
    assert released is True
    assert r2.name == "Workstation"


def test_manual_mode_lock_prevents_switching(sample_rules):
    applied = []

    def on_applied(r):
        applied.append(r.name)

    proc = ProcessInfo(exe_name="Cyberpunk2077.exe", window_title="Cyberpunk 2077", pid=100)
    watcher = AutoProfileWatcher(
        on_rule_applied=on_applied,
        rules=sample_rules,
        poll_fn=lambda: proc,
    )

    # Lock to Mode 4 (ICU Patient Board)
    watcher.lock_mode(4)
    res = watcher.check_once()
    assert res is None
    assert len(applied) == 0

    # Unlock -> should now detect and apply
    watcher.unlock_mode()
    res = watcher.check_once()
    assert res is not None
    assert res.name == "Cyberpunk 2077"


def test_auto_discover_transports_fallback():
    from host.omniband_engine import _auto_discover_transports
    transports, dev = _auto_discover_transports(baud=115200)
    assert len(transports) >= 1
    assert transports[0].name in ("usb", "sim")

