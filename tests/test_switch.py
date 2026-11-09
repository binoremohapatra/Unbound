"""Unit tests for adapters/switch.py (Step 11)."""
import pytest

from adapters.switch import SwitchSource, create_source
from core.events import SignalEvent


def test_switch_source_edge_detection():
    keys_down = set()

    def fake_key_state(key: str) -> bool:
        return key in keys_down

    src = SwitchSource(monitored_keys=("space", "f12"), key_state_fn=fake_key_state)

    # 1. Initial poll: no keys down
    assert src.poll(1.0) == []

    # 2. Key "space" pressed down -> rising edge
    keys_down.add("space")
    evs1 = src.poll(2.0)
    assert len(evs1) == 1
    assert evs1[0] == SignalEvent("switch", "space", 1.0, 2.0)

    # 3. Key "space" remains held -> no chatter
    assert src.poll(2.1) == []

    # 4. Key "space" released -> falling edge
    keys_down.remove("space")
    evs2 = src.poll(3.0)
    assert len(evs2) == 1
    assert evs2[0] == SignalEvent("switch", "space", 0.0, 3.0)

    # 5. Remains released -> no chatter
    assert src.poll(3.1) == []


def test_switch_source_inject_method():
    src = SwitchSource(monitored_keys=("space",))
    # Inject programmatic press
    src.inject("space", pressed=True, t=10.0)
    evs = src.poll(10.0)
    assert evs == [SignalEvent("switch", "space", 1.0, 10.0)]

    # Inject programmatic release
    src.inject("space", pressed=False, t=10.5)
    evs2 = src.poll(10.5)
    assert evs2 == [SignalEvent("switch", "space", 0.0, 10.5)]


def test_multiple_switches_independent_edges():
    keys_down = set()
    src = SwitchSource(monitored_keys=("space", "f12"), key_state_fn=lambda k: k in keys_down)

    # Space pressed
    keys_down.add("space")
    assert src.poll(1.0) == [SignalEvent("switch", "space", 1.0, 1.0)]

    # F12 also pressed
    keys_down.add("f12")
    assert src.poll(1.1) == [SignalEvent("switch", "f12", 1.0, 1.1)]

    # Space released while F12 still held
    keys_down.remove("space")
    assert src.poll(1.2) == [SignalEvent("switch", "space", 0.0, 1.2)]

    # F12 released
    keys_down.remove("f12")
    assert src.poll(1.3) == [SignalEvent("switch", "f12", 0.0, 1.3)]


def test_create_source_factory():
    # Without force and without env var, raises RuntimeError (so dry-run without camera doesn't hang)
    with pytest.raises(RuntimeError):
        create_source(monitored_keys=("a", "b"))

    # With force=True, creates SwitchSource
    src = create_source(monitored_keys=("a", "b"), force=True)
    assert hasattr(src, "poll")
    assert hasattr(src, "close")
    assert src.name == "switch"
    src.close()


def test_key_state_function_exception_handled_safely():
    def broken_fn(key: str) -> bool:
        raise RuntimeError("hardware glitch")

    src = SwitchSource(monitored_keys=("space",), key_state_fn=broken_fn)
    # Must never crash when polling
    assert src.poll(1.0) == []
