"""Tests for the confirmed-unpair helper and the reset_bridge error path.

async_unpair_bridge_slot fires ble_unpair and waits for the bridge's
`unpaired` status event, so a silent failure (call returns but the bond
stays) is no longer mistaken for success. The reset_bridge config-flow
step surfaces that as an error instead of dropping the user back onto the
still-bonded status screen.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)
from custom_components.philips_sonicare_ble.transport import (
    ESP_STATUS_EVENT_NAME,
    async_unpair_bridge_slot,
    UNPAIR_FAILED,
    UNPAIR_OK,
    UNPAIR_UNAVAILABLE,
    UNPAIR_UNCONFIRMED,
)


class _FakeBus:
    def __init__(self) -> None:
        self._cb = None

    def async_listen(self, event_name, cb):
        assert event_name == ESP_STATUS_EVENT_NAME
        self._cb = cb
        return lambda: setattr(self, "_cb", None)

    def emit(self, data: dict) -> None:
        if self._cb is not None:
            self._cb(SimpleNamespace(data=data))


class _FakeServices:
    def __init__(self, *, has: bool, confirm: bool, raises: bool = False) -> None:
        self._has = has
        self._confirm = confirm
        self._raises = raises
        self.calls = 0

    def has_service(self, domain, name) -> bool:
        return self._has

    async def async_call(self, domain, name, data, blocking=False):
        self.calls += 1
        if self._raises:
            raise RuntimeError("boom")
        # A confirming bridge fires the `unpaired` event during the call.
        if self._confirm:
            self._bus.emit({"status": "unpaired", "bridge_id": self._bridge_id})


def _hass(services: _FakeServices, bus: _FakeBus):
    services._bus = bus
    return SimpleNamespace(services=services, bus=bus)


BRIDGE = "atom-lite"
SLOT = "sonicare_1"


async def test_unpair_confirmed() -> None:
    bus = _FakeBus()
    svcs = _FakeServices(has=True, confirm=True)
    svcs._bridge_id = SLOT
    result = await async_unpair_bridge_slot(_hass(svcs, bus), BRIDGE, SLOT)
    assert result == UNPAIR_OK
    assert svcs.calls == 1


async def test_unpair_unavailable_when_service_missing() -> None:
    bus = _FakeBus()
    svcs = _FakeServices(has=False, confirm=False)
    result = await async_unpair_bridge_slot(_hass(svcs, bus), BRIDGE, SLOT)
    assert result == UNPAIR_UNAVAILABLE
    assert svcs.calls == 0


async def test_unpair_failed_on_call_error() -> None:
    bus = _FakeBus()
    svcs = _FakeServices(has=True, confirm=False, raises=True)
    result = await async_unpair_bridge_slot(_hass(svcs, bus), BRIDGE, SLOT)
    assert result == UNPAIR_FAILED


async def test_unpair_unconfirmed_on_timeout() -> None:
    bus = _FakeBus()
    svcs = _FakeServices(has=True, confirm=False)  # never emits the event
    svcs._bridge_id = SLOT
    result = await async_unpair_bridge_slot(
        _hass(svcs, bus), BRIDGE, SLOT, timeout=0.05
    )
    assert result == UNPAIR_UNCONFIRMED


async def test_unpair_ignores_other_bridge_event() -> None:
    """An `unpaired` event for a different slot must not count."""
    bus = _FakeBus()
    svcs = _FakeServices(has=True, confirm=False)
    svcs._bridge_id = SLOT

    async def call(domain, name, data, blocking=False):
        svcs.calls += 1
        bus.emit({"status": "unpaired", "bridge_id": "sonicare_9"})

    svcs.async_call = call
    result = await async_unpair_bridge_slot(
        _hass(svcs, bus), BRIDGE, SLOT, timeout=0.05
    )
    assert result == UNPAIR_UNCONFIRMED


# --- reset_bridge step wiring --------------------------------------------

def _flow() -> PhilipsSonicareConfigFlow:
    flow = PhilipsSonicareConfigFlow()
    flow.flow_id = "test-flow"
    flow.handler = "philips_sonicare_ble"
    flow._esp_device_name = "atom-lite"
    flow._esp_bridge_id = "sonicare_1"
    flow._bridge_info = {"identity_address": "24:E5:AA:BE:9C:1B"}
    return flow


def _hass_task(done: bool = False):
    """A hass stub whose async_create_task returns a controllable task."""
    def _create(coro, *a, **k):
        coro.close()  # don't run the real unpair coroutine in unit tests
        return SimpleNamespace(done=lambda: done, result=lambda: UNPAIR_OK)
    return SimpleNamespace(async_create_task=_create)


async def test_reset_bridge_submit_launches_progress() -> None:
    """Clicking Submit starts the unpair task behind a progress spinner."""
    flow = _flow()
    flow.hass = _hass_task()

    result = await flow.async_step_reset_bridge({})

    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "unpairing"
    assert flow._unpair_task is not None


async def test_reset_finish_success_continues() -> None:
    flow = _flow()
    flow._unpair_outcome = UNPAIR_OK
    flow._esp_bridge_health_check = AsyncMock(return_value={"type": "sentinel"})

    result = await flow.async_step_reset_finish()

    assert result == {"type": "sentinel"}
    assert flow._bridge_info is None  # cleared before re-probe
    assert flow._just_unpaired is True


async def test_reset_finish_unconfirmed_shows_alert() -> None:
    """A schema-less step can't render errors["base"], so the failure
    must appear as an <ha-alert> injected into the description."""
    flow = _flow()
    flow._unpair_outcome = UNPAIR_UNCONFIRMED
    flow._esp_bridge_health_check = AsyncMock(return_value={"type": "sentinel"})

    result = await flow.async_step_reset_finish()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reset_bridge"
    alert = result["description_placeholders"]["error"]
    assert 'ha-alert alert-type="error"' in alert
    assert "confirm" in alert.lower()
    flow._esp_bridge_health_check.assert_not_awaited()


async def test_reset_finish_offline_shows_alert() -> None:
    flow = _flow()
    flow._unpair_outcome = UNPAIR_UNAVAILABLE

    result = await flow.async_step_reset_finish()

    alert = result["description_placeholders"]["error"]
    assert 'ha-alert alert-type="error"' in alert
    assert "online" in alert.lower()


async def test_reset_bridge_initial_render_has_empty_error() -> None:
    """The confirmation render (no user_input) carries an empty error slot."""
    flow = _flow()
    result = await flow.async_step_reset_bridge()

    assert result["description_placeholders"]["error"] == ""
    assert "<" not in result["description_placeholders"]["error"]


async def test_reset_finish_success_sets_unpaired_notice() -> None:
    """A confirmed unpair flags request_pair to show a success notice."""
    flow = _flow()
    flow._unpair_outcome = UNPAIR_OK
    flow._esp_bridge_health_check = AsyncMock(return_value={"type": "sentinel"})

    await flow.async_step_reset_finish()
    assert flow._just_unpaired is True


async def test_request_pair_shows_unpaired_notice_once() -> None:
    flow = _flow()
    flow._just_unpaired = True

    first = await flow.async_step_request_pair()
    notice = first["description_placeholders"]["notice"]
    assert 'ha-alert alert-type="success"' in notice
    assert "removed" in notice.lower()
    assert flow._just_unpaired is False

    second = await flow.async_step_request_pair()
    assert second["description_placeholders"]["notice"] == ""


async def test_request_pair_no_notice_by_default() -> None:
    flow = _flow()
    result = await flow.async_step_request_pair()
    assert result["description_placeholders"]["notice"] == ""
