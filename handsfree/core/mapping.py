"""Profile loader and Intent-to-Output mapping engine."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from handsfree.core.events import Intent


@dataclass
class OutputCommand:
    """Low-level command dispatched to the hardware gateway."""
    device: str
    command_type: str
    target: str
    value: float = 1.0


class ProfileMapper:
    """Translates high-level Intents into low-level OutputCommands according to profile JSON."""

    def __init__(self, profile_data: Optional[Dict[str, Any]] = None):
        self.profile_data = profile_data or {}
        self.name = self.profile_data.get("name", "Default Profile")
        self.mappings = self.profile_data.get("mappings", {})

    @classmethod
    def from_file(cls, path: Path) -> "ProfileMapper":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(data)

    def map_intent(self, intent: Intent) -> List[OutputCommand]:
        commands: List[OutputCommand] = []

        layer_mappings = self.mappings.get(intent.layer, {})
        mapping_entry = layer_mappings.get(intent.action)

        if not mapping_entry and intent.layer != "base":
            mapping_entry = self.mappings.get("base", {}).get(intent.action)

        if not mapping_entry:
            return commands

        entries = mapping_entry if isinstance(mapping_entry, list) else [mapping_entry]

        for entry in entries:
            device = entry.get("device", "gamepad")
            cmd_type = entry.get("type", "button")
            target = entry.get("target", "")
            val = intent.value * entry.get("scale", 1.0)

            commands.append(
                OutputCommand(
                    device=device,
                    command_type=cmd_type,
                    target=target,
                    value=val,
                )
            )

        return commands

