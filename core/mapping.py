"""Turns Intents into OutputCommands using a profile.
It decides WHAT an intent does. It never decides whether it is ALLOWED
(that is the state machine) and never touches the OS (that is the gateway)."""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set, Union

from core.events import Intent, OutputCommand
from core.profile import DEFAULT_PROFILE, load_profile


def _resolve_profile(profile: Union[Dict[str, Any], str, Path, None]) -> Dict[str, Any]:
    if isinstance(profile, dict):
        return profile
    if isinstance(profile, (str, Path)):
        p_path = Path(profile)
        if p_path.is_file():
            p, _ = load_profile(p_path)
            return p
        # Check standard profile locations
        for cand in (
            Path("profiles") / f"{p_path.stem}.json",
            Path("handsfree") / "profiles" / f"{p_path.stem}.json",
        ):
            if cand.is_file():
                p, _ = load_profile(cand)
                return p
        p, _ = load_profile(p_path)
        return p
    return DEFAULT_PROFILE


class Mapper:
    def __init__(
        self,
        profile: Union[Dict[str, Any], str, Path, None] = None,
        start_layer: Optional[str] = None,
    ):
        self.p = _resolve_profile(profile)
        self.output = self.p.get("output", "gamepad")
        self.kind = "button" if self.output == "gamepad" else "key"
        layers = self.p.get("layers", {})
        self.layer_name = start_layer or (next(iter(layers)) if layers else "main")
        self.last_fire: Dict[str, float] = {}  # action name -> time of last accepted action
        self.held: Dict[str, str] = {}         # action name -> target currently held down
        self.toggled: Set[str] = set()         # action names currently toggled on
        self.key_dir = {"left": False, "right": False, "up": False, "down": False}  # keyboard stick state
        self.paused = False

    @property
    def layer(self) -> Dict[str, Any]:
        return self.p.get("layers", {}).get(self.layer_name, {})

    def _cmd(self, target: str, pressed: bool) -> OutputCommand:
        return OutputCommand(kind=self.kind, name=target, pressed=pressed)

    def _target(self, act: Dict[str, Any]) -> str:
        return str(act.get("button" if self.output == "gamepad" else "key", ""))

    def _throttle_on(self) -> List[OutputCommand]:
        v = self.layer.get("auto_throttle")
        if v is None or self.output != "gamepad":
            return []
        return [OutputCommand(kind="axis", name="right_trigger", value=float(v))]

    def _reset(self) -> None:
        self.held.clear()
        self.toggled.clear()
        self.last_fire.clear()
        self.key_dir = {"left": False, "right": False, "up": False, "down": False}

    def start(self) -> List[OutputCommand]:
        """Commands to send when the app starts (e.g. auto-throttle)."""
        return self._throttle_on()

    def handle(self, intent: Intent, now: Optional[float] = None) -> List[OutputCommand]:
        now = time.monotonic() if now is None else now
        k = intent.kind

        if k == "cancel_all":
            self._reset()
            return [OutputCommand(kind="release_all", name="")]
        if k == "pause":
            self.paused = True
            self._reset()
            return [OutputCommand(kind="release_all", name="")]
        if k == "resume":
            was = self.paused
            self.paused = False
            return self._throttle_on() if was else []
        if self.paused:
            return []

        if k == "stick":
            return self._stick(intent)
        if k == "action":
            return self._action(intent.name, now)
        if k == "hold":
            return self._hold(intent.name, now)
        if k == "release":
            return self._release(intent.name)
        if k == "layer":
            return self._switch_layer(intent.name)

        return []

    def _switch_layer(self, name: str) -> List[OutputCommand]:
        layers = self.p.get("layers", {})
        if name not in layers or name == self.layer_name:
            return []
        cmds = [OutputCommand(kind="release_all", name="")]
        self._reset()
        self.layer_name = name
        return cmds + self._throttle_on()

    def _stick(self, intent: Intent) -> List[OutputCommand]:
        st = self.layer.get("stick")
        if not st:
            return []

        def clamp(v: float) -> float:
            return max(-1.0, min(1.0, float(v)))

        if self.output == "gamepad":
            cmds: List[OutputCommand] = []
            if "x_axis" in st:
                cmds.append(OutputCommand(kind="axis", name=st["x_axis"], value=clamp(intent.x)))
            if "y_axis" in st:
                cmds.append(OutputCommand(kind="axis", name=st["y_axis"], value=clamp(intent.y)))
            return cmds

        # keyboard mode: stick becomes left/right keys past a threshold
        th = float(st.get("threshold", 0.5))
        cmds: List[OutputCommand] = []
        checks = [
            ("left", st.get("left_key"), intent.x <= -th),
            ("right", st.get("right_key"), intent.x >= th),
            ("down", st.get("down_key"), intent.y <= -th),
            ("up", st.get("up_key"), intent.y >= th),
        ]
        for side, key_name, on in checks:
            if key_name and on != self.key_dir.get(side, False):
                self.key_dir[side] = on
                cmds.append(self._cmd(key_name, on))
        return cmds

    def _hold(self, name: str, now: float) -> List[OutputCommand]:
        """Timed assist start. The state machine sends 'release' when time is up."""
        act = self.layer.get("actions", {}).get(name)
        if act is None or name in self.held:
            return []
        self.last_fire[name] = now
        target = self._target(act)
        self.held[name] = target
        return [self._cmd(target, True)]

    def _action(self, name: str, now: float) -> List[OutputCommand]:
        act = self.layer.get("actions", {}).get(name)
        if act is None:
            return []

        cd = act.get("cooldown_ms", 0) / 1000.0
        last = self.last_fire.get(name)
        if last is not None and now - last < cd:
            return []

        self.last_fire[name] = now
        target = self._target(act)
        mode = act.get("mode", "tap")

        if mode == "tap":
            return [self._cmd(target, True), self._cmd(target, False)]

        if mode == "toggle":
            if name in self.toggled:
                self.toggled.discard(name)
                return [self._cmd(target, False)]
            self.toggled.add(name)
            return [self._cmd(target, True)]

        # mode == "hold": stays down until a "release" intent
        if name in self.held:
            return []
        self.held[name] = target
        return [self._cmd(target, True)]

    def _release(self, name: str) -> List[OutputCommand]:
        if not name:
            cmds = [self._cmd(target, False) for target in self.held.values()]
            self.held.clear()
            return cmds
        target = self.held.pop(name, None)
        if target is None:
            return []
        return [self._cmd(target, False)]

    def tick(self, now: Optional[float] = None) -> List[OutputCommand]:
        return []
