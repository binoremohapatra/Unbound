"""Fuzz / property tests for the safety pipeline (Binore).

>= 200 seeds x 600 ticks of random events through the REAL Engine + StateMachine + Gateway
(RecordingBackend). After EVERY tick the invariants are checked:

  I1  SAFE_IDLE or CONVERSE_LOCK -> nothing held in gateway/backend
  I2  no hold exceeds max_hold_ms (persistent toggles exempt)
  I3  no double key-down / mouse-down
  I4  after a safety cancel (face loss, freeze, kill, cancel events, context change,
      release_all) nothing is held that same tick
  I5  hard_blink / pop / voice_cancel / voice_stop while LATCHED releases that tick
  I6  face loss releases on the first absent tick; ACTIVE needs 300 ms continuous face
  I7  after shutdown release_all nothing is held

Also exercised: loop stalls (ticks skipped = time passing without the loop running),
producer exceptions, context (Alt+Tab) changes, and unknown events.
"""
import random

import pytest

from core.events import SignalEvent
from gateway import Gateway, RecordingBackend
from main import BasicMapper, DefaultProducer, Engine
from state_machine import SMConfig, State, StateMachine

SEEDS = 220
TICKS = 600
DT = 1 / 60
MAX_HOLD_S = SMConfig().max_hold_ms / 1000.0
FACE_REGAIN_S = SMConfig().face_regain_ms / 1000.0

# events that always force a release when they fire (value >= 0.5)
ALWAYS_CANCEL = {"voice_freeze", "kill"}
LATCH_CANCEL = {"pop", "hard_blink", "voice_cancel", "voice_stop"}


def _event(rng, t):
    r = rng.random()
    if r < 0.10:
        return SignalEvent("webcam", "face", 1.0 if rng.random() < 0.7 else 0.0, t)
    if r < 0.25:
        return SignalEvent("webcam", "jaw_open", 0.9 if rng.random() < 0.6 else 0.0, t)
    if r < 0.40:
        return SignalEvent("webcam", rng.choice(["head_yaw", "head_pitch", "head_roll"]),
                           round(rng.uniform(-30, 30), 2), t)
    if r < 0.45:
        return SignalEvent("webcam", "brow_raise", 0.9 if rng.random() < 0.5 else 0.0, t)
    if r < 0.49:
        return SignalEvent("webcam", "hard_blink", 1.0, t)
    if r < 0.53:
        return SignalEvent("mic", "pop", 1.0, t)
    if r < 0.56:
        return SignalEvent("mic", "voice_freeze", 1.0, t)
    if r < 0.59:
        return SignalEvent("mic", "voice_resume", 1.0, t)
    if r < 0.61:
        return SignalEvent("mic", "voice_cancel", 1.0, t)
    if r < 0.63:
        return SignalEvent("mic", "voice_stop", 1.0, t)
    if r < 0.65:
        return SignalEvent("switch", "kill", 1.0, t)
    if r < 0.68:
        return SignalEvent("mic", "speech_active", 1.0 if rng.random() < 0.5 else 0.0, t)
    if r < 0.71:
        return SignalEvent("switch", "escape", 1.0 if rng.random() < 0.5 else 0.0, t)
    if r < 0.75:
        return SignalEvent("webcam", "mystery_signal", 0.7, t)      # unknown: must be ignored
    return None


class FlakyProducer:
    """Wraps DefaultProducer and randomly raises, like a buggy teammate module."""

    def __init__(self, seed):
        self.inner = DefaultProducer()
        self._rng = random.Random(seed ^ 0x5EED)

    def process(self, events, now):
        if self._rng.random() < 0.02:
            raise RuntimeError("flaky producer boom")
        return self.inner.process(events, now)


class HeldTracker:
    """Replays the backend log incrementally; flags any down-event for an already-held key."""

    def __init__(self, backend):
        self.backend = backend
        self.pos = 0
        self.down: set = set()
        self.violations: list = []

    def feed(self):
        for rec in self.backend.log[self.pos:]:
            kind, name, val = rec[0], rec[1], rec[2]
            if kind == "key":
                if val and name in self.down:
                    self.violations.append(("double key-down", name))
                (self.down.add if val else self.down.discard)(name)
            elif kind == "mouse":
                m = f"mouse_{name}"
                if val and m in self.down:
                    self.violations.append(("double mouse-down", name))
                (self.down.add if val else self.down.discard)(m)
        self.pos = len(self.backend.log)


CONTEXTS = ["supertuxkart.exe", "chrome.exe", "notepad.exe"]


def _run_seed(seed):
    rng = random.Random(seed)
    backend = RecordingBackend()
    gw = Gateway(backend)
    sm = StateMachine(SMConfig())
    eng = Engine(sm, BasicMapper(), gw, [FlakyProducer(seed), DefaultProducer()])
    tracker = HeldTracker(backend)

    face_lost_at = None
    face_back_at = None
    prev_ctx = None
    context = CONTEXTS[0]
    history: list = []
    t = 1000.0 + seed * 0.001

    for tick in range(TICKS):
        now = t + tick * DT
        events = [e for e in (_event(rng, now) for _ in range(rng.choice([0, 0, 1, 1, 2, 3]))) if e]
        # occasional loop stall: time jumps, no step happens (watchdog covers the real loop)
        if rng.random() < 0.01:
            continue
        ctx = context
        if rng.random() < 0.02:
            ctx = rng.choice(CONTEXTS)
            context = ctx
        ctx_changed = prev_ctx is not None and ctx != prev_ctx

        pre_state = sm.state
        pre_held = bool(gw.held)
        cancel_names = {e.name for e in events if e.value >= 0.5}

        eng.step(events, now, context=ctx)
        tracker.feed()
        history.append((round(now, 3), [(e.name, round(e.value, 2)) for e in events], ctx))
        history[:] = history[-40:]

        # ---- I1: SAFE_IDLE / CONVERSE_LOCK -> nothing held anywhere
        if sm.state in (State.SAFE_IDLE, State.CONVERSE_LOCK):
            assert not gw.held and not backend.keys and not backend.mouse, \
                f"I1 violated: state={sm.state} held={gw.held} keys={backend.keys} mouse={backend.mouse}"

        # ---- I2: no hold outlives max_hold_ms (persistent toggles exempt)
        for name, exp in sm._latches.items():
            if exp is not None:
                assert exp - now <= MAX_HOLD_S + 1e-6, \
                    f"I2 violated: latch {name} has {(exp - now) * 1000:.0f} ms left > {MAX_HOLD_S * 1000:.0f}"

        # ---- I3: no double downs
        assert not tracker.violations, f"I3 violated: {tracker.violations[-3:]}"

        # ---- I4/I5: a safety cancel that had something to release leaves nothing held.
        # (A cancel arriving while already empty / already locked is a no-op; fresh input in
        # the same tick may legitimately hold afterwards.)
        face0 = any(e.name == "face" and e.value < 0.5 for e in events)
        hard_cancel = pre_held and (bool(cancel_names & ALWAYS_CANCEL) or face0 or ctx_changed)
        latch_cancel = pre_state == State.LATCHED and bool(cancel_names & LATCH_CANCEL)
        if hard_cancel or latch_cancel:
            assert not gw.held, \
                f"I4/I5 violated: held={gw.held} after cancel (state was {pre_state}, events={history[-1][1]})"

        # ---- I6: face loss / regain rules
        for e in events:
            if e.name == "face":
                if e.value < 0.5:
                    face_lost_at, face_back_at = now, None
                elif face_lost_at is not None and face_back_at is None:
                    face_back_at = now
        if face_lost_at is not None and (face_back_at is None or face_back_at < face_lost_at):
            assert sm.state == State.SAFE_IDLE, \
                f"I6 violated: face absent but state={sm.state}"
        if (face_back_at is not None and face_lost_at is not None
                and face_back_at > face_lost_at and now - face_back_at < FACE_REGAIN_S - 0.02):
            assert sm.state != State.ACTIVE, \
                f"I6 violated: ACTIVE after only {(now - face_back_at) * 1000:.0f} ms of face"

        prev_ctx = ctx

    # ---- I7: shutdown release leaves nothing held
    gw.release_all("fuzz shutdown")
    assert not gw.held and not backend.keys and not backend.mouse
    return True


@pytest.mark.parametrize("seed", range(SEEDS))
def test_fuzz_safety_invariants(seed):
    try:
        _run_seed(seed)
    except AssertionError as e:
        pytest.fail(f"seed={seed}: {e}")
