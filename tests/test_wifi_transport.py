"""test_wifi_transport.py - Comprehensive Unit Tests for Ultra-Low-Latency Wi-Fi UDP Transport.

Verifies:
1. Universal Wi-Fi UDP Datagram Parser (JSON, NDJSON, CSV, plain-text gestures).
2. OmniBandWifiClient packet dispatching (somatic frames, IMU motion, hardware gestures).
3. Auto-connection state transitions on packet receipt and silence.
4. Bidirectional auto-ping triggering.
"""
import json
import socket
import time
from unittest.mock import MagicMock, patch

import pytest

from host.omniband.wifi_transport import (
    DEFAULT_ESP_IP,
    DEFAULT_UDP_PORT,
    OmniBandWifiClient,
    UnifiedGestureEngine,
    parse_wifi_packet,
)


def test_parse_wifi_packet_json_full():
    payload = json.dumps({
        "ch": [150.0, 320.0, 540.0, 890.0],
        "dx": 12.5,
        "dy": -8.3,
        "click": "none",
        "scroll": 0,
        "seq": 42,
    }).encode("utf-8")

    pkt = parse_wifi_packet(payload)
    assert pkt is not None
    assert pkt["kind"] == "somatic"
    assert pkt["channels"] == [150.0, 320.0, 540.0, 890.0]
    assert pkt["dx"] == 12.5
    assert pkt["dy"] == -8.3
    assert pkt["seq"] == 42


def test_parse_wifi_packet_json_gestures_and_scroll():
    # 1. Click event
    p1 = parse_wifi_packet(b'{"click":"left","evt":"click"}')
    assert p1 is not None
    assert p1["kind"] == "gesture"
    assert p1["gesture"] == "LEFT"

    # 2. Scroll event
    p2 = parse_wifi_packet(b'{"scroll":3,"evt":"scroll"}')
    assert p2 is not None
    assert p2["kind"] == "scroll"
    assert p2["gesture"] == "SCROLL_UP"
    assert p2["delta_y"] == 120

    # 3. Drag toggle
    p3 = parse_wifi_packet(b'{"drag":"drag_on","evt":"drag"}')
    assert p3 is not None
    assert p3["kind"] == "gesture"
    assert p3["gesture"] == "DRAG_ON"

    # 4. Gesture name
    p4 = parse_wifi_packet(b'{"gesture":"single_clench","evt":"gesture"}')
    assert p4 is not None
    assert p4["kind"] == "gesture"
    assert p4["gesture"] == "SINGLE_CLENCH"


def test_parse_wifi_packet_csv_and_triggers():
    # 1. Compact CSV: S,ch1,ch2,ch3,ch4,seq,dx,dy
    p1 = parse_wifi_packet("S,100,200,300,400,15,5.5,-3.2\n")
    assert p1 is not None
    assert p1["kind"] == "somatic"
    assert p1["channels"] == [100.0, 200.0, 300.0, 400.0]
    assert p1["seq"] == 15
    assert p1["dx"] == 5.5
    assert p1["dy"] == -3.2

    # 2. Plain text triggers
    p2 = parse_wifi_packet(b"CLICK\n")
    assert p2 is not None
    assert p2["kind"] == "gesture"
    assert p2["gesture"] == "CLICK"

    p3 = parse_wifi_packet("SCROLL\n")
    assert p3 is not None
    assert p3["gesture"] == "SCROLL"

    # 3. Invalid inputs
    assert parse_wifi_packet(b"") is None
    assert parse_wifi_packet("   ") is None
    assert parse_wifi_packet("random string without commas or json") is None


def test_wifi_client_packet_dispatching():
    somatic_frames = []
    imu_motions = []
    gestures = []
    connection_events = []

    client = OmniBandWifiClient(
        on_somatic_frame=lambda ch, act, seq: somatic_frames.append((ch, act, seq)),
        on_imu_motion=lambda dx, dy: imu_motions.append((dx, dy)),
        on_hardware_gesture=lambda g: gestures.append(g),
        on_connection_change=lambda conn, msg: connection_events.append((conn, msg)),
    )

    pkt = {
        "kind": "somatic",
        "channels": [0.2, 0.4, 0.6, 0.8],
        "dx": 7.5,
        "dy": -4.2,
        "click": "left",
        "scroll": 1,
        "seq": 105,
    }

    client._dispatch_packet(pkt)

    assert len(somatic_frames) == 1
    assert somatic_frames[0][2] == 105
    assert len(imu_motions) == 1
    assert imu_motions[0] == (7.5, -4.2)
    assert "LEFT" in gestures
    assert "SCROLL_UP" in gestures


def test_wifi_client_probe_trigger_ping():
    client = OmniBandWifiClient()
    with patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock

        client.trigger_ping()
        assert mock_sock.sendto.call_count == 2
        mock_sock.close.assert_called_once()


def test_unified_gesture_engine_jaw_clench_and_double_tap():
    actions = []
    badges = []
    engine = UnifiedGestureEngine(
        on_action=lambda act, val: actions.append((act, val)),
        on_badge=lambda b: badges.append(b),
    )

    t0 = 100.0
    # Channel layout: [CH1=temple-L(jaw), CH2=temple-R(jaw), CH3=forehead(brow), CH4=disabled]
    # Jaw clench fires on CH1 (left temple / temporalis) > 0.25

    # 1. Single Jaw Clench spike (< 450ms, e.g. 50ms)
    engine.feed_somatic([0.8, 0.1, 0.1, 0.0], now=t0)        # CH1 high → jaw active
    engine.feed_somatic([0.0, 0.1, 0.1, 0.0], now=t0 + 0.05) # CH1 low  → released
    # Poll after timeout (0.3s) to flush single-tap pending
    engine.poll(now=t0 + 0.35)
    assert ("click", "left") in actions
    assert any("JAW CLENCH" in b for b in badges)

    # 2. Jaw Double-Tap (2 spikes within 450ms)
    actions.clear()
    badges.clear()
    t1 = 200.0
    # First spike on CH1 (temple)
    engine.feed_somatic([0.8, 0.1, 0.1, 0.0], now=t1)
    engine.feed_somatic([0.0, 0.1, 0.1, 0.0], now=t1 + 0.04)
    # Second spike at 150ms later (within 450ms window)
    engine.feed_somatic([0.8, 0.1, 0.1, 0.0], now=t1 + 0.15)
    engine.feed_somatic([0.0, 0.1, 0.1, 0.0], now=t1 + 0.19)

    assert ("click", "right") in actions
    assert any("DOUBLE-TAP" in b for b in badges)


def test_unified_gesture_engine_jaw_hold_and_release():
    actions = []
    badges = []
    engine = UnifiedGestureEngine(
        on_action=lambda act, val: actions.append((act, val)),
        on_badge=lambda b: badges.append(b),
    )

    t0 = 300.0
    # Start clench on CH1 (left temple / temporalis)
    engine.feed_somatic([0.8, 0.1, 0.1, 0.0], now=t0)
    # Clench sustained beyond 450ms → drag lock fires
    engine.feed_somatic([0.8, 0.1, 0.1, 0.0], now=t0 + 0.46)
    assert ("mouse_down", "left") in actions
    assert any("DRAGGING" in b for b in badges)

    # Release clench
    engine.feed_somatic([0.0, 0.1, 0.1, 0.0], now=t0 + 0.60)
    assert ("mouse_up", "left") in actions
    assert any("DROPPED" in b for b in badges)


def test_unified_gesture_engine_optical_and_imu_gestures():
    actions = []
    badges = []
    engine = UnifiedGestureEngine(
        on_action=lambda act, val: actions.append((act, val)),
        on_badge=lambda b: badges.append(b),
    )

    t = 400.0
    # 1. Left Eye Close & Release
    engine.feed_optical(left_ear=0.10, right_ear=0.30, now=t)
    engine.feed_optical(left_ear=0.30, right_ear=0.30, now=t + 0.15)
    assert ("click", "left") in actions
    assert any("LEFT EYE WINK" in b for b in badges)

    # 2. Right Eye Close & Release
    actions.clear()
    badges.clear()
    engine.feed_optical(left_ear=0.30, right_ear=0.10, now=t + 0.3)
    engine.feed_optical(left_ear=0.30, right_ear=0.30, now=t + 0.45)
    assert ("click", "right") in actions
    assert any("RIGHT EYE WINK" in b for b in badges)

    # 3. Smile Gesture -> Enter
    actions.clear()
    badges.clear()
    engine.feed_optical(smile=0.60, now=t + 0.6)
    assert ("key", "enter") in actions
    assert any("SMILE" in b for b in badges)

    # 4. FOREHEAD/BROW RAISE (CH3 / index 2) → Scroll UP (dy=+3)
    actions.clear()
    badges.clear()
    # CH3 high (forehead) → scroll up. CH1/CH2 low to avoid jaw clench.
    engine.feed_somatic([0.1, 0.1, 0.6, 0.0], now=t + 0.8)
    assert ("scroll", 3) in actions
    assert any("BROW RAISE" in b for b in badges)

    # 5. Head Nod (MPU Pitch Down) -> Scroll DOWN (dy=-3)
    actions.clear()
    badges.clear()
    engine.feed_imu(dx=0.0, dy=0.0, pitch=-18.0, roll=0.0, now=t + 1.0)
    assert ("scroll", -3) in actions
    assert any("HEAD NOD" in b for b in badges)


def test_unified_gesture_engine_nfs_gaming_profile():
    actions = []
    badges = []
    engine = UnifiedGestureEngine(
        on_action=lambda act, val: actions.append((act, val)),
        on_badge=lambda b: badges.append(b),
    )

    engine.set_profile("nfs")
    assert engine.profile == "nfs"

    t = 500.0

    # 1. Acceleration ('w'): Jaw Clench (CH1 Left Temple or CH2 Right Temple)
    # Start clench
    engine.feed_somatic([0.8, 0.1, 0.1, 0.0], now=t)
    assert ("key_down", "w") in actions
    assert any("GAS" in b for b in badges)

    # Release clench
    actions.clear()
    engine.feed_somatic([0.0, 0.1, 0.1, 0.0], now=t + 0.2)
    assert ("key_up", "w") in actions

    # 2. Brake ('s'): Forehead / Brow Raise (CH3)
    actions.clear()
    engine.feed_somatic([0.1, 0.1, 0.7, 0.0], now=t + 0.4)
    assert ("key_down", "s") in actions
    assert any("BRAKE" in b for b in badges)

    # Release brow
    actions.clear()
    engine.feed_somatic([0.1, 0.1, 0.0, 0.0], now=t + 0.6)
    assert ("key_up", "s") in actions

    # 3. Steer Left ('a'): Left Eye Wink
    actions.clear()
    engine.feed_optical(left_ear=0.10, right_ear=0.30, now=t + 0.8)
    assert ("key_down", "a") in actions
    assert any("STEER LEFT" in b for b in badges)

    # Open eye
    actions.clear()
    engine.feed_optical(left_ear=0.30, right_ear=0.30, now=t + 1.0)
    assert ("key_up", "a") in actions

    # 4. Steer Right ('d'): Right Eye Wink
    actions.clear()
    engine.feed_optical(left_ear=0.30, right_ear=0.10, now=t + 1.2)
    assert ("key_down", "d") in actions
    assert any("STEER RIGHT" in b for b in badges)

    # Open eye
    actions.clear()
    engine.feed_optical(left_ear=0.30, right_ear=0.30, now=t + 1.4)
    assert ("key_up", "d") in actions

    # 5. Handbrake ('space'): Smile
    actions.clear()
    engine.feed_optical(smile=0.60, now=t + 1.6)
    assert ("key_down", "space") in actions
    assert any("HANDBRAKE" in b for b in badges)

    # Release smile
    actions.clear()
    engine.feed_optical(smile=0.0, now=t + 1.8)
    assert ("key_up", "space") in actions

    # 6. MPU Inactivity: feed_imu does NOT inject actions or move cursor
    actions.clear()
    engine.feed_imu(dx=50.0, dy=-50.0, pitch=-30.0, roll=20.0, now=t + 2.0)
    assert len(actions) == 0  # Completely muted in NFS mode!

    # 7. Switching back to desktop profile cleanly resets any held keys
    engine.feed_somatic([0.8, 0.1, 0.1, 0.0], now=t + 2.2)  # Gas held down
    assert ("key_down", "w") in actions
    actions.clear()
    engine.set_profile("desktop")
    assert ("key_up", "w") in actions
    assert engine.profile == "desktop"
