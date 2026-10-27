"""The setup flow warns when a bridge cannot follow a rotating address.

On every chip but the original ESP32 the Bluetooth controller resolves
rotating addresses, so a brush bonded under one does not reconnect after
the first session. Bridge firmware 1.14.2+ reports that in pair_complete
(rpa=true, addr_res=ctrl); the status screen after pairing then shows a
warning with the build option instead of the success notice.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)
from custom_components.philips_sonicare_ble.const import (
    ROTATING_ADDRESS_ISSUE_URL,
)

BONDED_INFO = {
    "version": "1.14.2",
    "mac": "72:DD:5C:66:1F:88",
    "paired": "true",
    "ble_connected": "false",
    "pair_capable": "false",
}


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


async def _finish(result: dict) -> PhilipsSonicareConfigFlow:
    flow = _flow()
    flow._pair_result = result
    flow._pair_unsub = MagicMock()
    flow._pair_svc_name = "atom-s3r_ble_pair_mode_sonicare_1"
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_already_configured = MagicMock()
    flow._abort_if_esp_entry_configured = MagicMock()
    flow._esp_bridge_health_check = AsyncMock(return_value={"type": "sentinel"})
    await flow.async_step_pair_finish()
    return flow


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"rpa": "true", "addr_res": "ctrl"}, True),
        ({"rpa": "true", "addr_res": "host"}, False),
        ({"rpa": "true", "addr_res": "ctrl_priv"}, False),
        ({"rpa": "false", "addr_res": "ctrl"}, False),
        ({}, False),  # bridge firmware before 1.14.2
    ],
)
async def test_pair_finish_flags_only_the_broken_constellation(extra, expected) -> None:
    flow = await _finish(
        {"status": "pair_complete", "identity_address": "72:DD:5C:66:1F:88", **extra}
    )
    assert flow._just_paired is True
    assert flow._paired_rpa_unsupported is expected


async def test_status_shows_warning_with_link_once(flow_text_blocks) -> None:
    flow = _flow()
    flow._bridge_info = dict(BONDED_INFO)
    flow._just_paired = True
    flow._paired_rpa_unsupported = True

    first = await flow.async_step_esp_bridge_status()
    alert = first["description_placeholders"]["alert"]
    assert alert.startswith('<ha-alert alert-type="warning">')
    assert "-DCONFIG_BT_BLE_RPA_SUPPORTED=0" in alert
    assert ROTATING_ADDRESS_ISSUE_URL in alert
    assert "{url}" not in alert
    assert flow._paired_rpa_unsupported is False

    second = await flow.async_step_esp_bridge_status()
    assert second["description_placeholders"]["alert"] == ""
