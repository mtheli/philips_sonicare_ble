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
import argparse
import sys
import warnings
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakCharacteristicNotFoundError

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
# Known characteristic registry (mirrors const.py from the integration)
# =====================================================================

_HANDLE_STATES = {
    0: "off", 1: "standby", 2: "run", 3: "charge",
    4: "shutdown", 6: "validate", 7: "background",
}
_BRUSHING_MODES = {
    0: "clean", 1: "white_plus", 2: "gum_health",
    3: "tongue_care", 4: "deep_clean_plus", 5: "sensitive",
}
_BRUSHING_STATES = {
    0: "off", 1: "on", 2: "pause", 3: "session_complete", 4: "session_aborted",
}
_INTENSITIES = {0: "low", 1: "medium", 2: "high"}
_BRUSHHEAD_TYPES = {
    0: "adaptive_clean", 1: "adaptive_white", 2: "adaptive_gums",
    3: "tongue_clean", 4: "premium_all_in_one", 5: "sensitive", 6: "non_rfid",
}


def _dec_enum(mapping):
    return lambda d: mapping.get(d[0], f"unknown({d[0]})") if d else "?"


def _dec_u16(d):
    return f"{struct.unpack('<H', d[:2])[0]}s" if len(d) >= 2 else d.hex()


def _dec_u32(d):
    return str(struct.unpack("<I", d[:4])[0]) if len(d) >= 4 else d.hex()


def _dec_str(d):
    return d.decode("utf-8", errors="replace")


def _dec_pct(d):
    return f"{d[0]}%" if d else "?"


def _dec_sensor_enable(d):
    if not d:
        return "?"
    v = d[0]
    parts = []
    if v & 1: parts.append("pressure")
    if v & 2: parts.append("temperature")
    if v & 4: parts.append("gyroscope")
    return f"0x{v:02X} → [{', '.join(parts) or 'none'}]"


# UUID → (display_name, category, decode_fn_or_None)
# category: "device_info" | "standard_ble" | "legacy" | "newer_proto"
KNOWN_CHARS = {
    # Standard BLE — Generic Access / Generic Attribute / common
    "00002a00-0000-1000-8000-00805f9b34fb": ("Device Name",             "standard_ble", _dec_str),
    "00002a01-0000-1000-8000-00805f9b34fb": ("Appearance",              "standard_ble", None),
    "00002a04-0000-1000-8000-00805f9b34fb": ("Preferred Conn Params",   "standard_ble", None),
    "00002a05-0000-1000-8000-00805f9b34fb": ("Service Changed",         "standard_ble", None),
    "00002a23-0000-1000-8000-00805f9b34fb": ("System ID",               "standard_ble", None),
    "00002a2a-0000-1000-8000-00805f9b34fb": ("IEEE Regulatory Cert",    "standard_ble", None),
    "00002a50-0000-1000-8000-00805f9b34fb": ("PnP ID",                  "standard_ble", None),
    "00002aa6-0000-1000-8000-00805f9b34fb": ("Central Addr Resolution", "standard_ble", None),
    # Standard BLE — Device Information / Battery
    "00002a19-0000-1000-8000-00805f9b34fb": ("Battery Level",           "device_info", _dec_pct),
    "00002a24-0000-1000-8000-00805f9b34fb": ("Model Number",            "device_info", _dec_str),
    "00002a25-0000-1000-8000-00805f9b34fb": ("Serial Number",           "device_info", _dec_str),
    "00002a26-0000-1000-8000-00805f9b34fb": ("Firmware Revision",       "device_info", _dec_str),
    "00002a27-0000-1000-8000-00805f9b34fb": ("Hardware Revision",       "device_info", _dec_str),
    "00002a28-0000-1000-8000-00805f9b34fb": ("Software Revision",       "device_info", _dec_str),
    "00002a29-0000-1000-8000-00805f9b34fb": ("Manufacturer Name",       "device_info", _dec_str),
    # Sonicare Service (0x0001) — legacy protocol
    "477ea600-a260-11e4-ae37-0002a5d54010": ("Handle State",            "legacy", _dec_enum(_HANDLE_STATES)),
    "477ea600-a260-11e4-ae37-0002a5d54020": ("Available Routines",      "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54022": ("Available Routine IDs",   "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54030": ("Unknown 4030",            "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54040": ("Motor Runtime",           "legacy", _dec_u32),
    "477ea600-a260-11e4-ae37-0002a5d54050": ("Handle Time",             "legacy", _dec_u32),
    # Routine Service (0x0002) — legacy protocol
    "477ea600-a260-11e4-ae37-0002a5d54070": ("Session ID",              "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54080": ("Brushing Mode",           "legacy", _dec_enum(_BRUSHING_MODES)),
    "477ea600-a260-11e4-ae37-0002a5d54082": ("Brushing State",          "legacy", _dec_enum(_BRUSHING_STATES)),
    "477ea600-a260-11e4-ae37-0002a5d54090": ("Brushing Time",           "legacy", _dec_u16),
    "477ea600-a260-11e4-ae37-0002a5d54091": ("Routine Length",          "legacy", _dec_u16),
    "477ea600-a260-11e4-ae37-0002a5d540a0": ("Unknown 40A0",            "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d540b0": ("Intensity",               "legacy", _dec_enum(_INTENSITIES)),
    "477ea600-a260-11e4-ae37-0002a5d540c0": ("Unknown 40C0",            "legacy", None),
    # Storage Service (0x0004) — legacy protocol
    "477ea600-a260-11e4-ae37-0002a5d540d0": ("Latest Session ID",       "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d540d2": ("Session Count",           "legacy", _dec_u16),
    "477ea600-a260-11e4-ae37-0002a5d540d5": ("Session Type",            "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d540e0": ("Active Session ID",       "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54100": ("Session Data",            "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54110": ("Session Action",          "legacy", None),
    # Sensor Service (0x0005) — legacy protocol
    "477ea600-a260-11e4-ae37-0002a5d54120": ("Sensor Enable",           "legacy", _dec_sensor_enable),
    "477ea600-a260-11e4-ae37-0002a5d54130": ("Sensor Data",             "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54140": ("Sensor Unknown 4140",     "legacy", None),
    # Brush Head Service (0x0006) — legacy protocol
    "477ea600-a260-11e4-ae37-0002a5d54210": ("Brushhead NFC Version",   "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54220": ("Brushhead Type",          "legacy", _dec_enum(_BRUSHHEAD_TYPES)),
    "477ea600-a260-11e4-ae37-0002a5d54230": ("Brushhead Serial",        "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54240": ("Brushhead Date",          "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54250": ("Brushhead Unknown 4250",  "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54254": ("Brushhead Unknown 4254",  "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54260": ("Brushhead Unknown 4260",  "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54270": ("Brushhead Unknown 4270",  "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54280": ("Brushhead Lifetime Limit","legacy", _dec_u32),
    "477ea600-a260-11e4-ae37-0002a5d54290": ("Brushhead Lifetime Usage","legacy", _dec_u32),
    "477ea600-a260-11e4-ae37-0002a5d542a0": ("Brushhead Unknown 42A0",  "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d542a2": ("Brushhead Unknown 42A2",  "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d542a4": ("Brushhead Unknown 42A4",  "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d542a6": ("Brushhead Unknown 42A6",  "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d542b0": ("Brushhead Payload",       "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d542c0": ("Brushhead Ring ID",       "legacy", None),
    # Diagnostic Service (0x0007) — legacy protocol
    "477ea600-a260-11e4-ae37-0002a5d54310": ("Error Persistent",        "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54320": ("Error Volatile",          "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54330": ("Diag Unknown 4330",       "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54360": ("Diag Unknown 4360",       "legacy", None),
    # Extended Service (0x0008) — legacy protocol
    "477ea600-a260-11e4-ae37-0002a5d54410": ("Extended Unknown 4410",   "legacy", None),
    "477ea600-a260-11e4-ae37-0002a5d54420": ("Settings",                "legacy", None),
    # Newer protocol transport
    "e50b0001-af04-4564-92ad-fef019489de6": ("Proto RX",                "newer_proto", None),
    "e50b0002-af04-4564-92ad-fef019489de6": ("Proto RX ACK",            "newer_proto", None),
    "e50b0003-af04-4564-92ad-fef019489de6": ("Proto TX",                "newer_proto", None),
    "e50b0004-af04-4564-92ad-fef019489de6": ("Proto TX ACK",            "newer_proto", None),
    "e50b0005-af04-4564-92ad-fef019489de6": ("Proto Config",            "newer_proto", None),
    "e50b0006-af04-4564-92ad-fef019489de6": ("Proto Server Config",     "newer_proto", None),
    "e50b0007-af04-4564-92ad-fef019489de6": ("Proto Client Config",     "newer_proto", None),
}

# Model-based feature support (mirrors const.py)
_MODE_WRITE_MODELS = ("HX999", "HX9996", "HX74")
_SETTINGS_WRITE_MODELS = ("HX999", "HX9996")


def _supports_mode_write(model: str) -> bool:
    upper = (model or "").upper()
    return any(upper.startswith(p) for p in _MODE_WRITE_MODELS)


def _supports_settings_write(model: str) -> bool:
    upper = (model or "").upper()
    return any(upper.startswith(p) for p in _SETTINGS_WRITE_MODELS)


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

        # Read protocol config (optional — not present on all firmware versions).
        # When absent, the app falls back to the CLIENT_CFG / SERVER_CFG negotiation path below.
        try:
            cfg = await self.client.read_gatt_char(CHAR_PROTO_CFG)
            print(f"  Protocol Config: {cfg.hex()}")
            if len(cfg) >= 3:
                print(f"    Version={cfg[0]}, InBuf={cfg[1]}, OutBuf={cfg[2]}")
        except BleakCharacteristicNotFoundError:
            print("  Protocol Config: characteristic absent — using negotiation fallback")

        # Subscription order mirrors the Sonicare app: SERVER_CFG first, then
        # CLIENT_CFG write, await the server response, then TX + RX ACK.
        # Subscribing to TX before negotiation has been observed to make the
        # brush drop the connection on some firmwares (e.g. HX742X 1.8.20.0).

        print("\n  [1/5] Protocol negotiation...")
        self.server_cfg_event.clear()
        try:
            await asyncio.wait_for(
                self.client.start_notify(CHAR_SERVER_CFG, self._on_server_cfg),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            print("      !!! Timeout subscribing to Server Config — aborting probe")
            return
        except Exception as e:
            print(f"      !!! Error subscribing to Server Config: {e} — aborting probe")
            return
        if not self.client.is_connected:
            print("      !!! Disconnected during Server Config subscribe — aborting probe")
            return

        try:
            await self.client.write_gatt_char(
                CHAR_CLIENT_CFG, bytes([0x03, 0x04]), response=False
            )
        except Exception as e:
            print(f"      !!! Write to CLIENT_CFG failed: {e} — aborting probe")
            return

        try:
            await asyncio.wait_for(self.server_cfg_event.wait(), 5.0)
        except asyncio.TimeoutError:
            print("      !!! No server config response — continuing anyway")
        if not self.client.is_connected:
            print("      !!! Disconnected waiting for Server Config response — aborting probe")
            return
        await asyncio.sleep(0.3)

        # Now that the transport is negotiated, subscribe to data channels.
        for uuid, cb, label in [
            (CHAR_TX, self._on_tx, "TX"),
            (CHAR_RX_ACK, self._on_rx_ack, "RX ACK"),
        ]:
            try:
                await asyncio.wait_for(self.client.start_notify(uuid, cb), timeout=5.0)
            except asyncio.TimeoutError:
                print(f"  !!! Timeout subscribing to {label} ({uuid}) — aborting probe")
                return
            except Exception as e:
                print(f"  !!! Error subscribing to {label} ({uuid}): {e} — aborting probe")
                return
            if not self.client.is_connected:
                print(f"  !!! Device disconnected during subscribe to {label} — aborting probe")
                return

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


def _adv_summary(adv) -> str:
    """Return a short inline string of notable advertisement data."""
    parts = []
    if adv and adv.manufacturer_data:
        for company_id, data in adv.manufacturer_data.items():
            vendor = "Philips" if company_id == 477 else f"0x{company_id:04X}"
            try:
                text = data.decode("utf-8")
                payload = f'"{text}"' if text.isprintable() and text.strip() else data.hex()
            except (UnicodeDecodeError, ValueError):
                payload = data.hex()
            parts.append(f"{vendor}:{payload}")
    return f"  [{', '.join(parts)}]" if parts else ""


async def find_sonicare():
    """Scan for any Sonicare toothbrush nearby. Returns (address, adv)."""
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
        return None, None

    if len(found) == 1:
        device, adv = found[0]
        print(f"Found: {device.name} ({device.address}), RSSI={adv.rssi}{_adv_summary(adv)}")
        return device.address, adv

    print(f"Found {len(found)} Sonicare devices:")
    for i, (device, adv) in enumerate(found):
        print(f"  [{i+1}] {device.name} ({device.address}), RSSI={adv.rssi}{_adv_summary(adv)}")
    print()
    choice = input(f"Select device [1-{len(found)}]: ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(found):
            return found[idx][0].address, found[idx][1]
    except ValueError:
        pass
    print("Invalid selection.")
    return None, None


async def _negotiate_mtu(client: BleakClient, requested: int | None) -> None:
    """Trigger an ATT MTU exchange — BlueZ does not do this automatically."""
    if requested is not None:
        # Explicit override: assume the caller knows what the link supports.
        # Used when the auto-exchange is unavailable or for debugging.
        try:
            client._mtu_size = requested
            print(f"MTU forced to {requested} (no exchange)")
            return
        except Exception as e:
            print(f"MTU force failed: {e} — falling back to auto-exchange")

    acquire = getattr(client, "_acquire_mtu", None)
    if acquire is None:
        return
    try:
        await acquire()
    except Exception as e:
        print(f"MTU auto-exchange failed: {e}")


async def scan_device(address: str, mtu: int | None = None, pair: bool = False):
    """Connect to a Sonicare and dump all GATT services."""
    print(f"\nConnecting to {address} ...")
    async with BleakClient(address, timeout=30) as client:
        print(f"Connected: {client.is_connected}")

        if pair:
            print("Pairing (Just Works)...")
            try:
                result = await client.pair()
                print(f"Paired: {result}")
            except Exception as e:
                print(f"Pairing failed: {e} — continuing without encryption")

        await _negotiate_mtu(client, mtu)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            mtu_actual = client.mtu_size
        print(f"MTU: {mtu_actual}\n")

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
                char_info = KNOWN_CHARS.get(char.uuid.lower())
                char_label = f"  [{char_info[0]}]" if char_info else ""
                print(f"  Char: {char.uuid}  [{props}]  handle=0x{char.handle:04X}{char_label}")
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

                        # Print feature flags when model number is read
                        if char.uuid.lower() == "00002a24-0000-1000-8000-00805f9b34fb":
                            try:
                                model_number = value.decode("utf-8", errors="replace").strip()
                                mode = "YES" if _supports_mode_write(model_number) else "NO"
                                settings = "YES" if _supports_settings_write(model_number) else "NO"
                                print(f"    HA: mode write={mode}, settings write={settings}")
                            except Exception:
                                pass

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
    parser = argparse.ArgumentParser(description="Sonicare GATT scanner and newer-protocol probe")
    parser.add_argument("mac", nargs="?", help="BLE MAC address (optional — scans if omitted)")
    parser.add_argument(
        "--mtu",
        type=int,
        default=None,
        help="Force a specific ATT MTU (e.g. 247). Default: auto-negotiate.",
    )
    parser.add_argument(
        "--pair",
        action="store_true",
        help="Pair with the brush before scanning (enables BLE encryption for CCCD writes).",
    )
    args = parser.parse_args()

    if args.mac:
        print(f"Scanning for {args.mac} (10s)...")
        device = await BleakScanner.find_device_by_address(args.mac, timeout=10)
        if not device:
            print(f"Device {args.mac} not found. If it is connected to another process (e.g. bluetoothctl),")
            sys.exit(1)
        print(f"Found: {device.name} ({device.address})")
        address = args.mac
    else:
        address, _adv = await find_sonicare()
        if not address:
            sys.exit(1)

    await scan_device(address, mtu=args.mtu, pair=args.pair)


asyncio.run(main())
