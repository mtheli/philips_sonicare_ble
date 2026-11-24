"""Build an anonymized GATT capture of a brush connected through the bridge.

The snapshot has the same shape as ``scripts/sonicare_scan.py --json`` so it
can go straight into ``tests/fixtures/``: ``gatt_services[*].characteristics[*]``
with uuid/name/properties/handle/value_hex/value_text. The ``condor`` section
(port properties) stays empty: the bridge capture only reads GATT values.
"""

from __future__ import annotations

import re
import time
from typing import Any

from . import const as _const

# Placeholder MAC for anonymized captures (same as the test fixtures)
FIXTURE_MAC = "24:E5:AA:00:00:01"

_STANDARD_NAMES = {
    "00002a00-0000-1000-8000-00805f9b34fb": "Device Name",
    "00002a01-0000-1000-8000-00805f9b34fb": "Appearance",
    "00002a04-0000-1000-8000-00805f9b34fb": "Preferred Conn Params",
    "00002a05-0000-1000-8000-00805f9b34fb": "Service Changed",
    "00002a19-0000-1000-8000-00805f9b34fb": "Battery Level",
    "00002a23-0000-1000-8000-00805f9b34fb": "System ID",
    "00002a24-0000-1000-8000-00805f9b34fb": "Model Number",
    "00002a25-0000-1000-8000-00805f9b34fb": "Serial Number",
    "00002a26-0000-1000-8000-00805f9b34fb": "Firmware Revision",
    "00002a27-0000-1000-8000-00805f9b34fb": "Hardware Revision",
    "00002a28-0000-1000-8000-00805f9b34fb": "Software Revision",
    "00002a29-0000-1000-8000-00805f9b34fb": "Manufacturer Name",
    "00002a2a-0000-1000-8000-00805f9b34fb": "IEEE Regulatory Cert",
    "00002a50-0000-1000-8000-00805f9b34fb": "PnP ID",
    "00002aa6-0000-1000-8000-00805f9b34fb": "Central Addr Resolution",
}
# Shown in device_info, as sonicare_scan.py does
_DEVICE_INFO_UUIDS = {
    "00002a19-0000-1000-8000-00805f9b34fb",
    "00002a23-0000-1000-8000-00805f9b34fb",
    "00002a24-0000-1000-8000-00805f9b34fb",
    "00002a25-0000-1000-8000-00805f9b34fb",
    "00002a26-0000-1000-8000-00805f9b34fb",
    "00002a27-0000-1000-8000-00805f9b34fb",
    "00002a28-0000-1000-8000-00805f9b34fb",
    "00002a29-0000-1000-8000-00805f9b34fb",
    "00002a2a-0000-1000-8000-00805f9b34fb",
    "00002a50-0000-1000-8000-00805f9b34fb",
}
# Values that identify the individual unit: replaced, length preserved
PRIVATE_CHARS = {
    "00002a25-0000-1000-8000-00805f9b34fb",  # Serial Number
    "00002a23-0000-1000-8000-00805f9b34fb",  # System ID
    _const.CHAR_BRUSHHEAD_SERIAL,             # NFC tag id of the brush head
}
# An all-zero brush head serial means "no chip read" to the integration, so
# that one is replaced by a non-zero placeholder (first byte, the NFC chip
# maker, kept); an all-zero value stays as it is.
_PLACEHOLDER_CHARS = {_const.CHAR_BRUSHHEAD_SERIAL}
# Characteristics that hold text. Only these get value_text: arbitrary
# bytes that happen to be printable (battery 0x5a = "Z") are not text.
# Same set as the _dec_str entries in scripts/sonicare_scan.py.
TEXT_CHARS = {
    "00002a00-0000-1000-8000-00805f9b34fb",  # Device Name
    "00002a24-0000-1000-8000-00805f9b34fb",  # Model Number
    "00002a25-0000-1000-8000-00805f9b34fb",  # Serial Number
    "00002a26-0000-1000-8000-00805f9b34fb",  # Firmware Revision
    "00002a27-0000-1000-8000-00805f9b34fb",  # Hardware Revision
    "00002a28-0000-1000-8000-00805f9b34fb",  # Software Revision
    "00002a29-0000-1000-8000-00805f9b34fb",  # Manufacturer Name
    _const.CHAR_BRUSHHEAD_DATE,               # e.g. "260322 43M"
    _const.CHAR_BRUSHHEAD_PAYLOAD,            # NFC URL
}
_BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"
_DEVICE_NAME = "00002a00-0000-1000-8000-00805f9b34fb"
_CLASSIC_PREFIX = "477ea600-a260-11e4-ae37-0002a5d5"

_CONST_NAMES = {
    value.lower(): name.removeprefix("CHAR_").replace("_", " ").title()
    for name, value in vars(_const).items()
    if name.startswith("CHAR_") and isinstance(value, str) and len(value) == 36
}


def _char_name(uuid: str) -> str | None:
    return _STANDARD_NAMES.get(uuid) or _CONST_NAMES.get(uuid)


def text_value(raw: bytes) -> str | None:
    """Text of a string characteristic without trailing NULs, None if empty."""
    text = raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    return text if text.strip() else None


def _private_value(uuid: str, raw: bytes) -> bytes:
    """Replacement for a value that identifies the unit, same length."""
    if uuid in _PLACEHOLDER_CHARS:
        if not any(raw):
            return raw
        return raw[:1] + b"\xaa" * (len(raw) - 1)
    if uuid in TEXT_CHARS:
        return b"0" * len(raw)
    return bytes(len(raw))


def _mac_bytes(addresses: list[str | None]) -> list[bytes]:
    """The 6 bytes of each usable MAC address."""
    macs: list[bytes] = []
    for address in addresses:
        if not address:
            continue
        try:
            raw = bytes.fromhex(address.replace(":", "").replace("-", ""))
        except ValueError:
            continue
        if len(raw) == 6 and any(raw) and raw not in macs:
            macs.append(raw)
    return macs


def _is_distinctive(pattern: bytes) -> bool:
    """Long and varied enough to blank out without hitting ordinary data.

    Padding (NUL, spaces) is stripped by the caller; a placeholder such as
    "0000000000" or 04 00 00 … has too few distinct bytes and would match
    firmware strings or zero-filled session blobs.
    """
    return len(pattern) >= 4 and len(set(pattern)) >= 3


def _identifier_patterns(
    values: dict[str, bytes | None], addresses: list[str | None]
) -> tuple[set[bytes], list[re.Pattern[bytes]]]:
    """Byte patterns and text regexes for the unit's identifiers."""
    raw_patterns: set[bytes] = set()
    hex_texts: list[bytes] = []
    for mac in _mac_bytes(addresses):
        for order in (mac, mac[::-1]):
            raw_patterns.add(order)
            hex_texts.append(order.hex().encode())
            # EUI-64 form, as in a System ID derived from the MAC
            eui64 = order[:3] + b"\xff\xfe" + order[3:]
            raw_patterns.update({eui64, eui64[::-1]})
    for uuid in PRIVATE_CHARS:
        raw = values.get(uuid)
        if not raw:
            continue
        stripped = raw.strip(b"\x00 ")
        for candidate in (raw, stripped):
            if _is_distinctive(candidate):
                raw_patterns.add(candidate)
        if _is_distinctive(stripped) and uuid not in TEXT_CHARS:
            hex_texts.append(stripped.hex().encode())
    regexes = [
        # hex text in either case, bytes optionally separated by ':' or '-'
        re.compile(
            rb"[:\-]?".join(re.escape(text[i:i + 2]) for i in range(0, len(text), 2)),
            re.IGNORECASE,
        )
        for text in hex_texts
    ]
    return raw_patterns, regexes


def scrub_values(
    values: dict[str, bytes | None], addresses: list[str | None] = ()
) -> dict[str, bytes | None]:
    """Replace values that identify the unit, wherever they turn up.

    The serial numbers are replaced in their own characteristics; their raw
    bytes and the brush's MAC addresses (both byte orders, EUI-64 form, and
    as hex text in any case with or without ':'/'-') are also blanked out of
    every other value, for models that mirror them somewhere we do not know
    about.
    """
    raw_patterns, regexes = _identifier_patterns(values, list(addresses))
    out: dict[str, bytes | None] = {}
    for uuid, raw in values.items():
        if raw is None:
            out[uuid] = None
            continue
        if uuid in PRIVATE_CHARS:
            out[uuid] = _private_value(uuid, raw) if raw else raw
            continue
        for pattern in sorted(raw_patterns, key=len, reverse=True):
            if pattern in raw:
                raw = raw.replace(pattern, bytes(len(pattern)))
        for regex in regexes:
            raw = regex.sub(lambda m: bytes(len(m.group(0))), raw)
        out[uuid] = raw
    return out


def readable_chars(services: list[dict[str, Any]]) -> list[str]:
    """UUIDs of all characteristics with the read property, without repeats."""
    seen: dict[str, None] = {}
    for service in services:
        for char in service["characteristics"]:
            if "read" in char["properties"]:
                seen.setdefault(char["uuid"], None)
    return list(seen)


# Requests per read batch. The bridge queues at most 64 calls
# (MAX_PENDING_CALLS) and live notifications share that queue; a full
# Classic table has more readable characteristics than that.
READ_BATCH = 12


async def async_read_values(
    transport: Any, readable: list[str]
) -> dict[str, bytes | None]:
    """Read every characteristic in small batches, retry gaps one by one.

    Stops as soon as the brush disconnects instead of letting each
    remaining read run into its timeout. A characteristic that answered
    with 0 bytes (blank Hardware/Software Revision on some models) comes
    back as b"", not None.
    """
    values: dict[str, bytes | None] = {}
    for i in range(0, len(readable), READ_BATCH):
        if not transport.is_connected:
            return values
        values.update(await transport.read_chars(readable[i:i + READ_BATCH]))
    was_empty = getattr(transport, "read_was_empty", lambda _uuid: False)
    for char_uuid in [u for u in readable if values.get(u) is None]:
        if was_empty(char_uuid):
            values[char_uuid] = b""
            continue
        if not transport.is_connected:
            break
        value = await transport.read_char(char_uuid)
        values[char_uuid] = b"" if value is None and was_empty(char_uuid) else value
    return values


def _protocol(service_uuids: set[str]) -> str:
    classic = any(u.startswith(_CLASSIC_PREFIX) for u in service_uuids)
    condor = _const.SVC_CONDOR in service_uuids
    if classic and condor:
        return "Both Classic + Condor (supported by philips_sonicare_ble)"
    if classic:
        return "Classic (supported by philips_sonicare_ble)"
    if condor:
        return "Condor (supported by philips_sonicare_ble)"
    return "Unknown"


def build_snapshot(
    services: list[dict[str, Any]],
    values: dict[str, bytes | None],
    *,
    source: str = "esp_bridge",
    addresses: list[str | None] = (),
) -> dict[str, Any]:
    """Assemble an anonymized capture from the GATT table and read values.

    ``addresses`` are the brush's MAC addresses (detected, bond, identity);
    they and the serial numbers are blanked out of every value.
    """
    values = scrub_values(values, list(addresses))
    device_info: dict[str, str] = {}
    gatt_services: list[dict[str, Any]] = []
    for service in services:
        chars = []
        for char in service["characteristics"]:
            uuid = char["uuid"]
            name = _char_name(uuid)
            raw = values.get(uuid)
            value_hex = raw.hex() if raw is not None else None
            value_text = text_value(raw) if raw and uuid in TEXT_CHARS else None
            if uuid in _DEVICE_INFO_UUIDS and value_hex is not None and name:
                if uuid == _BATTERY_LEVEL and raw:
                    device_info[name] = f"{raw[0]}%"
                elif uuid in TEXT_CHARS:
                    device_info[name] = value_text or ""
                else:
                    device_info[name] = value_hex
            chars.append({
                "uuid": uuid,
                "name": name,
                "properties": list(char["properties"]),
                # Fixtures carry the declaration handle (as BlueZ/bleak
                # report it); the bridge sends the value handle, one above.
                "handle": (
                    char["value_handle"] - 1
                    if char.get("value_handle") is not None
                    else None
                ),
                "value_hex": value_hex,
                "value_text": value_text,
            })
        gatt_services.append({"uuid": service["uuid"], "characteristics": chars})

    device_name = next(
        (
            c["value_text"]
            for s in gatt_services for c in s["characteristics"]
            if c["uuid"] == _DEVICE_NAME
        ),
        None,
    )
    return {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "address": FIXTURE_MAC,
        # Not the advertised name (the bridge does not pass that on) but the
        # GAP Device Name, when the brush exposes it.
        "adv_name": device_name,
        "protocol": _protocol({s["uuid"] for s in services}),
        "source": source,
        "device_info": device_info,
        "condor": {},
        "gatt_services": gatt_services,
    }
