"""auto_profile.py - Background 4 Hz Win32 Foreground Process Watcher.

Hooks into active games and applications:
- Win32 GetForegroundWindow -> GetWindowThreadProcessId -> OpenProcess -> QueryFullProcessImageNameW
- Evaluates rules from host/profiles/auto_rules.json
- Automatically switches profiles, reveals/hides HUD overlay, and calls release_all() on focus loss
- Supports manual mode lock to prevent unexpected switches in ICU or pinned modes
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger("omniband.autoprofile")

RULES_FILE = Path(__file__).resolve().parent.parent.parent / "profiles" / "auto_rules.json"


@dataclass(frozen=True)
class ProcessInfo:
    exe_name: str
    window_title: str
    pid: int


@dataclass
class ProfileRule:
    name: str
    executables: List[str]
    title_keywords: List[str]
    mode: int
    profile: str
    show_hud: bool
    badge: str
    tts_announcement: Optional[str]


def load_rules(path: Path | None = None) -> List[ProfileRule]:
    p = path or RULES_FILE
    if not p.exists():
        log.warning("rules file %s does not exist", p)
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        rules = []
        for r in data.get("rules", []):
            rules.append(ProfileRule(
                name=r.get("name", "Unknown"),
                executables=[x.lower() for x in r.get("executables", [])],
                title_keywords=[x.lower() for x in r.get("title_keywords", [])],
                mode=int(r.get("mode", 0)),
                profile=r.get("profile", ""),
                show_hud=bool(r.get("show_hud", False)),
                badge=r.get("badge", ""),
                tts_announcement=r.get("tts_announcement"),
            ))
        return rules
    except Exception as e:
        log.error("failed reading auto_rules.json: %s", e)
        return []


def match_process_rule(proc: ProcessInfo, rules: List[ProfileRule]) -> Optional[ProfileRule]:
    """Pure matcher: given ProcessInfo and rules list, return matched rule or None."""
    exe = proc.exe_name.lower()
    title = proc.window_title.lower()
    for rule in rules:
        if exe in rule.executables:
            return rule
        for kw in rule.title_keywords:
            if kw and kw in title:
                return rule
    return None


def get_active_process_win32() -> ProcessInfo:
    """Query currently foregrounded window on Windows via Win32 ctypes."""
    if sys.platform != "win32":
        return ProcessInfo(exe_name="", window_title="", pid=0)
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ProcessInfo(exe_name="", window_title="", pid=0)

        # Window Title
        length = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        title = title_buf.value

        # PID
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return ProcessInfo(exe_name="", window_title=title, pid=0)

        # Process Executable Path
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h_proc:
            return ProcessInfo(exe_name="", window_title=title, pid=pid.value)

        exe_buf = ctypes.create_unicode_buffer(1024)
        buf_size = wintypes.DWORD(1024)
        success = kernel32.QueryFullProcessImageNameW(h_proc, 0, exe_buf, ctypes.byref(buf_size))
        kernel32.CloseHandle(h_proc)

        exe_name = ""
        if success:
            exe_name = Path(exe_buf.value).name

        return ProcessInfo(exe_name=exe_name, window_title=title, pid=pid.value)
    except Exception as e:
        log.debug("error querying win32 foreground window: %s", e)
        return ProcessInfo(exe_name="", window_title="", pid=0)


class AutoProfileWatcher:
    """Monitors foreground window at 4 Hz and triggers profile transitions."""

    def __init__(
        self,
        on_rule_applied: Optional[Callable[[ProfileRule], None]] = None,
        on_release_all: Optional[Callable[[], None]] = None,
        rules: Optional[List[ProfileRule]] = None,
        poll_fn: Optional[Callable[[], ProcessInfo]] = None,
    ) -> None:
        self.rules = rules if rules is not None else load_rules()
        self.on_rule_applied = on_rule_applied
        self.on_release_all = on_release_all
        self._poll_fn = poll_fn or get_active_process_win32

        self.enabled = True
        self.manual_lock = False  # If True, manual mode selection is protected
        self.locked_mode: Optional[int] = None

        self._active_rule: Optional[ProfileRule] = None
        self._last_exe: str = ""
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def lock_mode(self, mode: int) -> None:
        self.manual_lock = True
        self.locked_mode = mode
        log.info("auto profile watcher locked to mode %d", mode)

    def unlock_mode(self) -> None:
        self.manual_lock = False
        self.locked_mode = None
        log.info("auto profile watcher unlocked")

    def check_once(self) -> Optional[ProfileRule]:
        """Perform a single foreground check cycle."""
        if not self.enabled or self.manual_lock:
            return None

        proc = self._poll_fn()
        if not proc.exe_name and not proc.window_title:
            return None

        if proc.exe_name == self._last_exe:
            return None

        old_exe = self._last_exe
        self._last_exe = proc.exe_name
        matched = match_process_rule(proc, self.rules)

        # Focus switch away from an active game -> always release held keys!
        if self._active_rule and (not matched or matched.name != self._active_rule.name):
            log.info("Focus switched from '%s' to '%s'; releasing keys", old_exe, proc.exe_name)
            if self.on_release_all:
                self.on_release_all()

        if matched and matched != self._active_rule:
            self._active_rule = matched
            log.info("Auto-hook applied: %s (Mode %d, HUD: %s)", matched.name, matched.mode, matched.show_hud)
            if self.on_rule_applied:
                self.on_rule_applied(matched)
            return matched

        return None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="auto-profile-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        while self._running:
            try:
                self.check_once()
            except Exception as e:
                log.debug("error in auto-profile loop: %s", e)
            time.sleep(0.25)  # 4 Hz polling rate

