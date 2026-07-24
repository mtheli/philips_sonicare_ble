"""Tests for the device-registry fields the coordinator maintains.

The handle reports model, firmware, hardware revision and its own serial
number over the Device Information Service. All of them belong on the device
page, but a read that only answered part of them must never wipe what an
earlier read established — and handles that expose no serial answer with all
zeros, which is "unknown" rather than a serial worth showing.
"""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.philips_sonicare_ble.const import (
    CONF_ADDRESS,
    CONF_ESP_DEVICE_NAME,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    TRANSPORT_ESP_BRIDGE,
)
from custom_components.philips_sonicare_ble.coordinator import (
    PhilipsSonicareCoordinator,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


class StubTransport:
    """Just enough transport for coordinator construction."""

    is_connected = False
    disconnect_count = 0


def make_coordinator(hass) -> tuple[PhilipsSonicareCoordinator, MockConfigEntry]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_TRANSPORT_TYPE: TRANSPORT_ESP_BRIDGE,
            CONF_ESP_DEVICE_NAME: "sonicare-bridge",
            CONF_SERVICES: [],
            "model": "HX9996",
        },
    )
    entry.add_to_hass(hass)
    return PhilipsSonicareCoordinator(hass, entry, StubTransport()), entry


def register_device(hass, entry: MockConfigEntry):
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, ADDRESS)},
    )


def test_identity_fields_land_on_the_device(hass) -> None:
    coordinator, entry = make_coordinator(hass)
    device = register_device(hass, entry)

    coordinator._apply_parsed(
        {
            "model_number": "HX9996",
            "firmware": "1.4.3",
            "hardware_revision": "B",
            "serial_number": "1A2B3C4D5E",
        }
    )

    device = dr.async_get(hass).async_get(device.id)
    assert device.model == "HX9996"
    assert device.sw_version == "1.4.3"
    assert device.hw_version == "B"
    assert device.serial_number == "1A2B3C4D5E"


def test_all_zero_serial_is_not_written(hass) -> None:
    """Handles without a serial answer with zeros — that is not a serial."""
    coordinator, entry = make_coordinator(hass)
    device = register_device(hass, entry)

    coordinator._apply_parsed(
        {
            "model_number": "HX9996",
            "serial_number": "0000000000",
        }
    )

    device = dr.async_get(hass).async_get(device.id)
    assert device.model == "HX9996"
    assert device.serial_number is None


def test_partial_read_keeps_previously_known_fields(hass) -> None:
    """A later read without firmware/serial must not clear them."""
    coordinator, entry = make_coordinator(hass)
    device = register_device(hass, entry)

    coordinator._apply_parsed(
        {
            "model_number": "HX9996",
            "firmware": "1.4.3",
            "hardware_revision": "B",
            "serial_number": "1A2B3C4D5E",
        }
    )
    coordinator._apply_parsed({"battery": 80})

    device = dr.async_get(hass).async_get(device.id)
    assert device.sw_version == "1.4.3"
    assert device.hw_version == "B"
    assert device.serial_number == "1A2B3C4D5E"


def test_model_falls_back_when_the_handle_reports_none(hass) -> None:
    """Firmware alone still names the device, as before."""
    coordinator, entry = make_coordinator(hass)
    device = register_device(hass, entry)

    coordinator._apply_parsed({"firmware": "1.4.3"})

    device = dr.async_get(hass).async_get(device.id)
    assert device.model == "Philips Sonicare"
    assert device.sw_version == "1.4.3"
