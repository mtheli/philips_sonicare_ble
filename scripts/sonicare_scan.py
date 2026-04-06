#!/usr/bin/env python3
"""Scan and enumerate GATT services on any Philips Sonicare toothbrush.

Usage:
  python3 sonicare_scan.py              # Auto-detect nearby Sonicare
  python3 sonicare_scan.py AA:BB:CC:DD:EE:FF  # Scan specific MAC address

Requirements:
  pip install bleak
"""

import asyncio
import sys
from bleak import BleakClient, BleakScanner

LEGACY_PREFIX = "477ea600"
NEWER_PREFIX = "e50ba3c0"
SONICARE_NAMES = ("philips ohc", "philips sonicare")


async def find_sonicare():
    """Scan for any Sonicare toothbrush nearby."""
    print("Scanning for Sonicare devices (20s)...")
    print("Tip: Wake up the brush by pressing the power button or placing it on the charger.\n")

    devices = await BleakScanner.discover(timeout=20)
    found = []
    for d in devices:
        if d.name and d.name.lower().startswith(SONICARE_NAMES):
            found.append(d)

    if not found:
        print("No Sonicare devices found.")
        print("Make sure:")
        print("  - Bluetooth is enabled on this machine")
        print("  - The brush is awake (press button or place on charger)")
        print("  - You are close enough to the brush")
        return None

    if len(found) == 1:
        d = found[0]
        print(f"Found: {d.name} ({d.address}), RSSI={d.rssi}")
        return d.address

    print(f"Found {len(found)} Sonicare devices:")
    for i, d in enumerate(found):
        print(f"  [{i+1}] {d.name} ({d.address}), RSSI={d.rssi}")
    print()
    choice = input(f"Select device [1-{len(found)}]: ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(found):
            return found[idx].address
    except ValueError:
        pass
    print("Invalid selection.")
    return None


async def scan_device(address: str):
    """Connect to a Sonicare and dump all GATT services."""
    print(f"\nConnecting to {address} ...")
    async with BleakClient(address, timeout=30) as client:
        print(f"Connected: {client.is_connected}")
        print(f"MTU: {client.mtu_size}\n")

        has_legacy = False
        has_newer = False

        for service in client.services:
            if service.uuid.startswith(LEGACY_PREFIX):
                has_legacy = True
            if service.uuid.startswith(NEWER_PREFIX):
                has_newer = True

            print(f"Service: {service.uuid}")
            if service.description and service.description != service.uuid:
                print(f"  Description: {service.description}")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"  Char: {char.uuid}  [{props}]  handle=0x{char.handle:04X}")
                if "read" in char.properties:
                    try:
                        value = await client.read_gatt_char(char)
                        hex_str = value.hex()
                        try:
                            text = value.decode("utf-8")
                            if text.isprintable():
                                print(f"    Value: {hex_str} = \"{text}\"")
                            else:
                                print(f"    Value: {hex_str}")
                        except (UnicodeDecodeError, ValueError):
                            print(f"    Value: {hex_str}")
                    except Exception as e:
                        print(f"    Read error: {e}")
                for desc in char.descriptors:
                    print(f"    Desc: {desc.uuid}  handle=0x{desc.handle:04X}")
            print()

        print("=" * 60)
        if has_legacy and has_newer:
            protocol = "Both Legacy + Newer"
        elif has_legacy:
            protocol = "Legacy (supported by philips_sonicare_ble)"
        elif has_newer:
            protocol = "Newer (not yet supported)"
        else:
            protocol = "Unknown"
        print(f"Protocol: {protocol}")
        print(f"Total services: {len(client.services)}")
        print("=" * 60)


async def main():
    if len(sys.argv) > 1:
        address = sys.argv[1]
        print(f"Using provided address: {address}")
    else:
        address = await find_sonicare()
        if not address:
            sys.exit(1)

    await scan_device(address)


asyncio.run(main())
