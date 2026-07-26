"""Tests for the device-registry fields the coordinator maintains.

The handle reports model, firmware, hardware revision and its own serial
number over the Device Information Service. All of them belong on the device
page, but a read that only answered part of them must never wipe what an
earlier read established — and handles that expose no serial answer with all
zeros, which is "unknown" rather than a serial worth showing.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.philips_sonicare_ble.const import (
    CONF_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_ESP_DEVICE_NAME,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    TRANSPORT_ESP_BRIDGE,
)
from custom_components.philips_sonicare_ble.coordinator import (
    PhilipsSonicareCoordinator,
)
from custom_components.philips_sonicare_ble.entity import (
    PhilipsBrushHeadEntity,
    PhilipsConnectionEntity,
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


def test_zero_hardware_revision_is_not_written(hass) -> None:
    """Condor answers 2a27 with "00", Classic with "" — neither is a value."""
    coordinator, entry = make_coordinator(hass)
    device = register_device(hass, entry)

    coordinator._apply_parsed(
        {"model_number": "HX742X", "hardware_revision": "00"}
    )
    coordinator._apply_parsed(
        {"model_number": "HX999X", "hardware_revision": ""}
    )

    device = dr.async_get(hass).async_get(device.id)
    assert device.hw_version is None


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


def test_nul_padded_serial_is_not_written(hass) -> None:
    """A QP4530 answers the serial characteristic with twenty NUL bytes.

    They decode to "\\x00\\x00…", which is neither empty nor made of
    ASCII zeros — the earlier check let it through and the device page
    ended up showing a "Serial number:" row with nothing after it.
    """
    coordinator, entry = make_coordinator(hass)
    device = register_device(hass, entry)

    coordinator._apply_parsed({"serial_number": "\x00" * 20})

    device = dr.async_get(hass).async_get(device.id)
    assert device.serial_number is None


def test_previously_written_padding_is_cleared(hass) -> None:
    """A value an older version wrote through must not linger.

    It would never be overwritten — the new read is correctly ignored, so
    without an explicit clear the blank row would stay forever.
    """
    coordinator, entry = make_coordinator(hass)
    device = register_device(hass, entry)
    dr.async_get(hass).async_update_device(device.id, serial_number="\x00" * 20)

    # A later read returns the same padding — the clear must still happen.
    coordinator._apply_parsed({"serial_number": "\x00" * 20})

    device = dr.async_get(hass).async_get(device.id)
    assert device.serial_number is None


def _sub_device_info(hass, cls):
    """Build a sub-device's DeviceInfo the way the platforms do."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_TRANSPORT_TYPE: TRANSPORT_ESP_BRIDGE,
            CONF_ESP_DEVICE_NAME: "sonicare-bridge",
            CONF_DEVICE_NAME: "Bathroom Sonicare",
        },
    )
    entry.add_to_hass(hass)
    coordinator = PhilipsSonicareCoordinator(hass, entry, StubTransport())
    return cls(coordinator, entry)._attr_device_info


def test_sub_device_names_are_translatable(hass) -> None:
    """Both sub-devices are named through a translation key, so the trailing
    words follow the interface language. The parent name rides along as a
    placeholder instead of being baked into a fixed string.
    """
    for cls, key in (
        (PhilipsBrushHeadEntity, "brush_head"),
        (PhilipsConnectionEntity, "connection"),
    ):
        info = _sub_device_info(hass, cls)
        assert info["translation_key"] == key
        assert info["translation_placeholders"] == {"device_name": "Bathroom Sonicare"}
        # A leftover "name" would win over the key and silently undo this.
        assert "name" not in info


def test_sub_device_keys_resolve_to_the_localized_names(hass) -> None:
    """End-to-end through the registry: the keys we set must actually resolve.

    A wrong key path or a renamed placeholder makes async_get_or_create fall
    back to the bare key — "brush_head", parent name lost — which the
    dict-level test above cannot see. Templates come from the real
    strings.json, so a renamed key or a missing device section fails here.
    """
    strings = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components/philips_sonicare_ble/strings.json"
        ).read_text(encoding="utf-8")
    )
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: ADDRESS})
    entry.add_to_hass(hass)

    for key, suffix in (("brush_head", "Brush Head"), ("connection", "Connection")):
        cached = {
            f"component.{DOMAIN}.device.{key}.name": strings["device"][key]["name"]
        }
        with patch(
            "homeassistant.helpers.device_registry.translation."
            "async_get_cached_translations",
            return_value=cached,
        ):
            device = dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{ADDRESS}_{key}")},
                translation_key=key,
                translation_placeholders={"device_name": "Bathroom Sonicare"},
            )
        assert device.name == f"Bathroom Sonicare {suffix}"
