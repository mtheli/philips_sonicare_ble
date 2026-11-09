"""A pairing that stored no model gets it from the first live read.

The mode and intensity selects are gated on the entry's model. When the
pairing flow could not read the model number it stores an empty one, and
those platforms never set up. The first model the device reports is stored
on the entry once, followed by a reload.
"""

from __future__ import annotations

from unittest.mock import patch

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
    is_connected = False
    disconnect_count = 0


def make(hass, model: str):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_TRANSPORT_TYPE: TRANSPORT_ESP_BRIDGE,
            CONF_ESP_DEVICE_NAME: "sonicare-bridge",
            CONF_SERVICES: [],
            "model": model,
        },
    )
    entry.add_to_hass(hass)
    return PhilipsSonicareCoordinator(hass, entry, StubTransport()), entry


def test_empty_model_is_backfilled_and_reloaded(hass) -> None:
    coordinator, entry = make(hass, "")
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator._apply_parsed({"model_number": "HX999X"})
        coordinator._apply_parsed({"model_number": "HX999X"})
    assert entry.data["model"] == "HX999X"
    reload.assert_called_once_with(entry.entry_id)


def test_known_model_is_left_alone(hass) -> None:
    coordinator, entry = make(hass, "HX9996")
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        coordinator._apply_parsed({"model_number": "HX9996"})
    assert entry.data["model"] == "HX9996"
    reload.assert_not_called()
