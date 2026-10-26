"""Tests for async_remove_entry: bond cleanup, then Bluetooth rediscovery.

HA re-offers a removed entry's device only when the entry carries a
Bluetooth discovery key. A brush added by hand and removed in the same
run has none, so the integration asks for rediscovery itself, after the
bond cleanup and regardless of how that cleanup ended.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.philips_sonicare_ble as integration
from custom_components.philips_sonicare_ble import dbus_pairing
from custom_components.philips_sonicare_ble.const import (
    CONF_ADDRESS,
    CONF_ESP_BRIDGE_ID,
    CONF_ESP_DEVICE_NAME,
    CONF_TRANSPORT_TYPE,
    TRANSPORT_ESP_BRIDGE,
)
from custom_components.philips_sonicare_ble.transport import UNPAIR_UNAVAILABLE


@pytest.fixture(autouse=True)
def _no_issue_registry(monkeypatch):
    """async_remove_entry clears this entry's repair issue; the lightweight
    hass double here has no issue registry."""
    from homeassistant.helpers import issue_registry as ir

    monkeypatch.setattr(ir, "async_delete_issue", lambda *a, **kw: None)


ADDRESS = "24:E5:AA:BE:9C:1B"


def _hass() -> SimpleNamespace:
    return SimpleNamespace()


def _entry(data: dict, unique_id: str | None = ADDRESS) -> SimpleNamespace:
    return SimpleNamespace(entry_id="removed", unique_id=unique_id, data=data)


def _patch_common(monkeypatch) -> MagicMock:
    monkeypatch.setattr(integration, "async_remove_stored_data", AsyncMock())
    rediscover = MagicMock()
    monkeypatch.setattr(integration, "async_rediscover_address", rediscover)
    return rediscover


async def test_direct_ble_rediscovers_after_host_unpair(monkeypatch) -> None:
    rediscover = _patch_common(monkeypatch)
    order: list[str] = []

    async def remove_device(address):
        order.append(f"unpair {address}")

    rediscover.side_effect = lambda hass, address: order.append(
        f"rediscover {address}"
    )
    monkeypatch.setattr(dbus_pairing, "is_dbus_available", lambda: True)
    monkeypatch.setattr(dbus_pairing, "async_remove_device", remove_device)

    await integration.async_remove_entry(_hass(), _entry({CONF_ADDRESS: ADDRESS}))

    assert order == [f"unpair {ADDRESS}", f"rediscover {ADDRESS}"]


async def test_rediscovers_even_when_host_unpair_fails(monkeypatch) -> None:
    rediscover = _patch_common(monkeypatch)
    monkeypatch.setattr(dbus_pairing, "is_dbus_available", lambda: True)
    monkeypatch.setattr(
        dbus_pairing,
        "async_remove_device",
        AsyncMock(side_effect=RuntimeError("bluez gone")),
    )
    hass = _hass()

    await integration.async_remove_entry(hass, _entry({CONF_ADDRESS: ADDRESS}))

    rediscover.assert_called_once_with(hass, ADDRESS)


async def test_rediscovers_without_dbus(monkeypatch) -> None:
    rediscover = _patch_common(monkeypatch)
    monkeypatch.setattr(dbus_pairing, "is_dbus_available", lambda: False)
    hass = _hass()

    await integration.async_remove_entry(hass, _entry({CONF_ADDRESS: ADDRESS}))

    rediscover.assert_called_once_with(hass, ADDRESS)


async def test_bridge_entry_rediscovers_when_bridge_is_offline(monkeypatch) -> None:
    rediscover = _patch_common(monkeypatch)
    unpair = AsyncMock(return_value=UNPAIR_UNAVAILABLE)
    monkeypatch.setattr(integration, "async_unpair_bridge_slot", unpair)
    hass = _hass()
    entry = _entry({
        CONF_TRANSPORT_TYPE: TRANSPORT_ESP_BRIDGE,
        CONF_ADDRESS: ADDRESS,
        CONF_ESP_DEVICE_NAME: "atom-lite",
        CONF_ESP_BRIDGE_ID: "prestige",
    })

    await integration.async_remove_entry(hass, entry)

    unpair.assert_awaited_once_with(hass, "atom_lite", "prestige")
    rediscover.assert_called_once_with(hass, ADDRESS)


async def test_no_rediscovery_without_any_address(monkeypatch) -> None:
    rediscover = _patch_common(monkeypatch)
    monkeypatch.setattr(dbus_pairing, "is_dbus_available", lambda: False)

    await integration.async_remove_entry(_hass(), _entry({}, unique_id=None))

    rediscover.assert_not_called()

