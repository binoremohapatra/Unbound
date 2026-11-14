"""ble_transport.py - Resilient BLE & USB Serial Hybrid Hardware Transport for OmniBand V2.

Provides:
1. Resilient BLE Client for Windows WinRT + ESP32:
   - Broad device matching (case-insensitive "omniband", "unbound", "esp32", "nimble", or service UUID).
   - Direct BLEDevice object passing into BleakClient(device, timeout=12.0) to prevent WinRT cached address failures.
   - 3x fast retry with 0.5s backoff and re-discovery if first GATT handshake throws BleakError/TimeoutError.
   - Dynamic Characteristic Discovery: If primary CHAR_UUID is not exposed, scans all services and
     subscribes (start_notify) to ANY characteristic with "notify" or "indicate" in char.properties.
2. 1-Click USB Serial (COM Port) Auto-Connect Fallback:
   - Auto-scans COM ports using pyserial (serial.tools.list_ports).
   - Priority heuristic matching: CP210, CH340, CH9102, USB, UART, Silicon Labs, FTDI.
   - Connects at 115200 baud, reads streaming lines in daemon worker thread.
3. Universal Hardware Payload Parser:
   - Compact CSV: "S,ch1,ch2,ch3,ch4,seq" or "S,ch1,ch2,ch3,ch4,seq,dx,dy"
   - Raw CSV: "ch1,ch2,ch3,ch4,dx,dy" or "ch1,ch2,ch3,ch4" or "dx,dy"
   - NDJSON / JSON: {"p": [...], "dx": ..., "dy": ...} or {"k": "tele", ...}
   - Binary frame: 0xAA 0x55 magic header + float/int channels and IMU deltas.
   - Plain text triggers: "CLICK", "LEFT", "RIGHT", "SCROLL", "CH1".."CH4".
4. Unified HardwareTransportManager / OmniBandBleClient:
   - Hybrid auto-connect (USB Serial first, then BLE fallback).
   - Dispatches normalized somatic channels, active state, sequence, and IMU relative mouse deltas (dx, dy).
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import threading
import time
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

if TYPE_CHECKING:
    from host.omniband.hardware.hardware_calibration import HardwareCalibrator

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    serial = None
    SERIAL_AVAILABLE = False

log = logging.getLogger("omniband.ble_transport")

OMNIBAND_BLE_NAME = "OmniBand-BLE"
SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
CHAR_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a6"

KNOWN_DEVICE_KEYWORDS = ["omniband", "unbound", "esp32", "nimble"]
USB_PRIORITY_KEYWORDS = [
    "cp210", "ch340", "ch9102", "usb-serial", "usb serial",
    "uart", "silicon labs", "ftdi", "esp32", "arduino"
]


def parse_hardware_payload(data: bytes | str) -> Optional[dict]:
    """
    Universally parse a hardware payload from BLE or USB Serial.

    Supported formats:
    1. Binary struct with magic 0xAA 0x55:
       b'\\xaa\\x55' + 4 floats (channels) + 2 floats (dx, dy)
    2. CSV string:
       - 'S,ch1,ch2,ch3,ch4,seq'
       - 'S,ch1,ch2,ch3,ch4,seq,dx,dy'
       - 'ch1,ch2,ch3,ch4,dx,dy'
       - 'ch1,ch2,ch3,ch4'
       - 'dx,dy' (IMU only)
    3. JSON / NDJSON string:
       - '{"p": [ch1, ch2, ch3, ch4], "dx": ..., "dy": ...}'
       - '{"channels": [...], "dx": ..., "dy": ...}'
       - '{"k": "tele", "env": [...], "seq": ...}'
       - '{"k": "evt", "g": "CLICK", ...}'
    4. Plain text trigger:
       - 'CLICK', 'LEFT', 'RIGHT', 'SCROLL', 'CH1'..'CH4'
    """
    if isinstance(data, (bytes, bytearray)):
        # Binary frame check (Magic header 0xAA 0x55)
        if len(data) >= 18 and data[0] == 0xAA and data[1] == 0x55:
            try:
                # 0xAA, 0x55, 4 floats (16 bytes) -> channels
                ch = list(struct.unpack("<4f", data[2:18]))
                dx, dy = 0.0, 0.0
                if len(data) >= 26:
                    dx, dy = struct.unpack("<2f", data[18:26])
                return {
                    "kind": "somatic",
                    "channels": ch,
                    "dx": float(dx),
                    "dy": float(dy),
                    "seq": 0,
                    "t": time.perf_counter(),
                }
            except Exception as e:
                log.debug("Failed binary frame unpack: %s", e)
        # Otherwise decode as text
        data_str = data.decode("utf-8", errors="replace")
    else:
        data_str = str(data)

    s = data_str.strip()
    if not s:
        return None

    # Format 2: CSV
    if "," in s:
        parts = [p.strip() for p in s.split(",")]
        # Case A: 'S,ch1,ch2,ch3,ch4,seq[,dx,dy]'
        if parts[0] == "S" and len(parts) >= 6:
            try:
                ch1 = float(parts[1])
                ch2 = float(parts[2])
                ch3 = float(parts[3])
                ch4 = float(parts[4])
                seq = int(parts[5])
                dx = float(parts[6]) if len(parts) > 6 else 0.0
                dy = float(parts[7]) if len(parts) > 7 else 0.0
                return {
                    "kind": "somatic",
                    "channels": [ch1, ch2, ch3, ch4],
                    "dx": dx,
                    "dy": dy,
                    "seq": seq,
                    "t": time.perf_counter(),
                }
            except (ValueError, IndexError):
                return None

        # Case B: 'ch1,ch2,ch3,ch4,dx,dy' (6 values)
        elif len(parts) == 6 and parts[0] != "S":
            try:
                ch = [float(p) for p in parts[:4]]
                dx = float(parts[4])
                dy = float(parts[5])
                return {
                    "kind": "somatic",
                    "channels": ch,
                    "dx": dx,
                    "dy": dy,
                    "seq": 0,
                    "t": time.perf_counter(),
                }
            except ValueError:
                return None

        # Case C: 'ch1,ch2,ch3,ch4' (4 values)
        elif len(parts) == 4 and parts[0] != "S":
            try:
                ch = [float(p) for p in parts]
                return {
                    "kind": "somatic",
                    "channels": ch,
                    "dx": 0.0,
                    "dy": 0.0,
                    "seq": 0,
                    "t": time.perf_counter(),
                }
            except ValueError:
                return None

        # Case D: 'dx,dy' (2 values, IMU delta)
        elif len(parts) == 2 and parts[0] != "S":
            try:
                dx = float(parts[0])
                dy = float(parts[1])
                return {
                    "kind": "imu",
                    "channels": [0.0, 0.0, 0.0, 0.0],
                    "dx": dx,
                    "dy": dy,
                    "seq": 0,
                    "t": time.perf_counter(),
                }
            except ValueError:
                return None

    # Format 3: JSON / NDJSON
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                # OmniBand V2 Link protocol
                if obj.get("k") == "tele":
                    env = obj.get("env", [0.0, 0.0, 0.0, 0.0])
                    ch = [float(x) for x in env[:4]]
                    while len(ch) < 4:
                        ch.append(0.0)
                    return {
                        "kind": "somatic",
                        "channels": ch,
                        "dx": float(obj.get("dx", 0.0)),
                        "dy": float(obj.get("dy", 0.0)),
                        "seq": int(obj.get("seq", 0)),
                        "obj": obj,
                        "t": time.perf_counter(),
                    }
                elif obj.get("k") == "evt":
                    return {
                        "kind": "gesture",
                        "gesture": str(obj.get("g", "")),
                        "channels": [0.0, 0.0, 0.0, 0.0],
                        "dx": 0.0,
                        "dy": 0.0,
                        "seq": int(obj.get("seq", 0)),
                        "obj": obj,
                        "t": time.perf_counter(),
                    }
                # Generic JSON payloads
                channels = obj.get("p") or obj.get("channels") or [0.0, 0.0, 0.0, 0.0]
                ch = [float(x) for x in channels[:4]]
                while len(ch) < 4:
                    ch.append(0.0)
                dx = float(obj.get("dx", 0.0))
                dy = float(obj.get("dy", 0.0))
                seq = int(obj.get("seq", 0))
                return {
                    "kind": "somatic",
                    "channels": ch,
                    "dx": dx,
                    "dy": dy,
                    "seq": seq,
                    "obj": obj,
                    "t": time.perf_counter(),
                }
        except Exception:
            return None

    # Format 4: Plain text triggers
    upper_s = s.upper()
    valid_triggers = {"CLICK", "LEFT", "RIGHT", "SCROLL", "CH1", "CH2", "CH3", "CH4"}
    if upper_s in valid_triggers:
        return {
            "kind": "gesture",
            "gesture": upper_s,
            "channels": [0.0, 0.0, 0.0, 0.0],
            "dx": 0.0,
            "dy": 0.0,
            "seq": 0,
            "t": time.perf_counter(),
        }

    return None


def parse_ble_packet(data_str: str) -> Optional[dict]:
    """
    Backwards-compatible wrapper matching original test signature.
    Can be compact CSV: 'S,ch1,ch2,ch3,ch4,seq'
    or NDJSON line: '{"k":"tele", ...}'
    """
    s = data_str.strip()
    if not s:
        return None

    if s.startswith("S,"):
        parts = s.split(",")
        if len(parts) >= 6:
            try:
                ch1 = float(parts[1])
                ch2 = float(parts[2])
                ch3 = float(parts[3])
                ch4 = float(parts[4])
                seq = int(parts[5])
                return {
                    "kind": "somatic",
                    "channels": [ch1, ch2, ch3, ch4],
                    "seq": seq,
                    "t": time.perf_counter(),
                }
            except (ValueError, IndexError):
                return None
    elif s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return {"kind": "ndjson", "obj": obj, "t": time.perf_counter()}
        except Exception:
            return None

    return parse_hardware_payload(data_str)


def scan_usb_serial_ports() -> List[Tuple[str, str, int]]:
    """
    Scans all available COM ports on the system.
    Returns list of tuples: (port_device, description, priority_score)
    sorted by priority_score descending.
    """
    if not SERIAL_AVAILABLE or serial is None:
        return []

    results = []
    try:
        ports = list(serial.tools.list_ports.comports())
        for p in ports:
            port_name = p.device
            desc = p.description or ""
            mfg = getattr(p, "manufacturer", "") or ""
            info_str = f"{desc} {mfg}".lower()

            score = 10
            for kw in USB_PRIORITY_KEYWORDS:
                if kw in info_str:
                    score = 100
                    break

            results.append((port_name, desc, score))
    except Exception as e:
        log.warning("Serial port scan error: %s", e)

    results.sort(key=lambda item: (-item[2], item[0]))
    return results


class SerialTransportWorker:
    """Manages dedicated background reading thread for USB Serial COM ports."""

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        on_payload: Optional[Callable[[dict], None]] = None,
        on_status: Optional[Callable[[bool, str], None]] = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.on_payload = on_payload
        self.on_status = on_status

        self._running = False
        self._connected = False
        self._ser: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._active_port: str = ""

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ser is not None and self._ser.is_open

    @property
    def active_port(self) -> str:
        return self._active_port

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="OmniBandSerialWorker")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._disconnect()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _disconnect(self) -> None:
        self._connected = False
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        if self.on_status:
            self.on_status(False, "USB Serial Disconnected")

    def _run_loop(self) -> None:
        while self._running:
            if not self._connected:
                candidate_port = self.port
                if not candidate_port:
                    scanned = scan_usb_serial_ports()
                    if scanned:
                        candidate_port = scanned[0][0]

                if candidate_port and SERIAL_AVAILABLE and serial is not None:
                    try:
                        if self.on_status:
                            self.on_status(False, f"Opening USB Port {candidate_port}...")
                        ser = serial.Serial(
                            candidate_port,
                            self.baudrate,
                            timeout=0.5,
                            write_timeout=0.5,
                        )
                        self._ser = ser
                        self._active_port = candidate_port
                        self._connected = True
                        log.info("Connected to USB Serial port: %s @ %d baud", candidate_port, self.baudrate)
                        if self.on_status:
                            self.on_status(True, f"Connected: USB Serial ({candidate_port})")
                    except Exception as e:
                        log.debug("Failed opening serial port %s: %s", candidate_port, e)
                        self._connected = False
                        time.sleep(2.0)
                        continue
                else:
                    time.sleep(2.0)
                    continue

            # Read Loop
            try:
                if self._ser and self._ser.is_open:
                    line = self._ser.readline()
                    if line:
                        pkt = parse_hardware_payload(line)
                        if pkt and self.on_payload:
                            self.on_payload(pkt)
                else:
                    self._disconnect()
                    time.sleep(1.0)
            except Exception as e:
                log.warning("Serial read error on %s: %s", self._active_port, e)
                self._disconnect()
                time.sleep(1.5)


class OmniBandBleClient:
    """
    Unified Hardware Transport Manager:
    Seamlessly orchestrates Resilient Windows WinRT BLE + 1-Click USB Serial auto-connect.
    """

    def __init__(
        self,
        calibrator: Optional[HardwareCalibrator] = None,
        on_somatic_frame: Optional[Callable[[list[float], list[bool], int], None]] = None,
        on_connection_change: Optional[Callable[[bool, str], None]] = None,
        on_metrics_update: Optional[Callable[[int, float], None]] = None,
        on_imu_motion: Optional[Callable[[float, float], None]] = None,
        on_hardware_gesture: Optional[Callable[[str], None]] = None,
    ) -> None:
        if calibrator is not None:
            self.calibrator = calibrator
        else:
            from host.omniband.hardware.hardware_calibration import HardwareCalibrator
            self.calibrator = HardwareCalibrator()
        self.on_somatic_frame = on_somatic_frame
        self.on_connection_change = on_connection_change
        self.on_metrics_update = on_metrics_update
        self.on_imu_motion = on_imu_motion
        self.on_hardware_gesture = on_hardware_gesture

        self._running = False
        self._connected = False
        self._transport_type: str = "NONE"  # "BLE", "USB", or "NONE"
        self._device_address: Optional[str] = None
        self._device_name: str = ""
        self._client: Optional[BleakClient] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._active_notify_char: Optional[str] = None

        # USB Serial Worker
        self._serial_worker: Optional[SerialTransportWorker] = None

        # Instrumentation
        self._packet_count = 0
        self._last_metrics_calc_t = time.perf_counter()
        self._pkt_rate = 0.0
        self._rssi = -999
        self._last_status: str = "Hardware: Disconnected"

        # Sequence dedupe & last state
        self._last_seq = -1
        self._last_active = [False, False, False, False]
        self._lock = threading.RLock()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def transport_type(self) -> str:
        with self._lock:
            return self._transport_type

    @property
    def packet_rate(self) -> float:
        return self._pkt_rate

    @property
    def rssi(self) -> int:
        return self._rssi

    @property
    def status_message(self) -> str:
        return self._last_status

    def start(self) -> None:
        """Start background worker thread."""
        with self._lock:
            if self._running:
                return
            self._running = True

        self._thread = threading.Thread(
            target=self._run_async_loop,
            daemon=True,
            name="OmniBandTransportWorker",
        )
        self._thread.start()
        log.info("OmniBand Unified Hardware Transport worker started.")

    def stop(self) -> None:
        """Stop background worker thread and disconnect any active link."""
        with self._lock:
            self._running = False
            self._connected = False
            self._transport_type = "NONE"

        if self._serial_worker:
            self._serial_worker.stop()
            self._serial_worker = None

        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

        log.info("OmniBand Unified Hardware Transport worker stopped.")

    def trigger_scan_connect(self) -> None:
        """Trigger scan/reconnect from GUI button."""
        self.trigger_hybrid_connect()

    def trigger_hybrid_connect(self) -> None:
        """
        Hybrid 1-Click Connect:
        Step A: Auto-detect USB COM port. If openable and valid, connect immediately!
        Step B: Otherwise scan BLE for OmniBand / ESP32 with resilient WinRT retry.
        """
        if not self._running:
            self.start()

        # Try USB Serial first in background
        threading.Thread(target=self._attempt_hybrid_connect_step, daemon=True).start()

    def _attempt_hybrid_connect_step(self) -> None:
        self._update_status("Scanning USB COM ports & BLE...")
        # Step A: Check for USB Serial port
        scanned_ports = scan_usb_serial_ports()
        if scanned_ports and SERIAL_AVAILABLE:
            best_port, desc, score = scanned_ports[0]
            log.info("Attempting USB serial connection on %s (%s, score=%d)", best_port, desc, score)
            self._update_status(f"Found USB port {best_port} ({desc}), connecting...")

            try:
                # Stop existing serial worker if any
                if self._serial_worker:
                    self._serial_worker.stop()

                self._serial_worker = SerialTransportWorker(
                    port=best_port,
                    baudrate=115200,
                    on_payload=self._on_hardware_payload,
                    on_status=self._on_serial_status_change,
                )
                self._serial_worker.start()

                # Give it up to 1.5s to establish open stream
                t_end = time.perf_counter() + 1.5
                while time.perf_counter() < t_end:
                    if self._serial_worker.is_connected:
                        with self._lock:
                            self._connected = True
                            self._transport_type = "USB"
                            self._device_name = f"USB Serial ({best_port})"
                        msg = f"Connected: HARDWARE (USB {best_port})"
                        self._update_status(f"🟢 {msg}")
                        if self.on_connection_change:
                            self.on_connection_change(True, msg)
                        return
                    time.sleep(0.1)

            except Exception as e:
                log.debug("USB connect attempt failed: %s, falling back to BLE", e)

        # Step B: Fallback to BLE scanner
        self._update_status("USB not connected. Scanning BLE for OmniBand / ESP32...")
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._do_ble_connect_cycle(), self._loop)

    def _run_async_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._worker_coroutine())

    async def _worker_coroutine(self) -> None:
        """Continuous background monitor."""
        while self._running:
            if not self.is_connected:
                # If neither USB nor BLE is connected, run periodic BLE discovery
                await self._do_ble_connect_cycle()
            await asyncio.sleep(2.0)

    async def _do_ble_connect_cycle(self) -> None:
        """Execute a resilient BLE scan and connection handshake with 3x retry."""
        if self.is_connected and self._transport_type == "USB":
            return

        try:
            self._update_status("Scanning for OmniBand / ESP32 BLE...")
            devices = await BleakScanner.discover(timeout=3.0)
            target_device = None

            # Broad matching: name keywords OR advertised service UUID
            for d in devices:
                name_str = (d.name or "").lower()
                if any(kw in name_str for kw in KNOWN_DEVICE_KEYWORDS):
                    target_device = d
                    break
                if d.metadata and "uuids" in d.metadata:
                    if any(SERVICE_UUID.lower() == u.lower() for u in d.metadata["uuids"]):
                        target_device = d
                        break

            if not target_device:
                return

            rssi = getattr(target_device, "rssi", -60) or -60
            name = target_device.name or OMNIBAND_BLE_NAME
            self._update_status(f"Found {name} ({rssi} dBm), connecting GATT...")

            # 3x Fast Retry for Windows WinRT backend
            connected = False
            for attempt in range(1, 4):
                try:
                    log.info("Connecting to BLEDevice %s (%s), attempt %d/3...", name, target_device.address, attempt)
                    async with BleakClient(
                        target_device,
                        timeout=12.0,
                        disconnected_callback=self._on_ble_disconnect,
                    ) as client:
                        self._client = client
                        if client.is_connected:
                            connected = True
                            with self._lock:
                                self._connected = True
                                self._transport_type = "BLE"
                                self._device_address = target_device.address
                                self._device_name = name
                                self._rssi = rssi

                            # Dynamic Characteristic Discovery
                            subscribed_uuid = await self._subscribe_best_notify_characteristic(client)
                            self._active_notify_char = subscribed_uuid

                            msg = f"Connected to {name} via BLE ({subscribed_uuid})"
                            self._update_status(f"🟢 CONNECTED: HARDWARE (BLE)")
                            if self.on_connection_change:
                                self.on_connection_change(True, msg)

                            # Connection heartbeat loop
                            while self._running and client.is_connected:
                                await asyncio.sleep(0.5)
                                now = time.perf_counter()
                                dt = now - self._last_metrics_calc_t
                                if dt >= 1.0:
                                    self._pkt_rate = self._packet_count / dt
                                    self._packet_count = 0
                                    self._last_metrics_calc_t = now
                                    if self.on_metrics_update:
                                        self.on_metrics_update(self._rssi, self._pkt_rate)

                            if client.is_connected and subscribed_uuid:
                                try:
                                    await client.stop_notify(subscribed_uuid)
                                except Exception:
                                    pass
                            break

                except Exception as e:
                    log.debug("BLE connect attempt %d failed: %s", attempt, e)
                    if attempt < 3:
                        await asyncio.sleep(0.5)

            if not connected:
                self._update_status("BLE handshake timed out. Retrying...")

        except Exception as e:
            log.debug("BLE scan cycle error: %s", e)

    async def _subscribe_best_notify_characteristic(self, client: BleakClient) -> str:
        """
        Subscribes to primary CHAR_UUID if available; otherwise auto-discovers
        ANY characteristic that supports 'notify' or 'indicate'.
        """
        # 1. Try primary OmniBand notify characteristic
        try:
            await client.start_notify(CHAR_UUID, self._on_ble_notification)
            log.info("Subscribed to primary OmniBand CHAR_UUID: %s", CHAR_UUID)
            return CHAR_UUID
        except Exception as e:
            log.info("Primary CHAR_UUID not available (%s); scanning all services...", e)

        # 2. Iterate through all services for ANY notify or indicate characteristic
        for service in client.services:
            for char in service.characteristics:
                props = getattr(char, "properties", []) or []
                if "notify" in props or "indicate" in props:
                    try:
                        await client.start_notify(char.uuid, self._on_ble_notification)
                        log.info("Auto-subscribed to dynamic notify characteristic: %s", char.uuid)
                        return str(char.uuid)
                    except Exception as sub_err:
                        log.debug("Failed subscription on %s: %s", char.uuid, sub_err)

        raise RuntimeError("No notify or indicate characteristic found on BLE device!")

    def _on_ble_disconnect(self, client: BleakClient) -> None:
        with self._lock:
            if self._transport_type == "BLE":
                self._connected = False
                self._transport_type = "NONE"
        log.warning("OmniBand BLE disconnected.")
        self._update_status("Hardware: Disconnected")
        if self.on_connection_change:
            self.on_connection_change(False, "BLE Disconnected")

    def _on_serial_status_change(self, connected: bool, message: str) -> None:
        with self._lock:
            if connected:
                self._connected = True
                self._transport_type = "USB"
            else:
                if self._transport_type == "USB":
                    self._connected = False
                    self._transport_type = "NONE"
        self._update_status(message)
        if self.on_connection_change:
            self.on_connection_change(connected, message)

    def _update_status(self, msg: str) -> None:
        self._last_status = msg
        log.debug("Hardware transport status: %s", msg)

    def _on_ble_notification(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self._packet_count += 1
        pkt = parse_hardware_payload(data)
        if pkt:
            self._on_hardware_payload(pkt)

    def _on_hardware_payload(self, pkt: dict) -> None:
        """Centralized processing for both BLE and USB Serial packets."""
        self._packet_count += 1
        try:
            # 1. IMU Motion deltas (dx, dy)
            dx = float(pkt.get("dx", 0.0))
            dy = float(pkt.get("dy", 0.0))
            if (dx != 0.0 or dy != 0.0) and self.on_imu_motion:
                self.on_imu_motion(dx, dy)

            # 2. Somatic Channels (ch1..ch4)
            if pkt.get("kind") in ("somatic", "tele") or "channels" in pkt:
                raw_ch = pkt.get("channels", [0.0, 0.0, 0.0, 0.0])
                seq = int(pkt.get("seq", 0))

                # Apply calibration & cross-talk dominance gate
                norm_ch = self.calibrator.apply_filter(raw_ch)

                # Compute channel active flags using dynamic thresholds
                thresholds = self.calibrator.profile.thresholds
                active = [norm_ch[i] >= thresholds[i] for i in range(4)]
                self._last_active = active

                if self.on_somatic_frame:
                    self.on_somatic_frame(norm_ch, active, seq)

            elif pkt.get("kind") == "gesture":
                gesture_name = pkt.get("gesture", "")
                log.info("Hardware gesture received: %s", gesture_name)
                if getattr(self, "on_hardware_gesture", None):
                    self.on_hardware_gesture(gesture_name)

        except Exception as e:
            log.debug("Error processing hardware payload: %s", e)


HardwareTransportManager = OmniBandBleClient
