"""Coordinator with an MQTT gateway entry.

The whole path below the coordinator is real except the broker and the
brush: MqttGatewayTransport → CondorMqttLink → in-memory broker → reference
gateway → fake Condor link.
"""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.philips_sonicare_ble.const import (
    CONF_ADDRESS,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    SVC_CONDOR,
    SVC_DEVICE_INFO,
    TRANSPORT_MQTT_GATEWAY,
)
from custom_components.philips_sonicare_ble.condor_mqtt_link import base_topic
from custom_components.philips_sonicare_ble.coordinator import (
    PhilipsSonicareCoordinator,
)
from custom_components.philips_sonicare_ble.mqtt_transport import (
    MqttGatewayTransport,
)

from .mqtt_helpers import Broker, CondorMqttGateway, settle
from .test_condor_link import DictLink

ADDRESS = "AA:BB:CC:DD:EE:01"
BASE = base_topic(ADDRESS)


class Rig:
    def __init__(self, hass) -> None:
        self.broker = Broker()
        self.brush = DictLink({
            "1": {
                "Battery": {"BatteryPercent": 74},
                "RoutineStatus": {"Duration": 0},
                "Sonicare": {"HandleState": 1},
                "BrushHead": {},
                "SessionStorage": {},
            },
        })
        # DictLink declines BrushHead; accept everything here.
        self.brush.subscribe = self._subscribe_all
        self.gw_client = self.broker.client()
        self.gw_client.will = (f"{BASE}/status", "offline")
        self.gateway = CondorMqttGateway(
            self.brush, self.gw_client, BASE, {"address": ADDRESS, "model": "HX7410"},
        )
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=ADDRESS,
            data={
                CONF_ADDRESS: ADDRESS,
                CONF_TRANSPORT_TYPE: TRANSPORT_MQTT_GATEWAY,
                CONF_SERVICES: [SVC_CONDOR, SVC_DEVICE_INFO],
                "model": "HX7410",
            },
        )
        entry.add_to_hass(hass)
        self.transport = MqttGatewayTransport(None, ADDRESS, client=self.broker.client())
        self.coordinator = PhilipsSonicareCoordinator(hass, entry, self.transport)
        self.hass = hass
        self.task: asyncio.Task | None = None

    async def _subscribe_all(self, product, port):
        self.brush.calls.append(("sub", product, port))
        return 0, {}

    async def brush_connects(self) -> None:
        await self.brush.open()
        await self.gateway.session_up()

    def subscribes(self) -> int:
        return sum(1 for c in self.brush.calls if c[0] == "sub")

    async def wait(self, cond, rounds: int = 200) -> None:
        for _ in range(rounds):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not reached")


@pytest.fixture
async def rig(hass):
    r = Rig(hass)
    await r.gateway.start()
    r.task = hass.async_create_task(r.coordinator._start_live_monitoring())
    yield r
    r.task.cancel()
    try:
        await r.task
    except asyncio.CancelledError:
        pass
    await r.gateway.stop()


async def test_protocol_uses_the_mqtt_link(rig) -> None:
    assert rig.coordinator._use_condor
    assert rig.coordinator._protocol._link is rig.transport.link
    assert rig.coordinator.adapter_type == "mqtt_gateway"


async def test_live_monitoring_through_the_gateway(rig) -> None:
    await settle()
    assert not rig.transport.is_connected  # gateway up, brush away

    await rig.brush_connects()
    await rig.wait(lambda: rig.coordinator._live_setup_done)

    assert rig.coordinator.data["battery"] == 74
    assert rig.subscribes() == 5

    rig.brush.push("1", "RoutineStatus", {"Duration": 33})
    await rig.wait(lambda: rig.coordinator.data.get("brushing_time") == 33)


async def test_new_session_sets_up_again(rig) -> None:
    await rig.brush_connects()
    await rig.wait(lambda: rig.coordinator._live_setup_done)
    assert rig.subscribes() == 5

    # The brush dropped and came back before HA saw it disconnected:
    # same "connected: true", new session number.
    await rig.brush_connects()
    await rig.wait(lambda: rig.subscribes() == 10)
    await rig.wait(lambda: rig.coordinator._live_setup_done)
    assert rig.transport.link.is_open


async def test_brush_disconnect_marks_device_off(rig) -> None:
    await rig.brush_connects()
    await rig.wait(lambda: rig.coordinator._live_setup_done)

    await rig.gateway.session_down()
    await rig.wait(lambda: not rig.transport.is_connected)
    await rig.wait(lambda: rig.coordinator.data.get("handle_state") == "off")


async def test_gateway_last_will_ends_the_session(rig) -> None:
    await rig.brush_connects()
    await rig.wait(lambda: rig.coordinator._live_setup_done)

    rig.gw_client.die()
    await rig.wait(lambda: not rig.transport.is_connected)
    await rig.wait(lambda: not rig.coordinator._live_setup_done)
    assert not rig.transport.link.is_open


async def test_removing_the_entry_leaves_host_bonds_alone(hass, monkeypatch) -> None:
    from custom_components.philips_sonicare_ble import _async_release_bond
    from custom_components.philips_sonicare_ble import dbus_pairing

    called = []

    async def _boom(*args, **kwargs):
        called.append(args)

    monkeypatch.setattr(dbus_pairing, "async_remove_device", _boom)
    monkeypatch.setattr(dbus_pairing, "is_dbus_available", lambda: True)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: ADDRESS, CONF_TRANSPORT_TYPE: TRANSPORT_MQTT_GATEWAY},
    )
    await _async_release_bond(hass, entry)
    assert called == []
