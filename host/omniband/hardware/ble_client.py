"""ble_client.py - Native Windows BLE Client for OmniBand V2 Headband.

Connects to OmniBand-BLE GATT server:
  SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
  CHAR_UUID    = "beb5483e-36e1-4688-b7f5-ea07361b26a6"

Runs in an asynchronous background thread without blocking the Qt GUI.
Provides:
  - Background auto-discovery and reconnection.
  - Notification parsing: compact somatic CSV ("S,ch1,ch2,ch3,ch4,seq") and NDJSON.
  - Real-time RSSI and packet-rate instrumentation.
  - Integration with HardwareCalibrator cross-talk suppression and auto-gain.
  - Thread-safe callbacks for live GUI feeds and input routing.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

from .hardware_calibration import HardwareCalibrator

log = logging.getLogger("omniband.ble_client")

OMNIBAND_BLE_NAME = "OmniBand-BLE"
SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
CHAR_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a6"


def parse_ble_packet(data_str: str) -> Optional[dict]:
    """
    Parse a BLE notification packet.
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
            import json
            obj = json.loads(s)
            if isinstance(obj, dict):
                return {"kind": "ndjson", "obj": obj, "t": time.perf_counter()}
        except Exception:
            return None
    return None


class OmniBandBleClient:
    """Manages BLE background lifecycle, reconnection, and dispatch."""

    def __init__(
        self,
        calibrator: Optional[HardwareCalibrator] = None,
        on_somatic_frame: Optional[Callable[[list[float], list[bool], int], None]] = None,
        on_connection_change: Optional[Callable[[bool, str], None]] = None,
        on_metrics_update: Optional[Callable[[int, float], None]] = None,
    ) -> None:
        self.calibrator = calibrator or HardwareCalibrator()
        self.on_somatic_frame = on_somatic_frame
        self.on_connection_change = on_connection_change
        self.on_metrics_update = on_metrics_update

        self._running = False
        self._connected = False
        self._device_address: Optional[str] = None
        self._device_name: str = ""
        self._client: Optional[BleakClient] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Instrumentation
        self._packet_count = 0
        self._last_metrics_calc_t = time.perf_counter()
        self._pkt_rate = 0.0
        self._rssi = -999

        # Sequence dedupe & last state
        self._last_seq = -1
        self._last_active = [False, False, False, False]

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def packet_rate(self) -> float:
        return self._pkt_rate

    @property
    def rssi(self) -> int:
        return self._rssi

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_async_loop, daemon=True, name="OmniBandBleWorker")
        self._thread.start()
        log.info("OmniBand BLE worker started")

    def stop(self) -> None:
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._connected = False
        log.info("OmniBand BLE worker stopped")

    def trigger_scan_connect(self) -> None:
        """Trigger immediate scan/reconnect from GUI button."""
        if not self._running:
            self.start()

    def _run_async_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._worker_coroutine())

    async def _worker_coroutine(self) -> None:
        while self._running:
            if not self._connected:
                log.info("Scanning for %s (UUID: %s)...", OMNIBAND_BLE_NAME, SERVICE_UUID)
                try:
                    # Discover device by name or service UUID
                    device = None
                    devices = await BleakScanner.discover(timeout=3.0)
                    for d in devices:
                        if d.name and (OMNIBAND_BLE_NAME.lower() in d.name.lower() or "omniband" in d.name.lower()):
                            device = d
                            break
                        if d.metadata and "uuids" in d.metadata:
                            if any(SERVICE_UUID.lower() == u.lower() for u in d.metadata["uuids"]):
                                device = d
                                break

                    if device:
                        self._device_address = device.address
                        self._device_name = device.name or OMNIBAND_BLE_NAME
                        self._rssi = getattr(device, "rssi", -60) or -60
                        log.info("Found OmniBand BLE device: %s (%s, RSSI: %d dBm)", self._device_name, self._device_address, self._rssi)

                        async with BleakClient(device, disconnected_callback=self._on_ble_disconnect) as client:
                            self._client = client
                            if client.is_connected:
                                self._connected = True
                                log.info("Connected to OmniBand BLE server!")
                                if self.on_connection_change:
                                    self.on_connection_change(True, f"Connected to {self._device_name}")

                                await client.start_notify(CHAR_UUID, self._on_notification)

                                # Connection maintenance loop
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

                                if client.is_connected:
                                    await client.stop_notify(CHAR_UUID)

                except Exception as e:
                    log.debug("BLE scan/connect error (retrying): %s", e)
                    self._connected = False
                    if self.on_connection_change:
                        self.on_connection_change(False, str(e))

            await asyncio.sleep(1.5)

    def _on_ble_disconnect(self, client: BleakClient) -> None:
        self._connected = False
        log.warning("OmniBand BLE disconnected.")
        if self.on_connection_change:
            self.on_connection_change(False, "Disconnected")

    def _on_notification(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        self._packet_count += 1
        try:
            text = data.decode("utf-8", errors="replace")
            pkt = parse_ble_packet(text)
            if not pkt:
                return

            if pkt["kind"] == "somatic":
                raw_ch = pkt["channels"]
                seq = pkt["seq"]

                # Apply calibration & cross-talk dominance gate
                norm_ch = self.calibrator.apply_filter(raw_ch)

                # Compute channel active flags using dynamic thresholds
                thresholds = self.calibrator.profile.thresholds
                active = [norm_ch[i] >= thresholds[i] for i in range(4)]
                self._last_active = active

                if self.on_somatic_frame:
                    self.on_somatic_frame(norm_ch, active, seq)

            elif pkt["kind"] == "ndjson":
                # Forward to protocol handler if needed
                pass

        except Exception as e:
            log.debug("Error processing BLE notification: %s", e)
