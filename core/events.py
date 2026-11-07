"""Shared contract between all four owners. Written together in hour 0, then frozen.

Do not change field names or Literal values without telling everyone.
"""
from dataclasses import dataclass
from typing import Literal, Optional


@dataclass
class SignalEvent:
    src: Literal["webcam", "mic", "switch", "replay"]
    name: str            # jaw_open, brow_raise, head_roll, pop ...
    value: float         # 0..1, or degrees for head pose
    t: float             # monotonic clock
    conf: float = 1.0


@dataclass
class Intent:
    kind: Literal["stick", "action", "hold", "release", "layer",
                  "cancel_all", "pause", "resume"]
    name: str = ""
    x: float = 0.0       # stick only
    y: float = 0.0
    ms: int = 0          # hold only


@dataclass
class OutputCommand:     # mapping -> gateway
    kind: Literal["button", "key", "axis", "release_all"]
    name: str            # "X", "space", "left_x"
    pressed: Optional[bool] = None   # buttons and keys
    value: float = 0.0               # axis, -1..1
