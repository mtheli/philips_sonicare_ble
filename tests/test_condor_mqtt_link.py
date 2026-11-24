"""CondorProtocol over MQTT: CondorMqttLink ↔ broker ↔ reference gateway.

The gateway (scripts/condor_mqtt_gateway.py) serves a fake Condor link, the
broker is in memory. Covers the contract in docs/MQTT_GATEWAY.md: the
gateway runs the session (subscriptions, snapshot in retained state
topics), Home Assistant listens; requests only for the sensor stream and
single reads and writes.
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
from custom_components.philips_sonicare_ble.condor_protocol import (
    DEFAULT_SUBSCRIBE_PORTS,
    CondorProtocol,
)
from custom_components.philips_sonicare_ble.exceptions import TransportError

from .mqtt_helpers import Broker, CondorMqttGateway
from .test_condor_link import DictLink

BASE = base_topic("AA:BB:CC:DD:EE:FF")


async def _settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


class AcceptAll(DictLink):
    async def subscribe(self, product, port):
        self.calls.append(("sub", product, port))
        return 0, {}


class Setup:
    def __init__(self) -> None:
        self.broker = Broker()
        self.device = AcceptAll({
            "0": {"Firmware": {"Version": "1.8.20.0"}},
            "1": {
                "Battery": {"BatteryPercent": 74},
                "RoutineStatus": {"Duration": 0, "SessionID": 7},
                "SensorData": {"Sensors": 0},
                "SensorData.b": b"",
                "Empty": {},
            },
        })
        self.gw_client = self.broker.client()
        self.gw_client.will = (f"{BASE}/status", "offline")
        self.gateway = CondorMqttGateway(
            self.device, self.gw_client, BASE,
            {"address": "AA:BB:CC:DD:EE:FF", "model": "HX7410"},
        )
        self.link = self.new_link()
        self.protocol = CondorProtocol(transport=None, link=self.link)

    def new_link(self) -> CondorMqttLink:
        return CondorMqttLink(
            self.broker.client(), BASE, request_timeout=0.5, snapshot_timeout=0.5,
        )

    async def brush_connects(self) -> None:
        await self.device.open()
        await self.gateway.session_up()
        await _settle()

    def retained(self, topic: str):
        payload = self.broker.retained.get(f"{BASE}/{topic}")
        return json.loads(payload) if payload else payload


@pytest.fixture
async def setup() -> Setup:
    s = Setup()
    await s.gateway.start()
    await s.brush_connects()
    yield s
    await s.gateway.stop()


# --- Gateway side -----------------------------------------------------------


async def test_gateway_runs_the_session(setup) -> None:
    subs = [c[1:] for c in setup.device.calls if c[0] == "sub"]
    assert subs == list(DEFAULT_SUBSCRIBE_PORTS)

    link = setup.retained("link")
    assert link["connected"] and link["snapshot"]
    assert link["ports"] == ["0/Firmware", "1/Battery", "1/RoutineStatus", "1/SensorData"]
    state = setup.retained("state/1/Battery")
    assert state == {"session": link["session"], "props": {"BatteryPercent": 74}}
    # Empty ports and binary ports have no state.
    assert setup.retained("state/1/Empty") is None
    assert setup.retained("state/1/SensorData.b") is None


async def test_start_order_and_retained_topics() -> None:
    s = Setup()
    await s.gateway.start()
    order = [t for t, *_ in s.broker.log]
    # link before status: a stale retained "connected" must never pair with
    # a fresh "online".
    assert order.index(f"{BASE}/link") < order.index(f"{BASE}/status")
    assert s.retained("link")["connected"] is False
    # Starts from the clock, so a restarted gateway never repeats a session.
    assert s.retained("link")["session"] > 1_700_000_000
    info = s.retained("info")
    assert info["protocol"] == 1 and info["model"] == "HX7410"
    await s.gateway.stop()
    assert s.broker.retained[f"{BASE}/status"] == "offline"


async def test_indications_update_the_retained_state(setup) -> None:
    setup.device.push("1", "RoutineStatus", {"Duration": 42})
    await _settle()
    assert setup.retained("state/1/RoutineStatus")["props"] == {
        "Duration": 42, "SessionID": 7,
    }
    ind = [m for m in setup.broker.log if m[0] == f"{BASE}/ind/1/RoutineStatus"]
    assert json.loads(ind[-1][1]) == {"Duration": 42} and not ind[-1][3]


async def test_indication_while_reading_wins_over_the_read() -> None:
    s = Setup()
    await s.gateway.start()
    original = s.device.get_props

    async def get_props(product, port, timeout=5.0):
        result = await original(product, port, timeout)
        if port == "RoutineStatus":
            s.device.push("1", "RoutineStatus", {"Duration": 3})
        return result

    s.device.get_props = get_props
    await s.brush_connects()
    assert s.retained("state/1/RoutineStatus")["props"]["Duration"] == 3
    await s.gateway.stop()


async def test_ports_of_an_earlier_session_are_cleared(setup) -> None:
    del setup.device.products["1"]["Battery"]
    await setup.gateway.session_down()
    await setup.brush_connects()
    assert f"{BASE}/state/1/Battery" not in setup.broker.retained
    assert "1/Battery" not in setup.retained("link")["ports"]


# --- Home Assistant side ----------------------------------------------------


async def test_refresh_comes_from_the_snapshot(setup) -> None:
    gets_before = len(setup.device.calls)
    await setup.protocol.connect()
    data = await setup.protocol.refresh_all()
    assert data["battery"] == 74
    assert data["brushing_time"] == 0
    # No request reached the brush for it.
    assert len(setup.device.calls) == gets_before


async def test_live_updates_and_held_indications(setup) -> None:
    await setup.protocol.connect()
    await setup.protocol.refresh_all()
    # Arrives between snapshot and the end of setup: held, then replayed.
    setup.device.push("1", "RoutineStatus", {"Duration": 5})
    await _settle()
    deltas: list[dict] = []
    await setup.protocol.start_live_updates(deltas.append)
    setup.device.push("1", "RoutineStatus", {"Duration": 6})
    await _settle()
    assert [d.get("brushing_time") for d in deltas] == [5, 6]
    # HA never subscribes nor unsubscribes: the ports are the gateway's.
    subs = [c for c in setup.device.calls if c[0] in ("sub", "unsub")]
    await setup.protocol.stop_live_updates()
    assert [c for c in setup.device.calls if c[0] in ("sub", "unsub")] == subs


async def test_home_assistant_restart_reads_the_retained_snapshot(setup) -> None:
    setup.device.push("1", "Battery", {"BatteryPercent": 55})
    await _settle()
    calls = len(setup.device.calls)
    link = setup.new_link()  # a fresh HA, same gateway session
    protocol = CondorProtocol(transport=None, link=link)
    await protocol.connect()
    data = await protocol.refresh_all()
    assert data["battery"] == 55
    assert len(setup.device.calls) == calls


async def test_state_of_another_session_is_not_used(setup) -> None:
    await setup.protocol.connect()
    # A retained state left over from an older session.
    await setup.broker.client().publish(
        f"{BASE}/state/1/Battery",
        json.dumps({"session": 1, "props": {"BatteryPercent": 1}}), retain=True,
    )
    await _settle()
    data = await setup.protocol.refresh_all()
    assert "battery" not in data


async def test_snapshot_timeout(setup) -> None:
    # Brush connected, but the gateway never finishes reading.
    await setup.broker.client().publish(
        f"{BASE}/link",
        json.dumps({"connected": True, "session": 99, "snapshot": False}), retain=True,
    )
    await _settle()
    await setup.protocol.connect()
    with pytest.raises(TransportError, match="no snapshot"):
        await setup.protocol.refresh_all()


async def test_sensor_stream_is_one_command(setup) -> None:
    await setup.protocol.connect()
    deltas: list[dict] = []
    await setup.protocol.start_live_updates(deltas.append)

    assert await setup.protocol.start_sensor_stream(3)
    assert ("put", "1", "SensorData", {"Sensors": 3}) in setup.device.calls
    await _settle()
    assert setup.retained("link")["sensor_mask"] == 3

    frame = struct.pack("<HHhB", 1, 7, 300, 0)
    setup.device.push("1", "SensorData.b", frame)
    await _settle()
    msg = [m for m in setup.broker.log if m[0].endswith("/ind/1/SensorData.b")][0]
    assert json.loads(msg[1]) == {"hex": frame.hex()} and msg[2] == 0 and not msg[3]
    assert deltas[-1]["pressure"] == 300
    assert setup.retained("state/1/SensorData.b") is None

    await setup.protocol.stop_sensor_stream()
    await _settle()
    assert setup.retained("link")["sensor_mask"] == 0
    assert ("unsub", "1", "SensorData.b") in setup.device.calls


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
    assert setup.link.link_up and not setup.link.is_open
    await setup.protocol.connect()
    assert setup.link.is_open


async def test_last_will_overrides_retained_link(setup) -> None:
    await setup.protocol.connect()
    setup.gw_client.die()
    await _settle()

    assert setup.retained("link")["connected"] is True
    assert not setup.link.link_up and not setup.link.is_open
    with pytest.raises(TransportError):
        await setup.protocol.refresh_all()


async def test_unanswered_request_times_out(setup) -> None:
    await setup.protocol.connect()
    # The gateway hangs: still online, but no longer takes requests.
    setup.gateway._unsub()
    with pytest.raises(TransportError, match="no answer"):
        await setup.link.get_props("1", "Battery")


async def test_answers_for_others_and_late_answers_are_ignored(setup) -> None:
    await setup.protocol.connect()
    probe = setup.broker.client()
    await probe.publish(f"{BASE}/resp", json.dumps({"id": 1, "client": "other", "status": 9}))
    await probe.publish(f"{BASE}/resp", json.dumps({"id": 999, "status": 0, "data": {}}))
    await _settle()
    status, data = await setup.link.get_props("1", "Battery")
    assert status == 0 and data == {"BatteryPercent": 74}


async def test_gateway_answers_errors_and_duplicates() -> None:
    s = Setup()
    await s.gateway.start()
    answers: list[dict] = []
    await s.broker.client().subscribe(
        f"{BASE}/resp", lambda _t, p: answers.append(json.loads(p)),
    )
    probe = s.broker.client()
    await probe.publish(f"{BASE}/cmd", json.dumps({"id": 1, "client": "c", "op": "get_props", "product": "1", "port": "Battery"}))
    await probe.publish(f"{BASE}/cmd", json.dumps({"id": 2, "client": "c", "op": "subscribe", "product": "1", "port": "Battery"}))
    await probe.publish(f"{BASE}/cmd", json.dumps({"id": 3, "client": "c", "op": "get_props", "product": "1"}))
    await probe.publish(f"{BASE}/cmd", json.dumps({"id": 4, "client": "c", "op": "sensor_stream"}))
    await _settle(40)
    assert {a["id"]: a.get("error") for a in answers} == {
        1: "not_connected", 2: "bad_request", 3: "bad_request", 4: "bad_request",
    }
    assert all(a["client"] == "c" for a in answers)

    await s.brush_connects()
    answers.clear()
    put = {"id": 5, "client": "c", "op": "put_props", "product": "1",
           "port": "SensorData", "props": {"Sensors": 1}}
    await probe.publish(f"{BASE}/cmd", json.dumps(put))
    await probe.publish(f"{BASE}/cmd", json.dumps(put))  # QoS 1 duplicate
    await _settle(40)
    # Run once, answered once.
    assert [a["id"] for a in answers] == [5]
    assert sum(1 for c in s.device.calls if c[0] == "put") == 1
    # A copy after the answer gets the same answer again, still not run.
    await probe.publish(f"{BASE}/cmd", json.dumps(put))
    await _settle(40)
    assert [a["id"] for a in answers] == [5, 5]
    assert sum(1 for c in s.device.calls if c[0] == "put") == 1
    await s.gateway.stop()


def test_base_topic() -> None:
    assert base_topic("AA:BB:CC:DD:EE:FF") == "sonicare_gateway/aabbccddeeff"
