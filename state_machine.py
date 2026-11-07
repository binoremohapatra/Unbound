"""state_machine.py (Binore): decides whether an intent is ALLOWED right now.

Dishti's mapping decides what an intent *does* (which key). Antas's assist decides when a timed
hold starts and for how long. This module only enforces permission, expiry and cancel rules.

State is DERIVED from three facts, so it can never contradict itself:

    face_ok  locked  latches   ->  state
    False      *        *          SAFE_IDLE      (face lost; nothing is allowed)
    True     True       *          CONVERSE_LOCK  (user said "freeze" / F12; nothing is allowed)
    True     False   non-empty     LATCHED        (a timed hold is active)
    True     False     empty       ACTIVE

Priority when several things are true: SAFE_IDLE > CONVERSE_LOCK > LATCHED > ACTIVE.
Cancel / release intents are ALWAYS allowed, in every state, so a latch can never be trapped.

Event names this module listens to (agree these with Antas and Dishti):
    face          value 1.0 / 0.0   (webcam)  face present / lost
    hard_blink    value >= 0.5      (webcam)  deliberate blink
    pop           value >= 0.5      (mic)     tongue click
    voice_cancel, voice_stop, voice_freeze, voice_resume   (mic)
    kill          value >= 0.5      (switch)  F12 style kill switch, same as freeze
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Optional

from core.events import Intent, SignalEvent

log = logging.getLogger("omnicontrol.sm")


class State(str, Enum):
    ACTIVE = "ACTIVE"
    LATCHED = "LATCHED"
    CONVERSE_LOCK = "CONVERSE_LOCK"
    SAFE_IDLE = "SAFE_IDLE"


@dataclass
class SMConfig:
    default_hold_ms: int = 1800        # used when a hold intent has ms == 0
    max_hold_ms: int = 10000           # hard cap: no hold can ever outlive this
    face_regain_ms: int = 300          # face must be present this long before leaving SAFE_IDLE
    # Actions that behave like a toggle (cruise control). Tracked with no expiry, still
    # cancelled by pop / blink / "stop" / "cancel" / face loss / freeze.
    persistent_actions: tuple = ("cruise",)
    cancel_events: tuple = ("pop", "hard_blink", "voice_cancel", "voice_stop")


@dataclass(frozen=True)
class Snapshot:
    """Read-only view for Shaury's dashboard. Never written back."""
    state: State
    reason: str
    face_ok: bool
    locked: bool
    latched: tuple                     # names of active holds
    remaining_ms: tuple                # (name, ms left or None for no expiry)
    t: float


class StateMachine:
    def __init__(self, cfg: Optional[SMConfig] = None):
        self.cfg = cfg or SMConfig()
        self.face_ok = True
        self.locked = False
        self._latches: dict[str, Optional[float]] = {}   # name -> expiry time, None = persistent
        self._regain_since: Optional[float] = None
        self._context: Optional[str] = None
        self._state = State.ACTIVE
        self._reason = "boot"
        self._listeners: list[Callable[[Snapshot], None]] = []
        self.dropped = 0                                 # intents blocked by the gate (telemetry)

    # ---------------------------------------------------------------- public API
    @property
    def state(self) -> State:
        if not self.face_ok:
            return State.SAFE_IDLE
        if self.locked:
            return State.CONVERSE_LOCK
        if self._latches:
            return State.LATCHED
        return State.ACTIVE

    def subscribe(self, cb: Callable[[Snapshot], None]) -> None:
        """Dashboard registers here; called on every state change. Callbacks must not raise."""
        self._listeners.append(cb)

    def snapshot(self, now: float = 0.0) -> Snapshot:
        rem = tuple((n, None if e is None else max(0, int((e - now) * 1000)))
                    for n, e in self._latches.items())
        return Snapshot(self.state, self._reason, self.face_ok, self.locked,
                        tuple(self._latches), rem, now)

    def on_event(self, ev: SignalEvent, now: Optional[float] = None) -> tuple[list[Intent], bool]:
        """Safety signals. Returns (intents to send straight to mapping, consumed).

        consumed=True means the event was a safety signal and must NOT also reach the
        normal intent producers (a pop that cancels a latch must not also fire an action)."""
        now = ev.t if now is None else now
        name, on = ev.name, ev.value >= 0.5
        out: list[Intent] = []
        consumed = False

        if name == "face":
            consumed = True
            out = self._face(on, now)
        elif name in ("voice_freeze", "kill"):
            if on:
                consumed = True
                out = self._lock(name, now)
        elif name == "voice_resume":
            consumed = True
            out = self._unlock(now)
        elif name in self.cfg.cancel_events and on:
            if self._latches:                       # only a safety event while something is held
                consumed = True
                out = self._cancel_latches(name)
            elif name in ("voice_cancel", "voice_stop") and self.locked:
                consumed = True                     # words that mean nothing while locked
        self._refresh(now, name if (out or consumed) else None)
        return out, consumed

    def gate(self, intents: list[Intent], now: float) -> list[Intent]:
        """Permission filter for intents coming from the normal producers."""
        allowed_state = self.state in (State.ACTIVE, State.LATCHED)
        out: list[Intent] = []
        for it in intents:
            k = it.kind
            if k == "cancel_all":
                self._latches.clear()
                out.append(it)
            elif k == "release":
                if it.name:
                    self._latches.pop(it.name, None)
                else:
                    self._latches.clear()
                out.append(it)
            elif k == "pause":
                out.extend(self._lock("pause_intent", now))
            elif k == "resume":
                out.extend(self._unlock(now))
            elif not allowed_state:
                self.dropped += 1
            elif k == "hold":
                ms = it.ms if it.ms > 0 else self.cfg.default_hold_ms
                ms = min(ms, self.cfg.max_hold_ms)
                if it.name in self.cfg.persistent_actions and it.ms <= 0:
                    self._latches[it.name] = None
                else:
                    self._latches[it.name] = now + ms / 1000.0
                self._reason = f"hold:{it.name}"
                out.append(replace(it, ms=ms))
            elif k == "action":
                if it.name in self.cfg.persistent_actions:     # toggle bookkeeping
                    if it.name in self._latches:
                        del self._latches[it.name]
                    else:
                        self._latches[it.name] = None
                out.append(it)
            elif k == "layer":
                self._latches.clear()          # mapping releases held keys on a layer switch
                out.append(it)
            else:                              # stick
                out.append(it)
        self._refresh(now, None)
        return out

    def tick(self, now: float) -> list[Intent]:
        """Call every loop iteration: enforces hold expiry and the face-regain dwell."""
        out: list[Intent] = []
        for name, exp in list(self._latches.items()):
            if exp is not None and now >= exp:
                del self._latches[name]
                out.append(Intent("release", name))
        if (not self.face_ok and self._regain_since is not None
                and (now - self._regain_since) * 1000 >= self.cfg.face_regain_ms):
            self.face_ok = True
            self._regain_since = None
            self._reason = "face_back"
        self._refresh(now, "expiry" if out else None)
        return out

    def on_context_change(self, key: str, now: float) -> list[Intent]:
        """Foreground app changed (e.g. Alt+Tab out of the game): release everything."""
        prev, self._context = self._context, key
        if prev is None or prev == key:
            return []
        self._latches.clear()
        self._refresh(now, "context_change")
        return [Intent("cancel_all")]

    # ---------------------------------------------------------------- internals
    def _face(self, present: bool, now: float) -> list[Intent]:
        if present:
            if not self.face_ok and self._regain_since is None:
                self._regain_since = now
            return []
        self._regain_since = None
        if self.face_ok:
            self.face_ok = False
            self._latches.clear()
            return [Intent("cancel_all")]      # released on the FIRST absent frame
        return []

    def _lock(self, why: str, now: float) -> list[Intent]:
        if self.locked:
            return []
        self.locked = True
        self._latches.clear()                  # latch is cancelled first, never trapped
        self._reason = why
        return [Intent("cancel_all"), Intent("pause")]

    def _unlock(self, now: float) -> list[Intent]:
        if not self.locked:
            return []
        self.locked = False
        self._reason = "resume"
        return [Intent("resume")]

    def _cancel_latches(self, why: str) -> list[Intent]:
        out = [Intent("release", n) for n in self._latches]
        self._latches.clear()
        self._reason = why
        return out

    def _refresh(self, now: float, reason: Optional[str]) -> None:
        if reason:
            self._reason = reason
        new = self.state
        if new != self._state:
            log.info("state %s -> %s (%s)", self._state.value, new.value, self._reason)
            self._state = new
            snap = self.snapshot(now)
            for cb in self._listeners:
                try:
                    cb(snap)
                except Exception:              # the dashboard must never break the brain
                    log.exception("state listener failed")
