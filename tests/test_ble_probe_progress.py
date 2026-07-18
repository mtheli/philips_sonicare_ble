"""Tests for the direct-BLE probe as an async_show_progress task.

The capabilities probe (connect + characteristic reads, several seconds)
used to run synchronously inside bluetooth_confirm / user_bleak, freezing
the dialog on the submit spinner. It now runs as a background progress
task shared by both steps; ``async_step_ble_probe_finish`` routes the
boxed outcome back to whichever step started it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.data_entry_flow import FlowResultType

from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)

ADDRESS = "24:E5:AA:BE:9C:1B"


class _FakeTask:
    def __init__(self, done: bool, result=None) -> None:
        self._done = done
        self._result = result

    def done(self) -> bool:
        return self._done

    def result(self):
        return self._result


def _flow() -> PhilipsSonicareConfigFlow:
    flow = PhilipsSonicareConfigFlow()
    flow.flow_id = "test-flow"
    flow.handler = "philips_sonicare_ble"
    flow._address = ADDRESS
    flow._name = "Sonicare Prestige"
    created: list = []

    def _create_task(coro, *args, **kwargs):
        # Never actually run the coroutine in unit tests.
        coro.close()
        task = _FakeTask(done=False)
        created.append(task)
        return task

    flow.hass = SimpleNamespace(
        async_create_task=MagicMock(side_effect=_create_task),
    )
    flow._created_tasks = created  # type: ignore[attr-defined]
    return flow


# --- orchestration ----------------------------------------------------------

async def test_confirm_submit_starts_probe_progress() -> None:
    flow = _flow()
    flow._find_esp_bridge_for_mac = AsyncMock(return_value=None)

    result = await flow.async_step_bluetooth_confirm({})

    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "ble_probing"
    assert result["description_placeholders"]["name"] == "Sonicare Prestige"
    assert flow._ble_probe_task is not None
    assert flow._ble_probe_origin == "bluetooth_confirm"


async def test_running_probe_short_circuits_esp_autoroute() -> None:
    flow = _flow()
    flow._find_esp_bridge_for_mac = AsyncMock(return_value=None)
    flow._ble_probe_task = _FakeTask(done=False)

    result = await flow.async_step_bluetooth_confirm()

    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "ble_probing"
    # Progress re-entries must not re-probe ESP slots (seconds of latency).
    flow._find_esp_bridge_for_mac.assert_not_awaited()


async def test_done_probe_transitions_to_finish() -> None:
    flow = _flow()
    flow._find_esp_bridge_for_mac = AsyncMock(return_value=None)
    payload = {"ok": True, "data": {"model": "HX9992/12"}}
    flow._ble_probe_task = _FakeTask(done=True, result=payload)

    result = await flow.async_step_bluetooth_confirm()

    assert result["type"] == FlowResultType.SHOW_PROGRESS_DONE
    assert result["step_id"] == "ble_probe_finish"
    assert flow._ble_probe_result == payload
    assert flow._ble_probe_task is None


async def test_manual_submit_starts_probe_progress() -> None:
    flow = _flow()
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_already_configured = MagicMock()

    result = await flow.async_step_user_bleak({"address": ADDRESS.lower()})

    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "ble_probing"
    assert flow._ble_probe_origin == "user_bleak"
    assert flow._address == ADDRESS  # upper-cased


# --- finish routing ---------------------------------------------------------

def _finish_flow(result: dict) -> PhilipsSonicareConfigFlow:
    flow = _flow()
    flow._ble_probe_result = result
    return flow


async def test_finish_success_shows_capabilities() -> None:
    flow = _finish_flow(
        {"ok": True, "data": {"model": "HX9992/12", "connection_path": "hci0"}}
    )
    flow._ble_probe_origin = "bluetooth_confirm"
    flow._has_sonicare_services = MagicMock(return_value=True)
    flow.async_step_show_capabilities = AsyncMock(return_value={"type": "caps"})

    result = await flow.async_step_ble_probe_finish()

    assert result == {"type": "caps"}
    assert flow._fetched_data["model"] == "HX9992/12"
    assert flow._transport_type == "bleak"
    assert flow._ble_probe_result is None


async def test_finish_not_paired_routes_to_pairing_step() -> None:
    flow = _finish_flow({"ok": False, "error": "not_paired"})
    flow._ble_probe_origin = "bluetooth_confirm"
    flow.async_step_not_paired = AsyncMock(return_value={"type": "pairing"})

    result = await flow.async_step_ble_probe_finish()

    assert result == {"type": "pairing"}


async def test_finish_manual_asleep_aborts() -> None:
    flow = _finish_flow({"ok": False, "error": "asleep"})
    flow._ble_probe_origin = "user_bleak"

    result = await flow.async_step_ble_probe_finish()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "device_asleep"


async def test_finish_manual_no_connection_renders_error(monkeypatch) -> None:
    flow = _finish_flow({"ok": True, "data": {"model": "HX9992/12"}})
    flow._ble_probe_origin = "user_bleak"
    # No connection_path in the data → cannot_connect on the manual form.
    monkeypatch.setattr(
        "custom_components.philips_sonicare_ble.config_flow."
        "async_discovered_service_info",
        lambda hass: [],
    )

    result = await flow.async_step_ble_probe_finish()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user_bleak"
    assert result["errors"] == {"base": "cannot_connect"}
    # One-shot: consumed by the render above.
    assert flow._manual_error == ""


async def test_finish_manual_not_a_sonicare(monkeypatch) -> None:
    flow = _finish_flow(
        {"ok": True, "data": {"model": "Speaker", "connection_path": "hci0"}}
    )
    flow._ble_probe_origin = "user_bleak"
    flow._has_sonicare_services = MagicMock(return_value=False)
    monkeypatch.setattr(
        "custom_components.philips_sonicare_ble.config_flow."
        "async_discovered_service_info",
        lambda hass: [],
    )

    result = await flow.async_step_ble_probe_finish()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user_bleak"
    assert result["errors"] == {"base": "not_a_sonicare"}


async def test_finish_discovery_asleep_renders_alert() -> None:
    flow = _finish_flow({"ok": False, "error": "asleep"})
    flow._ble_probe_origin = "bluetooth_confirm"
    flow._find_esp_bridge_for_mac = AsyncMock(return_value=None)

    result = await flow.async_step_ble_probe_finish()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    status = result["description_placeholders"]["status"]
    assert "asleep" in status
    assert status.startswith('<ha-alert alert-type="error">')
    # One-shot: consumed by the render above.
    assert flow._confirm_status == ""


async def test_finish_discovery_failure_renders_alert() -> None:
    flow = _finish_flow({"ok": False, "error": "unknown"})
    flow._ble_probe_origin = "bluetooth_confirm"
    flow._find_esp_bridge_for_mac = AsyncMock(return_value=None)

    result = await flow.async_step_ble_probe_finish()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert "Could not read" in result["description_placeholders"]["status"]


# --- probe wrapper ----------------------------------------------------------

async def test_probe_wrapper_boxes_exceptions() -> None:
    from custom_components.philips_sonicare_ble.exceptions import (
        DeviceAsleepException,
        NotPairedException,
    )

    flow = _flow()

    flow._fetch_with_pair_retry = AsyncMock(side_effect=DeviceAsleepException)
    assert await flow._async_ble_probe(ADDRESS) == {
        "ok": False, "error": "asleep",
    }

    flow._fetch_with_pair_retry = AsyncMock(side_effect=NotPairedException)
    assert await flow._async_ble_probe(ADDRESS) == {
        "ok": False, "error": "not_paired",
    }

    flow._fetch_with_pair_retry = AsyncMock(side_effect=RuntimeError("boom"))
    assert await flow._async_ble_probe(ADDRESS) == {
        "ok": False, "error": "unknown",
    }

    flow._fetch_with_pair_retry = AsyncMock(return_value={"model": "X"})
    assert await flow._async_ble_probe(ADDRESS) == {
        "ok": True, "data": {"model": "X"},
    }


# --- determinate progress bar ------------------------------------------------

def test_bump_progress_noop_without_core_support() -> None:
    # HA < 2025.5 has no async_update_progress — must not raise.
    flow = _flow()
    assert not hasattr(flow, "async_update_progress")
    flow._bump_progress(0.5)


def test_bump_progress_forwards_clamped() -> None:
    flow = _flow()
    flow.async_update_progress = MagicMock()  # type: ignore[attr-defined]

    flow._bump_progress(0.4)
    flow._bump_progress(1.7)
    flow._bump_progress(-0.2)

    calls = [c.args[0] for c in flow.async_update_progress.call_args_list]
    assert calls == [0.4, 1.0, 0.0]


async def test_scan_and_bond_ticks_progress() -> None:
    import asyncio

    flow = _flow()
    flow.hass.loop = asyncio.get_running_loop()
    flow.async_update_progress = MagicMock()  # type: ignore[attr-defined]
    flow._pair_future = flow.hass.loop.create_future()
    flow._pair_future.set_result({"status": "pair_complete"})

    result = await flow._async_scan_and_bond()

    assert result == {"status": "pair_complete"}
    # At least the initial tick fired before the future resolved the wait.
    assert flow.async_update_progress.call_count >= 1
    assert all(
        0.0 <= c.args[0] <= 1.0
        for c in flow.async_update_progress.call_args_list
    )
