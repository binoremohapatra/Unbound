"""sim_device.py - simulated OmniBand V2 device for --simulate / tests.

Model fidelity (honest scope): this is a PROTOCOL-LEVEL device model. It
decodes gestures with the same GestureFsm the firmware uses (shared vectors
guarantee parity at the gesture layer) and emits spec-shaped tele/evt/ges/ack
lines. It does NOT model the ADC/DSP envelope pipeline - inject edges directly
via inject_edge(), exactly like the firmware's test hook.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

from .constants_gen import CONSTANTS, MODES, mode_of
from .fsm_core import GestureFsm
from . import protocol

log = logging.getLogger("omniband.sim")

K = CONSTANTS


class SimDevice:
    """Virtual device. feed_line() accepts host commands; step() advances time.

    The device runs its own clock (ms). Call run() in a thread for autonomous
    simulate mode, or step() manually in tests.
    """

    def __init__(self, baud: int = K["link"]["baud_boot"],
                 auto_gestures: bool = False) -> None:
        self.baud = baud
        self.mode = 0
        self.fsm = GestureFsm(0)
        self._t_ms = 0
        self._seq = 0
        self._rx_cb: Optional[Callable[[bytes], None]] = None
        self._tx_lines: list[bytes] = []
        self._tx_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.auto_gestures = auto_gestures
        self._last_auto_ms = 0
        self._auto_step = 0
        self._auto_release_at = -1
        self.scope_on = False
        self.scope_rate = 2
        self.compat_v1 = False
        self.thr_on = [float(x) for x in K["threshold"]["default_on"]]
        self.night = False
        self._pend_pong: list[tuple[str, int]] = []
        self._fsm_lock = threading.RLock()
        self._pend_acks: list[tuple[str, bool, str]] = []
        self._env = [0.0, 0.0, 0.0, 0.0]
        self._active = [False] * 4
        self.cal_step = ""
        self.last_rx_ms = -10**9
        self.stats = {"cmds": 0, "bad": 0}

    # ------------------------------------------------------------- plumbing
    def attach_host(self, cb: Callable[[bytes], None]) -> None:
        self._rx_cb = cb

    def _emit(self, obj: dict) -> None:
        obj["seq"] = self._seq
        self._seq += 1
        line = protocol.dumps(obj).encode("utf-8")
        with self._tx_lock:
            self._tx_lines.append(line)

    def drain_tx(self) -> list[bytes]:
        with self._tx_lock:
            out = self._tx_lines
            self._tx_lines = []
            return out

    def _push_to_host(self, line: bytes) -> None:
        if self._rx_cb:
            self._rx_cb(line)

    # ------------------------------------------------------------- clock
    def now_ms(self) -> int:
        return self._t_ms

    def step(self, dt_ms: int = 2) -> None:
        """Advance the device clock; emits tele at 30 Hz, runs FSM timers."""
        self._t_ms += dt_ms
        t = self._t_ms
        with self._fsm_lock:
            self.fsm.tick(t)
            self._emit_fsm_out()
        # tele @30 Hz
        if t % (1000 // K["timing"]["tele_hz"]) < dt_ms:
            self._emit_tele()
        if self.auto_gestures:
            self._maybe_auto_gesture(t)
        # flush queued replies
        while self._pend_acks:
            cid, ok, err = self._pend_acks.pop(0)
            self._emit({"k": "ack", "id": cid, "ok": ok, "err": err})
        while self._pend_pong:
            cid, th = self._pend_pong.pop(0)
            self._emit({"k": "pong", "id": cid, "th": th,
                        "t": self._t_us()})

    def _t_us(self) -> int:
        return self._t_ms * 1000

    def _emit_fsm_out(self) -> None:
        for g in self.fsm.out:
            if g.kind == "mode_set":
                self.mode = g.m
                self.fsm.set_mode(self.mode, g.t)
                self._emit({"k": "ges", "v": 2, "t": g.t * 1000, "g": "MODE_SET",
                            "m": g.m})
            else:
                self._emit({"k": "ges", "v": 2, "t": g.t * 1000, "g": g.g,
                            "a": g.a, "d": g.d, "m": g.m})
                if g.g in ("ESTOP", "SOS_CONFIRMED", "SOS_PENDING"):
                    self._emit({"k": "evt", "v": 2, "t": g.t * 1000, "ch": 4,
                                "ty": "down", "a": g.a})
        self.fsm.out.clear()

    def _emit_tele(self) -> None:
        st = self.fsm.state
        flags = 0
        if self.night:
            flags |= K["tele_flags"]["night"]
        if st == "SOS_PENDING":
            flags |= K["tele_flags"]["sos_pending"]
        if st == "PICKER":
            flags |= K["tele_flags"]["picker"]
        self._emit({"k": "tele", "v": 2, "t": self._t_us(), "m": self.mode,
                    "e": list(self._env), "s": flags, "st": st})

    # ------------------------------------------------------------- gestures
    def inject_edge(self, ch: int, down: bool, amp: float = 900.0,
                    dur_ms: int = 0) -> None:
        """Same hook the firmware exposes for tests/simulator."""
        with self._fsm_lock:
            self.fsm.edge(ch, down, int(amp), int(dur_ms), self._t_ms)
        ty = "down" if down else "up"
        self._emit({"k": "evt", "v": 2, "t": self._t_us(), "ch": ch, "ty": ty,
                    "a": int(amp), **({"d": dur_ms} if dur_ms else {})})
        if down:
            self._env[ch - 1] = amp
            self._active[ch - 1] = True
        else:
            self._env[ch - 1] = 0.0
            self._active[ch - 1] = False

    def inject_night(self, on: bool) -> None:
        self.night = on
        with self._fsm_lock:
            (self.fsm.night_on if on else self.fsm.night_off)(self._t_ms)

    def _maybe_auto_gesture(self, t: int) -> None:
        """Autonomous demo: a SINGLE tap every ~2.5 s on CH4 (mode 0 default)."""
        period = 2500
        if t - self._last_auto_ms >= period:
            self._last_auto_ms = t
            self.inject_edge(4, True, 950)
            # release 150 ms later (tap decode happens on the up edge)
            self._auto_release_at = t + 150
        if self._auto_release_at >= 0 and t >= self._auto_release_at:
            self._auto_release_at = -1
            self.inject_edge(4, False, 950, 150)

    # ------------------------------------------------------------- host cmds
    def feed_line(self, line: str) -> None:
        obj = protocol.loads(line)
        if obj is None:
            self.stats["bad"] += 1
            return
        self.stats["cmds"] += 1
        self.last_rx_ms = self._t_ms
        k = obj.get("k", "")
        cid = str(obj.get("id", ""))
        if k == "hello":
            self._emit_hello()
        elif k == "ping":
            self._pend_pong.append((cid, int(obj.get("th", 0))))
        elif k == "set_mode":
            m = int(obj.get("mode", 0))
            if 0 <= m < len(MODES):
                self.mode = m
                with self._fsm_lock:
                    self.fsm.set_mode(m, self._t_ms)
                self._pend_acks.append((cid, True, ""))
            else:
                self._pend_acks.append((cid, False, "bad_mode"))
        elif k == "set_thr":
            ch = int(obj.get("ch", 0))
            on = float(obj.get("on", 0))
            if 1 <= ch <= 4 and on > 0:
                self.thr_on[ch - 1] = on
                self.fsm.thr_on[ch - 1] = on
                self._pend_acks.append((cid, True, ""))
            else:
                self._pend_acks.append((cid, False, "bad_thr"))
        elif k == "recal":
            self._pend_acks.append((cid, obj.get("what") in
                                    K["recal_whats"], "bad_what"))
        elif k == "cal_step":
            self.cal_step = str(obj.get("step", ""))
            self._pend_acks.append((cid, self.cal_step in K["cal_steps"],
                                    "bad_step"))
        elif k == "set_cfg":
            key = str(obj.get("key", ""))
            if key == "compat_v1":
                self.compat_v1 = bool(obj.get("val"))
                self._pend_acks.append((cid, True, ""))
            elif key == "night":
                self.inject_night(bool(obj.get("val")))
                self._pend_acks.append((cid, True, ""))
            else:
                self._pend_acks.append((cid, False, "bad_key"))
        elif k == "save" or k == "defaults":
            if k == "defaults":
                self.mode = 0
                self.fsm.set_mode(0, self._t_ms)
                self.thr_on = [float(x) for x in K["threshold"]["default_on"]]
                with self._fsm_lock:
                    self.fsm.thr_on = list(self.thr_on)
            self._pend_acks.append((cid, True, ""))
        elif k == "scope":
            self.scope_on = bool(obj.get("on"))
            self.scope_rate = int(obj.get("rate", 2))
            self._pend_acks.append((cid, True, ""))
        elif k == "set_baud":
            b = int(obj.get("baud", 0))
            if b in K["baud_rates"]:
                self.baud = b
                self._pend_acks.append((cid, True, ""))
            else:
                self._pend_acks.append((cid, False, "bad_baud"))
        elif k == "night":
            self.inject_night(bool(obj.get("on")))
            self._pend_acks.append((cid, True, ""))
        elif k == "sos_cancel":
            with self._fsm_lock:
                self.fsm.sos_cancel(self._t_ms)
            self._pend_acks.append((cid, True, ""))
        elif k == "oled_msg" or k == "diag":
            if k == "diag":
                self._emit({"k": "diag", "v": 2, "t": self._t_us(),
                            "rx": self.stats["cmds"], "drop": self.stats["bad"],
                            "rl": 0, "baud": self.baud, "bt": 0})
            self._pend_acks.append((cid, True, ""))
        else:
            self._pend_acks.append((cid, False, "unknown"))

    def _emit_hello(self) -> None:
        caps = list(K["caps"])
        self._emit({"k": "hello", "v": 2, "t": self._t_us(), "fw": "2.0.0-sim",
                    "rr": 0, "mode": self.mode, "hz": K["timing"]["dsp_hz"],
                    "ov": K["adc"]["oversample"], "thr": list(self.thr_on),
                    "caps": caps, "baud": self.baud})

    # ------------------------------------------------------------- thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._t_ms = 0
        self._emit_hello()
        self._thread = threading.Thread(target=self._run, name="simdev",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            now = time.perf_counter()
            dt = int((now - last) * 1000)
            last = now
            if 0 < dt <= 50:
                self.step(dt)
            for line in self.drain_tx():
                self._push_to_host(line)
            time.sleep(0.002)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
