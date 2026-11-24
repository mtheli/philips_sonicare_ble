"""CondorProtocol over a link that is not BLE.

The message layer only sees parsed port operations, so any link that can
answer them — for example a gateway that holds the BLE connection itself —
drives the same entities.
"""

from __future__ import annotations

import struct
from typing import Any

import pytest

from custom_components.philips_sonicare_ble.condor_link import (
    STATUS_OK,
    CondorLink,
)
from custom_components.philips_sonicare_ble.condor_protocol import (
    DEFAULT_SUBSCRIBE_PORTS,
    CondorProtocol,
)
from custom_components.philips_sonicare_ble.exceptions import TransportError

NO_SUCH_PORT = 3


class DictLink(CondorLink):
    """Answers port operations from a dict, records every call."""

    def __init__(self, products: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self.products = products
        self.calls: list[tuple] = []
        self.up = True
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def link_up(self) -> bool:
        return self.up

    async def open(self) -> None:
        self._open = True

    def invalidate(self) -> None:
        self._open = False

    async def close(self) -> None:
        self.invalidate()

    async def get_prods(self):
        return STATUS_OK, {pid: {} for pid in self.products}

    async def get_ports(self, product):
        return STATUS_OK, list(self.products[product])

    async def get_props(self, product, port, timeout=5.0):
        if port not in self.products[product]:
            return NO_SUCH_PORT, None
        return STATUS_OK, self.products[product][port]

    async def put_props(self, product, port, props):
        self.calls.append(("put", product, port, props))
        return STATUS_OK, props

    async def subscribe(self, product, port):
        self.calls.append(("sub", product, port))
        return (STATUS_OK if port != "BrushHead" else NO_SUCH_PORT), {}

    async def unsubscribe(self, product, port):
        self.calls.append(("unsub", product, port))
        return STATUS_OK, {}

    def push(self, product: str, port: str, props) -> None:
        if self._change_handler is not None:
            self._change_handler(product, port, props)


@pytest.fixture
def link() -> DictLink:
    return DictLink({
        "0": {"Firmware": {}},
        "1": {
            "Battery": {"BatteryPercent": 74},
            "RoutineStatus": {"Duration": 0},
            "SensorData.b": b"",
        },
    })


async def test_refresh_reads_json_ports_through_the_link(link) -> None:
    protocol = CondorProtocol(transport=None, link=link)
    await protocol.connect()

    data = await protocol.refresh_all()

    assert data["battery"] == 74
    assert data["brushing_time"] == 0


async def test_requests_need_an_open_session(link) -> None:
    protocol = CondorProtocol(transport=None, link=link)
    with pytest.raises(TransportError):
        await protocol.refresh_all()


async def test_change_indications_reach_the_callback(link) -> None:
    protocol = CondorProtocol(transport=None, link=link)
    await protocol.connect()
    deltas: list[dict] = []

    await protocol.start_live_updates(deltas.append)
    link.push("1", "RoutineStatus", {"Duration": 12})
    link.push("1", "Unknown.b", b"\x01\x02")

    assert deltas == [{"brushing_time": 12}]
    # A port the device declines is not kept for the unsubscribe.
    assert ("1", "BrushHead") not in protocol._subscribed_ports
    assert len(protocol._subscribed_ports) == len(DEFAULT_SUBSCRIBE_PORTS) - 1


async def test_binary_sensor_frames_are_decoded(link) -> None:
    protocol = CondorProtocol(transport=None, link=link)
    await protocol.connect()
    deltas: list[dict] = []
    await protocol.start_live_updates(deltas.append)

    assert await protocol.start_sensor_stream(1)
    assert ("put", "1", "SensorData", {"Sensors": 1}) in link.calls
    # Pressure frame: type 1, counter, pressure 300, alarm 0.
    link.push("1", "SensorData.b", struct.pack("<HHhB", 1, 7, 300, 0))

    assert deltas and "pressure" in deltas[-1]


async def test_stop_skips_unsubscribe_when_the_link_is_gone(link) -> None:
    protocol = CondorProtocol(transport=None, link=link)
    await protocol.connect()
    await protocol.start_live_updates(lambda _d: None)
    link.up = False

    await protocol.stop_live_updates()

    assert not [c for c in link.calls if c[0] == "unsub"]
    assert link._change_handler is None


async def test_invalidate_clears_both_layers(link) -> None:
    protocol = CondorProtocol(transport=None, link=link)
    await protocol.connect()
    deltas: list[dict] = []
    await protocol.start_live_updates(deltas.append)

    protocol.invalidate_session()
    link.push("1", "RoutineStatus", {"Duration": 5})

    assert not link.is_open
    assert link._change_handler is None
    assert protocol._subscribed_ports == []
    assert deltas == []


async def test_ble_link_acks_change_indication_without_handler() -> None:
    """The brush expects every ChangeInd answered, listened to or not."""
    import asyncio

    from custom_components.philips_sonicare_ble.condor_link import (
        MSG_CHANGE_IND,
        MSG_CHANGE_IND_RESP,
        CondorBleLink,
    )

    class Recorder:
        auto_tx_ack = True
        is_connected = True

        def __init__(self) -> None:
            self.writes: list[bytes] = []

        async def write_char(self, _uuid, data) -> None:
            self.writes.append(bytes(data))

    transport = Recorder()
    ble = CondorBleLink(transport)
    body = b"1/Battery\x00{\"BatteryPercent\":50}\x00"
    frame = b"\xfe\xff" + bytes([MSG_CHANGE_IND]) + struct.pack(">H", len(body)) + body

    ble._dispatch_frame(frame[2], frame[5:])
    for _ in range(3):
        await asyncio.sleep(0)

    sent = b"".join(w[1:] for w in transport.writes)
    assert sent == b"\xfe\xff" + bytes([MSG_CHANGE_IND_RESP]) + b"\x00\x02\x00\x00"
