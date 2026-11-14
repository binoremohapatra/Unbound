"""ble_client.py - Re-exports from host.omniband.ble_transport for backwards compatibility."""
from host.omniband.ble_transport import (
    CHAR_UUID,
    HardwareTransportManager,
    KNOWN_DEVICE_KEYWORDS,
    OMNIBAND_BLE_NAME,
    OmniBandBleClient,
    SERVICE_UUID,
    SerialTransportWorker,
    USB_PRIORITY_KEYWORDS,
    parse_ble_packet,
    parse_hardware_payload,
    scan_usb_serial_ports,
)

__all__ = [
    "OMNIBAND_BLE_NAME",
    "SERVICE_UUID",
    "CHAR_UUID",
    "KNOWN_DEVICE_KEYWORDS",
    "USB_PRIORITY_KEYWORDS",
    "parse_ble_packet",
    "parse_hardware_payload",
    "scan_usb_serial_ports",
    "SerialTransportWorker",
    "OmniBandBleClient",
    "HardwareTransportManager",
]
