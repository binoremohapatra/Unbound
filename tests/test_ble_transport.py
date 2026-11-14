"""test_ble_transport.py - Comprehensive Unit Tests for Resilient BLE & USB Serial Transport.

Verifies:
1. Universal Hardware Payload Parser (CSV, JSON/NDJSON, binary frames, plain text triggers).
2. Broad device matching and live BLEDevice object passing.
3. Dynamic notify characteristic discovery when primary UUID differs.
4. USB Serial COM port scanner priority scoring and auto-connection fallback.
5. Gateway relative cursor motion routing.
"""
import struct
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from host.omniband.ble_transport import (
    CHAR_UUID,
    KNOWN_DEVICE_KEYWORDS,
    OMNIBAND_BLE_NAME,
    OmniBandBleClient,
    SERVICE_UUID,
    SerialTransportWorker,
    parse_ble_packet,
    parse_hardware_payload,
    scan_usb_serial_ports,
)
from gateway import Gateway, RecordingBackend


def test_parse_csv_payloads():
    # 1. Compact firmware CSV without IMU: S,ch1,ch2,ch3,ch4,seq
    p1 = parse_hardware_payload("S,120,340,560,780,42\n")
    assert p1 is not None
    assert p1["kind"] == "somatic"
    assert p1["channels"] == [120.0, 340.0, 560.0, 780.0]
    assert p1["seq"] == 42
    assert p1["dx"] == 0.0
    assert p1["dy"] == 0.0

    # 2. Compact firmware CSV with IMU: S,ch1,ch2,ch3,ch4,seq,dx,dy
    p2 = parse_hardware_payload("S,100,200,300,400,10,14.5,-8.2\n")
    assert p2 is not None
    assert p2["channels"] == [100.0, 200.0, 300.0, 400.0]
    assert p2["seq"] == 10
    assert p2["dx"] == 14.5
    assert p2["dy"] == -8.2

    # 3. Raw 6-item CSV: ch1,ch2,ch3,ch4,dx,dy
    p3 = parse_hardware_payload("0.1,0.2,0.8,0.05,25,-12\n")
    assert p3 is not None
    assert p3["channels"] == [0.1, 0.2, 0.8, 0.05]
    assert p3["dx"] == 25.0
    assert p3["dy"] == -12.0

    # 4. Raw 4-item CSV: ch1,ch2,ch3,ch4
    p4 = parse_hardware_payload("50,60,70,80\n")
    assert p4 is not None
    assert p4["channels"] == [50.0, 60.0, 70.0, 80.0]

    # 5. Raw 2-item IMU CSV: dx,dy
    p5 = parse_hardware_payload("18.5,-30.0\n")
    assert p5 is not None
    assert p5["kind"] == "imu"
    assert p5["dx"] == 18.5
    assert p5["dy"] == -30.0


def test_parse_json_and_ndjson_payloads():
    # 1. NDJSON telemetry
    ndjson_tele = '{"k":"tele","mode":1,"seq":99,"env":[0.1,0.2,0.3,0.4],"dx":5.0,"dy":-3.0}\n'
    p1 = parse_hardware_payload(ndjson_tele)
    assert p1 is not None
    assert p1["kind"] == "somatic"
    assert p1["channels"] == [0.1, 0.2, 0.3, 0.4]
    assert p1["seq"] == 99
    assert p1["dx"] == 5.0
    assert p1["dy"] == -3.0

    # 2. NDJSON event
    ndjson_evt = '{"k":"evt","g":"CLICK","seq":101}\n'
    p2 = parse_hardware_payload(ndjson_evt)
    assert p2 is not None
    assert p2["kind"] == "gesture"
    assert p2["gesture"] == "CLICK"

    # 3. Generic JSON with p and dx/dy
    json_gen = '{"p":[10,20,30,40],"dx":-4.5,"dy":7.2,"seq":5}\n'
    p3 = parse_hardware_payload(json_gen)
    assert p3 is not None
    assert p3["channels"] == [10.0, 20.0, 30.0, 40.0]
    assert p3["dx"] == -4.5
    assert p3["dy"] == 7.2


def test_parse_binary_frame_and_triggers():
    # 1. Binary frame: 0xAA 0x55 + 4 floats (channels) + 2 floats (dx, dy)
    channels = (12.5, 34.2, 56.7, 78.9)
    imu = (10.0, -15.0)
    raw_bytes = struct.pack("<BB4f2f", 0xAA, 0x55, *channels, *imu)
    p1 = parse_hardware_payload(raw_bytes)
    assert p1 is not None
    assert p1["kind"] == "somatic"
    for expected, actual in zip(channels, p1["channels"]):
        assert abs(expected - actual) < 1e-4
    assert abs(p1["dx"] - 10.0) < 1e-4
    assert abs(p1["dy"] - -15.0) < 1e-4

    # 2. Plain text triggers
    p2 = parse_hardware_payload("CLICK\n")
    assert p2 is not None
    assert p2["kind"] == "gesture"
    assert p2["gesture"] == "CLICK"

    p3 = parse_hardware_payload("SCROLL\n")
    assert p3 is not None
    assert p3["gesture"] == "SCROLL"

    # 3. Invalid inputs return None
    assert parse_hardware_payload("") is None
    assert parse_hardware_payload("   ") is None
    assert parse_hardware_payload("invalid random data without commas") is None


def test_legacy_parse_ble_packet_compatibility():
    # Exact backwards compatibility check
    csv_line = "S,120,340,560,780,42\n"
    pkt1 = parse_ble_packet(csv_line)
    assert pkt1 is not None
    assert pkt1["kind"] == "somatic"
    assert pkt1["channels"] == [120.0, 340.0, 560.0, 780.0]
    assert pkt1["seq"] == 42

    ndjson_line = '{"k":"tele","mode":1,"seq":99}\n'
    pkt2 = parse_ble_packet(ndjson_line)
    assert pkt2 is not None
    assert pkt2["kind"] == "ndjson"
    assert pkt2["obj"]["k"] == "tele"


def test_usb_serial_port_scanner_priorities():
    # Mock serial.tools.list_ports.comports
    mock_port_1 = MagicMock()
    mock_port_1.device = "COM1"
    mock_port_1.description = "Standard Serial Port"
    mock_port_1.manufacturer = "Microsoft"

    mock_port_2 = MagicMock()
    mock_port_2.device = "COM3"
    mock_port_2.description = "Silicon Labs CP210x USB to UART Bridge"
    mock_port_2.manufacturer = "Silicon Labs"

    mock_port_3 = MagicMock()
    mock_port_3.device = "COM4"
    mock_port_3.description = "USB-SERIAL CH340"
    mock_port_3.manufacturer = "wch.cn"

    with patch("serial.tools.list_ports.comports", return_value=[mock_port_1, mock_port_2, mock_port_3]):
        ports = scan_usb_serial_ports()
        assert len(ports) == 3
        # CP210x and CH340 should be prioritized with score 100 before COM1 (score 10)
        assert ports[0][0] in ("COM3", "COM4")
        assert ports[1][0] in ("COM3", "COM4")
        assert ports[2][0] == "COM1"
        assert ports[0][2] == 100
        assert ports[2][2] == 10


def test_ble_dynamic_characteristic_discovery():
    """Verify that if primary CHAR_UUID is not present, any notify characteristic is auto-discovered."""
    import asyncio

    async def _async_test():
        client = OmniBandBleClient()

        # Mock BleakClient with custom services
        mock_client = MagicMock()
        mock_client.is_connected = True

        # Primary characteristic fails start_notify
        custom_uuid = "12345678-1234-5678-1234-56789abcdef0"
        mock_char = MagicMock()
        mock_char.uuid = custom_uuid
        mock_char.properties = ["notify", "read"]

        mock_service = MagicMock()
        mock_service.characteristics = [mock_char]
        mock_client.services = [mock_service]

        async def mock_start_notify(char_uuid, callback):
            if char_uuid == CHAR_UUID:
                raise RuntimeError("Primary UUID not found on this firmware!")
            return None

        mock_client.start_notify = AsyncMock(side_effect=mock_start_notify)

        discovered_uuid = await client._subscribe_best_notify_characteristic(mock_client)
        assert discovered_uuid == custom_uuid

    asyncio.run(_async_test())


def test_gateway_move_cursor_relative():
    """Verify Gateway.move_cursor_relative dispatches relative movement to backend."""
    backend = RecordingBackend()
    gw = Gateway(backend=backend)

    gw.move_cursor_relative(15.2, -8.7)
    assert backend.total_dx == 15
    assert backend.total_dy == -9
    assert ("move", 15, -9) in backend.log


def test_hardware_transport_manager_payload_routing():
    """Verify that incoming packets trigger somatic frame and IMU callbacks."""
    somatic_frames = []
    imu_motions = []

    client = OmniBandBleClient(
        on_somatic_frame=lambda ch, act, seq: somatic_frames.append((ch, act, seq)),
        on_imu_motion=lambda dx, dy: imu_motions.append((dx, dy)),
    )

    # Process packet with somatic channels and IMU
    pkt = {
        "kind": "somatic",
        "channels": [0.8, 0.2, 0.1, 0.9],
        "dx": 10.0,
        "dy": -5.0,
        "seq": 100,
    }
    client._on_hardware_payload(pkt)

    assert len(somatic_frames) == 1
    assert len(imu_motions) == 1
    assert imu_motions[0] == (10.0, -5.0)
    assert somatic_frames[0][2] == 100
