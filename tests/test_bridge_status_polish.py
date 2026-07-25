"""Tests for the bonded-slot menu and the pairing-success alert.

A slot that is already bonded on the bridge but has no config entry yet
(e.g. a leftover bond after removing an entry while the bridge was
offline) is routed to a small menu — set it up as-is, or unpair it —
instead of the old inline reset toggle. A pairing completed by wait_pair
is still acknowledged with a success alert on the next status render.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)


def _flow(bridge_info: dict[str, str] | None) -> PhilipsSonicareConfigFlow:
    flow = PhilipsSonicareConfigFlow()
    flow.hass = SimpleNamespace(config=SimpleNamespace(components=set()))
    flow.flow_id = "test-flow"
    flow.handler = "philips_sonicare_ble"
    flow._esp_device_name = "atom-s3r"
    flow._esp_bridge_id = "sonicare_1"
    flow._bridge_info = bridge_info
    return flow


BONDED_INFO = {
    "version": "1.4.0",
    "mac": "24:E5:AA:BE:9C:1B",
    "paired": "true",
    "ble_connected": "false",
    "pair_capable": "false",
}


# --- routing to the bonded-slot menu -------------------------------------

async def test_bonded_slot_routes_to_menu() -> None:
    """A bonded slot with no fresh pairing lands on the action menu."""
    result = await _flow(dict(BONDED_INFO))._route_after_health_check()

    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "esp_slot_action"
    assert set(result["menu_options"]) == {"slot_setup", "slot_unpair"}
    assert result["description_placeholders"]["target"] == "atom-s3r / sonicare_1"


async def test_pair_capable_slot_skips_menu() -> None:
    """An empty pair-capable slot goes to the status/pair path, not the menu."""
    info = dict(BONDED_INFO, paired="false", pair_capable="true")
    result = await _flow(info)._route_after_health_check()

    assert result["step_id"] != "esp_slot_action"


async def test_fresh_pair_skips_menu() -> None:
    """Right after wait_pair bonded this brush, skip the menu — the
    status screen shows the success alert instead."""
    flow = _flow(dict(BONDED_INFO))
    flow._just_paired = True

    result = await flow._route_after_health_check()

    assert result["step_id"] != "esp_slot_action"


async def test_chosen_action_not_re_prompted() -> None:
    """Once an action was picked, re-entering the check won't re-menu."""
    flow = _flow(dict(BONDED_INFO))
    flow._slot_action_chosen = True

    result = await flow._route_after_health_check()

    assert result["step_id"] != "esp_slot_action"


async def test_menu_setup_reads_capabilities() -> None:
    flow = _flow(dict(BONDED_INFO))

    result = await flow.async_step_slot_setup()

    assert flow._slot_action_chosen is True
    # Bonded, not pair-capable → renders the status form (Read capabilities).
    assert result["step_id"] == "esp_bridge_status"


async def test_menu_unpair_routes_to_reset_bridge() -> None:
    flow = _flow(dict(BONDED_INFO, identity_address="24:E5:AA:BE:9C:1B"))

    result = await flow.async_step_slot_unpair()

    assert flow._slot_action_chosen is True
    assert result["step_id"] == "reset_bridge"
    assert result["description_placeholders"]["target"] == "atom-s3r / sonicare_1"


# --- status step no longer carries a toggle ------------------------------

async def test_status_form_has_no_schema() -> None:
    """The reset toggle is gone; the status form is schema-less again."""
    flow = _flow(dict(BONDED_INFO))
    flow._slot_action_chosen = True  # reach the form, not the menu

    result = await flow.async_step_esp_bridge_status()

    assert result["data_schema"].schema == {}


# --- success alert (unchanged behaviour) ---------------------------------

async def test_just_paired_renders_success_alert_once(flow_text_blocks) -> None:
    """The acknowledgement rides in as a translated text block; it must
    still be one-shot."""
    flow = _flow(dict(BONDED_INFO))
    flow._just_paired = True

    first = await flow.async_step_esp_bridge_status()
    assert first["step_id"] == "esp_bridge_status"
    assert first["description_placeholders"]["alert"] == (
        '<ha-alert alert-type="success">'
        f'{flow_text_blocks["esp_status_paired"]}</ha-alert>\n\n'
    )
    assert flow._just_paired is False

    second = await flow.async_step_esp_bridge_status()
    assert second["description_placeholders"]["alert"] == ""


async def test_no_alert_without_fresh_pairing() -> None:
    flow = _flow(dict(BONDED_INFO))
    flow._slot_action_chosen = True

    result = await flow.async_step_esp_bridge_status()

    assert result["description_placeholders"]["alert"] == ""


# --- ESP capabilities read as a progress task (review point #2) -----------

async def test_read_capabilities_launches_progress() -> None:
    """Clicking Read capabilities runs the read behind a progress spinner."""
    flow = _flow(dict(BONDED_INFO))

    def _create(coro, *a, **k):
        coro.close()
        return SimpleNamespace(done=lambda: False)

    flow.hass = SimpleNamespace(async_create_task=_create)
    flow._slot_action_chosen = True

    result = await flow.async_step_esp_bridge_status({})

    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "esp_reading"
    assert flow._esp_caps_task is not None


async def test_read_finish_success_shows_capabilities(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    flow = _flow(dict(BONDED_INFO))
    flow._esp_caps_result = {"ok": True, "caps": {"model": "HX9911/09"}}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_already_configured = MagicMock()
    flow.async_step_show_capabilities = AsyncMock(return_value={"type": "sentinel"})

    result = await flow.async_step_esp_read_finish()

    assert result == {"type": "sentinel"}
    assert flow._fetched_data["model"] == "HX9911/09"
    assert flow._fetched_data["pairing"] == "bonded"


async def test_read_finish_error_rerenders_with_alert(flow_text_blocks) -> None:
    flow = _flow(dict(BONDED_INFO))
    flow._esp_caps_result = {"ok": False, "error": "cannot_connect"}
    flow._slot_action_chosen = True  # avoid the bonded-slot menu on re-render

    result = await flow.async_step_esp_read_finish()

    assert result["type"] == FlowResultType.FORM
    # The failure wording lives in the translations, so the outcome picks
    # a text block rather than injecting an English sentence.
    assert result["step_id"] == "esp_bridge_status"
    assert result["description_placeholders"]["alert"] == (
        '<ha-alert alert-type="error">'
        f'{flow_text_blocks["esp_status_read_failed"]}</ha-alert>\n\n'
    )
    # one-shot: cleared after rendering
    assert flow._esp_read_error == ""


async def test_read_finish_unknown_error_uses_its_own_wording(
    flow_text_blocks,
) -> None:
    """An unexpected error points at the logs, not at the power button."""
    flow = _flow(dict(BONDED_INFO))
    flow._esp_caps_result = {"ok": False, "error": "unknown"}
    flow._slot_action_chosen = True

    result = await flow.async_step_esp_read_finish()

    assert flow_text_blocks["esp_status_read_error"] in (
        result["description_placeholders"]["alert"]
    )


async def test_status_table_values_are_language_neutral() -> None:
    """Row labels come from the translations; only symbols/IDs are passed."""
    flow = _flow(dict(BONDED_INFO))
    flow._slot_action_chosen = True

    ph = (await flow.async_step_esp_bridge_status())["description_placeholders"]

    assert ph["security"] in ("🔒", "🔓", "—")
    assert ph["ble_state"] in ("✅", "❌")
    assert ph["version"].startswith("v")
    for value in (ph["ble_state"], ph["security"], ph["mac"], ph["version"]):
        assert not re.search(r"[A-Za-z]{3,}", value), value
