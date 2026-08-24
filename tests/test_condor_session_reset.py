"""The Condor handshake state must never outlive the link it ran on.

A session that ends badly used to leave ``CondorProtocol._connected`` set.
The next setup then skipped the whole handshake — no notification
subscriptions, no channel open — and went straight to writing a framed
request into a channel the device had never opened. Nothing could answer
it, the link went away, and the retry hit the same short-circuit again:
once latched, only an integration reload recovered.
"""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.philips_sonicare_ble.condor_protocol import (
    CondorProtocol,
    MSG_INITIALIZE_RESP,
)
from custom_components.philips_sonicare_ble.const import (
    CHAR_CLIENT_CFG,
    CHAR_RX,
    CHAR_SERVER_CFG,
    CONF_ADDRESS,
    CONF_ESP_DEVICE_NAME,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    TRANSPORT_ESP_BRIDGE,
)
from custom_components.philips_sonicare_ble.coordinator import (
    PhilipsSonicareCoordinator,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


class HandshakeTransport:
    """Answers the V4 handshake the way a Condor device does."""

    is_connected = True
    disconnect_count = 0
    connection_path = None
    auto_tx_ack = True

    def __init__(self) -> None:
        self.subscribed: dict[str, object] = {}
        self.writes: list[tuple[str, bytes]] = []

    async def subscribe(self, char_uuid, cb) -> None:
        self.subscribed[char_uuid] = cb

    async def unsubscribe(self, char_uuid) -> None:
        self.subscribed.pop(char_uuid, None)

    async def write_char(self, char_uuid, data) -> None:
        self.writes.append((char_uuid, bytes(data)))
        cb = self.subscribed.get(CHAR_SERVER_CFG)
        if char_uuid == CHAR_CLIENT_CFG and cb is not None:
            if len(data) == 2:  # version negotiation → chosen version
                cb(CHAR_SERVER_CFG, bytes([4]))
            else:  # channel config → max_packet, ch0_buf, ch1_buf
                cb(CHAR_SERVER_CFG, b"\x14\x00\x00\x01\x00\x01")
            return
        if char_uuid == CHAR_RX:
            await self._answer_rx(bytes(data))

    async def _answer_rx(self, data: bytes) -> None:
        """Ack the channel-open packet, then answer Initialize."""
        if data == b"\x40":
            ack = self.subscribed.get("e50b0002-af04-4564-92ad-fef019489de6")
            if ack is not None:
                ack("e50b0002-af04-4564-92ad-fef019489de6", b"\x40")
            return
        tx = self.subscribed.get("e50b0003-af04-4564-92ad-fef019489de6")
        if tx is not None:
            frame = b"\xfe\xff" + bytes([MSG_INITIALIZE_RESP, 0x00, 0x00])
            tx("e50b0003-af04-4564-92ad-fef019489de6", b"\x00" + frame)


async def _open_session() -> tuple[CondorProtocol, HandshakeTransport]:
    transport = HandshakeTransport()
    protocol = CondorProtocol(transport)
    await protocol.connect()
    assert protocol._connected
    return protocol, transport


async def test_second_connect_on_open_session_is_a_no_op() -> None:
    """Baseline: an open session is reused, not re-negotiated."""
    protocol, transport = await _open_session()
    before = len(transport.writes)

    await protocol.connect()

    assert len(transport.writes) == before


async def test_invalidated_session_runs_the_handshake_again() -> None:
    """After invalidation the next connect starts from the beginning."""
    protocol, transport = await _open_session()
    transport.subscribed.clear()
    transport.writes.clear()

    protocol.invalidate_session()
    assert not protocol._connected

    await protocol.connect()

    # The handshake ran in full: config channel subscribed before anything
    # was written, and the first write is the version announcement — not a
    # framed request on the data channel.
    assert CHAR_SERVER_CFG in transport.subscribed
    assert transport.writes[0] == (CHAR_CLIENT_CFG, b"\x03\x04")
    assert protocol._connected


async def test_invalidation_forgets_the_negotiated_session_state() -> None:
    """Sequence counter and packet size come back as fresh-session values."""
    protocol, _transport = await _open_session()
    protocol._next_data_seq = 17
    protocol._rx_buffer = bytearray(b"\x01\x02")
    protocol._subscribed_ports = [("1", "Battery")]

    protocol.invalidate_session()

    assert protocol._next_data_seq == 1
    assert protocol._rx_buffer == bytearray()
    assert protocol._subscribed_ports == []


class FailingBridgeTransport:
    """ESP-bridge stand-in whose live setup always fails."""

    is_connected = True
    disconnect_count = 0
    connection_path = None
    bridge_version = "1.14.0"
    needs_resubscribe = False

    def set_disconnect_callback(self, cb) -> None:
        self._cb = cb

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def set_notify_throttle(self, ms) -> None:
        return None


async def test_failed_setup_invalidates_the_session(hass) -> None:
    """The retry path drops the session before it waits for another link."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_TRANSPORT_TYPE: TRANSPORT_ESP_BRIDGE,
            CONF_ESP_DEVICE_NAME: "sonicare-bridge",
            CONF_SERVICES: [],
            "model": "HX7425",
        },
    )
    entry.add_to_hass(hass)
    coordinator = PhilipsSonicareCoordinator(hass, entry, FailingBridgeTransport())
    coordinator._use_condor = True
    coordinator._is_esp_bridge = True

    protocol, _transport = await _open_session()
    coordinator._protocol = protocol

    async def _boom() -> int:
        raise TimeoutError("Condor response timeout for message type 10")

    coordinator._setup_condor_session = _boom

    task = hass.async_create_task(coordinator._start_live_monitoring())
    try:
        for _ in range(50):
            await asyncio.sleep(0)
            if not protocol._connected:
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not protocol._connected
