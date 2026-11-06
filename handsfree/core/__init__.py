"""Core modules for Handsfree control system."""

from handsfree.core.events import SignalEvent, SignalType, Intent
from handsfree.core.calibration import CalibrationManager
from handsfree.core.filters import OneEuroFilter, EMAFilter, HysteresisFilter, DebounceFilter
from handsfree.core.fastpath import FastPath
from handsfree.core.slowpath import SlowPath
from handsfree.core.state_machine import StateMachine, SystemState
from handsfree.core.mapping import ProfileMapper, OutputCommand
from handsfree.core.gateway import OutputGateway
from handsfree.core.watchdog import Watchdog

__all__ = [
    "SignalEvent",
    "SignalType",
    "Intent",
    "CalibrationManager",
    "OneEuroFilter",
    "EMAFilter",
    "HysteresisFilter",
    "DebounceFilter",
    "FastPath",
    "SlowPath",
    "StateMachine",
    "SystemState",
    "ProfileMapper",
    "OutputCommand",
    "OutputGateway",
    "Watchdog",
]

