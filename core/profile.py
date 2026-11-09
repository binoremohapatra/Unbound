"""Profile loading + validation. A bad profile never crashes the app:
it falls back to DEFAULT_PROFILE and tells you why."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# Buttons accepted by gateway (Binore's BUTTON_MAP + MOUSE_FLAGS)
GAMEPAD_BUTTONS = {
    "A", "B", "X", "Y", "LB", "RB", "LT", "RT", "START", "BACK", "L3", "R3",
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
}

# Axes accepted by gateway (Binore's AXIS_MAP)
GAMEPAD_AXES = {
    "left_x", "left_y", "right_x", "right_y",
}

# Keyboard keys accepted by gateway (DirectX Set-1 scancodes in gateway.SCANCODES)
KEYBOARD_KEYS = {
    "esc", "tab", "enter", "space", "backspace", "shift", "ctrl", "alt",
    "up", "down", "left", "right", "home", "end", "pgup", "pgdn", "insert", "delete",
    *(str(i) for i in range(10)),
    *list("abcdefghijklmnopqrstuvwxyz"),
    *(f"f{i+1}" for i in range(10)),
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
    errs: List[str] = []
    if not isinstance(p, dict):
        return ["profile must be a JSON object"]

    out = p.get("output")
    if out not in ("gamepad", "keyboard"):
        errs.append('"output" must be "gamepad" or "keyboard"')

    layers = p.get("layers")
    if not isinstance(layers, dict) or not layers:
        errs.append('"layers" must be a non-empty object')
        return errs

    for lname, layer in layers.items():
        if not isinstance(layer, dict):
            errs.append(f"layer {lname}: must be an object")
            continue

        actions = layer.get("actions")
        if actions is not None:
            if not isinstance(actions, dict):
                errs.append(f"layer {lname}: actions must be an object")
            else:
                for aname, act in actions.items():
                    where = f"layer {lname}, action {aname}"
                    if not isinstance(act, dict):
                        errs.append(f"{where}: must be an object")
                        continue

                    field = "button" if out == "gamepad" else "key"
                    target = act.get(field)
                    if not isinstance(target, str) or not target:
                        errs.append(f'{where}: missing "{field}"')
                    elif out == "gamepad" and target not in GAMEPAD_BUTTONS:
                        errs.append(f"{where}: unknown gamepad button {target}")
                    elif out == "keyboard" and target.lower() not in KEYBOARD_KEYS:
                        errs.append(f"{where}: unknown keyboard key {target}")

                    if act.get("mode", "tap") not in MODES:
                        errs.append(f"{where}: mode must be one of {sorted(MODES)}")

                    cd = act.get("cooldown_ms", 0)
                    if not isinstance(cd, (int, float)) or cd < 0:
                        errs.append(f"{where}: cooldown_ms must be a number >= 0")

        st = layer.get("stick")
        if st is not None:
            if not isinstance(st, dict):
                errs.append(f"layer {lname}: stick must be an object")
            elif out == "gamepad":
                for k in ("x_axis", "y_axis"):
                    if k in st and st[k] not in GAMEPAD_AXES:
                        errs.append(f"layer {lname}: unknown axis {st[k]}")
            elif out == "keyboard":
                for k in ("left_key", "right_key"):
                    if k in st and (not isinstance(st[k], str) or st[k].lower() not in KEYBOARD_KEYS):
                        errs.append(f"layer {lname}: unknown stick key {st.get(k)}")
                if "threshold" in st:
                    th = st["threshold"]
                    if not (isinstance(th, (int, float)) and 0 <= th <= 1):
                        errs.append(f"layer {lname}: stick threshold must be 0..1")

        at = layer.get("auto_throttle")
        if at is not None and not (isinstance(at, (int, float)) and 0 <= at <= 1):
            errs.append(f"layer {lname}: auto_throttle must be 0..1")

    a = p.get("assist")
    if a is not None:
        if not isinstance(a, dict):
            errs.append("assist must be an object")
        else:
            min_ms = a.get("min_ms", 0)
            hold_ms = a.get("hold_ms", 0)
            max_ms = a.get("max_ms", 10**9)
            if not (
                isinstance(min_ms, (int, float))
                and isinstance(hold_ms, (int, float))
                and isinstance(max_ms, (int, float))
                and min_ms <= hold_ms <= max_ms
            ):
                errs.append("assist: need min_ms <= hold_ms <= max_ms")

    return errs


def load_profile(path: Union[str, Path]) -> Tuple[Dict[str, Any], List[str]]:
    """Return (profile, problems). On any problem, profile is the safe default."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            p = json.load(f)
    except (OSError, ValueError, TypeError) as e:
        return deepcopy(DEFAULT_PROFILE), [f"could not read {path}: {e}"]

    errs = validate(p)
    if errs:
        return deepcopy(DEFAULT_PROFILE), errs
    return p, []
