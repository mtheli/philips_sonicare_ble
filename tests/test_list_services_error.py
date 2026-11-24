"""list_services stops waiting when the bridge reports a failure.

Bridge firmware 1.16.0 answers every failure of ble_list_services
(not connected, out of memory, GATT cache error) with an error event. Before, it stayed silent and
the integration ran into its timeout before falling back to the legacy probe.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.philips_sonicare_ble.transport import (
    ESP_SERVICES_EVENT_NAME,
    EspBridgeTransport,
)

MAC = "24:E5:AA:00:00:01"
SVC = "477ea600-a260-11e4-ae37-0002a5d50001"


class _FakeHass:
    """Just enough hass: the service call fires the given bridge events."""

    def __init__(self, replies: list[dict]) -> None:
        self.loop = asyncio.get_running_loop()
        self._replies = replies
        self._listeners: list = []
        self.calls: list[str] = []
        self.bus = SimpleNamespace(async_listen=self._listen)
        self.services = SimpleNamespace(async_call=self._call)

    def _listen(self, event_type, handler):
        assert event_type == ESP_SERVICES_EVENT_NAME
        self._listeners.append(handler)
        return lambda: self._listeners.remove(handler)

    async def _call(self, domain, service, data, blocking=False):
        self.calls.append(service)
        for reply in self._replies:
            for handler in list(self._listeners):
                handler(SimpleNamespace(data=reply))


def _transport(hass: _FakeHass) -> EspBridgeTransport:
    transport = EspBridgeTransport.__new__(EspBridgeTransport)
    transport._hass = hass
    transport._device_name = "sonicare"
    transport._esp_bridge_id = ""
    transport._detected_mac = MAC
    transport._bond_address = None
    transport._identity_cb = None
    return transport


@pytest.fixture(autouse=True)
def _connected(monkeypatch):
    monkeypatch.setattr(
        EspBridgeTransport, "is_connected", property(lambda self: True)
    )


@pytest.mark.parametrize("error", ["not_connected", "no_memory", "gattc_error"])
async def test_error_event_ends_wait(error: str) -> None:
    hass = _FakeHass([{"mac": MAC, "error": error}])
    start = time.monotonic()
    assert await _transport(hass).list_services(timeout=5.0) == []
    assert time.monotonic() - start < 1.0
    assert hass.calls == ["sonicare_ble_list_services"]


async def test_error_of_other_brush_is_ignored() -> None:
    hass = _FakeHass([{"mac": "11:22:33:44:55:66", "error": "not_connected"}])
    start = time.monotonic()
    assert await _transport(hass).list_services(timeout=0.2) == []
    assert time.monotonic() - start >= 0.2


async def test_empty_table_ends_wait() -> None:
    hass = _FakeHass([{
        "mac": MAC, "service_count": "0", "service_index": "0",
        "service_uuid": "", "service_chars": "",
    }])
    assert await _transport(hass).list_services(timeout=5.0) == []


async def test_services_are_collected() -> None:
    hass = _FakeHass([
        {"mac": MAC, "service_count": "2", "service_index": "0",
         "service_uuid": "180f", "service_chars": "2a19/RN"},
        {"mac": MAC, "service_count": "2", "service_index": "1",
         "service_uuid": SVC, "service_chars": ""},
    ])
    assert await _transport(hass).list_services(timeout=5.0) == [
        "0000180f-0000-1000-8000-00805f9b34fb",
        SVC,
    ]


async def test_error_reaches_identity_entry() -> None:
    """A brush with a rotating address: the bridge tags the event with
    peer_identity, so an entry on the identity stops waiting too."""
    bond = "77:F0:B2:D9:FD:17"
    hass = _FakeHass([{"mac": bond, "peer_identity": MAC, "error": "gattc_error"}])
    start = time.monotonic()
    assert await _transport(hass).list_services(timeout=5.0) == []
    assert time.monotonic() - start < 1.0
