"""test_gesture_vectors.py - run shared gesture vectors through the Python FSM.

The same tests/vectors/gesture_vectors.json is consumed by
firmware/tests/native/test_core.cpp (C++). If this test and the native test
disagree, the simulator has drifted from the firmware - that is the point.
"""
import json
from pathlib import Path

import pytest

from host.omniband.fsm_core import GestureFsm

VECTORS = Path(__file__).parent / "vectors" / "gesture_vectors.json"


def _load():
    return json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]


def _run_vector(v):
    fsm = GestureFsm(v["mode"])
    steps = []
    t_end = 0
    for s in v["in"]:
        steps.append(s)
        t_end = max(t_end, s["t"])
    expects = v["expect"]
    t_end = max(t_end, max((e["t"] for e in expects), default=0)) + 50
    steps.sort(key=lambda s: s["t"])
    si = 0
    got = []
    for t in range(t_end + 1):
        while si < len(steps) and steps[si]["t"] == t:
            s = steps[si]
            si += 1
            if "cmd" in s:
                if s["cmd"] == "set_mode":
                    fsm.set_mode(s["mode"], t)
                elif s["cmd"] == "night":
                    (fsm.night_on if s["on"] else fsm.night_off)(t)
                elif s["cmd"] == "sos_cancel":
                    fsm.sos_cancel(t)
            else:
                fsm.edge(s["ch"], s["down"], s["a"], s.get("d", 0), t)
        fsm.tick(t)
        for g in fsm.out:
            got.append(g)
        fsm.out.clear()
    return got, expects


def _match(got, expects):
    """Every expected gesture produced at exact t; nothing unexpected."""
    matched = [False] * len(expects)
    for g in got:
        name = g.g
        for i, e in enumerate(expects):
            if not matched[i] and e["t"] == g.t and e["g"] == name and \
                    ("d" not in e or e["d"] == g.d):
                matched[i] = True
                break
        else:
            return False, f"unexpected ges {name} @{g.t}"
    for i, e in enumerate(expects):
        if not matched[i]:
            return False, f"missing ges {e['g']} @{e['t']}"
    return True, ""


@pytest.mark.parametrize("v", _load(), ids=lambda v: v["name"])
def test_gesture_vector(v):
    got, expects = _run_vector(v)
    ok, why = _match(got, expects)
    assert ok, f"{v['name']}: {why}"
