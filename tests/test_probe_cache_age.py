"""The slot picker must not render a probe from an earlier lifetime.

A zeroconf discovery builds the whole picker step in the background, and
the banner it produces can sit unopened for hours. Home Assistant re-runs
the step when the banner is finally clicked, so the dialog would be
correct — except that the probe behind it was cached without an expiry,
so it showed the bridge state from when the flow was created. Live case:
a slot unbonded at 20:44 still rendered as bonded at 20:45, from a probe
taken at 08:53.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import custom_components.philips_sonicare_ble.config_flow as cf
from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)

BONDED = {
    "bridge_id": "hx742a", "friendly_name": "Sonicare 7100",
    "mac": "61:B2:CA:94:73:5C", "paired": "true", "ble_connected": "false",
    "pair_capable": "false", "version": "1.10.0", "mode": "standalone",
}
FREE = {
    "bridge_id": "hx742a", "friendly_name": "Sonicare 7100",
    "mac": "00:00:00:00:00:00", "paired": "false", "ble_connected": "false",
    "pair_capable": "true", "version": "1.10.0", "mode": "standalone",
}


OTHER = {
    "bridge_id": "prestige", "friendly_name": "Prestige 9900",
    "mac": "00:00:00:00:00:00", "paired": "false", "ble_connected": "false",
    "pair_capable": "true", "version": "1.10.0", "mode": "standalone",
}


def _labels(result) -> list[str]:
    """Option labels of the picker's select — schema keys are vol markers."""
    for value in result["data_schema"].schema.values():
        options = getattr(value, "config", {}).get("options")
        if options:
            return [option["label"] for option in options]
    raise AssertionError("no select with options in the picker schema")


def _flow(monkeypatch, probe_result):
    flow = PhilipsSonicareConfigFlow()
    flow.flow_id = "t"
    flow.handler = "philips_sonicare_ble"
    flow.hass = SimpleNamespace(
        config=SimpleNamespace(components=set()), data={}
    )
    flow._esp_device_name = "atom_lite"
    flow._esp_bridge_ids = ["hx742a", "prestige"]
    flow._async_current_entries = lambda: []
    flow._probe_sonicare_bridges = AsyncMock(
        return_value=[("hx742a", probe_result), ("prestige", OTHER)]
    )
    return flow


async def test_stale_probe_is_not_reused(monkeypatch) -> None:
    """A probe older than the max age must be discarded."""
    flow = _flow(monkeypatch, FREE)
    # Seed the cache as a background discovery would have, but long ago.
    flow._probed_bridges = {
        "atom_lite": [("hx742a", BONDED), ("prestige", OTHER)]
    }
    flow._probed_at = {"atom_lite": 0.0}
    monkeypatch.setattr(
        cf.time, "monotonic", lambda: cf._PROBE_CACHE_MAX_AGE + 1.0
    )

    result = await flow.async_step_esp_select_device()

    flow._probe_sonicare_bridges.assert_awaited_once()
    labels = _labels(result)
    # pair_capable slots render as a bare name — no lock, no MAC.
    assert "Sonicare 7100" in labels, labels
    assert "61:B2:CA:94:73:5C" not in "".join(labels)


async def test_fresh_probe_is_reused(monkeypatch) -> None:
    """Within the window the dropdown's probe still carries over."""
    flow = _flow(monkeypatch, FREE)
    flow._probed_bridges = {
        "atom_lite": [("hx742a", BONDED), ("prestige", OTHER)]
    }
    flow._probed_at = {"atom_lite": 0.0}
    monkeypatch.setattr(
        cf.time, "monotonic", lambda: cf._PROBE_CACHE_MAX_AGE - 1.0
    )

    result = await flow.async_step_esp_select_device()

    flow._probe_sonicare_bridges.assert_not_awaited()
    labels = _labels(result)
    assert any("61:B2:CA:94:73:5C" in label for label in labels), labels


async def test_refreshed_probe_still_feeds_the_health_check(monkeypatch) -> None:
    """After a re-probe the submit hop must see the *new* state.

    _seed_bridge_info_from_probe reads the same cache, so the refresh has
    to write through — otherwise the health check would run on the state
    the picker just discarded.
    """
    flow = _flow(monkeypatch, FREE)
    flow._probed_bridges = {
        "atom_lite": [("hx742a", BONDED), ("prestige", OTHER)]
    }
    flow._probed_at = {"atom_lite": 0.0}
    monkeypatch.setattr(
        cf.time, "monotonic", lambda: cf._PROBE_CACHE_MAX_AGE + 1.0
    )
    await flow.async_step_esp_select_device()

    flow._esp_bridge_id = "hx742a"
    flow._seed_bridge_info_from_probe()

    assert flow._bridge_info is not None
    assert flow._bridge_info["paired"] == "false"
    assert flow._bridge_info["mac"] == "00:00:00:00:00:00"


async def test_slot_unbonded_meanwhile_is_reprobed_within_the_window(
    monkeypatch,
) -> None:
    """A slot we un-bonded ourselves must refresh even inside the window.

    The user removes a brush and re-opens the discovery banner seconds
    later — too fast for the age check, but the picker would otherwise
    show the slot as still bonded. Only the affected slot is re-probed;
    the untouched one keeps its cached entry.
    """
    from custom_components.philips_sonicare_ble.transport import note_slot_changed

    flow = _flow(monkeypatch, FREE)
    flow._probed_bridges = {
        "atom_lite": [("hx742a", BONDED), ("prestige", OTHER)]
    }
    flow._probed_at = {"atom_lite": 0.0}
    now = cf._PROBE_CACHE_MAX_AGE - 1.0
    monkeypatch.setattr(cf.time, "monotonic", lambda: now)
    import custom_components.philips_sonicare_ble.transport as tp
    monkeypatch.setattr(tp.time, "monotonic", lambda: now)
    # The unpair happened after the probe was taken.
    note_slot_changed(flow.hass, "atom_lite", "hx742a")

    result = await flow.async_step_esp_select_device()

    # Re-probed, but only the stale slot.
    assert flow._probe_sonicare_bridges.await_args.args[1] == ["hx742a"]
    labels = _labels(result)
    assert "61:B2:CA:94:73:5C" not in "".join(labels), labels
    assert any("Prestige 9900" in label for label in labels), labels


async def test_silent_slot_stays_visible(monkeypatch) -> None:
    """A slot that did not answer is shown as offline, not dropped.

    It used to vanish from the list entirely, so a busy or offline slot
    looked like it did not exist. Now it renders with a ⚪ marker — the
    user can see which one needs attention.
    """
    flow = _flow(monkeypatch, FREE)
    flow._probe_sonicare_bridges = AsyncMock(
        return_value=[("hx742a", None), ("prestige", OTHER)]
    )
    flow._probed_bridges = {}
    flow._probed_at = {}

    result = await flow.async_step_esp_select_device()

    labels = _labels(result)
    assert any(label.startswith("⚪") and "hx742a" in label for label in labels), labels
    assert any("Prestige 9900" in label for label in labels), labels


async def test_probe_not_covering_the_slot_list_is_discarded(monkeypatch) -> None:
    """A cached probe from a different slot set must not be reused."""
    flow = _flow(monkeypatch, FREE)
    # Cache knows only one of the two slots this picker renders.
    flow._probed_bridges = {"atom_lite": [("hx742a", BONDED)]}
    flow._probed_at = {"atom_lite": 0.0}
    monkeypatch.setattr(
        cf.time, "monotonic", lambda: cf._PROBE_CACHE_MAX_AGE - 1.0
    )

    await flow.async_step_esp_select_device()

    flow._probe_sonicare_bridges.assert_awaited_once()
    assert flow._probe_sonicare_bridges.await_args.args[1] == [
        "hx742a", "prestige"
    ]


async def test_all_slots_silent_aborts(monkeypatch) -> None:
    """No answer at all still aborts instead of listing ⚪ entries.

    The list now keeps silent slots, so "empty" no longer means "nobody
    answered" — the abort has to test for that explicitly.
    """
    from homeassistant.data_entry_flow import FlowResultType

    flow = _flow(monkeypatch, FREE)
    flow._probe_sonicare_bridges = AsyncMock(
        return_value=[("hx742a", None), ("prestige", None)]
    )
    flow._probed_bridges = {}
    flow._probed_at = {}

    result = await flow.async_step_esp_select_device()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


# --- offline ESPs in the dropdown ----------------------------------------

def _esp_entry(title, device_name, *, available):
    return SimpleNamespace(
        title=title,
        data={"device_name": device_name},
        disabled_by=None,
        runtime_data=SimpleNamespace(available=available),
    )


async def test_offline_esp_listed_only_when_it_is_ours() -> None:
    """An unreachable ESP cannot be probed, so it may only be shown when an
    existing entry proves it is our bridge.

    philips_shaver registers the same ESPHome service names, so without a
    probe there is nothing to tell the two apart — listing every offline
    node would advertise the other integration's hardware.
    """
    flow = PhilipsSonicareConfigFlow()
    flow.hass = SimpleNamespace(
        config=SimpleNamespace(components=set()), data={},
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [
                _esp_entry("Atom Lite BLE Bridge", "atom-lite", available=False),
                _esp_entry("Atom S3R BLE Bridge", "atom-s3r", available=False),
            ]
        ),
        services=SimpleNamespace(
            has_service=lambda *_: False,
            async_services=lambda: {"esphome": {
                "atom_lite_ble_read_char_hx742a": None,
                "atom_s3r_ble_read_char_shaver_1": None,
            }},
        ),
    )
    # Only the Atom Lite was ever set up as a Sonicare bridge.
    flow._async_current_entries = lambda: [
        SimpleNamespace(data={"esp_device_name": "atom-lite"})
    ]

    options = await flow._get_esphome_device_options()

    values = [o["value"] for o in options]
    assert values == ["atom_lite"], values
    assert all(o["label"].startswith("⚪") for o in options), options


async def test_offline_esp_is_not_auto_selected() -> None:
    """A sole ESP listed without a probe must still show the picker."""
    flow = PhilipsSonicareConfigFlow()
    flow.hass = SimpleNamespace(
        config=SimpleNamespace(components=set()), data={},
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [
                _esp_entry("Atom Lite BLE Bridge", "atom-lite", available=False),
            ]
        ),
        services=SimpleNamespace(
            has_service=lambda *_: False,
            async_services=lambda: {"esphome": {
                "atom_lite_ble_read_char_hx742a": None,
            }},
        ),
    )
    flow._async_current_entries = lambda: [
        SimpleNamespace(data={"esp_device_name": "atom-lite"})
    ]
    flow._esp_bridge_health_check = AsyncMock(return_value={"type": "sentinel"})

    result = await flow.async_step_esp_bridge()

    assert result["type"] != "sentinel"
    flow._esp_bridge_health_check.assert_not_awaited()


async def test_reachable_esp_that_answers_no_probe_is_not_listed() -> None:
    """The case that actually happens: the other integration's bridge.

    A shaver bridge is online and registers the same ESPHome service
    names, so it looks reachable — it just never answers on our event
    channel. Without an entry vouching for it, that is indistinguishable
    from our own bridge being unreachable, so it must stay hidden.
    """
    flow = PhilipsSonicareConfigFlow()
    flow.hass = SimpleNamespace(
        config=SimpleNamespace(components=set()), data={},
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [
                _esp_entry("Atom Lite BLE Bridge", "atom-lite", available=True),
                _esp_entry("Atom S3R BLE Bridge", "atom-s3r", available=True),
            ]
        ),
        services=SimpleNamespace(
            has_service=lambda *_: False,
            async_services=lambda: {"esphome": {
                "atom_lite_ble_read_char_hx742a": None,
                "atom_s3r_ble_read_char_shaver_1": None,
            }},
        ),
    )
    flow._async_current_entries = lambda: [
        SimpleNamespace(data={"esp_device_name": "atom-lite"})
    ]
    flow._probe_sonicare_bridges = AsyncMock(
        side_effect=lambda dev, dids: [(did, None) for did in dids]
    )

    options = await flow._get_esphome_device_options()

    values = [o["value"] for o in options]
    assert values == ["atom_lite"], values
    assert options[0]["label"].startswith("⚪"), options
