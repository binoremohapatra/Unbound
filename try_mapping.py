"""try_mapping.py – Interactive REPL: type intents, see OutputCommands.

No camera, no game, no OS input needed.

Usage:
    python try_mapping.py profiles/<game>.json

Commands (case-insensitive):
    jaw_open              → action intent for "jaw_open"
    brow_raise            → action intent for "brow_raise"
    <name>                → action intent for any action name in the profile
    hold <name>           → hold intent (timed-assist press)
    release <name>        → release intent (timed-assist release)
    stick <x> [<y>]       → stick intent (floats, e.g. "stick 0.7 -0.3")
    layer <name>          → switch to named layer
    cancel                → cancel_all intent
    pause                 → pause intent
    resume                → resume intent
    tick                  → call mapper.tick() (shows any pending output)
    layers                → list layers in the current profile
    actions               → list actions in the current layer
    profile               → show loaded profile path and output type
    help / ?              → show this help
    quit / exit / q       → exit

OutputCommands are printed as they are emitted.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap – make sure both root and core are on the path
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.events import Intent
from core.mapping import Mapper
from core.profile import load_profile, DEFAULT_PROFILE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_commands(cmds: list) -> None:
    if not cmds:
        return
    for c in cmds:
        print(f"  → {c}")


def _now() -> float:
    return time.monotonic()


def _help() -> None:
    print(__doc__)


def _list_layers(profile: dict) -> None:
    layers = list(profile.get("layers", {}).keys())
    print(f"  layers: {layers}")


def _list_actions(mapper: Mapper) -> None:
    layer = mapper.layer
    actions = list((layer.get("actions") or {}).keys())
    print(f"  actions in '{mapper.layer_name}': {actions}")


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def run_repl(profile_path: str) -> None:
    profile, errs = load_profile(profile_path)
    if errs:
        print(f"⚠  Profile problems (using default profile):")
        for e in errs:
            print(f"   • {e}")
    else:
        print(f"✓  Loaded: {profile_path}  (output={profile['output']})")

    mapper = Mapper(profile)
    start_cmds = mapper.start()
    if start_cmds:
        print("  [start]")
        _print_commands(start_cmds)

    _list_layers(profile)
    _list_actions(mapper)
    print("  Type 'help' for commands, 'quit' to exit.\n")

    while True:
        try:
            line = input("intent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break

        if not line:
            continue

        parts = line.split()
        cmd = parts[0].lower()

        # --- meta commands --------------------------------------------------
        if cmd in ("quit", "exit", "q"):
            print("bye.")
            break
        elif cmd in ("help", "?"):
            _help()
            continue
        elif cmd == "layers":
            _list_layers(profile)
            continue
        elif cmd == "actions":
            _list_actions(mapper)
            continue
        elif cmd == "profile":
            print(f"  path={profile_path}  output={profile['output']}"
                  f"  layer={mapper.layer_name}")
            continue
        elif cmd == "tick":
            cmds = mapper.tick(_now())
            if cmds:
                _print_commands(cmds)
            else:
                print("  (no output)")
            continue

        # --- intent commands ------------------------------------------------
        intent: Intent | None = None
        now = _now()

        if cmd == "hold":
            name = parts[1] if len(parts) > 1 else ""
            if not name:
                print("  usage: hold <action_name>")
                continue
            intent = Intent(kind="hold", name=name)

        elif cmd == "release":
            name = parts[1] if len(parts) > 1 else ""
            if not name:
                print("  usage: release <action_name>")
                continue
            intent = Intent(kind="release", name=name)

        elif cmd == "stick":
            try:
                x = float(parts[1]) if len(parts) > 1 else 0.0
                y = float(parts[2]) if len(parts) > 2 else 0.0
            except ValueError:
                print("  usage: stick <x_float> [<y_float>]")
                continue
            intent = Intent(kind="stick", x=x, y=y)

        elif cmd == "layer":
            name = parts[1] if len(parts) > 1 else ""
            if not name:
                print("  usage: layer <layer_name>")
                continue
            intent = Intent(kind="layer", name=name)

        elif cmd == "cancel":
            intent = Intent(kind="cancel_all")

        elif cmd == "pause":
            intent = Intent(kind="pause")

        elif cmd == "resume":
            intent = Intent(kind="resume")

        else:
            # Treat as an action name
            intent = Intent(kind="action", name=cmd)

        if intent is not None:
            cmds = mapper.handle(intent, now)
            if cmds:
                _print_commands(cmds)
            else:
                print("  (no output — cooldown, paused, or unknown action?)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        # Default to the default profile if none specified
        profile_path = str(_ROOT / "profiles" / "default.json")
        print(f"No profile specified. Using default: {profile_path}")
    else:
        profile_path = sys.argv[1]

    run_repl(profile_path)


if __name__ == "__main__":
    main()
