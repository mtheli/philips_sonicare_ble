"""Direct-BLE entries keyed on the brush's identity, like bridge entries.

A Condor brush is found under a rotating address. Pairing hands BlueZ its
identity key, after which BlueZ files the device under the identity and the
found address is gone (live 2026-10-30: entry on 74:14:48:41:AE:09 timed
out forever, BlueZ only knew 24:E5:AA:92:32:F9).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.philips_sonicare_ble import config_flow as cf
from custom_components.philips_sonicare_ble.const import (
    CONF_BOND_ADDRESS,
    CONF_TRANSPORT_TYPE,
    TRANSPORT_BLEAK,
)

RPA = "74:14:48:41:AE:09"
IDENTITY = "24:E5:AA:92:32:F9"


def _flow():
    flow = cf.PhilipsSonicareConfigFlow()
    flow.flow_id = "setup-flow"
    flow.handler = "philips_sonicare_ble"
    flow.hass = SimpleNamespace(config=SimpleNamespace(components=set()))
    flow.context = {}
    flow._address = RPA
    flow._name = "Philips Sonicare"
    flow._build_default_name = lambda: "Bad"
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_already_configured = MagicMock()
    return flow


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("24:e5:aa:92:32:f9", IDENTITY),
        ("00:00:00:00:00:00", ""),
        ("/org/bluez/hci1/dev_24", ""),
        (None, ""),
    ],
)
def test_valid_identity(raw, expected) -> None:
    assert cf._valid_identity(raw) == expected


async def test_probe_identity_keys_the_entry() -> None:
    flow = _flow()
    flow._ble_probe_result = {
        "ok": True,
        "data": {"model": "HX742X", "connection_path": "hci1", "identity": IDENTITY},
    }
    flow._ble_probe_origin = "bluetooth_confirm"
    flow._has_sonicare_services = MagicMock(return_value=True)
    flow.async_step_show_capabilities = AsyncMock(return_value={"type": "caps"})

    await flow.async_step_ble_probe_finish()

    assert flow._address == IDENTITY
    assert flow._bond_address == RPA
    flow.async_set_unique_id.assert_awaited_once_with(
        IDENTITY, raise_on_progress=False
    )
    flow._abort_if_already_configured.assert_called_once()


async def test_without_identity_nothing_changes() -> None:
    flow = _flow()

    await flow._adopt_identity(None)
    await flow._adopt_identity(RPA.lower())

    assert flow._address == RPA
    assert flow._bond_address == ""
    flow.async_set_unique_id.assert_not_called()


async def test_entry_stores_identity_and_bond_address() -> None:
    flow = _flow()
    flow._transport_type = TRANSPORT_BLEAK
    flow._fetched_data = {"model": "HX742X", "services": [], "connection_path": "hci1"}
    await flow._adopt_identity(IDENTITY)

    result = await flow.async_step_show_capabilities({"device_name": "Bad"})

    assert result["data"]["address"] == IDENTITY
    assert result["data"][CONF_BOND_ADDRESS] == RPA
    assert result["data"][CONF_TRANSPORT_TYPE] == TRANSPORT_BLEAK


async def test_fixed_address_entry_has_no_bond_address() -> None:
    flow = _flow()
    flow._address = IDENTITY
    flow._transport_type = TRANSPORT_BLEAK
    flow._fetched_data = {"model": "HX9992", "services": [], "connection_path": "hci0"}

    result = await flow.async_step_show_capabilities({"device_name": "Bad"})

    assert CONF_BOND_ADDRESS not in result["data"]
