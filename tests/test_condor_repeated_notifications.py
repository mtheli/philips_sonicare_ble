"""Notifications the host delivers more than once (issue #45).

On some hosts every notification reached the program ~60 times within
microseconds although the brush sent it once. Each copy was acked on
TX_ACK and appended to the frame buffer again; the brush answered the ack
flood by disconnecting right after the session opened.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from custom_components.philips_sonicare_ble import condor_link as cp
from custom_components.philips_sonicare_ble.const import (
    CHAR_SERVER_CFG,
    CHAR_TX,
    CHAR_TX_ACK,
)

# One complete frame: FE FF <type> <len16> <body>
FRAME = bytes([0xFE, 0xFF, 0x05, 0x00, 0x02, 0xAA, 0xBB])


class AckTransport:
    auto_tx_ack = False

    def __init__(self) -> None:
        self.acks: list[bytes] = []

    async def write_char(self, char_uuid, data) -> None:
        assert char_uuid == CHAR_TX_ACK
        self.acks.append(bytes(data))


@pytest.fixture
def link(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cp, "_monotonic", lambda: clock[0])
    proto = cp.CondorBleLink(AckTransport())
    frames = []
    proto._dispatch_frame = lambda msg_type, body: frames.append((msg_type, body))
    proto.clock = clock
    proto.frames = frames
    return proto


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_copies_are_acked_and_reassembled_once(link, caplog) -> None:
    packet = bytes([0x01]) + FRAME

    with caplog.at_level(logging.INFO, logger=cp.__name__):
        for _ in range(60):
            link._on_tx(CHAR_TX, packet)
        await _settle()

    assert link._transport.acks == [b"\x01"]
    assert link.frames == [(0x05, b"\xaa\xbb")]
    assert link._copies_dropped == 59
    assert caplog.text.count("delivered again") == 1


async def test_next_packet_is_not_a_copy(link) -> None:
    # Same payload, next sequence number: a new packet.
    link._on_tx(CHAR_TX, bytes([0x01]) + FRAME)
    link._on_tx(CHAR_TX, bytes([0x02]) + FRAME)
    await _settle()

    assert link._transport.acks == [b"\x01", b"\x02"]
    assert len(link.frames) == 2


async def test_late_resend_is_acked_again_but_not_reassembled(
    link, caplog
) -> None:
    packet = bytes([0x07]) + FRAME
    with caplog.at_level(logging.INFO, logger=cp.__name__):
        link._on_tx(CHAR_TX, packet)
        link.clock[0] += cp._COPY_WINDOW + 0.2
        link._on_tx(CHAR_TX, packet)
        await _settle()

    assert link._transport.acks == [b"\x07", b"\x07"]
    assert len(link.frames) == 1
    # A resend is the brush, not the host — no host-copy claim in the log.
    assert link._resends == 1
    assert link._copies_dropped == 0
    assert "delivered again" not in caplog.text


async def test_bridge_acks_itself(link) -> None:
    link._transport.auto_tx_ack = True
    packet = bytes([0x07]) + FRAME
    link._on_tx(CHAR_TX, packet)
    link.clock[0] += 1.0
    link._on_tx(CHAR_TX, packet)
    await _settle()

    assert link._transport.acks == []
    assert len(link.frames) == 1


async def test_new_session_forgets_the_last_packet(link) -> None:
    packet = bytes([0x01]) + FRAME
    link._on_tx(CHAR_TX, packet)
    link.invalidate()
    link._on_tx(CHAR_TX, packet)
    await _settle()

    assert link._transport.acks == [b"\x01", b"\x01"]
    assert len(link.frames) == 2


def test_server_cfg_copy_does_not_answer_the_next_request(link) -> None:
    link._on_server_cfg(CHAR_SERVER_CFG, b"\x04")
    link._server_cfg_event.clear()  # channel-config request goes out

    link._on_server_cfg(CHAR_SERVER_CFG, b"\x04")  # late copy
    assert not link._server_cfg_event.is_set()

    link._on_server_cfg(CHAR_SERVER_CFG, b"\x14\x00\x00\x01\x00\x01")
    assert link._server_cfg_data == b"\x14\x00\x00\x01\x00\x01"
