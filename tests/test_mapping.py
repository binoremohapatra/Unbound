"""Unit tests for Intent-to-OutputCommand mapping (Step 4: Tap + Cooldown)."""
import pytest

from core.events import Intent, OutputCommand
from core.mapping import Mapper
from core.profile import DEFAULT_PROFILE


def test_tap_action_gamepad():
    m = Mapper(DEFAULT_PROFILE)
    # DEFAULT_PROFILE has jaw_open -> button A (cooldown 400ms)
    cmds = m.handle(Intent("action", "jaw_open"), now=100.0)
    assert cmds == [
        OutputCommand(kind="button", name="A", pressed=True),
        OutputCommand(kind="button", name="A", pressed=False),
    ]


def test_tap_action_keyboard():
    prof = {
        "output": "keyboard",
        "layers": {
            "main": {
                "actions": {
                    "fire": {"key": "space", "cooldown_ms": 150},
                }
            }
        },
    }
    m = Mapper(prof)
    cmds = m.handle(Intent("action", "fire"), now=10.0)
    assert cmds == [
        OutputCommand(kind="key", name="space", pressed=True),
        OutputCommand(kind="key", name="space", pressed=False),
    ]


def test_per_action_cooldown_suppression():
    m = Mapper(DEFAULT_PROFILE)
    # First tap at 100.0 succeeds
    cmds1 = m.handle(Intent("action", "jaw_open"), now=100.0)
    assert len(cmds1) == 2

    # Second tap at 100.2 (200ms < 400ms cooldown) is suppressed
    cmds2 = m.handle(Intent("action", "jaw_open"), now=100.2)
    assert cmds2 == []

    # Second tap at 100.39 (390ms < 400ms cooldown) is suppressed
    cmds3 = m.handle(Intent("action", "jaw_open"), now=100.39)
    assert cmds3 == []

    # Third tap at 100.41 (410ms >= 400ms cooldown) succeeds
    cmds4 = m.handle(Intent("action", "jaw_open"), now=100.41)
    assert len(cmds4) == 2
    assert cmds4[0].name == "A"


def test_different_actions_have_independent_cooldowns():
    m = Mapper(DEFAULT_PROFILE)
    # jaw_open at 100.0 succeeds
    cmds_jaw = m.handle(Intent("action", "jaw_open"), now=100.0)
    assert len(cmds_jaw) == 2

    # brow_raise at 100.05 succeeds despite jaw_open being on cooldown
    cmds_brow = m.handle(Intent("action", "brow_raise"), now=100.05)
    assert len(cmds_brow) == 2
    assert cmds_brow[0].name == "B"


def test_zero_cooldown_allows_consecutive_fires():
    prof = {
        "output": "gamepad",
        "layers": {
            "main": {
                "actions": {
                    "rapid": {"button": "X", "cooldown_ms": 0},
                }
            }
        },
    }
    m = Mapper(prof)
    cmds1 = m.handle(Intent("action", "rapid"), now=1.0)
    cmds2 = m.handle(Intent("action", "rapid"), now=1.001)
    assert len(cmds1) == 2
    assert len(cmds2) == 2


def test_unknown_action_returns_empty_and_does_not_raise():
    m = Mapper(DEFAULT_PROFILE)
    cmds = m.handle(Intent("action", "non_existent_gesture"), now=1.0)
    assert cmds == []
