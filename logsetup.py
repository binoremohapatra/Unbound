"""logsetup.py (Binore): rotating logs, a flight-recorder ring, and crash reports.

Crash rule: when anything dies with an unhandled exception, the FIRST thing that happens is
release_all (so a game is never left with a key stuck down); only then do we write the report.
Writing a file can be slow or fail (disk full, antivirus lock), and keys must not wait for it.

Covers: main thread (sys.excepthook), worker threads (threading.excepthook) and, via faulthandler,
a native crash (segfault inside a camera/audio library). A native crash cannot run Python, so it
only leaves a traceback in fatal.log; keys cannot be released from there.
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import platform
import sys
import tempfile
import threading
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from version import __version__

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_TAG = "_omnicontrol_handler"
KEEP_CRASH_REPORTS = 20


class RingHandler(logging.Handler):
    """Remembers the last N formatted log lines in memory, so a crash report can show what led up
    to it even if the log file is huge, rotated or unwritable."""

    def __init__(self, size: int = 200) -> None:
        super().__init__()
        self._lines: deque = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.append(self.format(record))
        except Exception:
            self.handleError(record)

    def dump(self) -> list[str]:
        self.acquire()
        try:
            return list(self._lines)
        finally:
            self.release()


def _level(name: str) -> int:
    return getattr(logging, str(name).upper(), logging.INFO)


def setup_logging(directory: Optional[Path], level: str = "INFO", console: bool = True,
                  max_bytes: int = 1_000_000, backups: int = 5) -> RingHandler:
    """Idempotent: calling it again replaces our handlers instead of stacking duplicates.
    Never raises: if the log folder is not writable we log to the console only."""
    teardown_logging()
    root = logging.getLogger()
    root.setLevel(_level(level))
    fmt = logging.Formatter(FORMAT)
    ring = RingHandler()
    handlers: list[logging.Handler] = [ring]
    if directory is not None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(
                directory / "omnicontrol.log", maxBytes=max_bytes, backupCount=backups,
                encoding="utf-8"))
        except OSError as e:
            if sys.stderr is not None:
                print(f"omnicontrol: cannot write logs to {directory} ({e})", file=sys.stderr)
    if console and sys.stderr is not None:        # a windowed exe has no stderr at all
        handlers.append(logging.StreamHandler(sys.stderr))
    for h in handlers:
        h.setFormatter(fmt)
        setattr(h, _TAG, True)
        root.addHandler(h)
    return ring


def teardown_logging() -> None:
    """Remove and close our handlers (also releases the log file lock on Windows)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _TAG, False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


def set_level(level: str) -> None:
    logging.getLogger().setLevel(_level(level))


# ------------------------------------------------------------------ crash reports
def _prune(directory: Path) -> None:
    reports = sorted(directory.glob("crash-*.txt"), key=lambda p: p.name)
    for old in reports[:-KEEP_CRASH_REPORTS]:
        try:
            old.unlink()
        except OSError:
            pass


def write_crash_report(directory: Path, exc_type, exc, tb, where: str,
                       ring: Optional[RingHandler] = None) -> Optional[Path]:
    """Returns the report path, or None if even the temp-dir fallback failed."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    body = "\n".join([
        f"OmniControl {__version__} crash report",
        f"time:     {stamp}",
        f"where:    {where}",
        f"python:   {sys.version.split()[0]} ({platform.platform()})",
        f"argv:     {sys.argv}",
        "",
        "".join(traceback.format_exception(exc_type, exc, tb)).rstrip(),
        "",
        "--- last log lines ---",
        *(ring.dump() if ring else ["(no log ring)"]),
        "",
    ])
    for folder in (directory, Path(tempfile.gettempdir())):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"crash-{stamp}.txt"
            path.write_text(body, encoding="utf-8")
            _prune(folder)
            return path
        except OSError:
            continue
    return None


def install_crash_handlers(release: Callable[[str], None], crash_dir: Path,
                           ring: Optional[RingHandler] = None,
                           use_faulthandler: bool = True) -> Callable[[], None]:
    """Hook sys.excepthook and threading.excepthook. Returns an uninstall function."""
    prev_sys, prev_thread = sys.excepthook, threading.excepthook
    log = logging.getLogger("omnicontrol.crash")
    fault_file = None

    def _crash(exc_type, exc, tb, where: str) -> None:
        try:
            release(f"crash in {where}")                  # 1. keys first, always
        except Exception:
            log.exception("release during crash failed")
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            return                                        # a normal way to stop, not a crash
        path = write_crash_report(crash_dir, exc_type, exc, tb, where, ring)   # 2. then the paperwork
        log.critical("unhandled %s in %s; report: %s", exc_type.__name__, where, path)

    def sys_hook(exc_type, exc, tb) -> None:
        _crash(exc_type, exc, tb, "main thread")
        prev_sys(exc_type, exc, tb)

    def thread_hook(args) -> None:
        if args.exc_type is not SystemExit:               # Python ignores SystemExit in threads
            name = args.thread.name if args.thread else "unknown"
            _crash(args.exc_type, args.exc_value, args.exc_traceback, f"thread {name}")
        prev_thread(args)

    sys.excepthook, threading.excepthook = sys_hook, thread_hook
    if use_faulthandler:
        try:
            crash_dir.mkdir(parents=True, exist_ok=True)
            fault_file = open(crash_dir / "fatal.log", "a", encoding="utf-8")
            fault_file.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} "
                             f"OmniControl {__version__} started ===\n")
            fault_file.flush()
            faulthandler.enable(file=fault_file, all_threads=True)   # needs a real file: no stderr in a windowed exe
        except (OSError, RuntimeError):
            log.exception("faulthandler not enabled")
            fault_file = None

    def uninstall() -> None:
        sys.excepthook, threading.excepthook = prev_sys, prev_thread
        if fault_file is not None:
            faulthandler.disable()
            fault_file.close()

    return uninstall
