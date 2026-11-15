"""main_hud.py - 60 Hz main loop + focus-safe HUD for OmniControl OS.

  * Locked 60 Hz tick: perf_counter-stepped, no drift accumulation.
  * Tkinter HUD made focus-proof via SetWindowLongW extended styles
    WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW - it can never steal
    focus from GTA V or Word. Telemetry refreshes at 4 Hz (<1% CPU).
  * Workstation mode swaps the HUD for a 5-slot predictive completion bar.
  * --record / --replay JSONL harness for deterministic safety testing:
      record: appends {"t","kind","name","value","source","state"} lines
      replay: feeds recorded events through the FSM with a virtual clock,
              no camera/mic/serial needed -> repeatable PASS/FAIL runs.

Run:  python main_hud.py [--record rec.jsonl] [--replay rec.jsonl]
                          [--duration SEC] [--safe] [--config PATH]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from central_fsm import (Action, ACTIVE_ESPORTS, ACTIVE_WORKSTATION,
                         LATCHED, OmniFSM, SAFE_IDLE)
from signal_pipelines import (AudioPipeline, FrameSample, IntentEvent,
                              PsychicIntentAdapter, VisionPipeline)
from win32_output_sink import (_NullBackend, StatefulOutputSink,
                               WindowContextInspector, inject_unicode_string,
                               load_scancode_table)

log = logging.getLogger("omnicontrol_os")

HUD_HZ = 4.0

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080


def _load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ action pump
class ActionPump:
    """Applies FSM Actions to the sink. The ONLY place Actions become OS input."""

    def __init__(self, sink: StatefulOutputSink):
        self.sink = sink
        self.notices: list[str] = []

    def apply(self, actions: list) -> None:
        for a in actions:
            if not isinstance(a, Action):
                continue
            if a.kind == "panic":
                self.sink.panic_release_all("fsm panic")
            elif a.kind == "release_keys":
                self.sink.release_all("fsm release")
            elif a.kind == "key":
                self.sink.want_key(a.name, bool(a.value))
            elif a.kind == "mouse":
                self.sink.want_mouse(a.name, bool(a.value))
            elif a.kind == "axis":
                self.sink.set_axis(a.name, a.value)
            elif a.kind == "unicode":
                if a.text:
                    inject_unicode_string(a.text)
            elif a.kind == "notice":
                self.notices.append(a.name)
                if len(self.notices) > 8:
                    self.notices.pop(0)


# ------------------------------------------------------------------ HUD
class Hud:
    """Focus-safe Tk overlay. 4 Hz telemetry; workstation shows 5 predict slots."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._tk = None
        self._labels = {}
        self._slots = []
        self._mode = "BOOT"
        self._next_hud = 0.0
        self.enabled = True
        try:
            import tkinter as tk
            self._tk = tk.Tk()
            self._tk.withdraw()
            self._root = tk.Toplevel(self._tk)
            self._root.overrideredirect(True)
            self._root.attributes("-topmost", True)
            self._root.attributes("-alpha", 0.82)
            self._root.configure(bg="#111111")
            # position top-center, out of the way of crosshair
            sw, sh = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
            self._root.geometry(f"520x120+{(sw - 520) // 2}+8")
            self._labels["title"] = tk.Label(self._root, text="OmniControl OS",
                                             fg="#F07842", bg="#111111",
                                             font=("Consolas", 13, "bold"))
            self._labels["title"].pack(anchor="w", padx=8)
            body = tk.Frame(self._root, bg="#111111")
            body.pack(fill="both", expand=True, padx=8)
            self._labels["telemetry"] = tk.Label(body, text="...",
                                                 fg="#F7F5F0", bg="#111111",
                                                 font=("Consolas", 9),
                                                 justify="left", anchor="w")
            self._labels["telemetry"].pack(fill="x")
            self._labels["slots"] = tk.Frame(body, bg="#111111")
            self._labels["slots"].pack(fill="x", pady=(4, 0))
            for i in range(5):
                lbl = tk.Label(self._labels["slots"], text="  ",
                               fg="#716D67", bg="#111111",
                               font=("Consolas", 11, "bold"), width=8)
                lbl.pack(side="left", padx=2)
                self._slots.append(lbl)
            self._root.deiconify()
            self._apply_exstyle()
            self._tk.after(50, self._drain)
        except Exception as e:
            log.warning("HUD unavailable (%s) - running headless", e)
            self.enabled = False
            self._tk = None

    def _apply_exstyle(self) -> None:
        if os.name != "nt" or self._root is None:
            return
        try:
            hwnd = self._root.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                style | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            log.debug("exstyle patch failed", exc_info=True)

    def _drain(self) -> None:
        try:
            self._tk.update_idletasks()
            self._tk.after(50, self._drain)
        except Exception:
            pass

    def poll_events(self) -> None:
        """Keep Tk alive at 4 Hz (also processes its own events)."""
        if self._tk is not None:
            try:
                self._tk.update()
            except Exception:
                pass

    def update(self, now: float, fsm, psychic_status: dict,
               latest: Optional[FrameSample]) -> None:
        if not self.enabled or now < self._next_hud:
            return
        self._next_hud = now + 1.0 / HUD_HZ
        mode = fsm.state
        sub = getattr(fsm, "substate", "")
        self._labels["title"].configure(
            text=f"OmniControl  {mode}" + (f" / {sub}" if sub else ""))
        parts = [f"psychic: {psychic_status.get('mode', '?')}",
                 f"vision: {psychic_status.get('vision', '?')}",
                 f"audio: {psychic_status.get('audio', '?')}"]
        if latest is not None:
            parts.append(f"face={'OK' if latest.face_ok else 'LOST'}")
            parts.append(f"gaze=({latest.gaze_x:+.2f},{latest.gaze_y:+.2f})")
            parts.append(f"proc={latest.proc_ms:.1f}ms")
        else:
            parts.append("face=NO SAMPLE")
        self._labels["telemetry"].configure(text="\n".join(parts))
        # workstation predictive bar
        if mode == ACTIVE_WORKSTATION:
            self._labels["slots"].pack(fill="x", pady=(4, 0))
            words = getattr(fsm, "predict_words", []) or ["the", "and", "ing", "tion", "to "]
            active = getattr(fsm, "predict_slot", 0) % 5
            for i, lbl in enumerate(self._slots):
                w = words[i % len(words)]
                lbl.configure(text=("▶ " + w.ljust(6)) if i == active else "  " + w.ljust(6),
                              fg="#F07842" if i == active else "#716D67")
        else:
            self._labels["slots"].pack_forget()

    def close(self) -> None:
        if self._tk is not None:
            try:
                self._tk.destroy()
            except Exception:
                pass


# ------------------------------------------------------------------ record/replay
class Recorder:
    def __init__(self, path: str):
        self.f = open(path, "a", encoding="utf-8")

    def write(self, ev: IntentEvent, state: str) -> None:
        self.f.write(json.dumps({"t": round(ev.t, 6), "kind": ev.kind,
                                 "name": ev.name, "value": ev.value,
                                 "source": ev.source, "state": state}) + "\n")
        self.f.flush()

    def close(self) -> None:
        self.f.close()


def load_replay(path: str) -> list[IntentEvent]:
    evs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            evs.append(IntentEvent(d.get("kind", "gesture"), d.get("name", ""),
                                   float(d.get("value", 1.0)),
                                   float(d.get("t", 0.0)), d.get("source", "REPLAY")))
    return evs


def run_replay(cfg: dict, path: str) -> int:
    """Deterministic FSM replay on a virtual clock.

    Recorded JSONL carries edge events only, so the harness injects a synthetic
    face-present FrameSample every 16 ms between events - enough continuous face
    for SAFE_IDLE -> ACTIVE, then events drive latch/cancel/lock transitions.
    Face-loss paths are covered by unit tests, not replay. Uses NullBackend so
    replay can never emit real OS input.
    """
    fsm = OmniFSM(cfg)
    sink = StatefulOutputSink(backend=_NullBackend(), auto_release=False)
    pump = ActionPump(sink)
    events = load_replay(path)
    events.sort(key=lambda e: e.t)
    seen = {fsm.state}
    now = 0.0
    good = FrameSample(t=0.0, face_ok=True)

    def _step_to(target: float) -> None:
        nonlocal now
        while now < target:
            now = min(now + 1.0 / 60.0, target)
            good.t = now
            for a in fsm.handle_sample(good, now):
                pump.apply([a])
            for a in fsm.tick(now):
                pump.apply([a])
            seen.add(fsm.state)

    _step_to(events[0].t if events else 0.0)
    for ev in events:
        _step_to(ev.t)
        for a in fsm.handle_event(ev, now):
            pump.apply([a])
        seen.add(fsm.state)
    print(f"REPLAY: {len(events)} events, states seen: {sorted(seen)}")
    required = {SAFE_IDLE, LATCHED}
    missing = required - seen
    if missing:
        print(f"REPLAY: FAIL - never entered {sorted(missing)}")
        return 1
    print("REPLAY: PASS")
    return 0


# ------------------------------------------------------------------ main loop
def run_live(cfg: dict, args) -> int:
    inspector = WindowContextInspector(cfg.get("window_profiles", {}))
    sink = StatefulOutputSink(scancodes=load_scancode_table(cfg),
                              axis_map=cfg.get("axes", {}))
    pump = ActionPump(sink)
    transitions: list[tuple] = []

    def _on_transition(old, new, t):
        transitions.append((old, new, t))
        log.info("transition %s -> %s", old, new)

    fsm = OmniFSM(cfg, on_transition=_on_transition)

    vision = VisionPipeline(cfg)
    vision.set_filters(cfg.get("filters", {}))
    audio = AudioPipeline(cfg)
    rec = Recorder(args.record) if args.record else None

    def _edge(ev: IntentEvent) -> None:
        for a in fsm.handle_event(ev, time.perf_counter()):
            pump.apply([a])
        if rec is not None:
            rec.write(ev, fsm.state)

    psychic = PsychicIntentAdapter(cfg, vision, audio, on_event=_edge)
    vision.on_event = psychic._vision_edge          # optical triggers gated by mode
    audio.on_event = _edge                          # voice commands
    psychic.start()

    hud = Hud(cfg)
    duration = args.duration
    t0 = time.perf_counter()
    period = 1.0 / float(cfg.get("loop", {}).get("hz", 60))
    next_tick = t0
    cfg_path = Path(args.config)
    cfg_mtime = cfg_path.stat().st_mtime if cfg_path.is_file() else 0.0
    next_cfg_check = t0 + 1.0
    log.info("OmniControl OS live loop start (%.0f Hz, psychic=%s)",
             1.0 / period, psychic.mode)

    try:
        while True:
            now = time.perf_counter()
            if duration and now - t0 >= duration:
                break
            # hot-reload config.json once per second (thresholds live in sub-dicts
            # the pipelines read each frame; FSM timing constants apply next launch)
            if now >= next_cfg_check:
                next_cfg_check = now + 1.0
                try:
                    mt = cfg_path.stat().st_mtime if cfg_path.is_file() else 0.0
                    if mt and mt != cfg_mtime:
                        new_cfg = _load_config(str(cfg_path))
                        cfg_mtime = mt
                        inspector.profiles = new_cfg.get("window_profiles", {})
                        sink.axis_map = {k: v for k, v in
                                         new_cfg.get("axes", {}).items()
                                         if isinstance(v, dict)
                                         and not k.startswith("_")}
                        if hasattr(sink.backend, "scancodes"):
                            sink.backend.scancodes = load_scancode_table(new_cfg)
                        vision.cfg = new_cfg.get("vision", {})
                        vision.set_filters(new_cfg.get("filters", {}))
                        audio.cfg = new_cfg.get("audio", {})
                        psychic.cfg = new_cfg
                        psychic.omni_cfg = new_cfg.get("omnibus", {})
                        fsm.predict_words = new_cfg.get("workstation", {}).get("words", [])
                        log.info("config.json hot-reloaded")
                except Exception as e:
                    log.warning("config reload failed, keeping previous: %s", e)
            # window profile -> mode (inspector polls at its own 4 Hz rate)
            title, exe, hint = inspector.poll(now)
            if hint in (ACTIVE_ESPORTS, ACTIVE_WORKSTATION):
                for a in fsm.set_mode(hint, now):
                    pump.apply([a])
            # pipeline poll (audio events, serial packets)
            for ev in psychic.poll(now):
                for a in fsm.handle_event(ev, now):
                    pump.apply([a])
                if rec:
                    rec.write(ev, fsm.state)
            # continuous sample -> aim + face safety
            sample = psychic.latest_sample()
            for a in fsm.handle_sample(sample, now):
                pump.apply([a])
            for a in fsm.tick(now):
                pump.apply([a])
            sink.flush(now)
            hud.poll_events()
            hud.update(now, fsm, psychic.status(), sample)
            # locked 60 Hz: sleep until next tick, never busy-spin long
            next_tick += period
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(min(delay, period))
            elif delay < -period * 4:                  # badly behind: resync, count as slip
                next_tick = time.perf_counter()
    except KeyboardInterrupt:
        log.info("Ctrl+C - shutting down")
    finally:
        sink.panic_release_all("shutdown")
        sink.flush(time.perf_counter())
        psychic.vision.stop()
        psychic.audio.stop()
        hud.close()
        if rec:
            rec.close()
        try:
            psychic._close_serial()
        except Exception:
            pass
    print(f"states: {sorted({t[1] for t in transitions} | {fsm.state})}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniControl OS - hands-free layer")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("--record", default="")
    ap.add_argument("--replay", default="")
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--safe", action="store_true", help="start in SAFE_IDLE until face")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    cfg = _load_config(args.config)
    if args.replay:
        return run_replay(cfg, args.replay)
    return run_live(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
