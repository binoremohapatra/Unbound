"""Hierarchical State Machine: states, layers, latches, and allowed-intent filtering."""

from enum import Enum
from typing import Dict, Optional, Set
from handsfree.core.events import Intent


class SystemState(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    MENU = "menu"
    CALIBRATING = "calibrating"
    EMERGENCY_STOP = "emergency_stop"


class StateMachine:
    """Controls modes, active layers, latching buttons, and intent filtering."""

    def __init__(self, initial_state: SystemState = SystemState.ACTIVE):
        self.current_state = initial_state
        self.active_layer: str = "base"
        self.latches: Dict[str, bool] = {}

        self.allowed_intents: Dict[SystemState, Set[str]] = {
            SystemState.ACTIVE: {"*"},
            SystemState.MENU: {"stick_x", "stick_y", "menu_select", "menu_back", "pause", "emergency_stop"},
            SystemState.PAUSED: {"unpause", "pause", "emergency_stop"},
            SystemState.CALIBRATING: {"calibrate_step", "calibrate_done", "cancel"},
            SystemState.EMERGENCY_STOP: {"emergency_reset"},
        }

    def transition_to(self, new_state: SystemState) -> None:
        self.current_state = new_state

    def set_layer(self, layer_name: str) -> None:
        self.active_layer = layer_name

    def toggle_latch(self, action: str) -> bool:
        current = self.latches.get(action, False)
        self.latches[action] = not current
        return self.latches[action]

    def is_latched(self, action: str) -> bool:
        return self.latches.get(action, False)

    def is_intent_allowed(self, intent: Intent) -> bool:
        allowed = self.allowed_intents.get(self.current_state, set())
        if "*" in allowed:
            return True
        return intent.action in allowed

    def filter_intent(self, intent: Intent) -> Optional[Intent]:
        if not self.is_intent_allowed(intent):
            return None

        intent.layer = self.active_layer
        return intent

