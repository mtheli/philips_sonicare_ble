"""CondorProtocol over MQTT: CondorMqttLink ↔ broker ↔ reference gateway.

The gateway (scripts/condor_mqtt_gateway.py) serves a fake Condor link, the
broker is in memory. Covers the contract in docs/MQTT_GATEWAY.md: requests
and answers, change indications (JSON and hex), session changes, the last
will, and the error answers.
"""

from __future__ import annotations

import asyncio
import json
import struct

import pytest

from custom_components.philips_sonicare_ble.condor_mqtt_link import (
    CondorMqttLink,
    base_topic,
)
from custom_components.philips_sonicare_ble.condor_protocol import CondorProtocol
from custom_components.philips_sonicare_ble.exceptions import TransportError

from .mqtt_helpers import Broker, CondorMqttGateway
from .test_condor_link import DictLink

BASE = base_topic("AA:BB:CC:DD:EE:FF")


async def _settle(rounds: int = 10) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


class Setup:
    def __init__(self) -> None:
        self.broker = Broker()
        self.device = DictLink({
            "0": {"Firmware": {}},
            "1": {
                "Battery": {"BatteryPercent": 74},
                "RoutineStatus": {"Duration": 0},
                "SensorData": {"Sensors": 0},
                "SensorData.b": b"",
            },
        })
        gw_client = self.broker.client()
        gw_client.will = (f"{BASE}/status", "offline")
        self.gw_client = gw_client
        self.gateway = CondorMqttGateway(
            self.device, gw_client, BASE,
            {"address": "AA:BB:CC:DD:EE:FF", "model": "HX7410"},
        )
        self.link = CondorMqttLink(self.broker.client(), BASE, request_timeout=0.5)
        self.protocol = CondorProtocol(transport=None, link=self.link)

    async def brush_connects(self) -> None:
        await self.device.open()
        await self.gateway.session_up()
        await _settle()


@pytest.fixture
async def setup() -> Setup:
    s = Setup()
    await s.gateway.start()
    await s.brush_connects()
    yield s
    await s.gateway.stop()


async def test_refresh_and_live_updates_through_the_gateway(setup) -> None:
    await setup.protocol.connect()
    data = await setup.protocol.refresh_all()
    assert data["battery"] == 74

    deltas: list[dict] = []
    await setup.protocol.start_live_updates(deltas.append)
    setup.device.push("1", "RoutineStatus", {"Duration": 12})
    await _settle()

    assert deltas == [{"brushing_time": 12}]
    ind = [m for m in setup.broker.log if m[0].endswith("/ind/1/RoutineStatus")]
    assert ind and json.loads(ind[0][1]) == {"Duration": 12}


async def test_binary_port_travels_as_hex(setup) -> None:
    await setup.protocol.connect()
    deltas: list[dict] = []
    await setup.protocol.start_live_updates(deltas.append)
    assert await setup.protocol.start_sensor_stream(1)

    frame = struct.pack("<HHhB", 1, 7, 300, 0)
    setup.device.push("1", "SensorData.b", frame)
    await _settle()

    topic, payload, qos, retain = [
        m for m in setup.broker.log if m[0].endswith("/ind/1/SensorData.b")
    ][0]
    assert json.loads(payload) == {"hex": frame.hex()}
    assert qos == 0 and not retain
    assert deltas[-1]["pressure"] == 300


async def test_error_status_is_passed_through(setup) -> None:
    await setup.protocol.connect()
    # DictLink declines BrushHead with NoSuchPort: not an exception, a status.
    status, _ = await setup.link.subscribe("1", "BrushHead")
    assert status == 3
    with pytest.raises(TransportError):
        await setup.protocol.get_props("1", "NoSuchPort")


async def test_open_fails_while_brush_is_away() -> None:
    s = Setup()
    await s.gateway.start()
    await _settle()
    with pytest.raises(TransportError, match="brush not connected"):
        await asyncio.wait_for(s.protocol.connect(), 5)
    await s.gateway.stop()


async def test_new_session_ends_the_open_one(setup) -> None:
    await setup.protocol.connect()
    changes: list[int] = []
    setup.link.on_state_change = lambda: changes.append(1)
    assert setup.link.is_open

    await setup.gateway.session_down()
    await _settle()
    assert not setup.link.is_open and not setup.link.link_up
    assert changes

    await setup.brush_connects()
    # Brush is back, but in a new session: the old one stays closed until
    # the owner sets up again.
    assert setup.link.link_up and not setup.link.is_open
    await setup.protocol.connect()
    assert setup.link.is_open


async def test_last_will_overrides_retained_link(setup) -> None:
    await setup.protocol.connect()
    setup.gw_client.die()
    await _settle()

    assert setup.broker.retained[f"{BASE}/link"].find('"connected": true') >= 0
    assert not setup.link.link_up and not setup.link.is_open
    with pytest.raises(TransportError):
        await setup.protocol.discover_products()


async def test_unanswered_request_times_out(setup) -> None:
    await setup.protocol.connect()
    # The gateway hangs: still online, but no longer takes requests.
    setup.gateway._unsub()
    with pytest.raises(TransportError, match="no answer"):
        await setup.link.get_prods()


async def test_late_answer_is_ignored(setup) -> None:
    await setup.protocol.connect()
    await setup.broker.client().publish(
        f"{BASE}/resp", json.dumps({"id": 999, "status": 0, "data": {}}),
    )
    await _settle()
    data = await setup.protocol.refresh_all()
    assert data["battery"] == 74


async def test_gateway_answers_errors() -> None:
    s = Setup()
    await s.gateway.start()
    answers: list[dict] = []
    await s.broker.client().subscribe(
        f"{BASE}/resp", lambda _t, p: answers.append(json.loads(p)),
    )
    probe = s.broker.client()
    await probe.publish(f"{BASE}/cmd", json.dumps({"id": 1, "op": "get_prods"}))
    await probe.publish(f"{BASE}/cmd", json.dumps({"id": 2, "op": "explode"}))
    await probe.publish(f"{BASE}/cmd", json.dumps({"id": 3, "op": "get_props", "product": "1"}))
    await _settle(30)

    assert {a["id"]: a.get("error") for a in answers} == {
        1: "not_connected", 2: "bad_request", 3: "bad_request",
    }
    await s.gateway.stop()


async def test_info_and_status_are_retained() -> None:
    s = Setup()
    await s.gateway.start()
    assert s.broker.retained[f"{BASE}/status"] == "online"
    info = json.loads(s.broker.retained[f"{BASE}/info"])
    assert info["protocol"] == 1 and info["model"] == "HX7410"
    link = json.loads(s.broker.retained[f"{BASE}/link"])
    assert link == {"connected": False, "session": 0}
    await s.gateway.stop()
    assert s.broker.retained[f"{BASE}/status"] == "offline"


def test_base_topic() -> None:
    assert base_topic("AA:BB:CC:DD:EE:FF") == "sonicare_gateway/aabbccddeeff"
