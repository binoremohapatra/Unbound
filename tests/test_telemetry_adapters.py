"""tests/test_telemetry_adapters.py - T-09 Telemetry Adapters & Profiles Test Suite."""
import json
import pytest
from pathlib import Path

from host.omniband.adapters.telemetry.arena_http import ArenaTelemetryAdapter
from host.omniband.adapters.telemetry.forza_dataout import ForzaTelemetryAdapter
from host.omniband.adapters.telemetry.recorded import RecordedTelemetryAdapter


def test_arena_telemetry_ingest():
    adapter = ArenaTelemetryAdapter()
    sample = {
        "speed": 24.5,
        "e": 0.35,
        "psi_e": -0.05,
        "kappa_ahead": [0.015, 0.020],
        "lane_index": 1,
        "lane_count": 3,
        "gaps": [[25.0, 5.0], None, None],
        "offroad": False,
        "paused": False,
    }
    assert adapter.ingest_json(sample, now=100.0) is True
    st = adapter.poll(now=100.05)
    assert st is not None
    assert st.speed == 24.5
    assert st.e == 0.35
    assert st.gaps[0] == (25.0, 5.0)
    assert adapter.is_healthy(now=100.05) is True

    # Test stale timeout
    assert adapter.poll(now=100.30) is None
    assert adapter.is_healthy(now=100.30) is False


def test_forza_packet_parsing():
    adapter = ForzaTelemetryAdapter()
    # Construct synthetic 324-byte packet with race_on = 1
    raw = bytearray(324)
    # is_race_on = 1 at offset 0
    raw[0:4] = (1).to_bytes(4, byteorder="little", signed=True)
    # vx, vy, vz at offset 32 (e.g. vx=15.0)
    import struct
    struct.pack_into("<fff", raw, 32, 15.0, 0.0, 0.0)
    # steer at offset 316
    raw[316] = 64 # ~ 0.50

    st = adapter.parse_packet(bytes(raw), now=50.0)
    assert st is not None
    assert abs(st.speed - 15.0) < 1e-3
    assert abs(st.steer_applied - (64 / 127.0)) < 1e-2


def test_profiles_schema_valid():
    profiles_dir = Path(__file__).resolve().parent.parent / "host" / "profiles"
    required = ["racing_arena.json", "racing_forza.json", "cyberpunk_2077_direct.json"]
    for fname in required:
        p = profiles_dir / fname
        assert p.is_file(), f"Profile missing: {fname}"
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert "profile_name" in data
            assert "target_window" in data

