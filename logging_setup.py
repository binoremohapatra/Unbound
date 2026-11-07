"""logging_setup.py (Binore): Rotating file logging + crash hooks for OmniControl.

Logs to %APPDATA%\\OmniControl\\logs:
  - omnicontrol.log (rotating: 5 files x 1 MB)
  - crash.log (unhandled exceptions via sys.excepthook / threading.excepthook)

SAFETY CRITICAL:
  The crash hook MUST call emergency_release() (which calls gateway.release_all)
  BEFORE writing the crash log and exiting. No stuck keys on unhandled exceptions!
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading
import traceback
from typing import Callable, Optional

from config import logs_dir

log = logging.getLogger("omnicontrol.logging")

_emergency_release_cb: Optional[Callable[[str], None]] = None


def set_emergency_release_callback(cb: Callable[[str], None]) -> None:
    global _emergency_release_cb
    _emergency_release_cb = cb


def emergency_release(reason: str) -> None:
    global _emergency_release_cb
    if _emergency_release_cb:
        try:
            _emergency_release_cb(reason)
        except Exception:
            pass


def _write_crash_log(exc_type, exc_value, exc_tb, thread_name: str = "main") -> None:
    try:
        crash_path = logs_dir() / "crash.log"
        with open(crash_path, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"CRASH REPORT - Thread: {thread_name}\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
            f.write(f"{'='*60}\n")
    except Exception:
        pass


def install_crash_handler(on_release: Optional[Callable[[str], None]] = None) -> None:
    """Install sys.excepthook and threading.excepthook.

    SAFETY: Releases all keys BEFORE logging or terminating.
    """
    if on_release:
        set_emergency_release_callback(on_release)

    orig_sys_hook = sys.excepthook

    def _sys_excepthook(exc_type, exc_value, exc_tb):
        emergency_release("uncaught exception in main thread")
        _write_crash_log(exc_type, exc_value, exc_tb, thread_name="main")
        if orig_sys_hook:
            orig_sys_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _sys_excepthook

    if hasattr(threading, "excepthook"):
        orig_thread_hook = threading.excepthook

        def _thread_excepthook(args):
            emergency_release(f"uncaught exception in thread {args.thread.name if args.thread else 'unknown'}")
            _write_crash_log(args.exc_type, args.exc_value, args.exc_traceback,
                             thread_name=args.thread.name if args.thread else "thread")
            if orig_thread_hook:
                orig_thread_hook(args)

        threading.excepthook = _thread_excepthook


def setup_logging(level_name: str = "INFO", console: bool = True) -> logging.Logger:
    """Configures root logger with RotatingFileHandler (5 x 1MB) and console stream."""
    root = logging.getLogger()
    level = getattr(logging, level_name.upper(), logging.INFO)
    root.setLevel(level)

    # Avoid duplicate handlers on re-init
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )

    log_file = logs_dir() / "omnicontrol.log"
    try:
        file_handler = RotatingFileHandler(
            str(log_file), maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root.addHandler(file_handler)
    except Exception as exc:
        print(f"Warning: could not open log file {log_file}: {exc}", file=sys.stderr)

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        root.addHandler(console_handler)

    return root

