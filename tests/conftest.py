"""Shared pytest fixtures for the Philips Sonicare BLE tests."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# --- HA test-env compat shim --------------------------------------------
# The pinned test environment ships an older Home Assistant core where
# ZeroconfServiceInfo still lives in homeassistant.components.zeroconf.
# config_flow imports the current location (helpers.service_info.zeroconf,
# HA >= 2025.2); provide it so the module stays importable here.
try:
    import homeassistant.helpers.service_info.zeroconf  # noqa: F401
except ModuleNotFoundError:
    _zc = types.ModuleType("homeassistant.helpers.service_info.zeroconf")

    class _ZeroconfServiceInfo:
        """Stand-in — only referenced in type annotations."""

    _zc.ZeroconfServiceInfo = _ZeroconfServiceInfo
    sys.modules["homeassistant.helpers.service_info.zeroconf"] = _zc

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_json_fixture(name: str) -> dict[str, Any]:
    """Load a captured probe snapshot from ``tests/fixtures``.

    These files are produced by ``scripts/sonicare_scan.py --json`` against a
    real device, so they double as golden inputs for the protocol adapters.
    """
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def chars_as_bytes(snapshot: dict[str, Any]) -> dict[str, bytes]:
    """Flatten a snapshot's readable GATT characteristics into ``{uuid: bytes}``.

    This is the shape ``ClassicProtocol.parse_results`` consumes, so a captured
    Classic snapshot feeds straight in.
    """
    out: dict[str, bytes] = {}
    for service in snapshot["gatt_services"]:
        for char in service["characteristics"]:
            hex_value = char.get("value_hex")
            if hex_value:
                out[char["uuid"]] = bytes.fromhex(hex_value)
    return out


@pytest.fixture
def condor_hx742x() -> dict[str, Any]:
    """A full Condor (newer protocol) probe snapshot from an HX742X brush."""
    return load_json_fixture("condor_hx742x.json")


@pytest.fixture
def classic_hx6340() -> dict[str, Any]:
    """A Classic (legacy protocol) snapshot from a Sonicare for Kids HX6340."""
    return load_json_fixture("classic_hx6340_kids.json")
