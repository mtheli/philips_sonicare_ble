"""Wake on the fixed Bluetooth adapter's own packets (direct BLE).

HA's callback delivers one advertisement per change, from whichever
scanner the manager prefers. With hci1 fixed and a stronger hci0, the
coordinator woke on hci0's packet and connected through hci1, which did
not hear the brush (live 2026-10-30).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.philips_sonicare_ble import coordinator as coord_mod
from custom_components.philips_sonicare_ble.const import (
    BLUETOOTH_ADAPTER_AUTO,
    CONF_ADDRESS,
    CONF_BLUETOOTH_ADAPTER,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    TRANSPORT_BLEAK,
)

ADDRESS = "24:E5:AA:BE:9C:1B"
HCI1 = "00:0A:CD:46:B2:2D"


class StubTransport:
    is_connected = False
    disconnect_count = 0


def _coordinator(hass, options):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_TRANSPORT_TYPE: TRANSPORT_BLEAK,
            CONF_SERVICES: [],
            "model": "HX999X",
        },
        options=options,
    )
    entry.add_to_hass(hass)
    return coord_mod.PhilipsSonicareCoordinator(hass, entry, StubTransport())


@pytest.fixture
def packets(monkeypatch):
    """Capture the per-packet registration and HA's change callback."""
    state = SimpleNamespace(cb=None, source=None, unsub=MagicMock(), ha=MagicMock())

    def _register(address, source, cb):
        state.cb, state.source = cb, source
        return state.unsub

    monkeypatch.setattr(coord_mod, "register_advertisements", _register)
    monkeypatch.setattr(coord_mod, "async_register_callback", state.ha)
    return state


def _packet(rssi):
    return SimpleNamespace(address=ADDRESS, rssi=rssi, source=HCI1)


async def test_fixed_adapter_wakes_on_its_own_packets(hass, packets) -> None:
    coordinator = _coordinator(hass, {CONF_BLUETOOTH_ADAPTER: HCI1})

    coordinator._start_advertisement_callback()

    assert packets.source == HCI1
    packets.ha.assert_not_called()
    packets.cb(_packet(-127))
    assert not coordinator._wake_event.is_set()
    packets.cb(_packet(-88))
    assert coordinator._wake_event.is_set() and coordinator._adv_wake

    coordinator._unsub_adv_debug()
    packets.unsub.assert_called_once()


async def test_pending_wake_ignores_further_packets(hass, packets) -> None:
    coordinator = _coordinator(hass, {CONF_BLUETOOTH_ADAPTER: HCI1})
    coordinator._start_advertisement_callback()
    wakes = MagicMock(wraps=coordinator._handle_wake)
    coordinator._handle_wake = wakes

    for _ in range(5):
        packets.cb(_packet(-80))

    assert wakes.call_count == 1


@pytest.mark.parametrize("options", [{}, {CONF_BLUETOOTH_ADAPTER: BLUETOOTH_ADAPTER_AUTO}])
async def test_automatic_keeps_ha_callback(hass, packets, options) -> None:
    coordinator = _coordinator(hass, options)

    coordinator._start_advertisement_callback()

    assert packets.cb is None
    packets.ha.assert_called_once()


async def test_core_without_per_packet_callback_keeps_ha_callback(
    hass, packets, monkeypatch
) -> None:
    def _unsupported(*_args):
        raise LookupError("no async_register_advertisement_callback")

    monkeypatch.setattr(coord_mod, "register_advertisements", _unsupported)
    coordinator = _coordinator(hass, {CONF_BLUETOOTH_ADAPTER: HCI1})

    coordinator._start_advertisement_callback()

    packets.ha.assert_called_once()
