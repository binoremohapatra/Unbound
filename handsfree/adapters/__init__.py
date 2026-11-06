"""Input adapters for Handsfree system."""

from handsfree.adapters.webcam import WebcamAdapter
from handsfree.adapters.audio import AudioAdapter
from handsfree.adapters.switch import SwitchAdapter
from handsfree.adapters.replay import ReplayAdapter

__all__ = ["WebcamAdapter", "AudioAdapter", "SwitchAdapter", "ReplayAdapter"]

