"""Tests for seeding bridge_info from the picker's slot probe.

Selecting a slot used to trigger a second ble_get_info roundtrip right
after the picker had already probed every slot — several seconds of
spinner on a busy bridge. The picker-submit paths now seed _bridge_info
from that probe so the immediately-following health check skips the
roundtrip. The reuse is scoped to that one hop: every other entry into
the health check (discovery, post-pair, post-unpair) leaves _bridge_info
None and fetches fresh, because the slot's bonded state may have changed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)

PAYLOAD = {
    "version": "1.9.0",
    "ble_connected": "false",
    "mac": "24:E5:AA:BE:9C:1B",
    "paired": "true",
    "mode": "external",
    "pair_capable": "false",
    "friendly_name": "Prestige 9900",
}


def _flow() -> PhilipsSonicareConfigFlow:
    flow = PhilipsSonicareConfigFlow()
    flow.flow_id = "test-flow"
    flow.handler = "philips_sonicare_ble"
    flow.hass = SimpleNamespace()
    flow._esp_device_name = "atom_lite"
    flow._esp_bridge_id = "sonicare_1"
    flow._probed_bridges = {"atom_lite": [("sonicare_1", dict(PAYLOAD))]}
    return flow


# --- seeding ---------------------------------------------------------------

def test_seed_populates_bridge_info() -> None:
    flow = _flow()
    flow._seed_bridge_info_from_probe()
    assert flow._bridge_info["paired"] == "true"
    assert flow._bridge_info["version"] == "1.9.0"
    assert flow._bridge_info["friendly_name"] == "Prestige 9900"
    # Defaults fill fields the probe didn't carry.
    assert flow._bridge_info["pair_mode_active"] == "false"
    assert flow._bridge_info["area"] == ""


def test_seed_matches_bridge_id_case_insensitively() -> None:
    flow = _flow()
    flow._esp_bridge_id = "SONICARE_1"
    flow._seed_bridge_info_from_probe()
    assert flow._bridge_info is not None
    assert flow._bridge_info["paired"] == "true"


def test_seed_noop_for_unknown_slot() -> None:
    flow = _flow()
    flow._esp_bridge_id = "sonicare_9"
    flow._seed_bridge_info_from_probe()
    assert flow._bridge_info is None


def test_seed_noop_without_probe_cache() -> None:
    flow = _flow()
    flow._probed_bridges = {}
    flow._seed_bridge_info_from_probe()
    assert flow._bridge_info is None


# --- health check honours a seed, fetches fresh otherwise ------------------

async def test_health_check_uses_seeded_info(monkeypatch) -> None:
    flow = _flow()
    flow._seed_bridge_info_from_probe()
    flow._route_after_health_check = AsyncMock(return_value={"type": "routed"})

    # A transport roundtrip would be a bug here — make it explode.
    class _Boom:
        def __init__(self, *a, **kw):
            raise AssertionError("health check must not probe on a seeded info")

    monkeypatch.setattr(
        "custom_components.philips_sonicare_ble.config_flow.EspBridgeTransport",
        _Boom,
    )

    result = await flow._esp_bridge_health_check()

    assert result == {"type": "routed"}
    assert flow._bridge_info["version"] == "1.9.0"


async def test_health_check_fetches_fresh_without_seed(monkeypatch) -> None:
    # Post-pair / post-unpair / discovery: no seed → live fetch. This is
    # the path that must NOT reuse a stale picker snapshot.
    flow = _flow()
    flow._route_after_health_check = AsyncMock(return_value={"type": "routed"})

    transport = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        get_bridge_info=AsyncMock(return_value={"paired": "true"}),
        bridge_version="1.9.0",
        is_device_connected=False,
        detected_mac="24:E5:AA:BE:9C:1B",
        ble_paired="true",
    )
    monkeypatch.setattr(
        "custom_components.philips_sonicare_ble.config_flow.EspBridgeTransport",
        lambda *a, **kw: transport,
    )

    result = await flow._esp_bridge_health_check()

    assert result == {"type": "routed"}
    transport.connect.assert_awaited_once()
    transport.get_bridge_info.assert_awaited_once()
    assert flow._bridge_info["version"] == "1.9.0"
