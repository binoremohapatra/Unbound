"""Centralized Command Router: Maps natural voice and gesture intents to safe Windows actions.

Safety Guarantees:
  - STRICT allowlist registry: never executes arbitrary shell commands or user strings.
  - Dedup prevention: prevents rapid double-activation of repetitive speech recognition frames (2.0s cooldown).
  - Safe Windows API invocation via ctypes, subprocess (allowlisted executables), and stdlib webbrowser.
  - Direct integration with Unbound Handsfree assistive services (OSK, Cursor, Screen Reader).
"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("handsfree.command_router")

# Windows Virtual Key Codes
VK_CONTROL = 0x11
VK_MENU = 0x12       # Alt key
VK_SHIFT = 0x10
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_F4 = 0x73
VK_F5 = 0x74
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_SNAPSHOT = 0x2C   # PrintScreen
KEYEVENTF_KEYUP = 0x0002
SW_MINIMIZE = 6
SW_MAXIMIZE = 3


def _send_key_combo(*keys: int) -> None:
    """Dispatches a hardware-level keyboard shortcut via Windows user32 keybd_event."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        for k in keys:
            user32.keybd_event(k, 0, 0, 0)
        time.sleep(0.04)
        for k in reversed(keys):
            user32.keybd_event(k, 0, KEYEVENTF_KEYUP, 0)
    except Exception as e:
        logger.error(f"Error sending key combo: {e}")


def _get_foreground_hwnd() -> int:
    """Returns the window handle of the currently focused foreground window."""
    if os.name != "nt":
        return 0
    try:
        return ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        return 0


class CommandRouter:
    """Centralized allowlisted command execution router for hands-free voice control."""

    def __init__(
        self,
        on_assistive_action: Optional[Callable[[str, Any], None]] = None,
        tts_speaker: Optional[Callable[[str], None]] = None,
        cooldown_seconds: float = 2.0,
    ):
        self.on_assistive_action = on_assistive_action
        self.tts_speaker = tts_speaker
        self.cooldown_seconds = cooldown_seconds

        self.last_command: str = ""
        self.last_exec_time: float = 0.0

        # Build allowlist registry
        self._init_registry()

    def speak(self, text: str) -> None:
        """Announces status to user via Text-to-Speech."""
        if self.tts_speaker:
            try:
                self.tts_speaker(text)
                return
            except Exception:
                pass

        # Native Windows PowerShell SAPI fallback
        try:
            safe_text = text.replace("'", "''").replace('"', '`"')
            cmd = f"$voice = New-Object -ComObject SAPI.SpVoice; $voice.Speak('{safe_text}')"
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            logger.info(f"[SPEECH FEEDBACK]: {text}")

    def _init_registry(self) -> None:
        """Initializes exact and pattern-matched command actions."""
        self.registry: Dict[str, Dict[str, Any]] = {
            # Browser Commands
            "open google": {
                "action": lambda: webbrowser.open("https://www.google.com"),
                "feedback": "Opening Google.",
                "category": "Browser",
            },
            "open youtube": {
                "action": lambda: webbrowser.open("https://www.youtube.com"),
                "feedback": "Opening YouTube.",
                "category": "Browser",
            },
            "open chatgpt": {
                "action": lambda: webbrowser.open("https://chatgpt.com"),
                "feedback": "Opening ChatGPT.",
                "category": "Browser",
            },
            "new tab": {
                "action": lambda: _send_key_combo(VK_CONTROL, 0x54),  # Ctrl + T
                "feedback": "New tab opened.",
                "category": "Browser",
            },
            "close tab": {
                "action": lambda: _send_key_combo(VK_CONTROL, 0x57),  # Ctrl + W
                "feedback": "Tab closed.",
                "category": "Browser",
            },
            "go back": {
                "action": lambda: _send_key_combo(VK_MENU, VK_LEFT),  # Alt + Left
                "feedback": "Navigating back.",
                "category": "Browser",
            },
            "go forward": {
                "action": lambda: _send_key_combo(VK_MENU, VK_RIGHT),  # Alt + Right
                "feedback": "Navigating forward.",
                "category": "Browser",
            },
            "refresh": {
                "action": lambda: _send_key_combo(VK_F5),  # F5
                "feedback": "Page refreshed.",
                "category": "Browser",
            },
            "next tab": {
                "action": lambda: _send_key_combo(VK_CONTROL, VK_TAB),  # Ctrl + Tab
                "feedback": "Switched to next tab.",
                "category": "Browser",
            },
            "previous tab": {
                "action": lambda: _send_key_combo(VK_CONTROL, VK_SHIFT, VK_TAB),  # Ctrl + Shift + Tab
                "feedback": "Switched to previous tab.",
                "category": "Browser",
            },

            # Desktop / Windows Commands
            "open notepad": {
                "action": lambda: subprocess.Popen(["notepad.exe"]),
                "feedback": "Opening Notepad.",
                "category": "Desktop",
            },
            "open calculator": {
                "action": lambda: subprocess.Popen(["calc.exe"]),
                "feedback": "Opening Calculator.",
                "category": "Desktop",
            },
            "open explorer": {
                "action": lambda: subprocess.Popen(["explorer.exe"]),
                "feedback": "Opening File Explorer.",
                "category": "Desktop",
            },
            "take screenshot": {
                "action": self._take_screenshot,
                "feedback": "Screenshot captured.",
                "category": "Desktop",
            },
            "minimize window": {
                "action": self._minimize_active_window,
                "feedback": "Window minimized.",
                "category": "Desktop",
            },
            "maximize window": {
                "action": self._maximize_active_window,
                "feedback": "Window maximized.",
                "category": "Desktop",
            },
            "switch window": {
                "action": lambda: _send_key_combo(VK_MENU, VK_TAB),  # Alt + Tab
                "feedback": "Switching active window.",
                "category": "Desktop",
            },
            "open settings": {
                "action": lambda: subprocess.Popen(["cmd", "/c", "start", "ms-settings:"], shell=True),
                "feedback": "Opening Windows Settings.",
                "category": "Desktop",
            },
            "close application": {
                "action": lambda: _send_key_combo(VK_MENU, VK_F4),  # Alt + F4
                "feedback": "Closing active application.",
                "category": "Desktop",
            },

            # Assistive Commands (dispatched to Handsfree core)
            "open keyboard": {
                "action": lambda: self._dispatch_assistive("show_keyboard"),
                "feedback": "On-screen keyboard opened.",
                "category": "Keyboard",
            },
            "close keyboard": {
                "action": lambda: self._dispatch_assistive("hide_keyboard"),
                "feedback": "On-screen keyboard closed.",
                "category": "Keyboard",
            },
            "read screen": {
                "action": lambda: self._dispatch_assistive("read_screen"),
                "feedback": "Reading screen content.",
                "category": "Accessibility",
            },
            "read selected text": {
                "action": lambda: self._dispatch_assistive("read_selected"),
                "feedback": "Reading selected text.",
                "category": "Accessibility",
            },
            "pause tracking": {
                "action": lambda: self._dispatch_assistive("pause_tracking"),
                "feedback": "Tracking paused.",
                "category": "Accessibility",
            },
            "resume tracking": {
                "action": lambda: self._dispatch_assistive("resume_tracking"),
                "feedback": "Tracking resumed.",
                "category": "Accessibility",
            },
            "recenter cursor": {
                "action": lambda: self._dispatch_assistive("recenter_cursor"),
                "feedback": "Cursor recentered.",
                "category": "Accessibility",
            },
            "activate webcam fallback": {
                "action": lambda: self._dispatch_assistive("activate_fallback"),
                "feedback": "Webcam fallback tracking activated.",
                "category": "Accessibility",
            },
            "emergency stop": {
                "action": lambda: self._dispatch_assistive("emergency_stop"),
                "feedback": "Emergency stop activated. All inputs released.",
                "category": "System",
            },
            "status": {
                "action": lambda: self._dispatch_assistive("status"),
                "feedback": "Checking Unbound Handsfree status.",
                "category": "System",
            },
            "exit unbound": {
                "action": lambda: self._dispatch_assistive("exit_app"),
                "feedback": "Exiting Unbound Handsfree.",
                "category": "System",
            },
        }

    def _dispatch_assistive(self, action_name: str, payload: Any = None) -> None:
        if self.on_assistive_action:
            try:
                self.on_assistive_action(action_name, payload)
            except Exception as e:
                logger.error(f"Error in assistive action {action_name}: {e}")

    def _take_screenshot(self) -> None:
        """Saves a timestamped screenshot to user's Pictures or Unbound folder."""
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            save_dir = Path.home() / "Pictures" / "UnboundScreenshots"
            save_dir.mkdir(parents=True, exist_ok=True)
            filename = save_dir / f"screenshot_{int(time.time())}.png"
            img.save(filename)
            logger.info(f"Saved screenshot: {filename}")
        except Exception:
            _send_key_combo(VK_SNAPSHOT)

    def _minimize_active_window(self) -> None:
        hwnd = _get_foreground_hwnd()
        if hwnd and os.name == "nt":
            ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)

    def _maximize_active_window(self) -> None:
        hwnd = _get_foreground_hwnd()
        if hwnd and os.name == "nt":
            ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)

    def normalize_text(self, text: str) -> str:
        """Cleans and standardizes raw speech text for deterministic matching."""
        clean = text.lower().strip()
        # Remove punctuation except spaces
        clean = re.sub(r"[^\w\s]", "", clean)
        # Collapse whitespace
        clean = re.sub(r"\s+", " ", clean)
        return clean

    def route(self, raw_text: str) -> Tuple[bool, str, str]:
        """Routes spoken text to allowlisted actions with dedup protection and voice feedback.

        Returns:
            Tuple[bool (success), str (feedback_message), str (matched_command_key)]
        """
        now = time.time()
        norm = self.normalize_text(raw_text)
        if not norm:
            return False, "Empty command.", ""

        # Dedup check: ignore same command within cooldown window
        if norm == self.last_command and (now - self.last_exec_time) < self.cooldown_seconds:
            logger.debug(f"Suppressed duplicate command '{norm}' within {self.cooldown_seconds}s cooldown.")
            return False, "Duplicate suppressed.", norm

        # 1. Search query special pattern: "search for <query>" or "google <query>"
        search_match = re.match(r"^(?:search for|google|look up|search)\s+(.+)$", norm)
        if search_match:
            query = search_match.group(1).strip()
            if query:
                self.last_command = norm
                self.last_exec_time = now
                encoded = urllib.parse.quote(query)
                webbrowser.open(f"https://www.google.com/search?q={encoded}")
                msg = f"Searching for {query}."
                self.speak(msg)
                return True, msg, "search"

        # 2. Check for exact and fuzzy substring matches in registry
        # Prioritize longer command matches (e.g. "close this application" over "close tab")
        matched_key = None
        for key in sorted(self.registry.keys(), key=lambda k: -len(k)):
            # Check direct equality or substring variations
            # (e.g., "can you open google please" -> contains "open google")
            if key == norm or key in norm:
                matched_key = key
                break
            # Handle common natural variations
            # e.g., "open a new tab" -> "new tab"
            alias_map = {
                "open a new tab": "new tab",
                "open new tab": "new tab",
                "close this tab": "close tab",
                "switch to next tab": "next tab",
                "switch to previous tab": "previous tab",
                "refresh this page": "refresh",
                "refresh page": "refresh",
                "file explorer": "open explorer",
                "open file explorer": "open explorer",
                "minimize this window": "minimize window",
                "maximize this window": "maximize window",
                "close this application": "close application",
                "read this screen": "read screen",
                "exit unbound handsfree": "exit unbound",
                "quit unbound": "exit unbound",
                "recenter": "recenter cursor",
            }
            for alias, target in alias_map.items():
                if alias in norm and target in self.registry:
                    matched_key = target
                    break
            if matched_key:
                break

        if not matched_key:
            logger.info(f"Command not recognized: '{raw_text}' (normalized: '{norm}')")
            return False, f"Command not recognized: '{raw_text}'", ""

        # Execute registered action safely
        self.last_command = norm
        self.last_exec_time = now
        entry = self.registry[matched_key]
        feedback = entry.get("feedback", f"Executing {matched_key}.")

        try:
            logger.info(f"Executing allowlisted command: [{matched_key}] -> {feedback}")
            entry["action"]()
            self.speak(feedback)
            return True, feedback, matched_key
        except Exception as e:
            err_msg = f"Failed to execute command '{matched_key}': {e}"
            logger.error(err_msg)
            return False, err_msg, matched_key

    def get_command_catalog(self) -> Dict[str, list]:
        """Returns catalog of supported voice commands categorized for HUD UI."""
        catalog: Dict[str, list] = {}
        for cmd_name, info in self.registry.items():
            cat = info.get("category", "General")
            if cat not in catalog:
                catalog[cat] = []
            catalog[cat].append({
                "command": cmd_name.title(),
                "description": info.get("feedback", ""),
            })
        return catalog

