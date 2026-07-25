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


@pytest.fixture(autouse=True)
def flow_text_blocks(monkeypatch) -> dict[str, str]:
    """Serve the real text blocks to the config-flow tests.

    In production the flow resolves these through Home Assistant's
    translation cache. The lightweight ``hass`` doubles used here have no
    such cache, so without this the flow would fall back on empty strings
    and the tests could not tell a correct block from a missing one.
    """
    import custom_components.philips_sonicare_ble.config_flow as cf

    strings = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components"
            / "philips_sonicare_ble"
            / "strings.json"
        ).read_text(encoding="utf-8")
    )
    # The reusable blocks live under config.error — hassfest validates
    # strings.json against a fixed schema and rejects a section of our own.
    blocks = strings["config"]["error"]

    def _flatten(obj, prefix=""):
        """Mirror how HA keys a category: dotted path below the domain."""
        out = {}
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                out.update(_flatten(value, path))
            elif isinstance(value, str):
                out[path] = value
        return out

    async def _fake_text_blocks(hass, category="config"):
        return _flatten(strings.get(category, {}))

    # Kept reachable so the resolver itself stays testable — see
    # ``unpatched_text_blocks``.
    _fake_text_blocks.unpatched = cf._async_text_blocks
    monkeypatch.setattr(cf, "_async_text_blocks", _fake_text_blocks)
    return blocks


@pytest.fixture
def error_texts() -> dict[str, str]:
    """The ``config.error`` strings, which schema-less forms show as alerts."""
    strings = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components"
            / "philips_sonicare_ble"
            / "strings.json"
        ).read_text(encoding="utf-8")
    )
    return strings["config"]["error"]


@pytest.fixture
def unpatched_text_blocks():
    """The real block resolver, which ``flow_text_blocks`` patches out."""
    import custom_components.philips_sonicare_ble.config_flow as cf

    return cf._async_text_blocks.unpatched


@pytest.fixture
def condor_hx742x() -> dict[str, Any]:
    """A full Condor (newer protocol) probe snapshot from an HX742X brush."""
    return load_json_fixture("condor_hx742x.json")


@pytest.fixture
def classic_hx6340() -> dict[str, Any]:
    """A Classic (legacy protocol) snapshot from a Sonicare for Kids HX6340."""
    return load_json_fixture("classic_hx6340_kids.json")
