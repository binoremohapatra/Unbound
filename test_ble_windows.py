"""
test_ble_windows.py - Windows BLE connection test for Omniband ESP32
Run this AFTER uploading the firmware to verify BLE works on Windows.
No pairing from Bluetooth Settings needed - bleak connects directly via WinRT.
"""
import asyncio
from bleak import BleakClient, BleakScanner

SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
CHAR_UUID    = "beb5483e-36e1-4688-b7f5-ea07361b26a6"

async def main():
    print("Scanning for Omniband... (5 seconds)")
    devices = await BleakScanner.discover(timeout=5.0)

    target = None
    for d in devices:
        print(f"  Found: {d.name!r} @ {d.address}")
        if d.name and "mniband" in d.name:
            target = d

    if not target:
        print("\n[FAIL] Omniband not found in scan.")
        print("  -> Make sure firmware is uploaded and ESP32 is powered on.")
        return

    print(f"\n[OK] Found: {target.name} @ {target.address}")
    print("Connecting via WinRT BLE (no Bluetooth Settings pairing needed)...")

    async with BleakClient(target.address, timeout=10.0) as client:
        if client.is_connected:
            print("[OK] Connected!")
        else:
            print("[FAIL] Could not connect.")
            return

        # Read the characteristic
        val = await client.read_gatt_char(CHAR_UUID)
        print(f"[OK] Read characteristic: {bytes(val).decode('utf-8', errors='replace')!r}")

        # List all services
        print("\nServices & Characteristics:")
        for service in client.services:
            print(f"  Service: {service.uuid}")
            for char in service.characteristics:
                print(f"    Char: {char.uuid}  props={char.properties}")

        print("\n[SUCCESS] Omniband BLE is working on Windows!")

asyncio.run(main())

