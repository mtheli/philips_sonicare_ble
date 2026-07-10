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
import contextlib
import json
import struct
import argparse
import subprocess
import sys
import time
import warnings
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakCharacteristicNotFoundError, BleakError

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
MSG_PUT_PROPS = 3
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


def _decode_sensor_frame(b: bytes) -> str:
    """Best-effort decode of a SensorData.b binary frame, printed alongside
    the raw hex so a live capture immediately shows the interpreted values.

    Layout (little-endian): bytes 0-1 = packet type. Type-specific tail:
      1 Pressure    : len==7 → value=u16LE@4, state=u8@6 ; else state=u8@4
      2 Temperature : temp = u8@4/256 + s8@5  (≈ degrees C in byte 5)
      4 IMU (≥16 B) : gyro xyz = s16LE @4/@6/@8, accel xyz = s16LE @10/@12/@14
    """
    if len(b) < 2:
        return f"type=? raw={b.hex()} (too short)"
    ptype = int.from_bytes(b[0:2], "little")
    ctr = b[2] if len(b) > 2 else None  # byte2 = per-type sample counter
    u16 = lambda o: int.from_bytes(b[o:o+2], "little") if len(b) >= o + 2 else None
    s16 = lambda o: int.from_bytes(b[o:o+2], "little", signed=True) if len(b) >= o + 2 else None
    if ptype == 1:
        # HX742A live (2026-07-10): 5B frames carry a state byte at @4, 3B
        # frames are counter-only. App spec: len==7 → value=u16LE@4, state@6.
        if len(b) >= 7:
            return f"type=1 PRESSURE ctr={ctr} value={u16(4)} state={b[6]}"
        if len(b) >= 5:
            return f"type=1 PRESSURE ctr={ctr} state={b[4]}"
        return f"type=1 PRESSURE ctr={ctr} (counter-only)"
    if ptype == 2:
        if len(b) >= 6:
            frac = b[4] / 256.0
            whole = int.from_bytes(b[5:6], "little", signed=True)
            return f"type=2 TEMP ctr={ctr} ≈{whole + frac:.2f}°C (byte5={whole} byte4={b[4]})"
        return f"type=2 TEMP ctr={ctr} raw={b.hex()} (short)"
    if ptype == 4 and len(b) >= 16:
        return (
            f"type=4 IMU ctr={ctr} gyro=({s16(4)},{s16(6)},{s16(8)}) "
            f"accel=({s16(10)},{s16(12)},{s16(14)})"
        )
    return f"type={ptype} raw={b.hex()} (unrecognized / sensors off?)"


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
        enable_sensors: int | None = None,
    ):
        self.client = client
        self.listen_seconds = listen_seconds
        # Enabling the sensor streams implies we want their binary payloads.
        self.enable_sensors = enable_sensors
        self.subscribe_binary = subscribe_binary or enable_sensors is not None
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
        # Every SensorData.b frame, timestamped, so a full capture survives
        # even if the terminal scrolls or the link drops mid-window.
        self.sensor_frames: list[dict] = []
        # Last pressure-state byte seen, to flag transitions live.
        self._last_pressure_state: int | None = None
        # Structured snapshot of the probe, filled during run(). Shape:
        #   {"<product_id>": {"name": str, "ports": {"<port>": <props|None>}}}
        # Each port's value is the decoded GenericResp JSON — exactly the dict
        # that condor_adapter.map_port_props(port, props) consumes, so a saved
        # capture doubles as a test fixture.
        self.capture: dict[str, dict] = {}

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
        """Format: <product>/<port>\\0<body>\\0. Note the path separator between
        product and port is '/', not NUL — different from the Subscribe
        request payload. Body is JSON for JSON ports, raw bytes for *.b
        binary ports. Ack with MSG_CHANGE_IND_RESP.
        """
        stamp = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"

        parts = payload.split(b"\x00", 1)
        if len(parts) < 2 or b"/" not in parts[0]:
            print(f"  [{stamp}] <<< ChangeInd (malformed, {len(payload)}B): {payload.hex()}")
            asyncio.get_event_loop().create_task(self._send_change_ind_ack())
            return

        header = parts[0].decode("ascii", errors="replace")
        prod, port = header.split("/", 1)
        body = parts[1]
        # Trailing NUL is present on JSON bodies coming from the device.
        while body.endswith(b"\x00"):
            body = body[:-1]

        # Ack FIRST, before any decode/log/print work, so the response goes
        # out as early as possible. The brush tears the link down (reason
        # 0x13) if the ChangeIndication ack slips past ~250 ms — decoding and
        # printing must not sit in front of it.
        asyncio.get_event_loop().create_task(self._send_change_ind_ack())

        key = (prod, port)
        self.indication_count[key] = self.indication_count.get(key, 0) + 1

        is_binary = port.endswith(".b")
        if is_binary:
            summary = f"{len(body)}B: {body.hex()}"
            if port == "SensorData.b" and body:
                summary += f"  ⟶ {_decode_sensor_frame(body)}"
                # Flag pressure-state transitions live so they can be
                # correlated with the brush's over-pressure vibration.
                if int.from_bytes(body[0:2], "little") == 1 and len(body) >= 5:
                    state = body[4]
                    if state != self._last_pressure_state:
                        print(
                            f"  [{stamp}] ⚠  PRESSURE STATE "
                            f"{self._last_pressure_state} → {state}"
                        )
                        self._last_pressure_state = state
        else:
            try:
                decoded = body.decode("utf-8")
                summary = decoded.strip()
            except UnicodeDecodeError:
                summary = f"(non-utf8 {len(body)}B) {body.hex()}"

        # Record every indication (JSON ports too) so one capture shows which
        # field carries the pressure state — it is not in SensorData.b byte4.
        self.sensor_frames.append(
            {"t": stamp, "port": port, "body": body.hex(), "summary": summary}
        )

        print(f"  [{stamp}] <<< ChangeInd prod={prod} port={port}: {summary}")

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

    async def _put_props(self, prod: str, port: str, props: dict) -> bool:
        """Write properties to a port (PutProps). Returns True on NoError.

        Payload: ``product\\0port\\0{json}`` — same framing as GetProps but
        with a JSON body. Used to flip the SensorData enable register so the
        device starts streaming its pressure/temperature binary substream.
        """
        body = json.dumps(props, separators=(",", ":")).encode("utf-8")
        payload = prod.encode() + b"\x00" + port.encode() + b"\x00" + body
        print(f"\n  -- PutProps prod={prod} port={port} {props} --")
        resp = await self._send_and_wait(MSG_PUT_PROPS, payload, timeout=5.0)
        if not resp:
            print("      !!! No PutProps response")
            return False
        status = resp[0] if resp else 255
        if status != 0:
            print(f"      !!! PutProps status={status} ({STATUS_NAMES.get(status, '?')})")
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

    async def _subscribe_data_channels(self) -> bool:
        """Subscribe to TX and RX_ACK. Common to both V3 and V4."""
        for uuid, cb, label in [
            (CHAR_TX, self._on_tx, "TX"),
            (CHAR_RX_ACK, self._on_rx_ack, "RX ACK"),
        ]:
            try:
                await asyncio.wait_for(self.client.start_notify(uuid, cb), timeout=5.0)
            except asyncio.TimeoutError:
                print(f"      !!! Timeout subscribing to {label} ({uuid})")
                return False
            except Exception as e:
                print(f"      !!! Error subscribing to {label} ({uuid}): {e}")
                return False
            if not self.client.is_connected:
                print(f"      !!! Disconnected during subscribe to {label}")
                return False
        return True

    async def _open_data_channel(self) -> bool:
        """Send the BIT_START packet on RX, wait for RX_ACK. Common to V3 + V4."""
        try:
            await self._send_handshake()
        except Exception as e:
            print(f"      !!! Channel-open write failed: {e}")
            return False
        try:
            await asyncio.wait_for(self.handshake_ack_event.wait(), 5.0)
        except asyncio.TimeoutError:
            print("      !!! No channel-open ACK on RX_ACK")
            return False
        return self.client.is_connected

    async def _handshake_v3(self, cfg_bytes: bytes) -> bool:
        """V3 handshake: no version/channel negotiation, PROTO_CFG is static.

        Packet size is fixed at 20 bytes per the V3 spec. Buffer sizes are
        reported by PROTO_CFG but the transport does not exchange anything
        with the device — we just subscribe the data channels and open
        channel 0. Verified on Philips OneBlade QP4530 (2026-04-24).
        """
        self.max_packet_size = 20
        print(f"      V3 static config → packet_size=20, in_buf={cfg_bytes[1]}, "
              f"out_buf={cfg_bytes[2]}")

        print("\n  [V3 1/2] Subscribe to TX and RX ACK...")
        if not await self._subscribe_data_channels():
            return False

        print("\n  [V3 2/2] Open data channel...")
        if not await self._open_data_channel():
            return False
        print("      Data channel open.")
        return True

    async def _handshake_v4(self) -> bool:
        """V4 handshake: version-negotiation → channel-config → channel-open.

        SERVER_CFG must be subscribed before any CLIENT_CFG write so we
        don't miss the response. Subscribing to TX/RX_ACK is deferred
        until after phase-1 negotiation: some firmwares (HX742X 1.8.20.0)
        drop the connection if TX is enabled too early.
        """
        print("\n  [V4 1/5] Subscribe to Server Config...")
        try:
            await asyncio.wait_for(
                self.client.start_notify(CHAR_SERVER_CFG, self._on_server_cfg),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            print("      !!! Timeout subscribing to Server Config")
            return False
        except Exception as e:
            print(f"      !!! Error subscribing to Server Config: {e}")
            return False
        if not self.client.is_connected:
            return False

        # Phase 1 — version negotiation. Device replies with a single byte
        # equal to the chosen transport version.
        print("\n  [V4 2/5] Version negotiation...")
        self.server_cfg_event.clear()
        try:
            await self.client.write_gatt_char(
                CHAR_CLIENT_CFG, self.NEG_VERSIONS, response=False
            )
        except Exception as e:
            print(f"      !!! Write to CLIENT_CFG failed: {e}")
            return False
        version_data = await self._await_server_cfg(expected_len=1)
        if version_data is None:
            return False
        chosen_version = version_data[0]
        print(f"      Chosen version: {chosen_version}")
        if chosen_version != 4:
            print(f"      !!! Only transport v4 is implemented in this branch.")
            return False
        if not self.client.is_connected:
            return False

        print("\n  [V4 3/5] Subscribe to TX and RX ACK...")
        if not await self._subscribe_data_channels():
            return False

        # Phase 2 — channel configuration. Device replies with 6 bytes:
        # 3× little-endian uint16 = (max_packet_size, ch0_buf, ch1_buf).
        print("\n  [V4 4/5] Channel configuration...")
        self.server_cfg_event.clear()
        try:
            await self.client.write_gatt_char(
                CHAR_CLIENT_CFG, self.CFG_REQUEST, response=False
            )
        except Exception as e:
            print(f"      !!! Channel-config request failed: {e}")
            return False
        cfg_data = await self._await_server_cfg(expected_len=6)
        if cfg_data is None:
            return False
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
            return False

        # Phase 3 — open the data channel with an empty start packet. The
        # device acks it on RX_ACK; until that ack arrives, any framed data
        # we send would be discarded.
        print("\n  [V4 5/5] Open data channel...")
        if not await self._open_data_channel():
            return False
        print("      Data channel open.")
        return True

    async def run(self):
        """Run the full newer-protocol probe."""
        print("\n--- Newer Protocol Probe ---\n")

        # PROTO_CFG detection selects V3 vs V4:
        #   - Present + byte[0] == 3 → V3 path (OneBlade QP4530 confirmed; no
        #     known V3 Sonicare as of 2026-04-24, but the transport layer is
        #     identical across the Philips platform so this should just work)
        #   - Absent or byte[0] != 3 → V4 path (HX742X etc)
        version_hint: int | None = None
        cfg_bytes: bytes = b""
        try:
            cfg_bytes = bytes(await self.client.read_gatt_char(CHAR_PROTO_CFG))
            print(f"  Protocol Config: {cfg_bytes.hex()}")
            if len(cfg_bytes) >= 3:
                print(f"    Version={cfg_bytes[0]}, InBuf={cfg_bytes[1]}, OutBuf={cfg_bytes[2]}")
                version_hint = cfg_bytes[0]
        except BleakCharacteristicNotFoundError:
            print("  Protocol Config: characteristic absent — assuming V4")
        except BleakError as e:
            print(f"  Protocol Config read failed: {e} — assuming V4")

        if version_hint == 3:
            print("\n  → Using V3 handshake (static PROTO_CFG, no negotiation)")
            if not await self._handshake_v3(cfg_bytes):
                print("      !!! V3 handshake failed — aborting probe")
                return
        else:
            print("\n  → Using V4 handshake (CLIENT_CFG/SERVER_CFG negotiation)")
            if not await self._handshake_v4():
                print("      !!! V4 handshake failed — aborting probe")
                return

        # Framed protocol now up — identical for both V3 and V4.
        print("\n  -- Framed exchange --")

        print("\n  -- Initialize --")
        await self._send_and_wait(MSG_INITIALIZE_REQ)
        await asyncio.sleep(0.3)

        print("\n  -- Get products --")
        prods_resp = await self._send_and_wait(MSG_GET_PRODS)
        prod_ids = _parse_json_ids(prods_resp)
        prods_meta = _parse_generic_resp_json(prods_resp) or {}
        for pid in prod_ids:
            name = ""
            if isinstance(prods_meta.get(pid), dict):
                name = prods_meta[pid].get("name", "")
            self.capture[pid] = {"name": name, "ports": {}}
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
                entry = self.capture.setdefault(prod_id, {"name": "", "ports": {}})
                for port in ports:
                    payload = prod_id.encode() + b"\x00" + port.encode() + b"\x00"
                    resp = await self._send_and_wait(MSG_GET_PROPS, payload, timeout=3.0)
                    # Binary ports (*.b) return an empty body — record null so
                    # the fixture still lists the port without inventing props.
                    entry["ports"][port] = _parse_generic_resp_json(resp)
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

        # Flip the SensorData enable register so the device starts streaming
        # its pressure/temperature binary substream. Subscribe the binary port
        # first (done above), then write {"Sensors": mask}; without this write
        # the SensorData.b port only emits an idle default, not real telemetry.
        # Bit0=pressure, bit1=temperature, bit2=gyroscope.
        if self.enable_sensors is not None:
            if "SensorData" in discovered_ports.get("1", []):
                await self._put_props("1", "SensorData", {"Sensors": self.enable_sensors})
            else:
                print("  -- Skip sensor enable (no SensorData port on device) --")

        print(
            f"\n  Listening for {self.listen_seconds}s on {len(subscribed)} port(s). "
            "Press the brush power button, switch modes, start/stop a session…"
        )
        try:
            # Poll instead of one long sleep so we exit promptly when the
            # brush is switched off (link drops) rather than looking frozen
            # for the rest of the window.
            for _ in range(self.listen_seconds):
                await asyncio.sleep(1)
                if not self.client.is_connected:
                    print("\n  Link dropped (brush switched off?) — ending listen.")
                    break
        except asyncio.CancelledError:
            print("\n  Listen interrupted.")

        print("\n--- Listen summary ---")
        if self.indication_count:
            for (prod, port), count in sorted(self.indication_count.items()):
                print(f"  prod={prod} port={port}: {count} indication(s)")
        else:
            print("  No ChangeIndications received.")

        self._dump_sensor_frames()

        print("\n--- Cleaning up subscriptions ---")
        for prod, port in subscribed:
            if not self.client.is_connected:
                break
            await self._unsubscribe_port(prod, port)
            await asyncio.sleep(0.1)

    def _dump_sensor_frames(self) -> None:
        """Persist every captured SensorData.b frame to a JSONL file so a
        full run survives even if the terminal scrolls or the link drops."""
        if not self.sensor_frames:
            return
        path = f"condor_sensordata_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for frame in self.sensor_frames:
                    fh.write(json.dumps(frame, ensure_ascii=False) + "\n")
            print(f"\n  {len(self.sensor_frames)} SensorData.b frame(s) written to {path}")
        except OSError as e:
            print(f"\n  !!! Could not write SensorData.b capture: {e}")


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
    """Scan for any Sonicare toothbrush nearby. Returns (BLEDevice, adv).

    Returns the BLEDevice object (not just the address): connecting via the
    discovered device object is bleak's recommended path and avoids the
    "device not found" failure that a bare address string hits when the
    device's BlueZ object has just been dropped (e.g. right after a bond
    removal) or when it advertises rotating RPAs.
    """
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
        return device, adv

    print(f"Found {len(found)} Sonicare devices:")
    for i, (device, adv) in enumerate(found):
        print(f"  [{i+1}] {device.name} ({device.address}), RSSI={adv.rssi}{_adv_summary(adv)}")
    print()
    choice = input(f"Select device [1-{len(found)}]: ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(found):
            return found[idx][0], found[idx][1]
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


AGENT_PATH = "/org/bluez/agent/sonicare_scan"
AGENT_CAPABILITY = "KeyboardDisplay"
PAIR_TIMEOUT = 30  # seconds


@contextlib.asynccontextmanager
async def _bluez_agent():
    """Register a BlueZ auto-confirm pairing agent for the duration of the block.

    The agent must exist BEFORE we connect, not just around an explicit
    ``client.pair()``. Two reasons:

    * The newer (e50b) brushes reject notify subscriptions until bonded, and
      their Just-Works / Secure-Connections handshake needs an agent to answer
      ``RequestConfirmation`` / ``RequestAuthorization``.
    * Bonded Classic brushes (HX999X Prestige) demand encryption during
      *service discovery* — BlueZ auto-triggers SMP inside ``connect()``. With
      no agent registered yet, that SMP fails and the device drops the link
      ("failed to discover services, device disconnected").

    Yields True if an agent is registered, False otherwise (non-Linux, missing
    dbus_fast). dbus_fast ships with bleak, so this needs no extra dependency.
    """
    if sys.platform != "linux":
        yield False
        return

    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
        from dbus_fast.errors import DBusError
        from dbus_fast.service import ServiceInterface, method
    except ImportError:
        print("  dbus_fast unavailable — pairing agent disabled")
        yield False
        return

    class _AutoConfirmAgent(ServiceInterface):
        """BlueZ Agent1 that auto-confirms pairing requests."""

        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")

        @method()
        def Release(self) -> None:  # noqa: N802
            pass

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # noqa: N802,F821
            print(f"  Auto-confirming pairing for {device} (passkey {passkey:06d})")

        @method()
        def RequestAuthorization(self, device: "o") -> None:  # noqa: N802,F821
            pass

        @method()
        def AuthorizeService(self, device: "o", uuid: "s") -> None:  # noqa: N802,F821
            pass

        @method()
        def Cancel(self) -> None:  # noqa: N802
            print("  Pairing cancelled by BlueZ")

    bus = None
    agent_registered = False
    try:
        bus = MessageBus(bus_type=BusType.SYSTEM)
        await bus.connect()

        agent = _AutoConfirmAgent()
        bus.export(AGENT_PATH, agent)

        bluez_intro = await bus.introspect("org.bluez", "/org/bluez")
        bluez_proxy = bus.get_proxy_object("org.bluez", "/org/bluez", bluez_intro)
        agent_mgr = bluez_proxy.get_interface("org.bluez.AgentManager1")

        try:
            await agent_mgr.call_register_agent(AGENT_PATH, AGENT_CAPABILITY)
            agent_registered = True
        except DBusError as err:
            if "AlreadyExists" not in str(err):
                print(f"  Could not register pairing agent: {err}")
                yield False
                return
            agent_registered = True
        try:
            await agent_mgr.call_request_default_agent(AGENT_PATH)
        except DBusError:
            pass

        yield True
    finally:
        if bus and bus.connected:
            if agent_registered:
                try:
                    intro = await bus.introspect("org.bluez", "/org/bluez")
                    proxy = bus.get_proxy_object("org.bluez", "/org/bluez", intro)
                    mgr = proxy.get_interface("org.bluez.AgentManager1")
                    await mgr.call_unregister_agent(AGENT_PATH)
                except Exception:
                    pass
            try:
                bus.unexport(AGENT_PATH)
            except Exception:
                pass
            bus.disconnect()


async def _client_pair(client: BleakClient) -> bool:
    """Run ``client.pair()`` on the live connection. A BlueZ auto-confirm agent
    must already be registered (see ``_bluez_agent``). Returns True on success.
    """
    try:
        await asyncio.wait_for(client.pair(), timeout=PAIR_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"  Pairing timed out after {PAIR_TIMEOUT}s")
        return False
    except Exception as err:
        print(f"  Pairing failed: {err}")
        return False
    print("  Paired (bonded via auto-confirm agent)")
    return True


# ATT/BlueZ error fragments that mean "this read needs a bond/encryption".
_AUTH_ERROR_HINTS = (
    "insufficient", "authentication", "encryption", "not permitted",
    "not paired", "not authorized", "0x05", "0x0f",
)


def _is_auth_error(msg: str) -> bool:
    low = msg.lower()
    return any(hint in low for hint in _AUTH_ERROR_HINTS)


async def _read_char(client, char, char_entry, char_info, device_info) -> str:
    """Read one characteristic into ``char_entry``. Returns a status string:
    "ok", "auth" (needs bonding), or "error" (other failure/disconnect).
    """
    try:
        value = await client.read_gatt_char(char)
    except Exception as e:
        msg = str(e)
        print(f"    Read error: {msg}")
        return "auth" if _is_auth_error(msg) else "error"

    hex_str = value.hex()
    char_entry["value_hex"] = hex_str
    text = None
    try:
        decoded = value.decode("utf-8")
        if decoded.isprintable():
            text = decoded
            print(f"    Value: {hex_str} = \"{decoded}\"")
        else:
            print(f"    Value: {hex_str}")
    except (UnicodeDecodeError, ValueError):
        print(f"    Value: {hex_str}")
    char_entry["value_text"] = text

    if char_info and char_info[1] in ("device_info", "standard_ble"):
        device_info[char_info[0]] = text if text is not None else hex_str

    if char.uuid.lower() == "00002a24-0000-1000-8000-00805f9b34fb":
        try:
            model_number = value.decode("utf-8", errors="replace").strip()
            mode = "YES" if _supports_mode_write(model_number) else "NO"
            settings = "YES" if _supports_settings_write(model_number) else "NO"
            print(f"    HA: mode write={mode}, settings write={settings}")
        except Exception:
            pass
    return "ok"


def _remove_sonicare_bonds(skip: str | None = None) -> list[str]:
    """Remove paired Sonicare / Philips OHC devices from BlueZ (Linux only).

    Stale bonds are a common cause of subscribe timeouts: BlueZ reports the
    device as paired, but the brush has forgotten the link key, so any
    encrypted operation fails silently. Starting from a clean slate avoids
    that failure mode.

    ``skip`` is the address we are about to connect to — never remove its
    bond. Removing the target's own (good) bond immediately before connecting
    drops its BlueZ object and yields "device not found", which is exactly
    what happens when a Condor brush advertises its public identity MAC that
    a prior run already bonded.
    """
    if sys.platform != "linux":
        return []
    skip_upper = skip.upper() if skip else None
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
        if skip_upper and mac.upper() == skip_upper:
            continue
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
    enable_sensors: int | None = None,
    json_path: str | None = None,
    adv_name: str | None = None,
    connect_target=None,
):
    """Connect to a Sonicare and dump all GATT services.

    ``connect_target`` is the discovered BLEDevice when available; connecting
    to the object rather than the address string is more robust for devices
    that rotate their advertisement address. Falls back to ``address``.
    """
    removed = _remove_sonicare_bonds(skip=address)
    if removed:
        print("Removed stale bonds before connecting:")
        for entry in removed:
            print(f"  - {entry}")
        print()

    target = connect_target if connect_target is not None else address
    print(f"Connecting to {address} ...")
    # Register the pairing agent BEFORE connecting: bonded Classic brushes
    # (HX999X Prestige) trigger SMP during service discovery, which fails and
    # drops the link if no agent is present yet.
    async with _bluez_agent(), BleakClient(target, timeout=30) as client:
        print(f"Connected: {client.is_connected}")

        await _negotiate_mtu(client, mtu)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            mtu_actual = client.mtu_size
        print(f"MTU: {mtu_actual}\n")

        # Snapshot the service table into plain Python objects up front. If a
        # brush drops the link mid-scan (open-GATT models do this when probed
        # wrong), iterating the live ``client.services`` property would raise
        # "Service Discovery has not been performed yet"; a snapshot keeps the
        # structure usable and lets us still write the JSON we gathered.
        services = list(client.services)
        has_legacy = any(s.uuid.startswith(LEGACY_PREFIX) for s in services)
        has_newer = any(s.uuid.startswith(NEWER_PREFIX) for s in services)

        # Pairing strategy: only the newer (Condor) probe needs a bond up
        # front. Legacy brushes are mixed — some are open GATT (Kids, HX992X)
        # and DROP the link if forced to pair, others bond (HX9992). So for
        # Legacy we pair reactively: read unencrypted, and pair only when a
        # read comes back with an auth/encryption error.
        paired = False
        if has_newer:
            print("Pairing (required for newer-protocol probe) ...")
            paired = await _client_pair(client)
            print()

        gatt_services: list[dict] = []
        device_info: dict[str, str] = {}

        for service in services:
            svc_entry = {"uuid": service.uuid, "characteristics": []}
            gatt_services.append(svc_entry)

            print(f"Service: {service.uuid}")
            if service.description and service.description != service.uuid:
                print(f"  Description: {service.description}")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                char_info = KNOWN_CHARS.get(char.uuid.lower())
                char_label = f"  [{char_info[0]}]" if char_info else ""
                print(f"  Char: {char.uuid}  [{props}]  handle=0x{char.handle:04X}{char_label}")
                char_entry = {
                    "uuid": char.uuid,
                    "name": char_info[0] if char_info else None,
                    "properties": list(char.properties),
                    "handle": char.handle,
                    "value_hex": None,
                    "value_text": None,
                }
                svc_entry["characteristics"].append(char_entry)
                if "read" in char.properties and client.is_connected:
                    status = await _read_char(
                        client, char, char_entry, char_info, device_info
                    )
                    # First auth failure on a Legacy brush → it wants a bond.
                    # Pair once (agent), then retry this read and continue.
                    if status == "auth" and not paired and not has_newer:
                        print("    → read needs encryption, pairing ...")
                        paired = await _client_pair(client)
                        if paired and client.is_connected:
                            await _read_char(
                                client, char, char_entry, char_info, device_info
                            )
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
        print(f"Total services: {len(services)}")
        print("=" * 60)

        # Probe newer protocol if detected and still connected
        condor_capture: dict = {}
        if has_newer and client.is_connected:
            probe = NewerProtocolProbe(
                client,
                listen_seconds=listen_seconds,
                subscribe_binary=subscribe_binary,
                enable_sensors=enable_sensors,
            )
            await probe.run()
            condor_capture = probe.capture

        if json_path:
            _write_capture(
                json_path,
                address=address,
                adv_name=adv_name,
                protocol=protocol,
                device_info=device_info,
                gatt_services=gatt_services,
                condor=condor_capture,
            )


def _write_capture(
    path: str,
    *,
    address: str,
    adv_name: str | None,
    protocol: str,
    device_info: dict,
    gatt_services: list,
    condor: dict,
) -> None:
    """Write a structured snapshot of the probe to a JSON file.

    The ``condor`` section mirrors what ``condor_adapter.map_port_props``
    consumes: ``condor[product_id]["ports"][port]`` is the decoded property
    dict (or null for binary/empty ports), so a saved capture is directly
    usable as a fixture for adapter tests.
    """
    snapshot = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "address": address,
        "adv_name": adv_name,
        "protocol": protocol,
        "device_info": device_info,
        "condor": condor,
        "gatt_services": gatt_services,
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
        print(f"\nCapture written to {path}")
    except OSError as e:
        print(f"\n!!! Could not write capture to {path}: {e}")


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
    parser.add_argument(
        "--enable-sensors",
        type=lambda s: int(s, 0),
        default=None,
        metavar="MASK",
        help=(
            "Before listening, write {\"Sensors\": MASK} to the SensorData "
            "port to switch on the telemetry substream, then log the raw "
            "SensorData.b frames. MASK is a bitmask: 1=pressure, 2=temperature, "
            "4=gyroscope (e.g. 3 for pressure+temperature, 7 for all). Implies "
            "--subscribe-binary. Run a session and apply brush-head pressure "
            "during the --listen window to capture real frames."
        ),
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        default=None,
        help=(
            "Write a structured snapshot (device info, GATT map, and — for "
            "the newer protocol — every product/port/property as decoded "
            "JSON) to PATH. The condor section is shaped for direct use as a "
            "condor_adapter test fixture."
        ),
    )
    args = parser.parse_args()

    adv_name = None
    if args.mac:
        print(f"Scanning for {args.mac} (10s)...")
        device = await BleakScanner.find_device_by_address(args.mac, timeout=10)
        if not device:
            print(f"Device {args.mac} not found. If it is connected to another process (e.g. bluetoothctl),")
            sys.exit(1)
        print(f"Found: {device.name} ({device.address})")
        adv_name = device.name
        address = args.mac
    else:
        device, _adv = await find_sonicare()
        if not device:
            sys.exit(1)
        address = device.address
        adv_name = device.name or "Philips Sonicare"

    await scan_device(
        address,
        mtu=args.mtu,
        listen_seconds=args.listen,
        subscribe_binary=args.subscribe_binary,
        enable_sensors=args.enable_sensors,
        json_path=args.json,
        adv_name=adv_name,
        connect_target=device,
    )


asyncio.run(main())
