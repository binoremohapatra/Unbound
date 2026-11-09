"""Unit tests for profile loading and validation (Step 2)."""
import json
from copy import deepcopy
import pytest

from core.profile import (
    DEFAULT_PROFILE,
    GAMEPAD_AXES,
    GAMEPAD_BUTTONS,
    KEYBOARD_KEYS,
    load_profile,
    validate,
)


def test_default_profile_is_valid():
    errs = validate(DEFAULT_PROFILE)
    assert errs == []


def test_valid_gamepad_profile():
    p = {
        "game": "TestGame",
        "output": "gamepad",
        "layers": {
            "main": {
                "stick": {"x_axis": "left_x", "y_axis": "left_y"},
                "auto_throttle": 0.75,
                "actions": {
                    "jump": {"button": "A", "mode": "tap", "cooldown_ms": 200},
                    "boost": {"button": "RT", "mode": "hold"},
                    "headlights": {"button": "Y", "mode": "toggle"},
                },
            }
        },
        "assist": {"min_ms": 100, "hold_ms": 1500, "max_ms": 4000},
    }
    assert validate(p) == []


def test_valid_keyboard_profile():
    p = {
        "game": "TestKeyboardGame",
        "output": "keyboard",
        "layers": {
            "drive": {
                "stick": {"left_key": "a", "right_key": "d", "threshold": 0.4},
                "actions": {
                    "steer_hard": {"key": "left", "mode": "hold"},
                    "brake": {"key": "space", "cooldown_ms": 100},
                    "gear": {"key": "shift", "mode": "toggle"},
                },
            }
        },
    }
    assert validate(p) == []


def test_invalid_output_mode():
    p = deepcopy(DEFAULT_PROFILE)
    p["output"] = "touchscreen"
    errs = validate(p)
    assert any('"output" must be "gamepad" or "keyboard"' in e for e in errs)


def test_missing_or_empty_layers():
    assert validate({}) != []
    assert any("layers" in e for e in validate({"output": "gamepad"}))
    assert any("layers" in e for e in validate({"output": "gamepad", "layers": {}}))
    assert any("object" in e for e in validate("not a dict"))


def test_unknown_gamepad_button():
    p = deepcopy(DEFAULT_PROFILE)
    p["layers"]["main"]["actions"]["test"] = {"button": "NOT_A_REAL_BUTTON"}
    errs = validate(p)
    assert any("unknown gamepad button NOT_A_REAL_BUTTON" in e for e in errs)


def test_unknown_keyboard_key():
    p = {
        "output": "keyboard",
        "layers": {
            "main": {
                "actions": {"fire": {"key": "invalid_magic_key"}}
            }
        },
    }
    errs = validate(p)
    assert any("unknown keyboard key invalid_magic_key" in e for e in errs)


def test_unknown_gamepad_axis():
    p = deepcopy(DEFAULT_PROFILE)
    p["layers"]["main"]["stick"] = {"x_axis": "super_axis"}
    errs = validate(p)
    assert any("unknown axis super_axis" in e for e in errs)


def test_invalid_mode():
    p = deepcopy(DEFAULT_PROFILE)
    p["layers"]["main"]["actions"]["jaw_open"]["mode"] = "spam"
    errs = validate(p)
    assert any("mode must be one of" in e for e in errs)


def test_invalid_cooldown():
    p = deepcopy(DEFAULT_PROFILE)
    p["layers"]["main"]["actions"]["jaw_open"]["cooldown_ms"] = -50
    errs = validate(p)
    assert any("cooldown_ms must be a number >= 0" in e for e in errs)


def test_invalid_auto_throttle():
    p = deepcopy(DEFAULT_PROFILE)
    p["layers"]["main"]["auto_throttle"] = 1.5
    errs = validate(p)
    assert any("auto_throttle must be 0..1" in e for e in errs)

    p["layers"]["main"]["auto_throttle"] = -0.1
    assert any("auto_throttle must be 0..1" in e for e in validate(p))


def test_invalid_assist_timings():
    p = deepcopy(DEFAULT_PROFILE)
    p["assist"] = {"min_ms": 3000, "hold_ms": 1000, "max_ms": 2000}
    errs = validate(p)
    assert any("assist: need min_ms <= hold_ms <= max_ms" in e for e in errs)


def test_load_profile_valid_file(tmp_path):
    prof_file = tmp_path / "valid.json"
    prof_file.write_text(json.dumps(DEFAULT_PROFILE), encoding="utf-8")
    loaded, errs = load_profile(prof_file)
    assert errs == []
    assert loaded["game"] == DEFAULT_PROFILE["game"]

