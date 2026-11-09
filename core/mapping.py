"""Turns Intents into OutputCommands using a profile.
It decides WHAT an intent does. It never decides whether it is ALLOWED
(that is the state machine) and never touches the OS (that is the gateway)."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from core.events import Intent, OutputCommand
from core.profile import DEFAULT_PROFILE, load_profile


class Mapper:
    """Minimal skeleton conforming to Binore's contract."""

    def __init__(
        self,
        profile: Union[Dict[str, Any], str, Path, None] = None,
        start_layer: Optional[str] = None,
    ):
        if isinstance(profile, (str, Path)):
            self.p, _ = load_profile(profile)
        elif isinstance(profile, dict):
            self.p = profile
        else:
            self.p = DEFAULT_PROFILE
        self.output = self.p.get("output", "gamepad")
        self.kind = "button" if self.output == "gamepad" else "key"
        layers = self.p.get("layers", {})
        self.layer_name = start_layer or (next(iter(layers)) if layers else "main")

    def start(self) -> List[OutputCommand]:
        return []

    def handle(self, intent: Intent, now: Optional[float] = None) -> List[OutputCommand]:
        return []

    def tick(self, now: Optional[float] = None) -> List[OutputCommand]:
        return []

