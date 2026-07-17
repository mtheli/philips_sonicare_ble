"""Tests for the async_show_progress two-phase wait_pair flow.

Config-flow review point 4: the pairing wait used to block the flow
handler for up to ~65 s behind a blank spinner. It now runs as two
background phases (arming → scanning) surfaced via async_show_progress,
and async_step_pair_finish renders the captured outcome.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)


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
    flow._esp_device_name = "atom-s3r"
    flow._esp_bridge_id = "sonicare_1"
    created: list = []

    def _create_task(coro, *args, **kwargs):
        # Never actually run the coroutine in unit tests.
        coro.close()
        task = _FakeTask(done=False)
        created.append(task)
        return task

    flow.hass = SimpleNamespace(
        async_create_task=MagicMock(side_effect=_create_task),
        services=SimpleNamespace(async_call=AsyncMock()),
    )
    flow._created_tasks = created  # type: ignore[attr-defined]
    return flow


# --- phase orchestration --------------------------------------------------

async def test_first_call_arms_and_shows_progress() -> None:
    flow = _flow()
    result = await flow.async_step_wait_pair()

    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "pair_arming"
    assert flow._pair_arm_task is not None
    assert "atom-s3r / sonicare_1" in result["description_placeholders"]["target"]


async def test_arm_success_transitions_to_scanning() -> None:
    flow = _flow()
    flow._pair_arm_task = _FakeTask(done=True, result=True)

    result = await flow.async_step_wait_pair()

    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "pair_scanning"
    assert flow._pair_arm_task is None
    assert flow._pair_scan_task is not None


async def test_arm_failure_finishes_with_error() -> None:
    flow = _flow()
    flow._pair_arm_task = _FakeTask(done=True, result=False)

    result = await flow.async_step_wait_pair()

    assert result["type"] == FlowResultType.SHOW_PROGRESS_DONE
    assert result["step_id"] == "pair_finish"
    assert flow._pair_result == {"error": "cannot_connect"}
    # No scan phase started after a failed arm.
    assert flow._pair_scan_task is None


async def test_scan_done_captures_result_and_finishes() -> None:
    flow = _flow()
    payload = {"status": "pair_complete", "identity_address": "24:E5:AA:BE:9C:1B"}
    flow._pair_scan_task = _FakeTask(done=True, result=payload)

    result = await flow.async_step_wait_pair()

    assert result["type"] == FlowResultType.SHOW_PROGRESS_DONE
    assert result["step_id"] == "pair_finish"
    assert flow._pair_result == payload
    assert flow._pair_scan_task is None


# --- pair_finish outcome rendering ---------------------------------------

def _finishable_flow(result: dict) -> PhilipsSonicareConfigFlow:
    flow = _flow()
    flow._pair_result = result
    flow._pair_unsub = MagicMock()
    flow._pair_svc_name = "atom-s3r_ble_pair_mode_sonicare_1"
    return flow


async def test_finish_success_runs_health_check() -> None:
    flow = _finishable_flow(
        {"status": "pair_complete", "identity_address": "24:E5:AA:BE:9C:1B"}
    )
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_already_configured = MagicMock()
    flow._esp_bridge_health_check = AsyncMock(return_value={"type": "sentinel"})
    unsub = flow._pair_unsub

    result = await flow.async_step_pair_finish()

    assert result == {"type": "sentinel"}
    assert flow._just_paired is True
    assert flow._address == "24:E5:AA:BE:9C:1B"
    flow.async_set_unique_id.assert_awaited_once()
    # Clean bond → listener removed, NO disarm call.
    unsub.assert_called_once()
    assert flow._pair_unsub is None
    flow.hass.services.async_call.assert_not_awaited()
    assert flow._pair_result is None


async def test_finish_timeout_returns_error_and_disarms() -> None:
    flow = _finishable_flow({"status": "pair_timeout"})
    unsub = flow._pair_unsub

    result = await flow.async_step_pair_finish()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "request_pair"
    assert result["errors"] == {"base": "pair_timeout"}
    # Not a clean bond → bridge told to stand down.
    flow.hass.services.async_call.assert_awaited_once()
    unsub.assert_called_once()


async def test_finish_arm_error_returns_cannot_connect() -> None:
    flow = _finishable_flow({"error": "cannot_connect"})

    result = await flow.async_step_pair_finish()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "request_pair"
    assert result["errors"] == {"base": "cannot_connect"}
    flow.hass.services.async_call.assert_awaited_once()


async def test_finish_complete_without_identity_is_unknown() -> None:
    flow = _finishable_flow({"status": "pair_complete"})

    result = await flow.async_step_pair_finish()

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
    # Bond reported complete → do NOT disarm.
    flow.hass.services.async_call.assert_not_awaited()


def test_progress_strings_present_and_placeholder_safe() -> None:
    import json
    import re
    from pathlib import Path

    comp = (
        Path(__file__).parent.parent
        / "custom_components"
        / "philips_sonicare_ble"
    )
    for name in ("strings.json", "translations/en.json"):
        data = json.loads((comp / name).read_text(encoding="utf-8"))
        progress = data["config"]["progress"]
        assert {"pair_arming", "pair_scanning"} <= set(progress), name
        arming_ph = set(re.findall(r"\{(\w+)\}", progress["pair_arming"]))
        assert arming_ph <= {"target"}, name
        for text in progress.values():
            # only the neutral {target} placeholder, no HTML (hassfest)
            assert set(re.findall(r"\{(\w+)\}", text)) <= {"target"}, name
            assert "<" not in text, name
