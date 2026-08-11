"""Tests for the ESPHome device lookup that feeds ``via_device_id``.

Home Assistant 2026.8 gave every device a single owning config entry and split
the ones that had several. An ESP running ``bluetooth_proxy`` is exactly that
case — its registry entry was shared by the esphome and the bluetooth entry —
so a lookup by MAC connection alone now matches both splits and is answered
with a synthesized read-only composite whose id refers to no registered
device. Passing that id as ``via_device_id`` is what HA deprecates: it
resolves to an arbitrary split today and stops resolving in 2027.8.

The pinned test stack is the HA 2025.1 line, where a device may still belong to
several config entries — the split cannot be represented there at all, and
``async_get_device_by_connection`` does not exist. The registry doubles below
supply the two shapes instead, so both branches of the fix are covered on the
pinned core. Two tests keep that from becoming self-fulfilling:
``test_the_double_reproduces_the_deprecated_lookup`` asserts the double really
does hand out an unresolvable composite id, and
``test_call_matches_the_core_signature`` starts checking the real API the
moment the pin moves to a core that has it.
"""

from __future__ import annotations

import inspect

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.philips_sonicare_ble import (
    _async_get_esphome_device,
    _async_link_via_esp_device,
)
from custom_components.philips_sonicare_ble.const import (
    CONF_ADDRESS,
    CONF_ESP_DEVICE_NAME,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    TRANSPORT_ESP_BRIDGE,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
ESP_MAC = "24:0a:c4:11:22:33"
ESP_DEVICE_NAME = "sonicare_bridge"  # service-id form, as we store it
ESP_MDNS_NAME = "sonicare-bridge"  # mDNS form, as ESPHome stores it
MAC_CONNECTION = (dr.CONNECTION_NETWORK_MAC, ESP_MAC)

# The shape HA names in its deprecation warning: the id of a pre-migration
# composite device. No device carries it — that is the whole complaint.
COMPOSITE_ID = "83b235700dcc7bc24248fad9d4e3ae50"


def add_esphome_entry(hass) -> MockConfigEntry:
    """The ESPHome config entry for the bridge, keyed by MAC as ESPHome does."""
    entry = MockConfigEntry(
        domain="esphome",
        data={"device_name": ESP_MDNS_NAME},
        unique_id=ESP_MAC,
    )
    entry.add_to_hass(hass)
    return entry


def add_bluetooth_entry(hass) -> MockConfigEntry:
    """The bluetooth config entry for the same ESP's proxy half."""
    entry = MockConfigEntry(domain="bluetooth", data={}, unique_id=ESP_MAC)
    entry.add_to_hass(hass)
    return entry


def add_sonicare_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_TRANSPORT_TYPE: TRANSPORT_ESP_BRIDGE,
            CONF_ESP_DEVICE_NAME: ESP_DEVICE_NAME,
        },
    )
    entry.add_to_hass(hass)
    return entry


class _RegistryDouble:
    """Base for the two registry shapes; delegates what it does not model."""

    def __init__(self, hass) -> None:
        self._real = dr.async_get(hass)

    def __getattr__(self, name: str):
        # async_get_device_by_connection must never leak through from the real
        # registry: whether it exists is what picks the branch under test, so
        # a future pin bump would otherwise silently rewire these tests.
        if name == "async_get_device_by_connection":
            raise AttributeError(name)
        return getattr(self._real, name)


class PreSplitRegistry(_RegistryDouble):
    """A core before 2026.8: one device, shared by both config entries.

    Nothing to disambiguate here, and no scoped lookup to do it with.
    """

    def __init__(self, hass, esp_device: dr.DeviceEntry) -> None:
        super().__init__(hass)
        self._esp_device = esp_device

    def async_get_device(self, identifiers=None, connections=None):
        if connections == {MAC_CONNECTION}:
            return self._esp_device
        return self._real.async_get_device(
            identifiers=identifiers, connections=connections
        )


class SplitRegistry(_RegistryDouble):
    """A core from 2026.8 on: the ESP's entry is split, one device per entry."""

    def __init__(self, hass, splits: dict[str, dr.DeviceEntry]) -> None:
        super().__init__(hass)
        self._splits = splits
        # HA synthesizes this on the fly and never registers it; its id is the
        # pre-migration device id, which is why it no longer resolves.
        self._composite = dr.DeviceEntry(
            id=COMPOSITE_ID, connections={MAC_CONNECTION}
        )

    def async_get_device(self, identifiers=None, connections=None):
        """By connection alone both splits match — answered with a composite."""
        if connections == {MAC_CONNECTION}:
            return self._composite
        return self._real.async_get_device(
            identifiers=identifiers, connections=connections
        )

    def async_get_device_by_connection(self, connection, config_entry_id):
        """Scoped to one entry, where connections are unique."""
        if connection != MAC_CONNECTION:
            return None
        return self._splits.get(config_entry_id)


def register_splits(hass, esphome_entry, bluetooth_entry):
    """Register the two halves the 2026.8 migration would have produced.

    Only the ESPHome half carries the MAC connection here — on the pinned core
    a second device claiming it would be merged into the first, and the
    ambiguity that merge papers over is precisely what ``SplitRegistry``
    models. The bluetooth half only has to exist and be resolvable, so that
    "the fallback picks an arbitrary split" is a real possibility.
    """
    reg = dr.async_get(hass)
    esphome_split = reg.async_get_or_create(
        config_entry_id=esphome_entry.entry_id,
        connections={MAC_CONNECTION},
        name=ESP_MDNS_NAME,
    )
    bluetooth_split = reg.async_get_or_create(
        config_entry_id=bluetooth_entry.entry_id,
        identifiers={("bluetooth", ESP_MAC)},
        name=f"{ESP_MDNS_NAME} (Bluetooth)",
    )
    return esphome_split, bluetooth_split


def test_pre_split_core_uses_the_plain_lookup(hass) -> None:
    """On an older core the previous call is still the correct one."""
    esp_entry = add_esphome_entry(hass)
    esp_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=esp_entry.entry_id, connections={MAC_CONNECTION}
    )

    found = _async_get_esphome_device(
        PreSplitRegistry(hass, esp_device), ESP_MAC, esp_entry
    )

    assert found is esp_device


def test_split_core_returns_the_esphome_node(hass) -> None:
    """The ESPHome half, not the composite spanning it and the proxy half."""
    esp_entry = add_esphome_entry(hass)
    bt_entry = add_bluetooth_entry(hass)
    esphome_split, bluetooth_split = register_splits(hass, esp_entry, bt_entry)
    double = SplitRegistry(
        hass,
        {esp_entry.entry_id: esphome_split, bt_entry.entry_id: bluetooth_split},
    )

    found = _async_get_esphome_device(double, ESP_MAC, esp_entry)

    assert found is esphome_split
    # The property the deprecation is actually about: the id we are about to
    # store points at a device that is registered.
    assert dr.async_get(hass).async_get(found.id) is not None


def test_the_double_reproduces_the_deprecated_lookup(hass) -> None:
    """Without the scope, the lookup hands back what HA warns about.

    This is what earns ``SplitRegistry`` its keep: if the composite it returns
    were resolvable, the test above would pass for the wrong reason.
    """
    esp_entry = add_esphome_entry(hass)
    bt_entry = add_bluetooth_entry(hass)
    esphome_split, bluetooth_split = register_splits(hass, esp_entry, bt_entry)
    double = SplitRegistry(
        hass,
        {esp_entry.entry_id: esphome_split, bt_entry.entry_id: bluetooth_split},
    )

    composite = double.async_get_device(connections={MAC_CONNECTION})

    assert composite.id == COMPOSITE_ID
    assert dr.async_get(hass).async_get(composite.id) is None


def test_link_lands_on_the_esphome_node(hass, monkeypatch) -> None:
    """End to end: both our devices hang off the ESPHome node afterwards.

    The bridge name also crosses the mDNS/service-id gap here — we store
    ``sonicare_bridge``, ESPHome stores ``sonicare-bridge``.
    """
    esp_entry = add_esphome_entry(hass)
    bt_entry = add_bluetooth_entry(hass)
    sonicare_entry = add_sonicare_entry(hass)
    esphome_split, bluetooth_split = register_splits(hass, esp_entry, bt_entry)

    real = dr.async_get(hass)
    sonicare_device = real.async_get_or_create(
        config_entry_id=sonicare_entry.entry_id, identifiers={(DOMAIN, ADDRESS)}
    )
    bridge_device = real.async_get_or_create(
        config_entry_id=sonicare_entry.entry_id,
        identifiers={(DOMAIN, f"{ADDRESS}_bridge")},
    )
    double = SplitRegistry(
        hass,
        {esp_entry.entry_id: esphome_split, bt_entry.entry_id: bluetooth_split},
    )
    monkeypatch.setattr(dr, "async_get", lambda _hass: double)

    _async_link_via_esp_device(hass, sonicare_entry)

    assert real.async_get(sonicare_device.id).via_device_id == esphome_split.id
    assert real.async_get(bridge_device.id).via_device_id == esphome_split.id


def test_missing_esphome_device_leaves_the_link_alone(hass) -> None:
    """Nothing to link to: the ESPHome node is not in the registry at all.

    Whichever branch runs, a lookup that comes up empty has to leave the link
    unset rather than reach for something adjacent.
    """
    esp_entry = add_esphome_entry(hass)
    sonicare_entry = add_sonicare_entry(hass)
    sonicare_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=sonicare_entry.entry_id, identifiers={(DOMAIN, ADDRESS)}
    )

    _async_link_via_esp_device(hass, sonicare_entry)

    assert dr.async_get(hass).async_get(sonicare_device.id).via_device_id is None


@pytest.mark.skipif(
    not hasattr(dr.DeviceRegistry, "async_get_device_by_connection"),
    reason="core predates 2026.8; the fix takes its fallback branch there",
)
def test_call_matches_the_core_signature() -> None:
    """The one check that talks to the installed core rather than a double.

    Everything above models 2026.8 instead of running it, so this is what
    would catch the call itself being wrong once the pinned stack moves to a
    core that has the method.
    """
    signature = inspect.signature(dr.DeviceRegistry.async_get_device_by_connection)

    signature.bind(object(), MAC_CONNECTION, "an_entry_id")
