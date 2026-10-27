"""The bridge status table shows the identity a brush handed over at pairing.

Bridge firmware 1.15.0+ reports it as peer_identity. A brush with a rotating
address is bonded under one of its private addresses, so the MAC row shows
that address and an extra row shows the identity. For a brush with a fixed
address both are the same, and the extra row stays away.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
    _identity_row,
)

TEXT = {"error.identity_row": "| **Identity** | `{identity}` |"}

BONDED_INFO = {
    "version": "1.15.0",
    "mac": "77:F0:B2:D9:FD:17",
    "paired": "true",
    "ble_connected": "false",
    "pair_capable": "false",
}


@pytest.mark.parametrize(
    ("identity", "mac", "expected"),
    [
        (
            "24:e5:aa:92:32:f9",
            "77:F0:B2:D9:FD:17",
            "\n| **Identity** | `24:E5:AA:92:32:F9` |",
        ),
        ("24:E5:AA:00:D8:0E", "24:e5:aa:00:d8:0e", ""),  # fixed address
        ("", "77:F0:B2:D9:FD:17", ""),  # bridge firmware before 1.15.0
    ],
)
def test_identity_row(identity, mac, expected) -> None:
    assert _identity_row(TEXT, identity, mac) == expected


def test_identity_row_without_translation() -> None:
    assert _identity_row({}, "24:E5:AA:92:32:F9", "77:F0:B2:D9:FD:17") == ""


def _flow() -> PhilipsSonicareConfigFlow:
    flow = PhilipsSonicareConfigFlow()
    flow.flow_id = "test-flow"
    flow.handler = "philips_sonicare_ble"
    flow._esp_device_name = "atom-s3r"
    flow._esp_bridge_id = "sonicare_1"
    flow.hass = SimpleNamespace(
        config=SimpleNamespace(components=set()),
        services=SimpleNamespace(async_call=AsyncMock()),
    )
    return flow


@pytest.mark.parametrize(
    ("peer_identity", "shown"),
    [("24:E5:AA:92:32:F9", True), ("77:F0:B2:D9:FD:17", False), (None, False)],
)
async def test_status_table_identity_row(
    flow_text_blocks, peer_identity, shown
) -> None:
    flow = _flow()
    flow._bridge_info = dict(BONDED_INFO)
    if peer_identity is not None:
        flow._bridge_info["peer_identity"] = peer_identity

    result = await flow.async_step_esp_bridge_status()
    row = result["description_placeholders"]["identity_row"]
    assert ("24:E5:AA:92:32:F9" in row) is shown
    if not shown:
        assert row == ""
