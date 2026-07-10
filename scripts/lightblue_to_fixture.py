#!/usr/bin/env python3
"""Convert a LightBlue (PunchThrough) session log into a probe snapshot JSON.

Community reports often include a LightBlue log instead of a
``sonicare_scan.py --json`` capture. This tool converts such a log into the
same snapshot schema so it can be dropped into ``tests/fixtures`` and fed to
the structural GATT-layout tests.

LightBlue logs carry the full service/characteristic table with properties,
but usually no characteristic values and no ATT handles, so the resulting
snapshot is *structural*: ``value_hex`` stays ``null`` except for the few
characteristics the log shows an explicit read for, and ``handle`` is always
``null``.

Usage:
    python3 scripts/lightblue_to_fixture.py dump.txt \
        --address 24:E5:AA:00:00:06 > tests/fixtures/classic_hx993x.json

The ``--address`` flag replaces the device MAC from the log (fixture files
use anonymized addresses).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# LightBlue property labels → bleak-style property strings used in snapshots.
_PROPERTY_MAP = {
    "readable": "read",
    "writable": "write",
    "writable without response": "write-without-response",
    "notify": "notify",
    "indicate": "indicate",
}

_UUID = r"[0-9a-fA-F-]{36}"
_SERVICE_RE = re.compile(rf"\bService ({_UUID})\s*$")
_CHAR_RE = re.compile(rf"^\|--({_UUID}):\s*(.+)$")
_DESCRIPTOR_RE = re.compile(rf"^\|-{{3,}}")
_CONNECT_RE = re.compile(r"Connect(?:ing|ed) to ([0-9A-Fa-f:]{17})")
_READ_CHAR_RE = re.compile(
    rf"Read characteristic ({_UUID}) \| value: ([0-9A-Fa-f ]+)$"
)

# Standard characteristics whose read values populate ``device_info``.
_DEVICE_INFO_CHARS = {
    "00002a24-0000-1000-8000-00805f9b34fb": "Model Number",
    "00002a25-0000-1000-8000-00805f9b34fb": "Serial Number",
    "00002a26-0000-1000-8000-00805f9b34fb": "Firmware Revision",
    "00002a27-0000-1000-8000-00805f9b34fb": "Hardware Revision",
    "00002a28-0000-1000-8000-00805f9b34fb": "Software Revision",
    "00002a29-0000-1000-8000-00805f9b34fb": "Manufacturer Name",
}

CLASSIC_SERVICE_PREFIX = "477ea600-a260-11e4-ae37-0002a5d5"
CONDOR_SERVICE = "e50ba3c0-af04-4564-92ad-fef019489de6"


def _parse_properties(raw: str) -> list[str]:
    props: list[str] = []
    for part in raw.split(","):
        label = part.strip().lower()
        if not label:
            continue
        mapped = _PROPERTY_MAP.get(label)
        if mapped is None:
            print(f"warning: unknown property label {part.strip()!r}", file=sys.stderr)
            continue
        props.append(mapped)
    return props


def parse_lightblue_log(text: str) -> dict:
    """Parse a LightBlue session log into a snapshot dict."""
    services: list[dict] = []
    values: dict[str, str] = {}
    address: str | None = None
    current: dict | None = None

    for line in text.splitlines():
        line = line.strip().strip('"')
        if not line:
            continue

        if m := _CONNECT_RE.search(line):
            address = m.group(1).upper()
            continue

        if m := _SERVICE_RE.search(line):
            current = {"uuid": m.group(1).lower(), "characteristics": []}
            services.append(current)
            continue

        if _DESCRIPTOR_RE.match(line) and not _CHAR_RE.match(line):
            continue  # descriptor row — not part of the snapshot schema

        if m := _CHAR_RE.match(line):
            if current is None:
                print(f"warning: characteristic before any service: {line}", file=sys.stderr)
                continue
            current["characteristics"].append(
                {
                    "uuid": m.group(1).lower(),
                    "name": None,
                    "properties": _parse_properties(m.group(2)),
                    "handle": None,
                    "value_hex": None,
                    "value_text": None,
                }
            )
            continue

        if m := _READ_CHAR_RE.search(line):
            values[m.group(1).lower()] = m.group(2).replace(" ", "").lower()

    device_info: dict[str, str] = {}
    for service in services:
        for char in service["characteristics"]:
            hex_value = values.get(char["uuid"])
            if hex_value is None:
                continue
            char["value_hex"] = hex_value
            text_value = _printable(bytes.fromhex(hex_value))
            char["value_text"] = text_value
            if label := _DEVICE_INFO_CHARS.get(char["uuid"]):
                device_info[label] = text_value or ""

    service_uuids = {s["uuid"] for s in services}
    has_classic = any(u.startswith(CLASSIC_SERVICE_PREFIX) for u in service_uuids)
    has_condor = CONDOR_SERVICE in service_uuids
    if has_classic and has_condor:
        protocol = "Both Legacy + Newer"
    elif has_classic:
        protocol = "Legacy (supported by philips_sonicare_ble)"
    elif has_condor:
        protocol = "Newer (not yet supported)"
    else:
        protocol = "Unknown"

    return {
        "captured_at": None,
        "address": address,
        "adv_name": "Philips Sonicare",
        "protocol": protocol,
        "device_info": device_info,
        "condor": {},
        "gatt_services": services,
    }


def _printable(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text.isprintable() else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logfile", type=Path, help="LightBlue session log (text)")
    parser.add_argument("--address", help="replace the device address (anonymize)")
    parser.add_argument("--captured-at", help="ISO timestamp of the original capture")
    args = parser.parse_args()

    snapshot = parse_lightblue_log(args.logfile.read_text(encoding="utf-8"))
    if args.address:
        snapshot["address"] = args.address.upper()
    if args.captured_at:
        snapshot["captured_at"] = args.captured_at

    if not snapshot["gatt_services"]:
        sys.exit("error: no services found — is this a LightBlue log?")

    json.dump(snapshot, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
