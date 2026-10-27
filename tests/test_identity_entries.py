"""ESP bridge entries keyed by the brush's identity.

A brush with a rotating address is bonded under one of its private
addresses, new with every pairing; bridge firmware 1.15.0 reports the
identity it keeps as peer_identity. New entries are keyed by it, entries
set up before switch to it at their next start, and pairing a known brush
again keeps its entry on its slot and is refused anywhere else.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.philips_sonicare_ble import _async_migrate_to_identity
from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)
from custom_components.philips_sonicare_ble.const import (
    CONF_ADDRESS,
    CONF_BOND_ADDRESS,
    CONF_ESP_BRIDGE_ID,
    CONF_ESP_DEVICE_NAME,
    CONF_PENDING_IDENTITY,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    TRANSPORT_ESP_BRIDGE,
)
from custom_components.philips_sonicare_ble.coordinator import (
    PhilipsSonicareCoordinator,
)
from custom_components.philips_sonicare_ble.transport import (
    EspBridgeTransport,
    bridge_bond_address,
    bridge_identity,
)

BOND = "77:F0:B2:D9:FD:17"
NEW_BOND = "76:99:0A:0D:D3:32"
IDENTITY = "24:E5:AA:92:32:F9"


# ── Reading the identity from bridge events ────────────────────────────────


@pytest.mark.parametrize(
    ("event", "identity", "bond"),
    [
        (
            {"peer_identity": IDENTITY, "identity_address": BOND, "mac": BOND},
            IDENTITY,
            BOND,
        ),
        ({"identity_address": BOND, "mac": BOND}, BOND, BOND),  # before 1.15.0
        ({"peer_identity": "", "identity_address": "", "mac": BOND}, BOND, BOND),
        (
            {"peer_identity": "", "identity_address": "",
             "mac": "00:00:00:00:00:00"},
            "",
            "",
        ),
    ],
)
def test_bridge_identity_and_bond(event, identity, bond) -> None:
    assert bridge_identity(event) == identity
    assert bridge_bond_address(event) == bond


# ── Transport: bond address, identity report ───────────────────────────────


def _transport(address: str, bond: str | None = None) -> EspBridgeTransport:
    transport = EspBridgeTransport.__new__(EspBridgeTransport)
    transport._detected_mac = address
    transport._bond_address = bond
    transport._identity_cb = None
    return transport


@pytest.mark.parametrize(
    ("event", "other"),
    [
        ({"mac": NEW_BOND, "peer_identity": IDENTITY}, False),
        # A bridge that stops naming the identity: matched by the bond address.
        ({"mac": BOND}, False),
        ({"mac": NEW_BOND}, True),
        ({"mac": NEW_BOND, "peer_identity": "11:22:33:44:55:66"}, True),
    ],
)
def test_identity_entry_filter(event, other) -> None:
    assert _transport(IDENTITY, bond=BOND)._is_other_brush(event) is other


def test_follows_new_bond_address_and_reports_identity() -> None:
    transport = _transport(IDENTITY, bond=BOND)
    cb = MagicMock()
    transport.set_identity_callback(cb)
    transport._note_identity({"mac": NEW_BOND, "peer_identity": IDENTITY})
    assert transport.bond_address == NEW_BOND
    cb.assert_called_once_with(IDENTITY, NEW_BOND)
    # Unpaired slot and bridges before 1.15.0 report nothing.
    cb.reset_mock()
    transport._note_identity({"mac": "00:00:00:00:00:00", "peer_identity": ""})
    transport._note_identity({"mac": BOND})
    cb.assert_not_called()


# ── Coordinator: what the entry remembers ───────────────────────────────────


def _bridge_entry(hass, address: str, **extra) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=address,
        title="Philips Sonicare (Sonicare 7100)",
        data={
            CONF_ADDRESS: address,
            CONF_TRANSPORT_TYPE: TRANSPORT_ESP_BRIDGE,
            CONF_ESP_DEVICE_NAME: "atom_s3r",
            CONF_ESP_BRIDGE_ID: "hx742a_s3",
            **extra,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator(hass, entry) -> PhilipsSonicareCoordinator:
    coord = PhilipsSonicareCoordinator.__new__(PhilipsSonicareCoordinator)
    coord.hass = hass
    coord.entry = entry
    return coord


async def test_legacy_entry_remembers_identity(hass) -> None:
    entry = _bridge_entry(hass, BOND)
    _coordinator(hass, entry)._on_bridge_identity(IDENTITY, BOND)
    assert entry.data[CONF_PENDING_IDENTITY] == IDENTITY
    assert entry.unique_id == BOND  # switches only at the next setup


async def test_identity_entry_tracks_bond_address(hass) -> None:
    entry = _bridge_entry(hass, IDENTITY, **{CONF_BOND_ADDRESS: BOND})
    _coordinator(hass, entry)._on_bridge_identity(IDENTITY, NEW_BOND)
    assert entry.data[CONF_BOND_ADDRESS] == NEW_BOND
    assert CONF_PENDING_IDENTITY not in entry.data


async def test_fixed_address_brush_changes_nothing(hass) -> None:
    address = "24:E5:AA:BE:9C:1B"
    entry = _bridge_entry(hass, address)
    _coordinator(hass, entry)._on_bridge_identity(address, address)
    assert CONF_PENDING_IDENTITY not in entry.data
    assert CONF_BOND_ADDRESS not in entry.data


# ── Migration at setup ──────────────────────────────────────────────────────


def _register(hass, entry, address: str):
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    brush = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, address)},
        connections={(dr.CONNECTION_BLUETOOTH, address)},
    )
    head = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{address}_brushhead")},
    )
    sensor = ent_reg.async_get_or_create(
        "sensor", DOMAIN, f"{address}_battery",
        config_entry=entry, device_id=brush.id,
        suggested_object_id="sonicare_7100_battery",
    )
    return brush, head, sensor


async def test_migration_rekeys_entities_devices_and_entry(hass) -> None:
    entry = _bridge_entry(hass, BOND, **{CONF_PENDING_IDENTITY: IDENTITY})
    brush, head, sensor = _register(hass, entry, BOND)

    _async_migrate_to_identity(hass, entry)

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    moved = ent_reg.async_get(sensor.entity_id)
    assert moved.unique_id == f"{IDENTITY}_battery"
    assert moved.entity_id == sensor.entity_id  # history stays with it
    assert dev_reg.async_get(brush.id).identifiers == {(DOMAIN, IDENTITY)}
    assert (dr.CONNECTION_BLUETOOTH, IDENTITY) in dev_reg.async_get(
        brush.id
    ).connections
    assert dev_reg.async_get(head.id).identifiers == {
        (DOMAIN, f"{IDENTITY}_brushhead")
    }
    assert entry.unique_id == IDENTITY
    assert entry.data[CONF_ADDRESS] == IDENTITY
    assert entry.data[CONF_BOND_ADDRESS] == BOND
    assert CONF_PENDING_IDENTITY not in entry.data


async def test_migration_gives_up_on_collision(hass) -> None:
    _bridge_entry(hass, IDENTITY)
    entry = _bridge_entry(hass, BOND, **{CONF_PENDING_IDENTITY: IDENTITY})
    _, _, sensor = _register(hass, entry, BOND)

    _async_migrate_to_identity(hass, entry)

    assert entry.unique_id == BOND
    assert entry.data[CONF_ADDRESS] == BOND
    assert CONF_PENDING_IDENTITY not in entry.data  # no retry on every start
    assert er.async_get(hass).async_get(sensor.entity_id).unique_id == (
        f"{BOND}_battery"
    )


# ── Pairing a known brush again ─────────────────────────────────────────────


def _flow(hass, esp: str, slot: str, bond: str) -> PhilipsSonicareConfigFlow:
    flow = PhilipsSonicareConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = "test-flow"
    flow.context = {"source": "user", "unique_id": IDENTITY}
    flow._esp_device_name = esp
    flow._esp_bridge_id = slot
    flow._bond_address = bond
    return flow


async def test_same_slot_keeps_entry(hass) -> None:
    entry = _bridge_entry(hass, IDENTITY, **{CONF_BOND_ADDRESS: BOND})
    flow = _flow(hass, "atom_s3r", "hx742a_s3", NEW_BOND)
    flow._async_undo_pairing = MagicMock()

    with pytest.raises(AbortFlow) as err:
        flow._abort_if_esp_entry_configured(just_paired=True)

    assert err.value.reason == "esp_bridge_repaired"
    assert entry.data[CONF_BOND_ADDRESS] == NEW_BOND
    flow._async_undo_pairing.assert_not_called()


@pytest.mark.parametrize(
    ("esp", "slot"),
    [("atom_lite", "hx742a"), ("atom_s3r", "spare")],
)
@pytest.mark.parametrize("just_paired", [True, False])
async def test_other_slot_is_refused(hass, esp, slot, just_paired) -> None:
    """The entry stays where it is; a pairing just made here is undone."""
    entry = _bridge_entry(hass, IDENTITY, **{CONF_BOND_ADDRESS: BOND})
    before = dict(entry.data)
    flow = _flow(hass, esp, slot, NEW_BOND)
    undone = []

    async def undo(undo_esp, undo_slot):
        undone.append((undo_esp, undo_slot))

    flow._async_undo_pairing = undo

    with pytest.raises(AbortFlow) as err:
        flow._abort_if_esp_entry_configured(just_paired=just_paired)
    await hass.async_block_till_done()

    assert err.value.reason == (
        "esp_bridge_elsewhere_undone" if just_paired else "esp_bridge_elsewhere"
    )
    assert entry.data == before
    assert undone == ([(esp, slot)] if just_paired else [])


async def test_legacy_entry_with_pending_identity_is_found(
    hass, monkeypatch
) -> None:
    entry = _bridge_entry(hass, BOND, **{CONF_PENDING_IDENTITY: IDENTITY})
    flow = _flow(hass, "atom_s3r", "hx742a_s3", NEW_BOND)
    reload = MagicMock()
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", reload)

    with pytest.raises(AbortFlow) as err:
        flow._abort_if_esp_entry_configured()

    assert err.value.reason == "esp_bridge_repaired"
    assert entry.data[CONF_BOND_ADDRESS] == NEW_BOND
    reload.assert_called_once_with(entry.entry_id)  # switches to the identity


async def test_unknown_brush_passes(hass) -> None:
    _bridge_entry(hass, "24:E5:AA:BE:9C:1B")
    flow = _flow(hass, "atom_s3r", "hx742a_s3", NEW_BOND)
    flow._abort_if_esp_entry_configured()  # no abort
