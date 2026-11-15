"""config.py - Configuration constants for Unbound Piezo Sensor Testing & Monitoring Tool.

Phase 2 standalone diagnostics suite.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
RECORDINGS_DIR = PROJECT_ROOT / "recordings"

# Serial communication defaults
DEFAULT_BAUDRATES: List[int] = [115200, 921600, 460800, 230400, 57600]
DEFAULT_BAUD: int = 115200
DEFAULT_TIMEOUT: float = 0.5  # seconds

# Wi-Fi UDP fallback configuration (from Phase 1 audit)
WIFI_UDP_PORT: int = 4210
WIFI_AP_SSID: str = "OmniBand_WiFi"

# Target Specifications (for frequency measurement comparison)
TARGET_HARDWARE_ADC_HZ: float = 60.0       # User target specification
FIRMWARE_SPEC_ADC_HZ: float = 500.0         # Actual firmware Core 0 ADC loop
FIRMWARE_SPEC_TELE_HZ: float = 62.5         # Actual firmware telemetry decimation (500Hz / 8)

# Visualization windows (in seconds)
WINDOW_DURATIONS: List[int] = [5, 10, 30, 60]
DEFAULT_WINDOW_SEC: int = 10
MAX_BUFFER_POINTS: int = 10000  # Max history points per channel in rolling buffer

# GUI Refresh parameters
GUI_REFRESH_HZ: int = 40  # 40 FPS graph update (~25 ms) to eliminate UI lag

@dataclass
class ChannelInfo:
    channel_id: int
    name: str
    pin: str
    muscle: str
    color_hex: str
    qcolor: Tuple[int, int, int]
    role: str

CHANNELS: Dict[int, ChannelInfo] = {
    0: ChannelInfo(
        channel_id=1,
        name="CH1 - Left Temple",
        pin="GPIO 32 (ADC1_CH4)",
        muscle="Temporalis (Left)",
        color_hex="#00E5FF",
        qcolor=(0, 229, 255),
        role="Primary Jaw Clench / Steer Left",
    ),
    1: ChannelInfo(
        channel_id=2,
        name="CH2 - Right Temple",
        pin="GPIO 34 (ADC1_CH6)",
        muscle="Temporalis (Right)",
        color_hex="#FF9100",
        qcolor=(255, 145, 0),
        role="Secondary Clench / Steer Right",
    ),
    2: ChannelInfo(
        channel_id=3,
        name="CH3 - Forehead",
        pin="GPIO 33 (ADC1_CH5)",
        muscle="Frontalis (Center)",
        color_hex="#00E676",
        qcolor=(0, 230, 118),
        role="Eyebrow Raise / Throttle",
    ),
    3: ChannelInfo(
        channel_id=4,
        name="CH4 - Jaw / Fallback",
        pin="GPIO 26 (ADC2_CH9)",
        muscle="Masseter / Digital",
        color_hex="#E040FB",
        qcolor=(224, 64, 251),
        role="Digital Pulse / Cross-Coupled",
    ),
}

# Theme Palette (Tactical High-Contrast Dark Theme)
BG_DARK = "#0F1117"
PANEL_BG = "#1A1D27"
CARD_BG = "#222634"
BORDER_COLOR = "#2E3446"
TEXT_PRIMARY = "#FFFFFF"
TEXT_MUTED = "#8B95A5"
ACCENT_BLUE = "#2979FF"
ACCENT_GREEN = "#00E676"
ACCENT_RED = "#FF1744"
ACCENT_WARN = "#FFC400"
