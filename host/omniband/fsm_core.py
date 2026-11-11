"""fsm_core.py - Python port of firmware GestureFsm (omniband_core.h).

Must stay semantically identical to the C++ core: both consume the same
channel-edge stream and must produce the same gestures for
tests/vectors/gesture_vectors.json. Times are int milliseconds.

This is used by the Simulator transport (host-side device model) and by the
shared-vector pytest. It is NOT the safety brain - the engine's own watchdog /
release logic lives elsewhere; this only decodes gestures like the firmware.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .constants_gen import CONSTANTS, MODES, mode_of

C = CONSTANTS
K = C  # shorthand

GESTURES = C["gestures"]
MODE_COUNT = len(C["modes"])


@dataclass
class Ges:
    """Decoded gesture event (mirrors firmware OUT_GES/OUT_MODE_SET)."""
    g: str
    t: int
    a: int = 0
    d: int = 0
    m: int = 0
    kind: str = "ges"          # "ges" | "mode_set"


@dataclass
class _Tap:
    release_t: int
    amp: int
    press_t: int = 0


class GestureFsm:
    """Port of omniband::GestureFsm. Edge API identical to C++."""

    def __init__(self, mode: int = 0):
        self.out: list[Ges] = []
        self.thr_on = list(C["threshold"]["default_on"])  # floats, counts
        # instance state (all overwritten by set_mode->_reset, listed for clarity)
        self._night = False
        self._st = "IDLE"
        self._press_start = -1
        self._press_amp = 0
        self._press_dur = 0
        self._taps: list[_Tap] = []
        self._recent: list[_Tap] = []
        self._first_press_t = -1
        self._gap_deadline = -1
        self._chew_lock_until = 0
        self._picker_sel = 0
        self._picker_deadline = -1
        self._picker_confirm_pending = False
        self._sos_deadline = -1
        self._sos_hold_since = -1
        self._ch4_pressed = 0
        self._brux_epoch_start = -1
        self._brux_last = -1
        self._brux_end_deadline = -1
        self._brux_pk = 0
        self._brux_fired = False
        self._ch3_pulses: list[int] = []
        self.set_mode(mode, 0)

    # ------------------------------------------------------------- policy
    def set_mode(self, mode: int, now: int) -> None:
        self.mode = mode if 0 <= mode < MODE_COUNT else 0
        self.cfg = mode_of(self.mode)
        self._reset(now)

    @property
    def night(self) -> bool:
        return self._night

    def night_on(self, now: int) -> None:
        if not self.cfg.get("night_mode"):
            return
        self._night = True
        self._st = "NIGHT"
        self._reset_taps()
        self._brux_clear()

    def night_off(self, now: int) -> None:
        self._night = False
        if self._st == "NIGHT":
            self._st = "IDLE"
        self._reset_taps()
        self._brux_clear()

    def sos_cancel(self, now: int) -> None:
        if self._st == "SOS_PENDING":
            self._emit("SOS_CANCELLED", now)
            self._st = "IDLE"

    # ------------------------------------------------------------- edges
    def edge(self, ch: int, down: bool, amp: int, dur_ms: int, now: int) -> None:
        if not 1 <= ch <= 4:
            return
        if self._night:
            self._night_edge(ch, down, amp, dur_ms, now)
            return
        if self._st == "PICKER":
            self._picker_edge(ch, down, amp, dur_ms, now)
            return
        if self._st == "SOS_PENDING":
            self._sos_edge(ch, down, amp, dur_ms, now)
            return
        if ch == 4:
            self._ch4_edge(down, amp, dur_ms, now)

    # ------------------------------------------------------------- timers
    def tick(self, now: int) -> None:
        cg = C["ch4_gestures"]
        if self._chew_lock_until and now >= self._chew_lock_until:
            self._chew_lock_until = 0
        st = self._st
        if st == "GAP_WAIT" and now >= self._gap_deadline:
            self._resolve_gap(now)
        elif st == "PRESSED":
            lh = self.cfg.get("long_hold_ms", 0)
            if lh and now - self._press_start >= lh:
                self._emit("LONG", now, a=self._press_amp,
                           d=now - self._press_start)
                self._open_picker(now)
        elif st == "SOS_PENDING":
            if self._sos_hold_since >= 0 and \
                    now - self._sos_hold_since >= cg["sos_hold_cancel_ms"]:
                self.sos_cancel(now)
            elif now >= self._sos_deadline:
                self._emit("SOS_CONFIRMED", now)
                self._st = "IDLE"
        elif st == "PICKER":
            if self._picker_confirm_pending and now >= self._gap_deadline:
                self._picker_confirm_pending = False
                m = self._picker_sel
                self.set_mode(m, now)
                self.out.append(Ges("MODE_SET", now, m=m, kind="mode_set"))
            elif now >= self._picker_deadline:
                self._emit("PICKER_CANCEL", now)
                self._st = "IDLE"
        elif st == "NIGHT":
            self._night_tick(now)

    # ------------------------------------------------------------- internals
    def _reset(self, now: int) -> None:
        self._st = "IDLE"
        self._reset_taps()
        self._picker_sel = self.mode
        self._sos_hold_since = -1
        self._picker_confirm_pending = False
        self._brux_clear()
        self._ch4_pressed = 0
        self._ch3_pulses = []

    def _reset_taps(self) -> None:
        self._taps: list[_Tap] = []
        self._recent: list[_Tap] = []
        self._first_press_t = -1
        self._gap_deadline = -1

    def _brux_clear(self) -> None:
        self._brux_epoch_start = -1
        self._brux_last = -1
        self._brux_end_deadline = -1
        self._brux_pk = 0
        self._brux_fired = False
        self._ch4_pressed = 0

    def _emit(self, g: str, now: int, a: int = 0, d: int = 0) -> None:
        self.out.append(Ges(g, now, a=a, d=d, m=self.mode))

    def _thr(self, ch: int) -> float:
        return self.thr_on[ch - 1]

    # ---------------- CH4 ----------------
    def _ch4_edge(self, down: bool, amp: int, dur_ms: int, now: int) -> None:
        cg = C["ch4_gestures"]
        if down:
            self._press_start = now
            self._press_amp = amp
            if self._st not in ("PRESSED", "LONG_FIRED"):
                self._st = "PRESSED"
            return
        if self._st not in ("PRESSED", "LONG_FIRED"):
            return
        self._press_dur = dur_ms if dur_ms > 0 else now - self._press_start
        ps, pa = self._press_start, self._press_amp
        if self._st == "LONG_FIRED":
            self._st = "IDLE"
            return
        self._st = "IDLE"
        if self._press_dur < cg["press_min_ms"]:
            return
        lh = self.cfg.get("long_hold_ms", 0)
        if lh and self._press_dur >= lh:
            return
        if self._press_dur > cg["tap_max_ms"]:
            return
        if self._chew_lock_until and now < self._chew_lock_until:
            return
        self._tap_release(now, pa, ps)

    def _tap_release(self, now: int, amp: int, press_t: int) -> None:
        cg = C["ch4_gestures"]
        win = cg["chew_guard_window_ms"]
        self._recent = [t for t in self._recent if now - t.release_t <= win]
        self._recent.append(_Tap(now, amp, press_t))
        if len(self._recent) > cg["chew_guard_taps"]:
            self._emit("REJECT", now, a=amp, d=self._press_dur)
            self._chew_lock_until = now + cg["chew_guard_lock_ms"]
            self._taps = []
            self._recent = []
            self._first_press_t = -1
            self._gap_deadline = -1
            return
        self._taps.append(_Tap(now, amp, press_t))
        if self._first_press_t < 0:
            self._first_press_t = press_t
        if len(self._taps) == 3 and \
                now - self._first_press_t <= cg["triple_total_max_ms"]:
            strong = all(t.amp >= cg["sos_amplitude_ratio"] * self._thr(4)
                         for t in self._taps)
            if strong:
                self._emit("TRIPLE", now, a=amp, d=self._press_dur)
                self._taps = []
                # _recent intentionally kept: chew guard counts all taps
                self._first_press_t = -1
                self._gap_deadline = -1
                self._after_triple(now)
                return
            # weak 3rd: resolve the strong pair as DOUBLE, restart with tap3
            self._emit("DOUBLE", now, a=self._taps[1].amp)
            self._taps = self._taps[-1:]
            self._first_press_t = press_t
        self._gap_deadline = now + cg["multi_gap_ms"]
        self._st = "GAP_WAIT"

    def _resolve_gap(self, now: int) -> None:
        if len(self._taps) == 1:
            self._emit("SINGLE", now, a=self._taps[0].amp)
        elif len(self._taps) == 2:
            self._emit("DOUBLE", now, a=self._taps[1].amp)
        self._taps = []
        self._first_press_t = -1
        self._gap_deadline = -1
        self._st = "IDLE"

    def _after_triple(self, now: int) -> None:
        if self.cfg.get("sos"):
            self._emit("SOS_PENDING", now)
            self._st = "SOS_PENDING"
            self._sos_deadline = now + self.cfg["sos_confirm_ms"]
            self._sos_hold_since = -1
        else:
            self._st = "IDLE"

    # ---------------- SOS_PENDING ----------------
    def _sos_edge(self, ch: int, down: bool, amp: int, dur_ms: int, now: int) -> None:
        if ch == 4:
            if down:
                self._press_start = now
                self._press_amp = amp
                self._sos_hold_since = now
            else:
                self._sos_hold_since = -1

    # ---------------- PICKER ----------------
    def _open_picker(self, now: int) -> None:
        self._st = "PICKER"
        self._picker_sel = self.mode
        self._picker_deadline = now + C["picker"]["timeout_ms"]
        self._picker_confirm_pending = False
        self._emit("PICKER_OPEN", now)

    def _picker_edge(self, ch: int, down: bool, amp: int, dur_ms: int, now: int) -> None:
        cg = C["ch4_gestures"]
        if ch == 1 and down:
            self._picker_sel = (self._picker_sel + MODE_COUNT - 1) % MODE_COUNT
            self._picker_deadline = now + C["picker"]["timeout_ms"]
        elif ch == 2 and down:
            self._picker_sel = (self._picker_sel + 1) % MODE_COUNT
            self._picker_deadline = now + C["picker"]["timeout_ms"]
        elif ch == 4:
            if down:
                self._press_start = now
                self._press_amp = amp
            else:
                d = dur_ms if dur_ms > 0 else now - self._press_start
                if cg["press_min_ms"] <= d <= cg["tap_max_ms"]:
                    self._gap_deadline = now + cg["multi_gap_ms"]
                    self._picker_confirm_pending = True

    # ---------------- NIGHT ----------------
    def _night_edge(self, ch: int, down: bool, amp: int, dur_ms: int, now: int) -> None:
        if ch == 4:
            if down:
                self._ch4_pressed += 1
                if self._brux_epoch_start < 0:
                    self._brux_epoch_start = now
                    self._brux_pk = 0
                    self._brux_fired = False
                if amp > self._brux_pk:
                    self._brux_pk = amp
            else:
                if self._ch4_pressed > 0:
                    self._ch4_pressed -= 1
                if amp > self._brux_pk:
                    self._brux_pk = amp
                self._brux_last = now
        elif ch == 3 and down and amp >= C["night"]["strong_ratio"] * self._thr(3):
            self._ch3_pulses.append(now)
            self._ch3_pulses = self._ch3_pulses[-16:]
            if self._night_exit_ready(now):
                self.night_off(now)

    def _night_tick(self, now: int) -> None:
        bridge = C["activity"]["dropout_bridge_ms"]
        last = now if self._ch4_pressed > 0 else self._brux_last
        if self._brux_epoch_start >= 0 and last >= 0 and \
                now - last > bridge and self._brux_end_deadline < 0:
            self._brux_end_deadline = last + bridge
        if self._brux_end_deadline >= 0 and now >= self._brux_end_deadline:
            if self._brux_fired:
                span = self._brux_last - self._brux_epoch_start
                if span < 0:
                    span = self._brux_end_deadline - self._brux_epoch_start
                self._emit("BRUX_END", now, a=self._brux_pk, d=int(span))
            self._brux_clear()
        if not self._brux_fired and self._brux_epoch_start >= 0 and \
                now - self._brux_epoch_start >= C["hold"]["bruxism_ms"]:
            self._brux_fired = True
            self._emit("BRUX", now, a=self._brux_pk,
                       d=now - self._brux_epoch_start)

    def _night_exit_ready(self, now: int) -> bool:
        win = C["night"]["window_ms"]
        need = C["night"]["strong_ch3_pulses"]
        cnt = 0
        for t in reversed(self._ch3_pulses):
            if now - t <= win:
                cnt += 1
            else:
                break
        return cnt >= need

    # state accessors for tests/simulator
    @property
    def state(self) -> str:
        return self._st
