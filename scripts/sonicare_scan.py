#!/usr/bin/env python3
"""Scan and enumerate GATT services on any Philips Sonicare toothbrush.

For devices using the newer protocol, this script also probes the device
for available products, ports and properties.

Usage:
  python3 sonicare_scan.py              # Auto-detect nearby Sonicare
  python3 sonicare_scan.py AA:BB:CC:DD:EE:FF  # Scan specific MAC address

Requirements:
  pip install bleak
"""

import asyncio
import struct
import sys
import warnings
from bleak import BleakClient, BleakScanner

LEGACY_PREFIX = "477ea600"
NEWER_PREFIX = "e50ba3c0"
SONICARE_NAMES = ("philips ohc", "philips sonicare")

# --- Newer protocol BLE UUIDs ---
CHAR_RX = "e50b0001-af04-4564-92ad-fef019489de6"
CHAR_RX_ACK = "e50b0002-af04-4564-92ad-fef019489de6"
CHAR_TX = "e50b0003-af04-4564-92ad-fef019489de6"
CHAR_TX_ACK = "e50b0004-af04-4564-92ad-fef019489de6"
CHAR_PROTO_CFG = "e50b0005-af04-4564-92ad-fef019489de6"
CHAR_SERVER_CFG = "e50b0006-af04-4564-92ad-fef019489de6"
CHAR_CLIENT_CFG = "e50b0007-af04-4564-92ad-fef019489de6"

# --- Newer protocol message types ---
MSG_INITIALIZE_REQ = 1
MSG_INITIALIZE_RESP = 2
MSG_GET_PROPS = 4
MSG_GENERIC_RESP = 7
MSG_CHANGE_IND_RESP = 9
MSG_GET_PRODS = 10
MSG_GET_PORTS = 11

MSG_NAMES = {
    1: "InitializeReq", 2: "InitializeResp", 3: "PutProps", 4: "GetProps",
    5: "Subscribe", 6: "Unsubscribe", 7: "GenericResp", 8: "ChangeIndReq",
    9: "ChangeIndResp", 10: "GetProds", 11: "GetPorts", 12: "AddProps",
    13: "DelProps", 15: "RawRequest",
}

STATUS_NAMES = {
    0: "NoError", 1: "NotUnderstood", 2: "OutOfMemory", 3: "NoSuchPort",
    4: "NotImplemented", 5: "VersionNotSupported", 6: "NoSuchProperty",
    7: "NoSuchOperation", 8: "NoSuchProduct", 9: "PropertyAlreadyExists",
    10: "NoSuchMethod", 11: "WrongParameters", 12: "InvalidParameter",
    13: "NotSubscribed", 14: "ProtocolViolation", 255: "Unknown",
}


# =====================================================================
# Newer protocol probe
# =====================================================================

class NewerProtocolProbe:
    """Probe a Sonicare device using the newer (e50b) BLE protocol."""

    def __init__(self, client: BleakClient):
        self.client = client
        self.seq_num = 0
        self.rx_buffer = bytearray()
        self.response_event = asyncio.Event()
        self.response_data = b""
        self.server_cfg_event = asyncio.Event()

    # --- ByteStreaming layer ---

    def _next_header(self, channel: int = 0, start: bool = True) -> int:
        hdr = self.seq_num & 0x3F
        if start:
            hdr |= 0x40
        if channel:
            hdr |= 0x80
        self.seq_num = (self.seq_num + 1) % 64
        return hdr

    async def _send_raw(self, data: bytes):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            mtu = self.client.mtu_size
        chunk_size = max(mtu - 3, 17)

        offset = 0
        first = True
        while offset < len(data):
            chunk = data[offset:offset + chunk_size]
            hdr = self._next_header(start=first)
            await self.client.write_gatt_char(CHAR_RX, bytes([hdr]) + chunk, response=False)
            offset += len(chunk)
            first = False

    # --- Condor frame layer ---

    async def _send_msg(self, msg_type: int, payload: bytes = b""):
        frame = b"\xFF\xFE" + bytes([msg_type]) + struct.pack("<H", len(payload)) + payload
        name = MSG_NAMES.get(msg_type, f"Type{msg_type}")
        print(f"  >>> {name} ({len(frame)}B): {frame.hex()}")
        await self._send_raw(frame)

    async def _send_and_wait(self, msg_type: int, payload: bytes = b"", timeout: float = 5.0) -> bytes:
        self.response_event.clear()
        self.response_data = b""
        await self._send_msg(msg_type, payload)
        try:
            await asyncio.wait_for(self.response_event.wait(), timeout)
        except asyncio.TimeoutError:
            print("      !!! Timeout waiting for response")
            return b""
        return self.response_data

    # --- Notification handlers ---

    def _on_tx(self, _sender, data: bytearray):
        if len(data) < 1:
            return
        hdr = data[0]
        start = (hdr >> 6) & 1
        payload = bytes(data[1:])

        if start:
            self.rx_buffer = bytearray(payload)
        else:
            self.rx_buffer.extend(payload)

        buf = bytes(self.rx_buffer)
        if len(buf) >= 5 and buf[0] == 0xFF and buf[1] == 0xFE:
            msg_type = buf[2]
            payload_len = struct.unpack("<H", buf[3:5])[0]
            if len(buf) >= 5 + payload_len:
                self._handle_message(msg_type, buf[5:5 + payload_len])
                self.rx_buffer = bytearray()

    def _on_rx_ack(self, _sender, data: bytearray):
        pass  # silently consume

    def _on_server_cfg(self, _sender, data: bytearray):
        print(f"  <<< Server Config: {bytes(data).hex()}")
        self.server_cfg_event.set()

    def _handle_message(self, msg_type: int, payload: bytes):
        name = MSG_NAMES.get(msg_type, f"Type{msg_type}")
        print(f"  <<< {name}: {payload.hex()}")

        if msg_type == MSG_GENERIC_RESP and len(payload) >= 1:
            status = payload[0]
            status_name = STATUS_NAMES.get(status, f"Unknown({status})")
            body = payload[1:]
            print(f"      Status: {status_name} ({status})")
            if body:
                print(f"      Body: {body.hex()}")
                try:
                    text = body.decode("utf-8", errors="replace")
                    if text.isprintable():
                        print(f"      Text: {text}")
                except Exception:
                    pass

        elif msg_type == MSG_INITIALIZE_RESP and len(payload) >= 1:
            status = payload[0]
            status_name = STATUS_NAMES.get(status, f"Unknown({status})")
            print(f"      Status: {status_name} ({status})")
            if len(payload) > 1:
                print(f"      Extra: {payload[1:].hex()}")

        elif len(payload) > 0:
            try:
                text = payload.decode("utf-8", errors="replace")
                if text.isprintable():
                    print(f"      Text: {text}")
            except Exception:
                pass

        self.response_data = payload
        self.response_event.set()

    # --- High-level probe sequence ---

    async def run(self):
        """Run the full newer-protocol probe."""
        print("\n--- Newer Protocol Probe ---\n")

        # Read protocol config
        cfg = await self.client.read_gatt_char(CHAR_PROTO_CFG)
        print(f"  Protocol Config: {cfg.hex()}")
        if len(cfg) >= 3:
            print(f"    Version={cfg[0]}, InBuf={cfg[1]}, OutBuf={cfg[2]}")

        # Subscribe to notifications
        await self.client.start_notify(CHAR_TX, self._on_tx)
        await self.client.start_notify(CHAR_RX_ACK, self._on_rx_ack)
        await self.client.start_notify(CHAR_SERVER_CFG, self._on_server_cfg)

        # Protocol negotiation
        print("\n  [1/5] Protocol negotiation...")
        self.server_cfg_event.clear()
        await self.client.write_gatt_char(CHAR_CLIENT_CFG, bytes([0x03, 0x04]), response=False)
        try:
            await asyncio.wait_for(self.server_cfg_event.wait(), 5.0)
        except asyncio.TimeoutError:
            print("      !!! No server config response — continuing anyway")
        await asyncio.sleep(0.3)

        # Initialize
        print("\n  [2/5] Initialize...")
        await self._send_and_wait(MSG_INITIALIZE_REQ)
        await asyncio.sleep(0.3)

        # Get products
        print("\n  [3/5] Get products...")
        await self._send_and_wait(MSG_GET_PRODS)
        await asyncio.sleep(0.3)

        # Get ports
        print("\n  [4/5] Get ports...")
        for prod_id in ["0", "1"]:
            payload = prod_id.encode() + b"\x00"
            await self._send_and_wait(MSG_GET_PORTS, payload)
            await asyncio.sleep(0.3)

        # Get properties for known ports
        print("\n  [5/5] Get properties...")
        known_ports = [
            "sonicare", "battery_service", "sensor_data", "brush_head",
            "routine_status", "storage", "extended", "device_diagnostics",
        ]
        for port in known_ports:
            for prod_id in ["0", "1"]:
                payload = prod_id.encode() + b"\x00" + port.encode() + b"\x00"
                await self._send_and_wait(MSG_GET_PROPS, payload, timeout=3.0)
                await asyncio.sleep(0.2)

        print("\n--- Probe complete ---")


# =====================================================================
# GATT scan (works for both protocols)
# =====================================================================

async def find_sonicare():
    """Scan for any Sonicare toothbrush nearby."""
    print("Scanning for Sonicare devices (20s)...")
    print("Tip: Wake up the brush by pressing the power button or placing it on the charger.\n")

    devices = await BleakScanner.discover(timeout=20, return_adv=True)
    found = []
    for _addr, (device, adv) in devices.items():
        if device.name and device.name.lower().startswith(SONICARE_NAMES):
            found.append((device, adv))

    if not found:
        print("No Sonicare devices found.")
        print("Make sure:")
        print("  - Bluetooth is enabled on this machine")
        print("  - The brush is awake (press button or place on charger)")
        print("  - You are close enough to the brush")
        return None

    if len(found) == 1:
        device, adv = found[0]
        print(f"Found: {device.name} ({device.address}), RSSI={adv.rssi}")
        return device.address

    print(f"Found {len(found)} Sonicare devices:")
    for i, (device, adv) in enumerate(found):
        print(f"  [{i+1}] {device.name} ({device.address}), RSSI={adv.rssi}")
    print()
    choice = input(f"Select device [1-{len(found)}]: ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(found):
            return found[idx][0].address
    except ValueError:
        pass
    print("Invalid selection.")
    return None


async def scan_device(address: str):
    """Connect to a Sonicare and dump all GATT services."""
    print(f"\nConnecting to {address} ...")
    async with BleakClient(address, timeout=30) as client:
        print(f"Connected: {client.is_connected}")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            mtu = client.mtu_size
        print(f"MTU: {mtu}\n")

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
        print(f"Total services: {sum(1 for _ in client.services)}")
        print("=" * 60)

        # Probe newer protocol if detected
        if has_newer:
            probe = NewerProtocolProbe(client)
            await probe.run()


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
