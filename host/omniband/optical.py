"""optical.py - optical fallback gesture source (P9).

Wraps the EXISTING unbound webcam pipeline (handsfree.adapters.webcam
WebcamAdapter: MediaPipe face mesh -> pose + jawOpen blendshape) and decodes a
small gesture vocabulary from jawOpen, then feeds the SAME engine gesture path
as hardware gestures. No new vision stack is introduced.

Gesture mapping (optical, conservative):
  jawOpen quick open->close  -> SINGLE   (mouth "click")
  jawOpen held >= long_hold  -> LONG     (sustained open)
  jawOpen double pulse       -> DOUBLE
Head pose is exposed for future use but does not generate gestures here.

Honest limits: no blink channel in the current adapter, no clench EN/EMG
equivalent - optical fallback is a reduced vocabulary. It is for accessibility
fallback, not esports parity. Requires opencv-python + mediapipe; without them
the adapter runs its built-in mock loop and this source reports UNAVAILABLE.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("omniband.optical")

try:
    from handsfree.adapters.webcam import WebcamAdapter, HAS_CV2, HAS_MEDIAPIPE
except Exception:  # noqa: BLE001 - handsfree package optional in some installs
    WebcamAdapter = None
    HAS_CV2 = HAS_MEDIAPIPE = False


class OpticalSource:
    """jawOpen -> gesture decoder. Emits via on_gesture(name, amp, dur_ms)."""

    def __init__(self, camera_id: int = 0,
                 on_gesture: Optional[Callable[[str, float, int], None]] = None,
                 long_hold_ms: int = 3000,
                 tap_max_ms: int = 450,
                 press_min_ms: int = 30,
                 multi_gap_ms: int = 320,
                 jaw_open_thr: float = 0.35,
                 jaw_close_thr: float = 0.18) -> None:
        self.on_gesture = on_gesture or (lambda g, a, d: None)
        self.long_hold_ms = long_hold_ms
        self.tap_max_ms = tap_max_ms
        self.press_min_ms = press_min_ms
        self.multi_gap_ms = multi_gap_ms
        self.jaw_open_thr = jaw_open_thr
        self.jaw_close_thr = jaw_close_thr
        self._adapter: Optional[WebcamAdapter] = None
        self._jaw_open = False
        self._press_t = 0.0
        self._press_peak = 0.0
        self._taps: list[float] = []
        self._pending_single: Optional[threading.Timer] = None
        self._pending_info: tuple[float, int] = (0.0, 0)
        self._lock = threading.Lock()
        self._running = False
        self.frames = 0
        self.last_event_t = 0.0

    # ------------------------------------------------------------- lifecycle
    @property
    def available(self) -> bool:
        return WebcamAdapter is not None and HAS_CV2 and HAS_MEDIAPIPE

    def start(self, camera_id: int = 0) -> bool:
        if not self.available:
            log.warning("optical source UNAVAILABLE: opencv/mediapipe missing")
            return False
        self._adapter = WebcamAdapter(
            camera_id=camera_id,
            on_event=self._on_signal,
            on_heartbeat=self._on_heartbeat,
        )
        self._adapter.start()
        self._running = True
        log.info("optical source started (camera_id=%d)", camera_id)
        return True

    def stop(self) -> None:
        self._running = False
        if self._adapter is not None:
            self._adapter.stop()
            self._adapter = None

    def _on_heartbeat(self) -> None:
        self.last_event_t = time.monotonic()

    # ------------------------------------------------------------- decode
    def _on_signal(self, ev) -> None:
        """SignalEvent from WebcamAdapter. We only consume jawOpen."""
        try:
            name = getattr(ev, "name", "")
            if name != "blendshapes":
                return
            val = ev.value if isinstance(ev.value, dict) else {}
            jaw = float(val.get("jawOpen", 0.0))
        except Exception:  # noqa: BLE001
            return
        self.frames += 1
        now = time.monotonic()
        with self._lock:
            self._decode_jaw(jaw, now)

    def _decode_jaw(self, jaw: float, now: float) -> None:
        if not self._jaw_open and jaw >= self.jaw_open_thr:
            self._jaw_open = True
            self._press_t = now
            self._press_peak = jaw
        elif self._jaw_open:
            if jaw > self._press_peak:
                self._press_peak = jaw
            if jaw <= self.jaw_close_thr:
                self._jaw_open = False
                self._release(now)
            elif (now - self._press_t) * 1000.0 >= self.long_hold_ms:
                # sustained open -> LONG once (re-arm on next press)
                self._jaw_open = False
                self.on_gesture("LONG", self._press_peak, self.long_hold_ms)

    def _release(self, now: float) -> None:
        dur_ms = int((now - self._press_t) * 1000.0)
        if dur_ms < self.press_min_ms or dur_ms > self.tap_max_ms:
            return
        peak = self._press_peak
        # second tap inside the multi-gap window -> DOUBLE (cancel pending SINGLE)
        recent = [t for t in self._taps
                  if (now - t) * 1000.0 <= self.multi_gap_ms]
        self._taps = recent + [now]
        self._taps = self._taps[-4:]
        if len(recent) >= 1:
            if self._pending_single is not None:
                self._pending_single.cancel()
                self._pending_single = None
            self.on_gesture("DOUBLE", peak, dur_ms)
            self._taps = []
            return
        # first tap: resolve as SINGLE only if no follow-up arrives in the gap
        info = (peak, dur_ms)
        self._pending_info = info
        timer = threading.Timer(self.multi_gap_ms / 1000.0,
                                self._resolve_single)
        timer.daemon = True
        self._pending_single = timer
        timer.start()

    def _resolve_single(self) -> None:
        with self._lock:
            self._pending_single = None
            peak, dur_ms = self._pending_info
            self.on_gesture("SINGLE", peak, dur_ms)
