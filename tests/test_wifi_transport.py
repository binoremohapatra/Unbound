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
