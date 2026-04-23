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
import json
import struct
import argparse
import subprocess
import sys
import time
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
MSG_SUBSCRIBE = 5
MSG_UNSUBSCRIBE = 6
MSG_GENERIC_RESP = 7
MSG_CHANGE_IND = 8
MSG_CHANGE_IND_RESP = 9
MSG_GET_PRODS = 10
MSG_GET_PORTS = 11

# Subscribe payload sends this timeout in seconds (= 1 year) — matches app behavior.
SUBSCRIBE_TIMEOUT_SECS = 31_536_000

# Default set of ports to subscribe to when --listen is active. JSON ports only;
# binary streaming ports (.b) are gated behind --subscribe-binary.
DEFAULT_SUBSCRIBE_PORTS = (
    ("1", "Sonicare"),
    ("1", "RoutineStatus"),
    ("1", "Battery"),
    ("1", "BrushHead"),
    ("1", "SessionStorage"),
)
BINARY_SUBSCRIBE_PORTS = (
    ("1", "SensorData.b"),
    ("1", "SessionStorage.b"),
)

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
_MODE_WRITE_MODELS = ("HX999", "HX9996")
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

def _parse_generic_resp_json(resp: bytes):
    """GenericResp payload is [status, body..., 0]. Return parsed JSON or None."""
    if not resp or len(resp) < 2 or resp[0] != 0:
        return None
    body = resp[1:].rstrip(b"\x00")
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None


def _parse_json_ids(resp: bytes) -> list[str]:
    """GetProds returns {"0":{...},"1":{...}} — keys are product IDs."""
    data = _parse_generic_resp_json(resp)
    return list(data.keys()) if isinstance(data, dict) else []


def _parse_json_list(resp: bytes) -> list[str]:
    """GetPorts returns a JSON array of port names."""
    data = _parse_generic_resp_json(resp)
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


class NewerProtocolProbe:
    """Probe a Sonicare device using the newer (e50b) BLE protocol."""

    # Channel identifier in the 1-byte transport header.
    CH_DATA = 0       # primary framed data channel
    CH_BINARY = 1     # auxiliary binary channel (unused here)

    # Transport bits packed into the 1-byte header before each chunk.
    BIT_CHANNEL = 0x80   # 0 = data, 1 = binary
    BIT_START = 0x40     # only set on the channel-open handshake packet
    MASK_SEQ = 0x3F      # 0..63, wraps

    # Phase-1 negotiation: announce supported transport versions.
    NEG_VERSIONS = bytes([0x03, 0x04])
    # Phase-2 channel-config request: ask the device for buffer/packet sizes.
    CFG_REQUEST = bytes([0xFF, 0xFF, 0xFF, 0xFF])
    # Default packet size before the channel-config response is parsed.
    DEFAULT_PACKET_SIZE = 20

    def __init__(
        self,
        client: BleakClient,
        listen_seconds: int = 0,
        subscribe_binary: bool = False,
    ):
        self.client = client
        self.listen_seconds = listen_seconds
        self.subscribe_binary = subscribe_binary
        # Outgoing data packets are sequenced 1..63, 0, 1, ...; seq 0 with
        # BIT_START is reserved for the channel-open handshake.
        self.next_data_seq = 1
        # Last incoming sequence we have observed on the data channel; used
        # to ack received notifications back to the device.
        self.last_incoming_seq = -1
        self.rx_buffer = bytearray()
        self.response_event = asyncio.Event()
        self.response_data = b""
        self.server_cfg_event = asyncio.Event()
        self.server_cfg_data = b""
        self.handshake_ack_event = asyncio.Event()
        # Filled in from the phase-2 server-config response.
        self.max_packet_size = self.DEFAULT_PACKET_SIZE
        # ChangeIndication counter for the listen summary.
        self.indication_count: dict[tuple[str, str], int] = {}

    # --- Transport (1-byte header + payload) ----------------------------

    def _data_header(self) -> int:
        """Header byte for a data-channel packet (no start bit)."""
        seq = self.next_data_seq & self.MASK_SEQ
        self.next_data_seq = (self.next_data_seq + 1) % 64
        # Skip seq 0 to avoid colliding with the handshake encoding.
        if self.next_data_seq == 0:
            self.next_data_seq = 1
        return seq  # channel=0, start=0

    async def _send_handshake(self):
        """Open the data channel: 1-byte packet, BIT_START set, seq=0."""
        self.handshake_ack_event.clear()
        await self.client.write_gatt_char(
            CHAR_RX, bytes([self.BIT_START]), response=False
        )

    async def _send_ack(self, seq: int):
        """Acknowledge an incoming data-channel packet on TX_ACK."""
        try:
            await self.client.write_gatt_char(
                CHAR_TX_ACK, bytes([seq & self.MASK_SEQ]), response=False
            )
        except Exception as e:
            print(f"      !!! TX_ACK write failed: {e}")

    # --- Frame layer (FEFF marker + msg type + length + payload) --------
    # Start bytes and length are big-endian — Java ByteBuffer default.

    async def _send_msg(self, msg_type: int, payload: bytes = b""):
        frame = b"\xFE\xFF" + bytes([msg_type]) + struct.pack(">H", len(payload)) + payload
        name = MSG_NAMES.get(msg_type, f"Type{msg_type}")
        print(f"  >>> {name} ({len(frame)}B): {frame.hex()}")

        # Each transport packet is 1 header byte + up to (max_packet_size - 1)
        # payload bytes. Fragments share the same header layout (channel=0,
        # start=0, seq incrementing).
        chunk_payload = max(self.max_packet_size - 1, 1)
        offset = 0
        while offset < len(frame):
            chunk = frame[offset:offset + chunk_payload]
            hdr = self._data_header()
            await self.client.write_gatt_char(
                CHAR_RX, bytes([hdr]) + chunk, response=False
            )
            offset += len(chunk)

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

    # --- Notification handlers ------------------------------------------

    def _on_tx(self, _sender, data: bytearray):
        if len(data) < 1:
            return
        hdr = data[0]
        seq = hdr & self.MASK_SEQ
        payload = bytes(data[1:])

        # Track the most recent incoming sequence and ack it back. Without
        # this the device's send window fills up and it stops emitting
        # further notifications.
        self.last_incoming_seq = seq
        asyncio.get_event_loop().create_task(self._send_ack(seq))

        # The transport may fragment a frame across several notifications;
        # they share a buffer until 5 + payload_len bytes are seen.
        self.rx_buffer.extend(payload)
        buf = bytes(self.rx_buffer)
        if len(buf) >= 5 and buf[0] == 0xFE and buf[1] == 0xFF:
            msg_type = buf[2]
            payload_len = struct.unpack(">H", buf[3:5])[0]
            if len(buf) >= 5 + payload_len:
                self._handle_message(msg_type, buf[5:5 + payload_len])
                self.rx_buffer = bytearray()

    def _on_rx_ack(self, _sender, data: bytearray):
        # First notification on RX_ACK after the handshake completes the
        # channel-open round-trip; subsequent ones acknowledge our outgoing
        # data packets and can be ignored.
        if not self.handshake_ack_event.is_set():
            print(f"  <<< Channel ACK: {bytes(data).hex()}")
            self.handshake_ack_event.set()

    def _on_server_cfg(self, _sender, data: bytearray):
        self.server_cfg_data = bytes(data)
        print(f"  <<< Server Config: {self.server_cfg_data.hex()}")
        self.server_cfg_event.set()

    def _handle_message(self, msg_type: int, payload: bytes):
        name = MSG_NAMES.get(msg_type, f"Type{msg_type}")

        # ChangeIndication is an unsolicited message from the device; it is
        # not tied to the request/response cycle and must be acked separately.
        if msg_type == MSG_CHANGE_IND:
            self._handle_change_indication(payload)
            return

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

    def _handle_change_indication(self, payload: bytes):
        """Format: <product>\\0<port>\\0<body>. Body is JSON for JSON ports,
        raw bytes for *.b binary ports. Ack with MSG_CHANGE_IND_RESP.
        """
        stamp = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"

        parts = payload.split(b"\x00", 2)
        if len(parts) < 3:
            print(f"  [{stamp}] <<< ChangeInd (malformed, {len(payload)}B): {payload.hex()}")
            asyncio.get_event_loop().create_task(self._send_change_ind_ack())
            return

        prod = parts[0].decode("ascii", errors="replace")
        port = parts[1].decode("ascii", errors="replace")
        body = parts[2]
        # Trailing NUL is present on JSON bodies coming from the device.
        while body.endswith(b"\x00"):
            body = body[:-1]

        key = (prod, port)
        self.indication_count[key] = self.indication_count.get(key, 0) + 1

        is_binary = port.endswith(".b")
        if is_binary:
            summary = f"{len(body)}B: {body.hex()}"
        else:
            try:
                decoded = body.decode("utf-8")
                summary = decoded.strip()
            except UnicodeDecodeError:
                summary = f"(non-utf8 {len(body)}B) {body.hex()}"

        print(f"  [{stamp}] <<< ChangeInd prod={prod} port={port}: {summary}")

        asyncio.get_event_loop().create_task(self._send_change_ind_ack())

    async def _send_change_ind_ack(self):
        """Acknowledge a ChangeIndication with a single status byte (NoError)."""
        try:
            await self._send_msg(MSG_CHANGE_IND_RESP, bytes([0]))
        except Exception as e:
            print(f"      !!! ChangeIndResp send failed: {e}")

    async def _subscribe_port(self, prod: str, port: str) -> bool:
        """Subscribe to ChangeIndications for a single port. Returns True on NoError."""
        body = json.dumps({"timeout": SUBSCRIBE_TIMEOUT_SECS}).encode("utf-8")
        payload = prod.encode() + b"\x00" + port.encode() + b"\x00" + body
        print(f"\n  -- Subscribe prod={prod} port={port} --")
        resp = await self._send_and_wait(MSG_SUBSCRIBE, payload, timeout=5.0)
        if not resp:
            return False
        status = resp[0] if resp else 255
        return status == 0

    async def _unsubscribe_port(self, prod: str, port: str) -> None:
        """Unsubscribe a port. Failures are logged and ignored."""
        payload = prod.encode() + b"\x00" + port.encode() + b"\x00" + b"{}"
        print(f"\n  -- Unsubscribe prod={prod} port={port} --")
        await self._send_and_wait(MSG_UNSUBSCRIBE, payload, timeout=3.0)

    # --- High-level probe sequence --------------------------------------

    async def _await_server_cfg(self, expected_len: int, timeout: float = 5.0) -> bytes | None:
        """Wait for the next SERVER_CFG notification and return its payload."""
        self.server_cfg_event.clear()
        self.server_cfg_data = b""
        try:
            await asyncio.wait_for(self.server_cfg_event.wait(), timeout)
        except asyncio.TimeoutError:
            print(f"      !!! No Server Config response (expected {expected_len}B)")
            return None
        if len(self.server_cfg_data) != expected_len:
            print(
                f"      !!! Server Config length mismatch: got {len(self.server_cfg_data)}B, "
                f"expected {expected_len}B"
            )
            return None
        return self.server_cfg_data

    async def run(self):
        """Run the full newer-protocol probe."""
        print("\n--- Newer Protocol Probe ---\n")

        # Read protocol config (optional — not present on all firmware versions).
        # When absent, fall back to the CLIENT_CFG / SERVER_CFG negotiation path below.
        try:
            cfg = await self.client.read_gatt_char(CHAR_PROTO_CFG)
            print(f"  Protocol Config: {cfg.hex()}")
            if len(cfg) >= 3:
                print(f"    Version={cfg[0]}, InBuf={cfg[1]}, OutBuf={cfg[2]}")
        except BleakCharacteristicNotFoundError:
            print("  Protocol Config: characteristic absent — using negotiation fallback")

        # SERVER_CFG must be subscribed before any CLIENT_CFG write so we
        # don't miss the response. Subscribing to TX/RX_ACK is deferred
        # until after phase-1 negotiation: some firmwares (HX742X 1.8.20.0)
        # drop the connection if TX is enabled too early.

        print("\n  [1/6] Subscribe to Server Config...")
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

        # Phase 1 — version negotiation. Device replies with a single byte
        # equal to the chosen transport version.
        print("\n  [2/6] Version negotiation...")
        self.server_cfg_event.clear()
        try:
            await self.client.write_gatt_char(
                CHAR_CLIENT_CFG, self.NEG_VERSIONS, response=False
            )
        except Exception as e:
            print(f"      !!! Write to CLIENT_CFG failed: {e} — aborting probe")
            return
        version_data = await self._await_server_cfg(expected_len=1)
        if version_data is None:
            print("      !!! Aborting — no usable version response")
            return
        chosen_version = version_data[0]
        print(f"      Chosen version: {chosen_version}")
        if chosen_version != 4:
            print(f"      !!! Only transport v4 is implemented here. Aborting.")
            return
        if not self.client.is_connected:
            print("      !!! Disconnected after version negotiation — aborting probe")
            return

        # Subscribe to data + ack channels before sending anything else.
        print("\n  [3/6] Subscribe to TX and RX ACK...")
        for uuid, cb, label in [
            (CHAR_TX, self._on_tx, "TX"),
            (CHAR_RX_ACK, self._on_rx_ack, "RX ACK"),
        ]:
            try:
                await asyncio.wait_for(self.client.start_notify(uuid, cb), timeout=5.0)
            except asyncio.TimeoutError:
                print(f"      !!! Timeout subscribing to {label} ({uuid}) — aborting probe")
                return
            except Exception as e:
                print(f"      !!! Error subscribing to {label} ({uuid}): {e} — aborting probe")
                return
            if not self.client.is_connected:
                print(f"      !!! Disconnected during subscribe to {label} — aborting probe")
                return

        # Phase 2 — channel configuration. Device replies with 6 bytes:
        # 3× little-endian uint16 = (max_packet_size, ch0_buf, ch1_buf).
        print("\n  [4/6] Channel configuration...")
        self.server_cfg_event.clear()
        try:
            await self.client.write_gatt_char(
                CHAR_CLIENT_CFG, self.CFG_REQUEST, response=False
            )
        except Exception as e:
            print(f"      !!! Channel-config request failed: {e} — aborting probe")
            return
        cfg_data = await self._await_server_cfg(expected_len=6)
        if cfg_data is None:
            print("      !!! Aborting — no usable channel-config response")
            return
        max_pkt, ch0_buf, ch1_buf = struct.unpack("<HHH", cfg_data)
        print(f"      max_packet_size={max_pkt}, ch0_buf={ch0_buf}, ch1_buf={ch1_buf}")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            link_mtu = self.client.mtu_size
        # The actual on-air packet is bounded by both sides' buffers and
        # the link MTU (BLE adds 3 bytes of ATT overhead).
        self.max_packet_size = max(min(max_pkt, link_mtu - 3), 4)
        print(f"      effective max_packet_size={self.max_packet_size} (link MTU={link_mtu})")
        if not self.client.is_connected:
            print("      !!! Disconnected after channel config — aborting probe")
            return

        # Phase 3 — open the data channel with an empty start packet. The
        # device acks it on RX_ACK; until that ack arrives, any framed data
        # we send would be discarded.
        print("\n  [5/6] Open data channel...")
        try:
            await self._send_handshake()
        except Exception as e:
            print(f"      !!! Channel-open write failed: {e} — aborting probe")
            return
        try:
            await asyncio.wait_for(self.handshake_ack_event.wait(), 5.0)
        except asyncio.TimeoutError:
            print("      !!! No channel-open ACK on RX_ACK — aborting probe")
            return
        if not self.client.is_connected:
            print("      !!! Disconnected after channel open — aborting probe")
            return
        print("      Data channel open.")

        # Phase 4 — drive the framed protocol now that the transport is up.
        print("\n  [6/6] Framed exchange...")

        print("\n  -- Initialize --")
        await self._send_and_wait(MSG_INITIALIZE_REQ)
        await asyncio.sleep(0.3)

        print("\n  -- Get products --")
        prods_resp = await self._send_and_wait(MSG_GET_PRODS)
        prod_ids = _parse_json_ids(prods_resp)
        await asyncio.sleep(0.3)

        product_ports: dict[str, list[str]] = {}
        if prod_ids:
            print("\n  -- Get ports --")
            for prod_id in prod_ids:
                payload = prod_id.encode() + b"\x00"
                ports_resp = await self._send_and_wait(MSG_GET_PORTS, payload)
                product_ports[prod_id] = _parse_json_list(ports_resp)
                await asyncio.sleep(0.3)

        if product_ports:
            print("\n  -- Get properties --")
            for prod_id, ports in product_ports.items():
                for port in ports:
                    payload = prod_id.encode() + b"\x00" + port.encode() + b"\x00"
                    await self._send_and_wait(MSG_GET_PROPS, payload, timeout=3.0)
                    await asyncio.sleep(0.2)

        if self.listen_seconds > 0:
            await self._listen_for_indications(product_ports)

        print("\n--- Probe complete ---")

    async def _listen_for_indications(
        self, discovered_ports: dict[str, list[str]]
    ) -> None:
        """Subscribe to default ports, log incoming ChangeIndications, then
        cleanly unsubscribe. Only ports actually discovered on this device
        are subscribed — skipping unknown ones avoids NoSuchPort errors.
        """
        print(f"\n--- Listen phase ({self.listen_seconds}s) ---")

        candidates = list(DEFAULT_SUBSCRIBE_PORTS)
        if self.subscribe_binary:
            candidates += list(BINARY_SUBSCRIBE_PORTS)

        subscribed: list[tuple[str, str]] = []
        for prod, port in candidates:
            if port not in discovered_ports.get(prod, []):
                print(f"  -- Skip prod={prod} port={port} (not on device) --")
                continue
            ok = await self._subscribe_port(prod, port)
            if ok:
                subscribed.append((prod, port))
            else:
                print(f"      !!! Subscribe failed for {prod}/{port}")
            await asyncio.sleep(0.2)

        if not subscribed:
            print("\n  No ports subscribed — nothing to listen for.")
            return

        print(
            f"\n  Listening for {self.listen_seconds}s on {len(subscribed)} port(s). "
            "Press the brush power button, switch modes, start/stop a session…"
        )
        try:
            await asyncio.sleep(self.listen_seconds)
        except asyncio.CancelledError:
            print("\n  Listen interrupted.")

        print("\n--- Listen summary ---")
        if self.indication_count:
            for (prod, port), count in sorted(self.indication_count.items()):
                print(f"  prod={prod} port={port}: {count} indication(s)")
        else:
            print("  No ChangeIndications received.")

        print("\n--- Cleaning up subscriptions ---")
        for prod, port in subscribed:
            if not self.client.is_connected:
                break
            await self._unsubscribe_port(prod, port)
            await asyncio.sleep(0.1)


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


def _remove_sonicare_bonds() -> list[str]:
    """Remove paired Sonicare / Philips OHC devices from BlueZ (Linux only).

    Stale bonds are a common cause of subscribe timeouts: BlueZ reports the
    device as paired, but the brush has forgotten the link key, so any
    encrypted operation fails silently. Starting from a clean slate avoids
    that failure mode.
    """
    if sys.platform != "linux":
        return []
    try:
        listing = subprocess.run(
            ["bluetoothctl", "devices", "Paired"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    removed: list[str] = []
    for line in listing.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 3 or parts[0] != "Device":
            continue
        mac, name = parts[1], parts[2]
        low = name.lower()
        if not any(tag in low for tag in ("sonicare", "philips ohc", "philips sonic")):
            continue
        try:
            subprocess.run(
                ["bluetoothctl", "remove", mac],
                capture_output=True, text=True, timeout=5, check=False,
            )
            removed.append(f"{mac} ({name})")
        except Exception:
            pass
    return removed


async def scan_device(
    address: str,
    mtu: int | None = None,
    listen_seconds: int = 0,
    subscribe_binary: bool = False,
):
    """Connect to a Sonicare and dump all GATT services."""
    removed = _remove_sonicare_bonds()
    if removed:
        print("Removed stale bonds before connecting:")
        for entry in removed:
            print(f"  - {entry}")
        print()

    print(f"Connecting to {address} ...")
    async with BleakClient(address, timeout=30) as client:
        print(f"Connected: {client.is_connected}")

        # Some models require BLE pairing before CCCD writes are accepted.
        # Try pairing unconditionally; failures are non-fatal for read-only probes.
        try:
            result = await client.pair()
            print(f"Paired: {result}")
        except Exception as e:
            print(f"Pairing skipped: {e}")

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
            probe = NewerProtocolProbe(
                client,
                listen_seconds=listen_seconds,
                subscribe_binary=subscribe_binary,
            )
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
        "--listen",
        type=int,
        default=0,
        metavar="SECONDS",
        help=(
            "After the newer-protocol probe, subscribe to the main ports "
            "(Sonicare, RoutineStatus, Battery, BrushHead, SessionStorage) "
            "and log incoming ChangeIndications for the given number of "
            "seconds. Press the power button, switch modes, or run a session "
            "during this window. Default: 0 (no listen phase)."
        ),
    )
    parser.add_argument(
        "--subscribe-binary",
        action="store_true",
        help=(
            "Additionally subscribe to the binary streaming ports "
            "(SensorData.b, SessionStorage.b) during --listen. These can be "
            "high-rate — use only when investigating live sensor data."
        ),
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

    await scan_device(
        address,
        mtu=args.mtu,
        listen_seconds=args.listen,
        subscribe_binary=args.subscribe_binary,
    )


asyncio.run(main())
