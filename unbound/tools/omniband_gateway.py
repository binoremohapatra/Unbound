"""omniband_gateway.py - Zero-Dependency Real-Time Host Gateway for OmniBand V2.

Hardware: ESP32-WROOM-32 via USB Serial (115200 baud) or Bluetooth Serial ("OmniBand-V2")
Telemetry format: {"m":int,"c":[d1,d2,d3,d4],"t":[t1,t2,t3,t4],"jaw":"NONE|SINGLE|DOUBLE|HOLD"}

Modes:
  Mode 0 (GAMING):
    - CH1: 'A' (Steer Left)
    - CH2: 'D' (Steer Right)
    - CH3: 'W' (Toggle latch cruise/gas)
    - CH4 SINGLE: Mouse Left Click
    - CH4 DOUBLE: 'R' (Reload) / Space (Handbrake/Jump)
  Mode 1 (WORKSTATION / CODING):
    - CH1: Scroll Up
    - CH2: Scroll Down
    - CH3: Alt+Tab application cycle
    - CH4 SINGLE: Mouse Left Click
    - CH4 DOUBLE: Ctrl+Shift+M (Teams/Meet Mic Mute shortcut)
  Mode 2 (ASSISTIVE / ACCESSIBILITY):
    - CH1: Step Cursor Left (-25px)
    - CH2: Step Cursor Right (+25px)
    - CH3: Step Cursor Up (-25px)
    - CH4 SINGLE: Mouse Left Click
    - CH4 DOUBLE: Key Enter
  Mode 3 (MEDIA / KITCHEN / PPT):
    - CH1: Left Arrow (Prev slide / Rewind)
    - CH2: Right Arrow (Next slide / Fast-Forward)
    - CH3: Volume Up
    - CH4 SINGLE: Space (Play / Pause)
    - CH4 DOUBLE: Volume Mute
  Mode 4 (ICU / INDUSTRIAL SOS):
    - CH1: "WATER / THIRST"
    - CH2: "PAIN / NURSE"
    - CH3: "YES / AFFIRMATIVE"
    - CH4 SINGLE: "NO / CANCEL"
    - CH4 DOUBLE / HOLD: "EMERGENCY SOS ALERT" (distinct audible pattern)
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
from ctypes import wintypes
import json
import os
import sys
import threading
import time
from typing import Optional

# Optional audio & speech
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

try:
    import serial
    import serial.tools.list_ports
    HAS_PYSERIAL = True
except ImportError:
    HAS_PYSERIAL = False

# -----------------------------------------------------------------------------
# Win32 Ctypes SendInput & Virtual Keyboard / Mouse Architecture
# -----------------------------------------------------------------------------
_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32
_WORD = ctypes.c_uint16
_ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", _LONG), ("dy", _LONG), ("mouseData", _DWORD),
        ("dwFlags", _DWORD), ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", _WORD), ("wScan", _WORD), ("dwFlags", _DWORD),
        ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", _DWORD), ("wParamL", _WORD), ("wParamH", _WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", _DWORD), ("u", _INPUT_UNION)]


# Enforce 64-bit alignment
if ctypes.sizeof(ctypes.c_void_p) == 8:
    assert ctypes.sizeof(INPUT) == 40, f"FATAL: sizeof(INPUT) is {ctypes.sizeof(INPUT)}, expected 40"

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP       = 0x0002
KEYEVENTF_SCANCODE    = 0x0008

MOUSEEVENTF_MOVE      = 0x0001
MOUSEEVENTF_LEFTDOWN  = 0x0002
MOUSEEVENTF_LEFTUP    = 0x0004
MOUSEEVENTF_WHEEL     = 0x0800

# Hardware Scan-codes (DirectX set-1)
SCANCODES = {
    "esc": (0x01, False), "tab": (0x0F, False), "enter": (0x1C, False),
    "space": (0x39, False), "backspace": (0x0E, False),
    "shift": (0x2A, False), "ctrl": (0x1D, False), "alt": (0x38, False),
    "w": (0x11, False), "a": (0x1E, False), "s": (0x1F, False), "d": (0x20, False),
    "r": (0x13, False), "m": (0x32, False),
    "left": (0x4B, True), "right": (0x4D, True), "up": (0x48, True), "down": (0x50, True),
    "vol_mute": (0x20, True), "vol_up": (0x30, True), "vol_down": (0x2E, True),
}

# -----------------------------------------------------------------------------
# Input Injector with Authoritative Held State Management
# -----------------------------------------------------------------------------
class WindowsInputSink:
    """Thread-safe SendInput engine with diff-based flush and panic release."""

    def __init__(self):
        self._lock = threading.RLock()
        self._held_keys: set[str] = set()
        atexit.register(self.panic_release_all)

    def key_down(self, name: str) -> None:
        with self._lock:
            if name in self._held_keys or os.name != "nt":
                return
            code, ext = SCANCODES.get(name, (0, False))
            if not code:
                return
            inp = INPUT(type=INPUT_KEYBOARD)
            flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if ext else 0)
            inp.u.ki = KEYBDINPUT(0, code, flags, 0, 0)
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            self._held_keys.add(name)

    def key_up(self, name: str) -> None:
        with self._lock:
            if name not in self._held_keys or os.name != "nt":
                return
            code, ext = SCANCODES.get(name, (0, False))
            if not code:
                return
            inp = INPUT(type=INPUT_KEYBOARD)
            flags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if ext else 0)
            inp.u.ki = KEYBDINPUT(0, code, flags, 0, 0)
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            self._held_keys.discard(name)

    def tap_key(self, name: str, hold_ms: float = 35.0) -> None:
        self.key_down(name)
        time.sleep(hold_ms / 1000.0)
        self.key_up(name)

    def toggle_key(self, name: str) -> bool:
        with self._lock:
            if name in self._held_keys:
                self.key_up(name)
                return False
            else:
                self.key_down(name)
                return True

    def mouse_click(self) -> None:
        if os.name != "nt":
            return
        down = INPUT(type=INPUT_MOUSE)
        down.u.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0)
        up = INPUT(type=INPUT_MOUSE)
        up.u.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0)
        batch = (INPUT * 2)(down, up)
        ctypes.windll.user32.SendInput(2, batch, ctypes.sizeof(INPUT))

    def mouse_move(self, dx: int, dy: int) -> None:
        if os.name != "nt":
            return
        inp = INPUT(type=INPUT_MOUSE)
        inp.u.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, 0)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def mouse_scroll(self, clicks: int) -> None:
        if os.name != "nt":
            return
        inp = INPUT(type=INPUT_MOUSE)
        # WHEEL_DELTA is 120
        inp.u.mi = MOUSEINPUT(0, 0, clicks * 120, MOUSEEVENTF_WHEEL, 0, 0)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def chord_alt_tab(self) -> None:
        if os.name != "nt":
            return
        self.key_down("alt")
        time.sleep(0.04)
        self.tap_key("tab")
        time.sleep(0.04)
        self.key_up("alt")

    def chord_mic_mute(self) -> None:
        """Teams / Google Meet Mic Mute: Ctrl+Shift+M"""
        if os.name != "nt":
            return
        self.key_down("ctrl")
        self.key_down("shift")
        time.sleep(0.03)
        self.tap_key("m")
        time.sleep(0.03)
        self.key_up("shift")
        self.key_up("ctrl")

    def panic_release_all(self) -> None:
        with self._lock:
            for k in list(self._held_keys):
                self.key_up(k)
            self._held_keys.clear()


# -----------------------------------------------------------------------------
# Mode Handlers
# -----------------------------------------------------------------------------
MODE_TITLES = [
    "GAMING (Esports / Kart)",
    "WORKSTATION (Coding & Reading)",
    "ASSISTIVE (Gaze Cursor)",
    "MEDIA / PPT / KITCHEN",
    "ICU SOS (Medical & Emergency)"
]


class ModeDispatcher:
    def __init__(self, sink: WindowsInputSink):
        self.sink = sink
        self.w_latched = False

    def dispatch(self, mode: int, triggers: list[int], jaw: str) -> str:
        t1, t2, t3, t4 = triggers[0], triggers[1], triggers[2], triggers[3]
        action_log = []

        if mode == 0:  # GAMING
            if t1:
                self.sink.tap_key("a", hold_ms=60)
                action_log.append("STEER LEFT [A]")
            if t2:
                self.sink.tap_key("d", hold_ms=60)
                action_log.append("STEER RIGHT [D]")
            if t3:
                self.w_latched = self.sink.toggle_key("w")
                action_log.append(f"GAS [W] {'LATCHED' if self.w_latched else 'RELEASED'}")
            if jaw == "SINGLE":
                self.sink.mouse_click()
                action_log.append("FIRE [Left Click]")
            elif jaw == "DOUBLE":
                self.sink.tap_key("space")
                action_log.append("HANDBRAKE / JUMP [Space]")

        elif mode == 1:  # WORKSTATION
            if t1:
                self.sink.mouse_scroll(+3)
                action_log.append("SCROLL UP")
            if t2:
                self.sink.mouse_scroll(-3)
                action_log.append("SCROLL DOWN")
            if t3:
                self.sink.chord_alt_tab()
                action_log.append("SWITCH APP [Alt+Tab]")
            if jaw == "SINGLE":
                self.sink.mouse_click()
                action_log.append("SELECT [Left Click]")
            elif jaw == "DOUBLE":
                self.sink.chord_mic_mute()
                action_log.append("MIC MUTE [Ctrl+Shift+M]")

        elif mode == 2:  # ASSISTIVE
            if t1:
                self.sink.mouse_move(-25, 0)
                action_log.append("CURSOR LEFT")
            if t2:
                self.sink.mouse_move(+25, 0)
                action_log.append("CURSOR RIGHT")
            if t3:
                self.sink.mouse_move(0, -25)
                action_log.append("CURSOR UP")
            if jaw == "SINGLE":
                self.sink.mouse_click()
                action_log.append("CLICK")
            elif jaw == "DOUBLE":
                self.sink.tap_key("enter")
                action_log.append("ENTER")

        elif mode == 3:  # MEDIA_PPT
            if t1:
                self.sink.tap_key("left")
                action_log.append("PREV SLIDE / REWIND [Left]")
            if t2:
                self.sink.tap_key("right")
                action_log.append("NEXT SLIDE / FWD [Right]")
            if t3:
                self.sink.tap_key("vol_up")
                action_log.append("VOLUME UP")
            if jaw == "SINGLE":
                self.sink.tap_key("space")
                action_log.append("PLAY / PAUSE [Space]")
            elif jaw == "DOUBLE":
                self.sink.tap_key("vol_mute")
                action_log.append("MUTE [Volume Mute]")

        elif mode == 4:  # ICU_SOS
            if t1:
                action_log.append("[PATIENT] WATER / THIRST REQUESTED")
                self._play_beep(600, 200)
            if t2:
                action_log.append("[PATIENT] PAIN / ASSISTANCE NEEDED")
                self._play_beep(900, 300)
            if t3:
                action_log.append("[PATIENT] YES / AFFIRMATIVE")
                self._play_beep(1200, 100)
            if jaw == "SINGLE":
                action_log.append("[PATIENT] NO / CANCEL")
                self._play_beep(400, 150)
            elif jaw in ("DOUBLE", "HOLD"):
                action_log.append("!!! CRITICAL EMERGENCY SOS ALERT !!!")
                self._play_sos_alarm()

        return " | ".join(action_log) if action_log else ""

    def _play_beep(self, freq: int, duration_ms: int) -> None:
        if HAS_WINSOUND and os.name == "nt":
            threading.Thread(target=winsound.Beep, args=(freq, duration_ms), daemon=True).start()

    def _play_sos_alarm(self) -> None:
        def _alarm():
            if HAS_WINSOUND and os.name == "nt":
                for _ in range(3):
                    winsound.Beep(2000, 120)
                    time.sleep(0.05)
                for _ in range(3):
                    winsound.Beep(1400, 250)
                    time.sleep(0.05)
                for _ in range(3):
                    winsound.Beep(2000, 120)
                    time.sleep(0.05)
        threading.Thread(target=_alarm, daemon=True).start()


# -----------------------------------------------------------------------------
# CLI Live Telemetry Dashboard Renderer
# -----------------------------------------------------------------------------
def render_dashboard(mode: int, deltas: list[int], triggers: list[int], jaw: str, last_action: str) -> None:
    # Clear screen using ANSI
    sys.stdout.write("\033[H\033[2J")

    mode_name = MODE_TITLES[mode] if 0 <= mode < len(MODE_TITLES) else f"MODE {mode}"

    lines = [
        "================================================================================",
        f"                 OMNIBAND V2 HARDWARE CONTROLLER GATEWAY                       ",
        "================================================================================",
        f" ACTIVE MODE: [{mode}] {mode_name}",
        f" JAW GESTURE: {jaw:<10}  |  HELD KEYS: {', '.join(g_sink._held_keys) or 'None'}",
        "--------------------------------------------------------------------------------",
        " SENSOR CHANNELS (ADC Delta Envelope):",
    ]

    labels = ["CH1 (Left Temple) ", "CH2 (C-Left Head) ", "CH3 (C-Right Head)", "CH4 (Jaw/R-Temple)"]
    max_bar_width = 32

    for i in range(4):
        val = max(0, min(deltas[i], 400))
        bar_len = int((val / 400.0) * max_bar_width)
        bar_str = "#" * bar_len + "-" * (max_bar_width - bar_len)
        trig_str = "[ACTIVE]" if triggers[i] else "        "
        lines.append(f"  {labels[i]} : [{bar_str}] {deltas[i]:4d} {trig_str}")

    lines.append("--------------------------------------------------------------------------------")
    lines.append(f" LAST ACTION EXECUTED: {last_action or 'Listening...'}")
    lines.append("================================================================================")
    lines.append(" Press Ctrl+C to disconnect and panic release all inputs.")

    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


# -----------------------------------------------------------------------------
# Auto-detect COM Port / Simulated Fallback
# -----------------------------------------------------------------------------
def find_esp32_port() -> Optional[str]:
    if not HAS_PYSERIAL:
        return None
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").lower()
        if "cp210" in desc or "ch340" in desc or "usb-serial" in desc or "esp32" in desc:
            return p.device
    ports = [p.device for p in serial.tools.list_ports.comports()]
    return ports[0] if ports else None


# Global input sink
g_sink = WindowsInputSink()


def run_simulated_mode(dispatcher: ModeDispatcher) -> None:
    print("\n[GATEWAY] Running in SIMULATED KEYBOARD TEST MODE (No physical ESP32 connected)")
    print("[HINT] Press 1-4 for Channel Triggers, 's' for Single Jaw, 'd' for Double Jaw, 'h' for Mode Switch.\n")

    current_mode = 0
    while True:
        try:
            cmd = input("Simulate Input [1,2,3,4, s, d, h, q=exit]: ").strip().lower()
            if cmd == "q":
                break
            deltas = [20, 20, 20, 20]
            triggers = [0, 0, 0, 0]
            jaw = "NONE"

            if cmd == "1":
                triggers[0] = 1
                deltas[0] = 250
            elif cmd == "2":
                triggers[1] = 1
                deltas[1] = 250
            elif cmd == "3":
                triggers[2] = 1
                deltas[2] = 250
            elif cmd == "4":
                triggers[3] = 1
                deltas[3] = 300
            elif cmd == "s":
                jaw = "SINGLE"
                triggers[3] = 1
                deltas[3] = 320
            elif cmd == "d":
                jaw = "DOUBLE"
                triggers[3] = 1
                deltas[3] = 350
            elif cmd == "h":
                jaw = "HOLD"
                current_mode = (current_mode + 1) % 5

            action = dispatcher.dispatch(current_mode, triggers, jaw)
            render_dashboard(current_mode, deltas, triggers, jaw, action)
        except (KeyboardInterrupt, EOFError):
            break


def main() -> int:
    parser = argparse.ArgumentParser(description="OmniBand V2 Host Gateway")
    parser.add_argument("--port", help="Serial port (e.g. COM3 or /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate (default: 115200)")
    parser.add_argument("--simulate", action="store_true", help="Force interactive keyboard simulation test")
    args = parser.parse_args()

    dispatcher = ModeDispatcher(g_sink)

    if args.simulate:
        run_simulated_mode(dispatcher)
        return 0

    port = args.port or find_esp32_port()

    if not port or not HAS_PYSERIAL:
        print("[WARN] ESP32 hardware COM port not found or pyserial missing.")
        run_simulated_mode(dispatcher)
        return 0

    print(f"[GATEWAY] Connecting to OmniBand V2 on {port} @ {args.baud} baud...")

    try:
        ser = serial.Serial(port, args.baud, timeout=0.1)
    except Exception as e:
        print(f"[ERROR] Could not open port {port}: {e}")
        run_simulated_mode(dispatcher)
        return 0

    last_action = ""
    last_render = 0.0

    try:
        while True:
            line = ser.readline().decode("utf-8", errors="replace").strip()
            now = time.perf_counter()

            if line.startswith("{") and line.endswith("}"):
                try:
                    pkt = json.loads(line)
                    mode = int(pkt.get("m", 0))
                    deltas = pkt.get("c", [0, 0, 0, 0])
                    triggers = pkt.get("t", [0, 0, 0, 0])
                    jaw = str(pkt.get("jaw", "NONE"))

                    action = dispatcher.dispatch(mode, triggers, jaw)
                    if action:
                        last_action = action

                    # Update CLI dashboard at 15 Hz to keep CPU <1%
                    if now - last_render >= 0.066:
                        render_dashboard(mode, deltas, triggers, jaw, last_action)
                        last_render = now

                except (json.JSONDecodeError, ValueError):
                    pass

    except KeyboardInterrupt:
        print("\n[GATEWAY] Clean exit requested.")
    finally:
        ser.close()
        g_sink.panic_release_all()
        print("[GATEWAY] All inputs panic released. Disconnected.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
