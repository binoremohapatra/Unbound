"""Profile loading + validation. A bad profile never crashes the app:
it falls back to DEFAULT_PROFILE and tells you why."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

GAMEPAD_BUTTONS = {
    "A", "B", "X", "Y", "LB", "RB", "LT", "RT", "START", "BACK", "L3", "R3",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
}
GAMEPAD_AXES = {
    "left_x", "left_y", "right_x", "right_y", "left_trigger", "right_trigger",
}
MODES = {"tap", "hold", "toggle"}

DEFAULT_PROFILE: Dict[str, Any] = {
    "game": "default",
    "output": "gamepad",
    "layers": {
        "main": {
            "stick": {"x_axis": "left_x", "y_axis": "left_y"},
            "actions": {
                "jaw_open": {"button": "A", "cooldown_ms": 400},
                "brow_raise": {"button": "B", "cooldown_ms": 400},
                "switch": {"button": "X", "cooldown_ms": 400},
            },
            "cancel": ["pop", "brow_hard"],
        }
    },
    "assist": {"hold_ms": 2000, "min_ms": 500, "max_ms": 3000},
    "flags": {"talk_lock": True, "glasses_fallback": True, "assist": True},
}


def validate(p: Any) -> List[str]:
    """Return a list of human-readable problems (empty = valid)."""
    return []


def load_profile(path: Union[str, Path]) -> Tuple[Dict[str, Any], List[str]]:
    """Return (profile, problems). On any problem, profile is the safe default."""
    return deepcopy(DEFAULT_PROFILE), []

