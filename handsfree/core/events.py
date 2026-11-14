"""SignalEvent and Intent dataclasses for Handsfree event-driven pipeline."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict
import time


class SignalType(str, Enum):
    """Types of raw/filtered signals emitted by input adapters."""
    HEAD_POSE = "head_pose"
    BLENDSHAPE = "blendshape"
    AUDIO_POP = "audio_pop"
    AUDIO_SPEECH = "audio_speech"
    SWITCH = "switch"
    SYSTEM = "system"


@dataclass
class SignalEvent:
    """Raw or filtered sensor signal emitted from an adapter or filter stage."""
    signal_type: SignalType
    name: str
    value: Any
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Intent:
    """Semantic action desired by user gesture/movement, ready for mapping."""
    action: str
    value: float = 1.0
    source: str = ""
    layer: str = "base"
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)



