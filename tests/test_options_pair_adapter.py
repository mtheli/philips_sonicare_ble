"""Pairing step when the Bluetooth adapter option moves to a new adapter.

Bonds are per adapter. Switching a bonded brush from hci1 to hci0 in the
options left hci0 without a bond, and every connect ended in "failed to
discover services, device disconnected" (live 2026-10-29). The options
flow now pairs on the new adapter before saving — and only when needed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.philips_sonicare_ble import config_flow as cf
from custom_components.philips_sonicare_ble import dbus_pairing as dp
from custom_components.philips_sonicare_ble.const import (
    BLUETOOTH_ADAPTER_AUTO,
    CONF_BLUETOOTH_ADAPTER,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    SVC_CONDOR,
    SVC_SONICARE,
    TRANSPORT_BLEAK,
)

ADDRESS = "24:E5:AA:BE:9C:1B"
HCI0 = "00:0A:CD:46:B2:2D"
HCI1 = "58:D8:12:3E:CA:B2"
PROXY = "98:88:E0:0E:DA:D2"

USER_INPUT = {
    "sensor_pressure": True,
    "sensor_temperature": False,
    "sensor_gyroscope": False,
    "warn_counterfeit_brushhead": True,
}


def _flow(monkeypatch, *, services=(SVC_SONICARE,), options=None):
    entry = SimpleNamespace(
        entry_id="entry-1",
        title="Bathroom",
        data={
            "address": ADDRESS,
            CONF_TRANSPORT_TYPE: TRANSPORT_BLEAK,
            CONF_SERVICES: list(services),
        },
        options=options or {CONF_BLUETOOTH_ADAPTER: HCI1},
    )
    monkeypatch.setattr(
        cf.PhilipsSonicareOptionsFlow, "config_entry", property(lambda self: entry)
    )
    flow = cf.PhilipsSonicareOptionsFlow()
    flow.flow_id = "options-flow"
    flow.handler = "philips_sonicare_ble"
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_unload=AsyncMock(return_value=True),
            async_schedule_reload=MagicMock(),
            async_update_entry=MagicMock(),
        ),
        async_create_task=lambda coro: asyncio.get_running_loop().create_task(coro),
    )
    return flow


@pytest.fixture
def adapters(monkeypatch):
    """hci0 and hci1 local, plus a proxy; the brush is bonded on hci1."""
    monkeypatch.setattr(cf, "describe_connectable_scanners", lambda hass, a: [
        {"name": "hci1 (58:D8:12:3E:CA:B2)", "source": HCI1, "rssi": -64, "is_local": True},
        {"name": "hci0 (00:0A:CD:46:B2:2D)", "source": HCI0, "rssi": -74, "is_local": True},
        {"name": "atom-s3r", "source": PROXY, "rssi": -56, "is_local": False},
    ])
    monkeypatch.setattr(dp, "is_dbus_available", lambda: True)
    paired = AsyncMock(return_value=[HCI1])
    monkeypatch.setattr(dp, "async_paired_adapters", paired)
    pair = AsyncMock()
    monkeypatch.setattr(dp, "async_pair_and_trust", pair)
    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(dp, "async_remove_device", remove)

    async def _blocks(hass, category="config"):
        keys = [
            "adapter", "adapter_auto", "pair_paired", "pair_already",
            "pair_not_needed", "pair_proxy", "pair_auto", "pair_unknown",
            "drop_done", "drop_failed", "drop_none", "drop_auto", "drop_proxy",
            "drop_unknown", "reload",
        ]
        blocks = {f"error.summary_{k}": f"{k}:{{adapter}}" for k in keys}
        blocks["error.pair_adapter_failed"] = "{error}"
        return blocks

    monkeypatch.setattr(cf, "_async_text_blocks", _blocks)

    real_sleep = asyncio.sleep

    async def _fast_sleep(delay, *args):
        await real_sleep(0)

    monkeypatch.setattr(cf.asyncio, "sleep", _fast_sleep)

    # The new adapter hears the brush right away, unless a test says not.
    watch = SimpleNamespace(heard=True, events=[], stopped=0)

    def _watch(address, source):
        event = asyncio.Event()
        if watch.heard:
            event.set()
        watch.events.append(event)

        def _stop():
            watch.stopped += 1

        return event, _stop

    monkeypatch.setattr(cf, "watch_advertisements", _watch)
    rssi = SimpleNamespace(value=None)
    monkeypatch.setattr(cf, "scanner_rssi", lambda hass, a, s: rssi.value)
    return SimpleNamespace(
        paired=paired, pair=pair, remove=remove, watch=watch, rssi=rssi,
    )


async def _run_pairing(flow):
    result = await flow.async_step_pair_adapter({})
    assert result["type"] == FlowResultType.SHOW_PROGRESS
    await flow._pair_task
    result = await flow.async_step_pair_adapter()
    assert result["type"] == FlowResultType.SHOW_PROGRESS_DONE
    return await flow.async_step_pair_adapter_finish()


async def test_switch_to_unbonded_local_adapter_asks_to_pair(monkeypatch, adapters):
    flow = _flow(monkeypatch)
    result = await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "pair_adapter"
    assert result["description_placeholders"]["adapter"] == "hci0"
    assert result["description_placeholders"]["paired_on"] == "hci1"
    assert result["description_placeholders"]["alert"] == ""
    # Nothing paused yet — only when the user starts pairing.
    flow.hass.config_entries.async_unload.assert_not_called()


def _checklist(result) -> list[str]:
    """Row keys of the closing checklist (test text blocks are 'key:…')."""
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "adapter_changed"
    return [
        line[2:].split(":")[0]
        for line in result["description_placeholders"]["summary"].split("\n")
    ]


@pytest.mark.parametrize(
    ("adapter", "rows"),
    [
        (BLUETOOTH_ADAPTER_AUTO, ["adapter_auto", "pair_auto", "drop_auto", "reload"]),
        (PROXY, ["adapter", "pair_proxy", "drop_proxy", "reload"]),
    ],
)
async def test_no_pair_step_when_not_needed(monkeypatch, adapters, adapter, rows):
    # Automatic and a proxy (which pairs by itself) save straight away; the
    # checklist says why nothing else happened.
    flow = _flow(monkeypatch)
    result = await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: adapter})
    assert _checklist(result) == rows
    adapters.pair.assert_not_called()
    flow.hass.config_entries.async_update_entry.assert_called_once()
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")


async def test_unchanged_adapter_saves_normally(monkeypatch, adapters):
    flow = _flow(monkeypatch)
    result = await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI1})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    adapters.pair.assert_not_called()


async def test_open_gatt_brush_never_gets_pair_step(monkeypatch, adapters):
    # Not bonded anywhere and no Condor: readable without a bond (HX992X,
    # Kids) — some of them even reject pairing.
    adapters.paired.return_value = []
    flow = _flow(monkeypatch)
    result = await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})
    assert _checklist(result) == ["adapter", "pair_not_needed", "drop_none", "reload"]


async def test_condor_without_any_bond_gets_pair_step(monkeypatch, adapters):
    adapters.paired.return_value = []
    flow = _flow(monkeypatch, services=(SVC_CONDOR,))
    result = await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})
    assert result["step_id"] == "pair_adapter"
    assert result["description_placeholders"]["paired_on"] == "—"


async def test_successful_pairing_saves_and_reloads(monkeypatch, adapters):
    flow = _flow(monkeypatch)
    await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})

    result = await _run_pairing(flow)

    assert _checklist(result) == ["adapter", "pair_paired", "drop_done", "reload"]
    assert "hci1" in result["description_placeholders"]["summary"]
    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["options"]
    assert saved[CONF_BLUETOOTH_ADAPTER] == HCI0
    flow.hass.config_entries.async_unload.assert_awaited_once_with("entry-1")
    adapters.pair.assert_awaited_once_with(ADDRESS, HCI0)
    # Only after the new bond exists: the old adapter's bond goes.
    adapters.remove.assert_awaited_once_with(ADDRESS, HCI1)
    # Exactly one reload brings the paused entry back — also after the
    # dialog closes.
    flow.async_remove()
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")


async def test_unchanged_adapter_never_asks_to_pair(monkeypatch, adapters):
    # Fixed on hci0 without a bond there (bond only on hci1): saving just
    # another option must not unload the entry and ask for a pairing.
    flow = _flow(monkeypatch, options={CONF_BLUETOOTH_ADAPTER: HCI0, **USER_INPUT})
    result = await flow.async_step_adapter(
        {**USER_INPUT, "sensor_gyroscope": True, CONF_BLUETOOTH_ADAPTER: HCI0}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    adapters.paired.assert_not_called()
    flow.hass.config_entries.async_unload.assert_not_called()


async def test_failed_pairing_shows_reason_and_close_restores(monkeypatch, adapters):
    adapters.pair.side_effect = dp.PairingError("Pairing timed out after 30s")
    monkeypatch.setattr(
        cf, "_async_text_blocks",
        AsyncMock(return_value={
            "error.pair_adapter_failed": "Pairing with {adapter} failed: {error}",
        }),
    )
    flow = _flow(monkeypatch)
    await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})

    result = await _run_pairing(flow)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "pair_adapter"
    alert = result["description_placeholders"]["alert"]
    assert "ha-alert" in alert and "hci0" in alert and "timed out" in alert
    flow.hass.config_entries.async_schedule_reload.assert_not_called()

    # User gives up and closes the dialog: the entry must come back.
    flow.async_remove()
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")


async def test_auto_to_fixed_drops_bonds_on_other_adapters(monkeypatch, adapters):
    # Bonded on both; fixing hci0 needs no pairing but removes hci1's bond,
    # with the entry unloaded first (it may be connected through hci1).
    adapters.paired.return_value = [HCI0, HCI1]
    flow = _flow(monkeypatch, options={})
    result = await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})

    assert _checklist(result) == ["adapter", "pair_already", "drop_done", "reload"]
    adapters.pair.assert_not_called()
    flow.hass.config_entries.async_unload.assert_awaited_once_with("entry-1")
    adapters.remove.assert_awaited_once_with(ADDRESS, HCI1)
    flow.async_remove()
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")


async def test_failed_removal_is_reported(monkeypatch, adapters):
    adapters.paired.return_value = [HCI0, HCI1]
    adapters.remove.return_value = False
    flow = _flow(monkeypatch, options={})
    result = await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})
    assert _checklist(result) == ["adapter", "pair_already", "drop_failed", "reload"]


@pytest.mark.parametrize(
    ("current", "chosen"),
    [
        (HCI0, BLUETOOTH_ADAPTER_AUTO),  # to Automatic: any adapter may be used
        (HCI1, PROXY),                   # to a proxy: keep the evidence of a bond
        (HCI1, HCI1),                    # no change
    ],
)
async def test_bonds_kept(monkeypatch, adapters, current, chosen):
    adapters.paired.return_value = [HCI0, HCI1]
    flow = _flow(monkeypatch, options={CONF_BLUETOOTH_ADAPTER: current})
    result = await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: chosen})

    assert result["type"] in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT)
    adapters.remove.assert_not_called()
    flow.hass.config_entries.async_unload.assert_not_called()


async def test_failed_pairing_keeps_old_bond(monkeypatch, adapters):
    adapters.pair.side_effect = dp.PairingError("timed out")
    monkeypatch.setattr(
        cf, "_async_text_blocks",
        AsyncMock(return_value={"error.pair_adapter_failed": "{error}"}),
    )
    flow = _flow(monkeypatch)
    await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})
    await _run_pairing(flow)
    adapters.remove.assert_not_called()


async def test_one_row_per_removed_adapter(monkeypatch, adapters):
    # Three local adapters, the brush bonded on all: fixing hci0 lists
    # each removed bond on its own line, led by the adapter.
    hci2 = "11:22:33:44:55:66"
    monkeypatch.setattr(cf, "describe_connectable_scanners", lambda hass, a: [
        {"name": "hci0 (00:0A:CD:46:B2:2D)", "source": HCI0, "rssi": -74, "is_local": True},
        {"name": "hci1 (58:D8:12:3E:CA:B2)", "source": HCI1, "rssi": -64, "is_local": True},
        {"name": "hci2 (11:22:33:44:55:66)", "source": hci2, "rssi": None, "is_local": True},
    ])
    adapters.paired.return_value = [HCI0, HCI1, hci2]
    flow = _flow(monkeypatch, options={})
    result = await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})

    lines = result["description_placeholders"]["summary"].split("\n")
    assert lines == [
        "- adapter:hci0",
        "- pair_already:hci0",
        "- drop_done:hci1",
        "- drop_done:hci2",
        "- reload:{adapter}",  # row without values; test text keeps the marker
    ]


async def test_pairs_only_after_fresh_advertisement(monkeypatch, adapters):
    # Silent brush after the disconnect: no pairing until the new adapter
    # receives an advertisement (live 2026-10-30, Pair() timed out).
    adapters.watch.heard = False
    flow = _flow(monkeypatch)
    await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})
    await flow.async_step_pair_adapter({})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    adapters.pair.assert_not_called()

    adapters.watch.events[0].set()  # power button pressed
    await flow._pair_task

    adapters.pair.assert_awaited_once_with(ADDRESS, HCI0)
    assert adapters.watch.stopped == 1


async def test_no_advertisement_asks_for_power_button(monkeypatch, adapters):
    adapters.watch.heard = False
    monkeypatch.setattr(cf, "PAIR_ADVERTISEMENT_WAIT", 0)
    monkeypatch.setattr(
        cf, "_async_text_blocks",
        AsyncMock(return_value={
            "error.pair_adapter_no_advertisement": "{adapter} silent for {seconds}s",
        }),
    )
    flow = _flow(monkeypatch)
    await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})

    result = await _run_pairing(flow)

    assert result["step_id"] == "pair_adapter"
    assert "hci0</b> silent for 0s" in result["description_placeholders"]["alert"]
    adapters.pair.assert_not_called()
    assert adapters.watch.stopped == 1


async def test_retry_keeps_bond_from_late_first_attempt(monkeypatch, adapters):
    # 1st Pair() timed out on our side, BlueZ finished it later; the
    # retry found the bond and removed it as "stale" (live 2026-10-30).
    adapters.pair.side_effect = dp.PairingError("Pairing timed out after 30s")
    flow = _flow(monkeypatch)
    await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})
    result = await _run_pairing(flow)
    assert result["step_id"] == "pair_adapter"

    adapters.paired.return_value = [HCI1, HCI0]
    result = await _run_pairing(flow)

    assert result["reason"] == "adapter_changed"
    adapters.pair.assert_awaited_once()  # no second Pair() over the bond
    adapters.remove.assert_awaited_once_with(ADDRESS, HCI1)


async def test_first_attempt_never_trusts_an_existing_bond(monkeypatch, adapters):
    # Without an earlier attempt in this dialog a bond on the new adapter
    # is not ours — the normal pairing (with its stale-bond check) runs.
    flow = _flow(monkeypatch)
    await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})
    adapters.paired.return_value = [HCI1, HCI0]

    await _run_pairing(flow)

    adapters.pair.assert_awaited_once_with(ADDRESS, HCI0)


@pytest.mark.parametrize(("rssi", "warned"), [(-96, True), (-74, False), (None, False)])
async def test_weak_signal_warning(monkeypatch, adapters, rssi, warned):
    adapters.rssi.value = rssi
    monkeypatch.setattr(
        cf, "_async_text_blocks",
        AsyncMock(return_value={"error.pair_adapter_weak": "{adapter} weak {rssi}"}),
    )
    flow = _flow(monkeypatch)

    result = await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})

    assert result["step_id"] == "pair_adapter"
    alert = result["description_placeholders"]["alert"]
    assert ("weak -96" in alert) is warned
    assert ('alert-type="warning"' in alert) is warned


@pytest.mark.parametrize("late_bond", [True, False])
async def test_timeout_cancels_and_takes_late_bond(late_bond):
    device = SimpleNamespace(call_cancel_pairing=AsyncMock())
    props = SimpleNamespace(
        call_get=AsyncMock(return_value=SimpleNamespace(value=late_bond))
    )

    assert await dp._async_cancel_and_check_paired(device, props, ADDRESS) is late_bond
    device.call_cancel_pairing.assert_awaited_once()


async def test_pairs_without_waiting_when_core_cannot_tell(monkeypatch, adapters):
    # No per-address advertisement callback (older habluetooth): pair as
    # before instead of waiting 45 s for a signal that can never come.
    def _unknown(address, source):
        raise LookupError("no async_register_advertisement_callback")

    monkeypatch.setattr(cf, "watch_advertisements", _unknown)
    flow = _flow(monkeypatch)
    await flow.async_step_adapter({**USER_INPUT, CONF_BLUETOOTH_ADAPTER: HCI0})

    result = await _run_pairing(flow)

    assert result["reason"] == "adapter_changed"
    adapters.pair.assert_awaited_once_with(ADDRESS, HCI0)
