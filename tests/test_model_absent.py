"""A handle that never told us its model number must still set up.

The Device Information Service is optional. A Condor handle can carry the
whole service tree without it — the model name arrives later, in-band, on
the firmware port. Until it does, the config entry holds ``model: None``,
which is not the same as the key being absent: ``get("model", "")`` hands
back that ``None`` and every model test downstream trips over it.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.philips_sonicare_ble import select, sensor, switch
from custom_components.philips_sonicare_ble.const import (
    CONF_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_ESP_DEVICE_NAME,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    SVC_CONDOR,
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


def make_entry(hass) -> MockConfigEntry:
    """A Condor entry as the flow writes it when 0x180A is absent."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_TRANSPORT_TYPE: TRANSPORT_ESP_BRIDGE,
            CONF_ESP_DEVICE_NAME: "sonicare-bridge",
            CONF_DEVICE_NAME: "Sonicare",
            CONF_SERVICES: [SVC_CONDOR],
            "model": None,
        },
    )
    entry.add_to_hass(hass)
    return entry


def test_coordinator_starts_without_a_model(hass) -> None:
    coordinator = PhilipsSonicareCoordinator(hass, make_entry(hass), StubTransport())

    assert coordinator is not None


@pytest.mark.parametrize("platform", (sensor, select, switch))
async def test_platforms_set_up_without_a_model(hass, platform) -> None:
    entry = make_entry(hass)
    coordinator = PhilipsSonicareCoordinator(hass, entry, StubTransport())
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coordinator}

    added: list = []
    await platform.async_setup_entry(hass, entry, lambda new, *_: added.extend(new))
