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


# --- Step 5: Hold and Toggle Modes ---


def test_hold_mode_action_presses_and_releases():
    prof = {
        "output": "gamepad",
        "layers": {
            "main": {
                "actions": {
                    "drift": {"button": "B", "mode": "hold", "cooldown_ms": 100},
                }
            }
        },
    }
    m = Mapper(prof)
    # Action intent presses button B down
    cmds_down = m.handle(Intent("action", "drift"), now=10.0)
    assert cmds_down == [OutputCommand(kind="button", name="B", pressed=True)]
    assert m.held == {"drift": "B"}

    # Repeated action while already held returns empty
    cmds_repeat = m.handle(Intent("action", "drift"), now=10.2)
    assert cmds_repeat == []

    # Release intent releases button B
    cmds_up = m.handle(Intent("release", "drift"), now=10.5)
    assert cmds_up == [OutputCommand(kind="button", name="B", pressed=False)]
    assert m.held == {}

    # Redundant release returns empty
    cmds_up2 = m.handle(Intent("release", "drift"), now=10.6)
    assert cmds_up2 == []


def test_toggle_mode_action_flips_state():
    prof = {
        "output": "gamepad",
        "layers": {
            "main": {
                "actions": {
                    "headlights": {"button": "Y", "mode": "toggle", "cooldown_ms": 200},
                }
            }
        },
    }
    m = Mapper(prof)
    # 1. First trigger turns ON (pressed=True)
    c1 = m.handle(Intent("action", "headlights"), now=1.0)
    assert c1 == [OutputCommand(kind="button", name="Y", pressed=True)]
    assert "headlights" in m.toggled

    # 2. Trigger during cooldown (100ms < 200ms) is suppressed
    c2 = m.handle(Intent("action", "headlights"), now=1.1)
    assert c2 == []
    assert "headlights" in m.toggled

    # 3. Second trigger after cooldown turns OFF (pressed=False)
    c3 = m.handle(Intent("action", "headlights"), now=1.3)
    assert c3 == [OutputCommand(kind="button", name="Y", pressed=False)]
    assert "headlights" not in m.toggled

    # 4. Third trigger turns ON again
    c4 = m.handle(Intent("action", "headlights"), now=1.6)
    assert c4 == [OutputCommand(kind="button", name="Y", pressed=True)]
    assert "headlights" in m.toggled


def test_hold_and_toggle_keyboard_mode():
    prof = {
        "output": "keyboard",
        "layers": {
            "main": {
                "actions": {
                    "sprint": {"key": "shift", "mode": "hold"},
                    "cruise": {"key": "w", "mode": "toggle", "cooldown_ms": 100},
                }
            }
        },
    }
    m = Mapper(prof)
    # Hold mode keyboard key
    assert m.handle(Intent("action", "sprint"), now=1.0) == [
        OutputCommand(kind="key", name="shift", pressed=True)
    ]
    assert m.handle(Intent("release", "sprint"), now=1.5) == [
        OutputCommand(kind="key", name="shift", pressed=False)
    ]

    # Toggle mode keyboard key
    assert m.handle(Intent("action", "cruise"), now=2.0) == [
        OutputCommand(kind="key", name="w", pressed=True)
    ]
    assert m.handle(Intent("action", "cruise"), now=2.2) == [
        OutputCommand(kind="key", name="w", pressed=False)
    ]


# --- Step 6: Timed Assist Hold/Release Pairing ---


def test_timed_assist_hold_and_release_pairing():
    m = Mapper(DEFAULT_PROFILE)
    # 1. State machine sends hold intent
    cmds_down = m.handle(Intent("hold", "jaw_open", ms=2000), now=10.0)
    assert cmds_down == [OutputCommand(kind="button", name="A", pressed=True)]
    assert m.held == {"jaw_open": "A"}

    # 2. Later state machine sends release intent on hold expiry
    cmds_up = m.handle(Intent("release", "jaw_open"), now=12.0)
    assert cmds_up == [OutputCommand(kind="button", name="A", pressed=False)]
    assert m.held == {}


def test_hold_intent_duplicate_ignored():
    m = Mapper(DEFAULT_PROFILE)
    cmds1 = m.handle(Intent("hold", "jaw_open", ms=2000), now=10.0)
    assert len(cmds1) == 1
    # Duplicate hold before release returns empty
    cmds2 = m.handle(Intent("hold", "jaw_open", ms=2000), now=10.5)
    assert cmds2 == []


def test_hold_intent_keyboard_mode():
    prof = {
        "output": "keyboard",
        "layers": {
            "main": {
                "actions": {
                    "fire": {"key": "space", "cooldown_ms": 100},
                }
            }
        },
    }
    m = Mapper(prof)
    cmds_down = m.handle(Intent("hold", "fire", ms=1500), now=1.0)
    assert cmds_down == [OutputCommand(kind="key", name="space", pressed=True)]
    assert m.held == {"fire": "space"}

    cmds_up = m.handle(Intent("release", "fire"), now=2.5)
    assert cmds_up == [OutputCommand(kind="key", name="space", pressed=False)]
    assert m.held == {}


def test_hold_intent_unknown_action_returns_empty():
    m = Mapper(DEFAULT_PROFILE)
    cmds = m.handle(Intent("hold", "unknown_action", ms=1000), now=1.0)
    assert cmds == []


def test_hold_paired_with_binores_state_machine():
    from state_machine import SMConfig, StateMachine
    sm = StateMachine(SMConfig(default_hold_ms=1000))
    m = Mapper(DEFAULT_PROFILE)

    # State machine gates a hold intent
    gated = sm.gate([Intent("hold", "jaw_open", ms=1000)], now=100.0)
    assert len(gated) == 1
    cmds_down = m.handle(gated[0], now=100.0)
    assert cmds_down == [OutputCommand(kind="button", name="A", pressed=True)]
    assert m.held == {"jaw_open": "A"}

    # Mid-hold tick: not expired yet
    mid_expired = sm.tick(now=100.5)
    assert mid_expired == []

    # Hold expiration tick: state machine emits Intent("release", "jaw_open")
    exp_intents = sm.tick(now=101.0)
    assert exp_intents == [Intent("release", "jaw_open")]

    # Mapper processes the release intent
    cmds_up = m.handle(exp_intents[0], now=101.0)
    assert cmds_up == [OutputCommand(kind="button", name="A", pressed=False)]
    assert m.held == {}


# --- Step 7: Stick -> Gamepad Axes and Keyboard Keys ---


def test_stick_gamepad_axes():
    m = Mapper(DEFAULT_PROFILE)
    cmds = m.handle(Intent("stick", x=0.75, y=-0.5))
    assert cmds == [
        OutputCommand(kind="axis", name="left_x", value=0.75),
        OutputCommand(kind="axis", name="left_y", value=-0.5),
    ]


def test_stick_gamepad_axes_clamped():
    m = Mapper(DEFAULT_PROFILE)
    cmds = m.handle(Intent("stick", x=1.5, y=-2.0))
    assert cmds == [
        OutputCommand(kind="axis", name="left_x", value=1.0),
        OutputCommand(kind="axis", name="left_y", value=-1.0),
    ]


def test_stick_keyboard_threshold_and_state_changes():
    prof = {
        "output": "keyboard",
        "layers": {
            "main": {
                "stick": {
                    "left_key": "left",
                    "right_key": "right",
                    "up_key": "up",
                    "down_key": "down",
                    "threshold": 0.5,
                }
            }
        },
    }
    m = Mapper(prof)

    # 1. Below threshold: no key event
    assert m.handle(Intent("stick", x=0.2, y=0.1)) == []

    # 2. Deflection right past threshold: presses right key
    c_right = m.handle(Intent("stick", x=0.8, y=0.0))
    assert c_right == [OutputCommand(kind="key", name="right", pressed=True)]
    assert m.key_dir["right"] is True

    # 3. Continued right deflection: no redundant press
    assert m.handle(Intent("stick", x=0.9, y=0.0)) == []

    # 4. Return to center: releases right key
    c_neutral = m.handle(Intent("stick", x=0.0, y=0.0))
    assert c_neutral == [OutputCommand(kind="key", name="right", pressed=False)]
    assert m.key_dir["right"] is False

    # 5. Deflection left past threshold: presses left key
    c_left = m.handle(Intent("stick", x=-0.6, y=0.0))
    assert c_left == [OutputCommand(kind="key", name="left", pressed=True)]
    assert m.key_dir["left"] is True

    # 6. Direct flip from left to right: releases left, presses right
    c_flip = m.handle(Intent("stick", x=0.7, y=0.0))
    assert c_flip == [
        OutputCommand(kind="key", name="left", pressed=False),
        OutputCommand(kind="key", name="right", pressed=True),
    ]
    assert m.key_dir["left"] is False
    assert m.key_dir["right"] is True

    # 7. Deflection up past threshold: presses up key
    c_up = m.handle(Intent("stick", x=0.7, y=0.8))
    assert c_up == [OutputCommand(kind="key", name="up", pressed=True)]
    assert m.key_dir["up"] is True


def test_stick_missing_configuration_returns_empty():
    prof = {
        "output": "gamepad",
        "layers": {
            "main": {
                "actions": {"jump": {"button": "A"}}
            }
        },
    }
    m = Mapper(prof)
    assert m.handle(Intent("stick", x=0.8, y=-0.5)) == []


# --- Step 8: Layers + Auto-Throttle ---


def test_start_emits_auto_throttle_on_gamepad():
    prof = {
        "output": "gamepad",
        "layers": {
            "drive": {
                "auto_throttle": 0.8,
                "actions": {"boost": {"button": "A"}},
            }
        },
    }
    m = Mapper(prof)
    cmds = m.start()
    assert cmds == [OutputCommand(kind="axis", name="right_trigger", value=0.8)]


def test_start_no_auto_throttle_on_keyboard():
    prof = {
        "output": "keyboard",
        "layers": {
            "drive": {
                "auto_throttle": 0.8,
                "actions": {"brake": {"key": "space"}},
            }
        },
    }
    m = Mapper(prof)
    assert m.start() == []


def test_layer_switch_changes_active_layer_and_actions():
    prof = {
        "output": "gamepad",
        "layers": {
            "drive": {
                "actions": {"jaw_open": {"button": "X", "cooldown_ms": 100}},
            },
            "menu": {
                "actions": {"jaw_open": {"button": "A", "cooldown_ms": 100}},
            },
        },
    }
    m = Mapper(prof, start_layer="drive")
    assert m.layer_name == "drive"

    # In "drive" layer, jaw_open maps to button X
    c_drive = m.handle(Intent("action", "jaw_open"), now=1.0)
    assert c_drive == [
        OutputCommand(kind="button", name="X", pressed=True),
        OutputCommand(kind="button", name="X", pressed=False),
    ]

    # Switch to "menu" layer
    c_switch = m.handle(Intent("layer", "menu"))
    assert c_switch == [OutputCommand(kind="release_all", name="")]
    assert m.layer_name == "menu"

    # In "menu" layer, jaw_open maps to button A
    c_menu = m.handle(Intent("action", "jaw_open"), now=2.0)
    assert c_menu == [
        OutputCommand(kind="button", name="A", pressed=True),
        OutputCommand(kind="button", name="A", pressed=False),
    ]


def test_layer_switch_clears_held_toggled_cooldowns_and_directions():
    prof = {
        "output": "gamepad",
        "layers": {
            "layer1": {
                "actions": {
                    "hold_act": {"button": "B", "mode": "hold"},
                    "toggle_act": {"button": "Y", "mode": "toggle"},
                    "tap_act": {"button": "X", "cooldown_ms": 1000},
                }
            },
            "layer2": {
                "actions": {
                    "other_act": {"button": "A"},
                }
            },
        },
    }
    m = Mapper(prof, start_layer="layer1")
    m.handle(Intent("action", "hold_act"), now=1.0)
    m.handle(Intent("action", "toggle_act"), now=1.0)
    m.handle(Intent("action", "tap_act"), now=1.0)
    m.key_dir["left"] = True

    assert len(m.held) == 1
    assert len(m.toggled) == 1
    assert len(m.last_fire) == 3

    # Switch layer clears all transient states
    cmds = m.handle(Intent("layer", "layer2"))
    assert cmds == [OutputCommand(kind="release_all", name="")]
    assert m.held == {}
    assert m.toggled == set()
    assert m.last_fire == {}
    assert m.key_dir == {"left": False, "right": False, "up": False, "down": False}


def test_layer_switch_with_auto_throttle():
    prof = {
        "output": "gamepad",
        "layers": {
            "menu": {
                "actions": {"select": {"button": "A"}},
            },
            "drive": {
                "auto_throttle": 0.85,
                "actions": {"fire": {"button": "RB"}},
            },
        },
    }
    m = Mapper(prof, start_layer="menu")
    cmds = m.handle(Intent("layer", "drive"))
    assert cmds == [
        OutputCommand(kind="release_all", name=""),
        OutputCommand(kind="axis", name="right_trigger", value=0.85),
    ]


def test_layer_switch_to_same_or_unknown_layer_returns_empty():
    prof = {
        "output": "gamepad",
        "layers": {
            "main": {"actions": {"jump": {"button": "A"}}}
        },
    }
    m = Mapper(prof)
    # Switch to currently active layer -> empty
    assert m.handle(Intent("layer", "main")) == []
    # Switch to unknown layer -> empty
    assert m.handle(Intent("layer", "non_existent_layer")) == []


# --- Step 9: cancel_all / pause / resume / layer-change resets ---


def test_cancel_all_resets_state_and_emits_release_all():
    m = Mapper(DEFAULT_PROFILE)
    # Put state in: hold action, toggle action, deflect stick
    m.handle(Intent("hold", "jaw_open"), now=1.0)
    m.handle(Intent("action", "brow_raise"), now=1.0)
    m.key_dir["left"] = True

    assert len(m.held) == 1
    assert len(m.last_fire) > 0

    cmds = m.handle(Intent("cancel_all"))
    assert cmds == [OutputCommand(kind="release_all", name="")]
    assert m.held == {}
    assert m.toggled == set()
    assert m.last_fire == {}
    assert m.key_dir == {"left": False, "right": False, "up": False, "down": False}


def test_pause_resets_state_and_emits_release_all():
    m = Mapper(DEFAULT_PROFILE)
    m.handle(Intent("hold", "jaw_open"), now=1.0)
    assert m.held == {"jaw_open": "A"}

    cmds = m.handle(Intent("pause"))
    assert cmds == [OutputCommand(kind="release_all", name="")]
    assert m.paused is True
    assert m.held == {}
    assert m.toggled == set()


def test_while_paused_ignores_intents_except_resume_and_cancel():
    m = Mapper(DEFAULT_PROFILE)
    m.handle(Intent("pause"))
    assert m.paused is True

    # Actions, sticks, holds, releases, layers ignored while paused
    assert m.handle(Intent("action", "jaw_open"), now=1.0) == []
    assert m.handle(Intent("stick", x=0.8, y=-0.5)) == []
    assert m.handle(Intent("hold", "jaw_open"), now=1.0) == []
    assert m.handle(Intent("release", "jaw_open")) == []
    assert m.handle(Intent("layer", "menu")) == []


def test_resume_restores_activity_and_auto_throttle():
    prof = {
        "output": "gamepad",
        "layers": {
            "drive": {
                "auto_throttle": 0.8,
                "actions": {"boost": {"button": "A"}},
            }
        },
    }
    m = Mapper(prof)
    # 1. Pause
    c_pause = m.handle(Intent("pause"))
    assert c_pause == [OutputCommand(kind="release_all", name="")]
    assert m.paused is True

    # 2. Resume restores paused=False and emits auto_throttle
    c_resume = m.handle(Intent("resume"))
    assert c_resume == [OutputCommand(kind="axis", name="right_trigger", value=0.8)]
    assert m.paused is False

    # 3. Actions function normally after resume
    c_action = m.handle(Intent("action", "boost"), now=10.0)
    assert len(c_action) == 2
    assert c_action[0].name == "A"


def test_resume_without_prior_pause_emits_nothing():
    prof = {
        "output": "gamepad",
        "layers": {
            "drive": {"auto_throttle": 0.8, "actions": {}}
        },
    }
    m = Mapper(prof)
    assert m.paused is False
    assert m.handle(Intent("resume")) == []


def test_cancel_all_while_paused():
    m = Mapper(DEFAULT_PROFILE)
    m.handle(Intent("pause"))
    assert m.paused is True
    # cancel_all while paused still returns release_all and stays safely reset
    cmds = m.handle(Intent("cancel_all"))
    assert cmds == [OutputCommand(kind="release_all", name="")]
    assert m.paused is True





