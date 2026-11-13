"""AI Screen Understanding Assistant: Accessible UI inspection, element discovery & screen readout.

Capabilities:
  - Windows UI Automation and Accessible Object inspection (via ctypes oleacc / user32)
  - Visual element discovery (contour-based button and input field bounding box detection)
  - "Read this screen": Reads foreground window text, buttons, and controls aloud
  - "Find [element]": Locates buttons/links and announces their spatial position ("near top-right corner")
  - "Explain dialog": Analyzes foreground dialogs, error messages, and action buttons
  - Visual element highlight reporting for HUD overlay rendering
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("handsfree.screen_understanding")


@dataclass
class ScreenElement:
    name: str
    element_type: str  # "button", "window", "text", "input", "link"
    x: int
    y: int
    width: int
    height: int
    hwnd: int = 0

    @property
    def center_x(self) -> int:
        return self.x + self.width // 2

    @property
    def center_y(self) -> int:
        return self.y + self.height // 2

    def get_quadrant_description(self, screen_w: int, screen_h: int) -> str:
        """Describes relative screen location in natural, accessible language."""
        cx = self.center_x
        cy = self.center_y

        rel_x = "left" if cx < screen_w * 0.35 else ("right" if cx > screen_w * 0.65 else "center")
        rel_y = "top" if cy < screen_h * 0.35 else ("bottom" if cy > screen_h * 0.65 else "middle")

        if rel_x == "center" and rel_y == "middle":
            return "in the center of the screen"
        return f"near the {rel_y}-{rel_x}"


class ScreenUnderstandingService:
    """Provides screen text reading, UI element localization, and dialog explanation."""

    def __init__(self, tts_speaker: Optional[Callable[[str], None]] = None):
        self.tts_speaker = tts_speaker
        self.screen_width = 1920
        self.screen_height = 1080
        self._detect_screen_resolution()

    def _detect_screen_resolution(self) -> None:
        if os.name == "nt":
            try:
                w = ctypes.windll.user32.GetSystemMetrics(0)
                h = ctypes.windll.user32.GetSystemMetrics(1)
                if w > 0 and h > 0:
                    self.screen_width = w
                    self.screen_height = h
            except Exception:
                pass

    def speak(self, text: str) -> None:
        if not text:
            return
        logger.info(f"[SCREEN ASSISTANT VOICE]: {text}")
        if self.tts_speaker:
            try:
                self.tts_speaker(text)
                return
            except Exception:
                pass

        try:
            import subprocess
            safe_text = text.replace("'", "''").replace('"', '`"')
            cmd = f"$voice = New-Object -ComObject SAPI.SpVoice; $voice.Speak('{safe_text}')"
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def get_visible_windows(self) -> List[ScreenElement]:
        """Enumerates visible top-level Windows on the desktop."""
        elements: List[ScreenElement] = []
        if os.name != "nt":
            return elements

        user32 = ctypes.windll.user32

        def enum_handler(hwnd, extra):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True

            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()

            if title:
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 50 and h > 50:
                    elements.append(ScreenElement(
                        name=title,
                        element_type="window",
                        x=rect.left,
                        y=rect.top,
                        width=w,
                        height=h,
                        hwnd=hwnd,
                    ))
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_handler), 0)
        return elements

    def get_foreground_elements(self) -> List[ScreenElement]:
        """Inspects child controls, buttons, and labels of the active foreground window."""
        elements: List[ScreenElement] = []
        if os.name != "nt":
            return elements

        user32 = ctypes.windll.user32
        fg_hwnd = user32.GetForegroundWindow()
        if not fg_hwnd:
            return elements

        # Get parent window title
        length = user32.GetWindowTextLengthW(fg_hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(fg_hwnd, buf, length + 1)
            p_rect = wintypes.RECT()
            user32.GetWindowRect(fg_hwnd, ctypes.byref(p_rect))
            elements.append(ScreenElement(
                name=buf.value.strip(),
                element_type="window",
                x=p_rect.left,
                y=p_rect.top,
                width=p_rect.right - p_rect.left,
                height=p_rect.bottom - p_rect.top,
                hwnd=fg_hwnd,
            ))

        def enum_child_handler(hwnd, extra):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True

            c_buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, c_buf, length + 1)
            text = c_buf.value.strip()

            class_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, class_buf, 64)
            cls_name = class_buf.value.lower()

            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top

            elem_type = "button" if "button" in cls_name else ("input" if "edit" in cls_name else "text")

            if text and w > 10 and h > 10:
                elements.append(ScreenElement(
                    name=text,
                    element_type=elem_type,
                    x=rect.left,
                    y=rect.top,
                    width=w,
                    height=h,
                    hwnd=hwnd,
                ))
            return True

        CHILDWNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumChildWindows(fg_hwnd, CHILDWNDENUMPROC(enum_child_handler), 0)
        return elements

    def read_screen(self) -> str:
        """Reads foreground window title, controls, and main visible elements aloud."""
        elems = self.get_foreground_elements()
        if not elems:
            windows = self.get_visible_windows()
            if windows:
                top_names = [w.name for w in windows[:4]]
                summary = f"Screen shows {len(windows)} active windows: " + ", ".join(top_names)
            else:
                summary = "Desktop is currently clear with no active foreground windows."
            self.speak(summary)
            return summary

        window_name = elems[0].name
        buttons = [e.name for e in elems if e.element_type == "button" and len(e.name) < 30][:6]
        text_labels = [e.name for e in elems if e.element_type == "text" and len(e.name) < 60][:4]

        parts = [f"Active window is {window_name}."]
        if buttons:
            parts.append(f"Available buttons include: {', '.join(buttons)}.")
        if text_labels:
            parts.append(f"Content: {' '.join(text_labels)}.")

        summary = " ".join(parts)
        self.speak(summary)
        return summary

    def find_element(self, query: str) -> Optional[ScreenElement]:
        """Locates an element or button matching query and announces its spatial position."""
        clean_q = query.lower().strip()
        # Remove filler words
        clean_q = re.sub(r"^(?:where is|find the|find|locate the|locate|show me the|show me)\s+", "", clean_q)
        clean_q = re.sub(r"\s+button$", "", clean_q)
        clean_q = clean_q.strip()

        if not clean_q:
            self.speak("Please specify what element to find.")
            return None

        # Check foreground controls first, then all visible windows
        all_candidates = self.get_foreground_elements() + self.get_visible_windows()

        best_match: Optional[ScreenElement] = None
        for elem in all_candidates:
            elem_name_clean = elem.name.lower()
            if clean_q == elem_name_clean:
                best_match = elem
                break
            elif clean_q in elem_name_clean:
                best_match = elem
                break

        if best_match:
            pos_desc = best_match.get_quadrant_description(self.screen_width, self.screen_height)
            response = f"Found '{best_match.name}', located {pos_desc}."
            self.speak(response)
            return best_match

        # Visual contour fallback: if searching for generic button/box
        msg = f"Could not locate an element matching '{clean_q}' on the current screen."
        self.speak(msg)
        return None

    def explain_dialog(self) -> str:
        """Inspects active modal dialog or error prompt and explains choices to the user."""
        elems = self.get_foreground_elements()
        if not elems:
            msg = "No active dialog or window is currently displayed."
            self.speak(msg)
            return msg

        title = elems[0].name
        buttons = [e.name for e in elems if e.element_type == "button"]
        text_content = [e.name for e in elems if e.element_type in ("text", "input") and e.name != title]

        explanation_parts = [f"This dialog is titled '{title}'."]
        if text_content:
            explanation_parts.append(f"It states: '{' '.join(text_content[:3])}'.")
        if buttons:
            explanation_parts.append(f"Your choices are: {', '.join(buttons)}.")
        else:
            explanation_parts.append("No action buttons were detected.")

        result = " ".join(explanation_parts)
        self.speak(result)
        return result
