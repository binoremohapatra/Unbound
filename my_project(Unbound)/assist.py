"""Assist layer: maps gesture actions to timed-hold intents.

Stateless by design. It never tracks elapsed time, releases or timeouts: the
emitted ``Intent(kind="hold", ms=...)`` is a request, and state_machine.py owns
its lifetime (expiry, cancellation, key release).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from core.events import Intent


@dataclass(frozen=True)
class HoldAssist:
    hold_ms: int
    name: Optional[str] = None   # hold target; defaults to the triggering action's name

    def __post_init__(self) -> None:
        if self.hold_ms <= 0:
            raise ValueError("hold_ms must be > 0")


class AssistEngine:
    __slots__ = ("_profile", "_enabled")

    def __init__(self, profile: Mapping[str, HoldAssist], enabled: bool = True) -> None:
        self._profile: dict[str, HoldAssist] = dict(profile)
        self._enabled = enabled

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any], enabled: bool = True) -> "AssistEngine":
        """cfg example: {"jaw_open": {"hold_ms": 800, "name": "sprint"}}"""
        profile = {
            action: HoldAssist(int(v["hold_ms"]), v.get("name"))
            for action, v in cfg.items()
        }
        return cls(profile, enabled=enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def process(self, intent: Intent) -> Intent:
        """Return a hold Intent if ``intent`` is an assisted action; otherwise
        return the input object unchanged (no copy, no allocation)."""
        if not self._enabled or intent.kind != "action":
            return intent
        rule = self._profile.get(intent.name)
        if rule is None:
            return intent
        return Intent(kind="hold", name=rule.name or intent.name, ms=rule.hold_ms)
