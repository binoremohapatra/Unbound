"""tests/test_key_leak_repro.py — Comprehensive Key-Leak and Sink-Level Process Gate Tests (K-01 to K-05)."""
from __future__ import annotations

import time
import pytest

from core.events import OutputCommand
from gateway import Gateway, ProcessGate, RecordingBackend, GATED_MOVEMENT_KEYS


def test_key_leak_gate_blocks_movement_keys_in_notepad():
    """K-01: Movement keys (WASD) must NEVER leak into Notepad or non-gaming windows."""
    backend = RecordingBackend()
    gate = ProcessGate()
    gate.set_foreground_provider(lambda: ("notepad.exe", "Untitled - Notepad"))
    gw = Gateway(backend, process_gate=gate)

    for k in ["a", "d", "w", "s", "space"]:
        gw.apply([OutputCommand(kind="key", name=k, pressed=True)])
        gw.flush(time.perf_counter())

    held = {cmd[1] for cmd in backend.log if cmd[0] == "key" and cmd[2] is True}
    assert not held, f"LEAK DETECTED: {held} emitted to non-gaming window!"
    assert gate.blocked_count >= 5


def test_key_leak_gate_allows_movement_keys_in_cyberpunk():
    """K-03: Gated keys are allowed when foreground executable is Cyberpunk2077.exe."""
    backend = RecordingBackend()
    gate = ProcessGate()
    gate.set_foreground_provider(lambda: ("cyberpunk2077.exe", "Cyberpunk 2077"))
    gw = Gateway(backend, process_gate=gate)

    gw.apply([OutputCommand(kind="key", name="w", pressed=True)])
    gw.flush(time.perf_counter())

    held = {cmd[1] for cmd in backend.log if cmd[0] == "key" and cmd[2] is True}
    assert "w" in held, "Gated key 'w' was unexpectedly blocked in Cyberpunk 2077!"


def test_key_leak_gate_blocks_spoofed_title_in_unauthorized_exe():
    """K-03/K-05: Window title 'Cyber-Arena' in an unauthorized exe (e.g. chrome.exe) is BLOCKED."""
    backend = RecordingBackend()
    gate = ProcessGate()
    gate.set_foreground_provider(lambda: ("chrome.exe", "Cyber-Arena - Google Chrome"))
    gw = Gateway(backend, process_gate=gate)

    gw.apply([OutputCommand(kind="key", name="a", pressed=True)])
    gw.flush(time.perf_counter())

    held = {cmd[1] for cmd in backend.log if cmd[0] == "key" and cmd[2] is True}
    assert not held, "Title spoofing vulnerability: key leaked because title had 'Cyber-Arena' in wrong exe!"


def test_key_leak_gate_allows_cyber_arena_inside_unbound_exe():
    """K-03: Keys are allowed inside Unbound's native Cyber-Arena simulator tab."""
    backend = RecordingBackend()
    gate = ProcessGate()
    gate.set_foreground_provider(lambda: ("unbound.exe", "Unbound - Cyber-Arena"))
    gw = Gateway(backend, process_gate=gate)

    gw.apply([OutputCommand(kind="key", name="d", pressed=True)])
    gw.flush(time.perf_counter())

    held = {cmd[1] for cmd in backend.log if cmd[0] == "key" and cmd[2] is True}
    assert "d" in held, "Keys blocked in native Cyber-Arena simulator!"


def test_key_leak_gate_releases_held_keys_on_foreground_switch():
    """K-03: When foreground switches from game to desktop mid-hold, held keys release IMMEDIATELY."""
    backend = RecordingBackend()
    gate = ProcessGate()
    current_fg = ["cyberpunk2077.exe", "Cyberpunk 2077"]
    gate.set_foreground_provider(lambda: (current_fg[0], current_fg[1]))
    gw = Gateway(backend, process_gate=gate)

    # Press 'w' inside Cyberpunk
    gw.apply([OutputCommand(kind="key", name="w", pressed=True)])
    gw.flush(10.0)
    assert "w" in gw.held

    # User Alt-Tabs to Notepad while 'w' was held down
    current_fg[0] = "notepad.exe"
    current_fg[1] = "Notes - Notepad"

    # Next tick / flush
    gw.flush(10.05)

    # 'w' must be released immediately by the gate!
    assert "w" not in gw.held, "Held key was not immediately released when focus switched out of game!"
    # Check that a key release for 'w' was sent to backend
    assert ("key", "w", False) in backend.log


def test_key_leak_gate_blocks_unreadable_process():
    """K-03: Unreadable or access-denied process (empty exe) treats the gate as CLOSED."""
    backend = RecordingBackend()
    gate = ProcessGate()
    gate.set_foreground_provider(lambda: ("", ""))
    gw = Gateway(backend, process_gate=gate)

    gw.apply([OutputCommand(kind="key", name="s", pressed=True)])
    gw.flush(time.perf_counter())

    held = {cmd[1] for cmd in backend.log if cmd[0] == "key" and cmd[2] is True}
    assert not held, "Movement key leaked when foreground process was unreadable!"
