"""Condor protocol implementation for newer Sonicare models.

`Condor` is the ASCII string these devices advertise in their
manufacturer-data (Philips Company ID 477). Confirmed on Sonicare
HX742X / Series 7100 running firmware 1.8.20.0.

Wire format (on top of GATT service ``e50ba3c0-…``):

- Version negotiation and channel-config exchange on ``…0006`` / ``…0007``
- Framed messages on ``…0001`` (RX, app→device) and ``…0003`` (TX,
  device→app), each fragment carrying a 1-byte transport header. Frame
  structure: ``FE FF`` + 1-byte type + 2-byte big-endian length + body.
- Properties are addressed as ``product_id/port_name`` (e.g. ``1/Battery``,
  ``1/RoutineStatus``). Bodies are UTF-8 JSON for named ports, raw bytes
  for ``*.b`` streaming ports.

Phase 2a implements the V4 handshake + frame transport; later phases add
the JSON-to-data-key adapter and live update routing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from typing import Any

from .condor_adapter import map_port_props
from .const import (
    CHAR_CLIENT_CFG,
    CHAR_PROTO_CFG,
    CHAR_RX,
    CHAR_RX_ACK,
    CHAR_SERVER_CFG,
    CHAR_TX,
    CHAR_TX_ACK,
    SVC_CONDOR,
)
from .exceptions import TransportError
from .protocol import SonicareProtocol, UpdateCallback
from .transport import SonicareTransport

_LOGGER = logging.getLogger(__name__)

# Message types on the framed layer
MSG_INITIALIZE_REQ = 1
MSG_INITIALIZE_RESP = 2
MSG_PUT_PROPS = 3
MSG_GET_PROPS = 4
MSG_SUBSCRIBE = 5
MSG_UNSUBSCRIBE = 6
MSG_GENERIC_RESP = 7
MSG_CHANGE_IND = 8
MSG_CHANGE_IND_RESP = 9
MSG_GET_PRODS = 10
MSG_GET_PORTS = 11

STATUS_OK = 0
STATUS_NAMES = {
    0: "NoError", 1: "NotUnderstood", 2: "OutOfMemory", 3: "NoSuchPort",
    4: "NotImplemented", 5: "VersionNotSupported", 6: "NoSuchProperty",
    7: "NoSuchOperation", 8: "NoSuchProduct", 9: "PropertyAlreadyExists",
    10: "NoSuchMethod", 11: "WrongParameters", 12: "InvalidParameter",
    13: "NotSubscribed", 14: "ProtocolViolation",
}

# Transport header bits (1-byte header before each BLE chunk)
_BIT_CHANNEL = 0x80  # 0 = data, 1 = binary
_BIT_START = 0x40    # set only on the channel-open handshake packet
_MASK_SEQ = 0x3F     # seq 0..63, wraps

# Subscribe timeout: ports stay subscribed for a year unless explicitly
# unsubscribed.
SUBSCRIBE_TIMEOUT_SECS = 31_536_000

# Default port subscription set. Confirmed live on HX742X 2026-04-23:
# Sonicare + RoutineStatus are chatty during sessions; Battery / BrushHead /
# SessionStorage fire only on change.
DEFAULT_SUBSCRIBE_PORTS: tuple[tuple[str, str], ...] = (
    ("1", "Sonicare"),
    ("1", "RoutineStatus"),
    ("1", "Battery"),
    ("1", "BrushHead"),
    ("1", "SessionStorage"),
)

# Safe default packet size before Phase-2 channel-config lands. BlueZ keeps
# ATT MTU at 23 unless negotiated, which leaves 20 bytes of payload.
_DEFAULT_PACKET_SIZE = 20
_HANDSHAKE_TIMEOUT = 5.0
_RESPONSE_TIMEOUT = 5.0


class CondorProtocol(SonicareProtocol):
    """Framed request/response + change-indication protocol."""

    def __init__(self, transport: SonicareTransport) -> None:
        super().__init__(transport)
        self._max_packet_size: int = _DEFAULT_PACKET_SIZE
        self._connected: bool = False
        self._live_callback: UpdateCallback | None = None

        # Handshake sync
        self._server_cfg_event = asyncio.Event()
        self._server_cfg_data = b""
        self._handshake_ack_event = asyncio.Event()

        # Frame reassembly + request/response demux
        self._rx_buffer = bytearray()
        self._response_event = asyncio.Event()
        self._response_data = b""

        # Outgoing data-channel sequence: 1..63 then wraps to 0 then back to
        # 1. Seq 0 with BIT_START is reserved for the channel-open packet.
        self._next_data_seq = 1

        # Serializes outbound RX writes so chunks of different frames never
        # interleave. Distinct from _response_lock, which serializes the
        # full request/response cycle (only one in flight at a time).
        self._send_lock = asyncio.Lock()
        self._response_lock = asyncio.Lock()

        # Ports currently under Subscribe. Tracked so stop_live_updates can
        # clean up symmetrically regardless of which set was requested.
        self._subscribed_ports: list[tuple[str, str]] = []

    # --- Session lifecycle -------------------------------------------------

    async def connect(self) -> None:
        """Run the Condor V4 handshake on top of an already-open transport.

        Phases: subscribe SERVER_CFG → version-negotiation → subscribe
        TX/RX_ACK → channel-config → channel-open → InitializeReq. Each
        phase is strictly ordered — HX742X FW 1.8.20.0 drops the link if
        TX is subscribed before version negotiation completes.
        """
        if self._connected:
            return

        await self._transport.subscribe(CHAR_SERVER_CFG, self._on_server_cfg)

        # Phase 1 — version negotiation. Announce [0x03, 0x04]; device
        # replies with 1 byte = chosen transport version.
        self._server_cfg_event.clear()
        await self._transport.write_char(CHAR_CLIENT_CFG, bytes([0x03, 0x04]))
        version_data = await self._await_server_cfg(1)
        chosen = version_data[0]
        if chosen != 4:
            raise TransportError(
                f"Condor transport v{chosen} is not supported (only v4 implemented)"
            )

        # Only now subscribe the data + ack channels — earlier subscriptions
        # cause FW 1.8.20.0 to disconnect.
        await self._transport.subscribe(CHAR_TX, self._on_tx)
        await self._transport.subscribe(CHAR_RX_ACK, self._on_rx_ack)

        # Phase 2 — channel config. Device replies with 6 bytes: 3× LE uint16
        # = (max_packet_size, ch0_buf, ch1_buf).
        self._server_cfg_event.clear()
        await self._transport.write_char(
            CHAR_CLIENT_CFG, bytes([0xFF, 0xFF, 0xFF, 0xFF])
        )
        cfg_data = await self._await_server_cfg(6)
        max_pkt, _ch0_buf, _ch1_buf = struct.unpack("<HHH", cfg_data)
        # Clamp to the safe BLE-MTU default; a real MTU exchange would let
        # us lift this, but BlueZ stays at 23 unless negotiated and we have
        # no cross-transport MTU accessor yet.
        self._max_packet_size = max(min(max_pkt, _DEFAULT_PACKET_SIZE), 4)

        # Phase 3 — open the data channel. 1-byte packet with BIT_START and
        # seq=0; device acks on RX_ACK. Until that ack lands, any framed
        # payload we send is discarded.
        self._handshake_ack_event.clear()
        await self._transport.write_char(CHAR_RX, bytes([_BIT_START]))
        try:
            await asyncio.wait_for(
                self._handshake_ack_event.wait(), _HANDSHAKE_TIMEOUT
            )
        except asyncio.TimeoutError as err:
            raise TransportError("No channel-open ACK from Condor device") from err

        # Phase 4 — drive the framed protocol. Empty Initialize marks the
        # session ready for GetProds / GetPorts / GetProps traffic.
        await self._send_and_wait(MSG_INITIALIZE_REQ)

        self._connected = True
        _LOGGER.debug(
            "Condor session open (max_packet=%d)", self._max_packet_size,
        )

    async def disconnect(self) -> None:
        """Tear down protocol-level subscriptions. The BLE link itself is
        the transport's concern — disconnect is idempotent."""
        self._connected = False
        self._live_callback = None
        for uuid in (CHAR_SERVER_CFG, CHAR_TX, CHAR_RX_ACK):
            try:
                await self._transport.unsubscribe(uuid)
            except Exception:  # noqa: BLE001
                pass
        self._rx_buffer = bytearray()
        self._next_data_seq = 1

    async def _await_server_cfg(self, expected_len: int) -> bytes:
        try:
            await asyncio.wait_for(
                self._server_cfg_event.wait(), _HANDSHAKE_TIMEOUT
            )
        except asyncio.TimeoutError as err:
            raise TransportError(
                f"No Condor SERVER_CFG response (expected {expected_len}B)"
            ) from err
        if len(self._server_cfg_data) != expected_len:
            raise TransportError(
                f"Condor SERVER_CFG length mismatch: "
                f"got {len(self._server_cfg_data)}B, expected {expected_len}B"
            )
        return self._server_cfg_data

    # --- Notification callbacks -------------------------------------------

    def _on_server_cfg(self, _uuid: str, data: bytes) -> None:
        self._server_cfg_data = bytes(data)
        self._server_cfg_event.set()

    def _on_rx_ack(self, _uuid: str, _data: bytes) -> None:
        # First RX_ACK after the channel-open write completes the handshake.
        # Subsequent ones ack our own outbound chunks; we don't gate on them.
        if not self._handshake_ack_event.is_set():
            self._handshake_ack_event.set()

    def _on_tx(self, _uuid: str, data: bytes) -> None:
        if len(data) < 1:
            return
        seq = data[0] & _MASK_SEQ
        payload = bytes(data[1:])

        # Every incoming data packet must be acked on TX_ACK — otherwise
        # the device's send window fills up and notifications stall.
        asyncio.get_running_loop().create_task(self._send_tx_ack(seq))

        # A single logical frame may land across several notifications;
        # reassemble until 5 + payload_len bytes are buffered.
        self._rx_buffer.extend(payload)
        buf = bytes(self._rx_buffer)
        if len(buf) < 5 or buf[0] != 0xFE or buf[1] != 0xFF:
            return
        payload_len = struct.unpack(">H", buf[3:5])[0]
        if len(buf) < 5 + payload_len:
            return
        msg_type = buf[2]
        body = buf[5:5 + payload_len]
        self._rx_buffer = bytearray(buf[5 + payload_len:])
        self._dispatch_frame(msg_type, body)

    def _dispatch_frame(self, msg_type: int, body: bytes) -> None:
        if msg_type == MSG_CHANGE_IND:
            # Unsolicited push notification — ack first so the device keeps
            # streaming, then route the decoded delta to the live callback.
            asyncio.get_running_loop().create_task(self._send_change_ind_ack())
            self._route_change_indication(body)
            return

        # Everything else is a response to the pending request. If no one
        # is waiting, drop it — spurious responses after a timeout are
        # harmless, and we don't want to leak state into the next cycle.
        self._response_data = body
        self._response_event.set()

    def _route_change_indication(self, body: bytes) -> None:
        """Parse a ChangeIndication payload and fan it out to the callback.

        Payload layout (device → app): ``<product>/<port>\\0<json_body>\\0``.
        Note the product/port separator is a slash, **not** a NUL — that
        differs from the Subscribe request payload, where both boundaries
        are NUL. Binary ``*.b`` streaming ports never go through the JSON
        adapter; they are noted at debug level and dropped for now — the
        pressure/temperature stream decode is a later phase.
        """
        if self._live_callback is None:
            return
        parts = body.split(b"\x00", 1)
        if len(parts) < 2 or b"/" not in parts[0]:
            _LOGGER.debug("Condor ChangeInd malformed (%dB): %s", len(body), body.hex())
            return
        try:
            header = parts[0].decode("ascii")
        except UnicodeDecodeError:
            _LOGGER.debug("Condor ChangeInd non-ascii header: %s", parts[0].hex())
            return
        prod, port = header.split("/", 1)
        raw_body = parts[1].rstrip(b"\x00")
        if port.endswith(".b"):
            _LOGGER.debug(
                "Condor ChangeInd binary port %s/%s (%dB) — skipped",
                prod, port, len(raw_body),
            )
            return
        if not raw_body:
            return
        try:
            props = json.loads(raw_body.decode("utf-8", errors="replace"))
        except ValueError:
            _LOGGER.debug(
                "Condor ChangeInd %s/%s body not JSON: %s", prod, port, raw_body.hex(),
            )
            return
        if not isinstance(props, dict):
            return
        delta = map_port_props(port, props)
        if not delta:
            return
        try:
            self._live_callback(delta)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Condor live callback failed: %s", err)

    async def _send_tx_ack(self, seq: int) -> None:
        try:
            await self._transport.write_char(
                CHAR_TX_ACK, bytes([seq & _MASK_SEQ])
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Condor TX_ACK write failed: %s", e)

    async def _send_change_ind_ack(self) -> None:
        try:
            await self._send_msg(MSG_CHANGE_IND_RESP, bytes([STATUS_OK]))
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Condor ChangeInd ACK send failed: %s", e)

    # --- Frame layer ------------------------------------------------------

    def _next_seq(self) -> int:
        seq = self._next_data_seq & _MASK_SEQ
        self._next_data_seq = (self._next_data_seq + 1) % 64
        # Skip seq 0 — reserved for the channel-open handshake.
        if self._next_data_seq == 0:
            self._next_data_seq = 1
        return seq

    async def _send_msg(self, msg_type: int, payload: bytes = b"") -> None:
        """Send one framed message, fragmented across RX writes with a
        1-byte header per chunk. Holds the send lock for the whole frame
        so chunks of different messages never interleave."""
        frame = (
            b"\xFE\xFF"
            + bytes([msg_type])
            + struct.pack(">H", len(payload))
            + payload
        )
        chunk_payload = max(self._max_packet_size - 1, 1)
        async with self._send_lock:
            offset = 0
            while offset < len(frame):
                chunk = frame[offset:offset + chunk_payload]
                hdr = self._next_seq()
                await self._transport.write_char(
                    CHAR_RX, bytes([hdr]) + chunk
                )
                offset += len(chunk)

    async def _send_and_wait(
        self,
        msg_type: int,
        payload: bytes = b"",
        timeout: float = _RESPONSE_TIMEOUT,
    ) -> bytes:
        """Send one request and wait for the matching response body.

        Only one request is in flight at any time — the response
        demultiplexer has a single slot and relies on FIFO ordering.
        ChangeIndications are handled out-of-band in ``_dispatch_frame``.
        """
        async with self._response_lock:
            self._response_event.clear()
            self._response_data = b""
            await self._send_msg(msg_type, payload)
            try:
                await asyncio.wait_for(
                    self._response_event.wait(), timeout
                )
            except asyncio.TimeoutError as err:
                raise TransportError(
                    f"Condor response timeout for message type {msg_type}"
                ) from err
            return self._response_data

    # --- Property discovery (framed layer primitives) ----------------------

    async def discover_products(self) -> list[str]:
        """List product IDs the device exposes.

        GetProds returns a JSON object keyed by product id (``"0"`` is
        firmware/OTA, ``"1"`` is the toothbrush functions on HX742X).
        """
        if not self._connected:
            raise TransportError("Condor session not established")
        resp = await self._send_and_wait(MSG_GET_PRODS)
        status, data = _parse_generic_resp(resp)
        if status != STATUS_OK:
            raise TransportError(
                f"GetProds failed: {_status_name(status)}"
            )
        return list(data.keys()) if isinstance(data, dict) else []

    async def discover_ports(self, product_id: str) -> list[str]:
        """List port names for a product.

        GetPorts returns a JSON array. Port names are case-sensitive on
        the device (e.g. ``Sonicare``, ``RoutineStatus``, ``SensorData.b``)
        — lower-casing them produces empty GetProps bodies, not errors.
        """
        if not self._connected:
            raise TransportError("Condor session not established")
        payload = product_id.encode("ascii") + b"\x00"
        resp = await self._send_and_wait(MSG_GET_PORTS, payload)
        status, data = _parse_generic_resp(resp)
        if status != STATUS_OK:
            raise TransportError(
                f"GetPorts({product_id}) failed: {_status_name(status)}"
            )
        if not isinstance(data, list):
            return []
        return [p for p in data if isinstance(p, str)]

    async def get_props(
        self, product_id: str, port: str, timeout: float = _RESPONSE_TIMEOUT,
    ) -> dict[str, Any]:
        """Read all properties of a single port.

        Returns the parsed JSON object from the device. NoSuchPort /
        NoSuchProduct status raises ``TransportError``; unknown-port
        names silently return an empty dict, which is how the device
        reports a lower-cased port name.
        """
        if not self._connected:
            raise TransportError("Condor session not established")
        payload = (
            product_id.encode("ascii") + b"\x00" + port.encode("ascii") + b"\x00"
        )
        resp = await self._send_and_wait(MSG_GET_PROPS, payload, timeout=timeout)
        status, data = _parse_generic_resp(resp)
        if status != STATUS_OK:
            raise TransportError(
                f"GetProps({product_id}/{port}) failed: {_status_name(status)}"
            )
        return data if isinstance(data, dict) else {}

    # --- SonicareProtocol surface ------------------------------------------

    async def refresh_all(self) -> dict[str, Any]:
        """Discover products + ports and read every JSON port's state.

        Iterates both the firmware product (``0``) and the device product
        (``1``), skipping binary streaming ports (``*.b``). Per-port
        failures are logged and skipped so one flaky port never blocks
        the rest of the refresh. The result is a flat dict keyed by
        ``coordinator.data`` names, already merged across ports.
        """
        merged: dict[str, Any] = {}
        products = await self.discover_products()
        for prod in products:
            try:
                ports = await self.discover_ports(prod)
            except TransportError as err:
                _LOGGER.debug("Condor discover_ports(%s) failed: %s", prod, err)
                continue
            for port in ports:
                if port.endswith(".b"):
                    # Binary streams are subscribe-only; refresh_all sticks
                    # to JSON ports that carry named properties.
                    continue
                try:
                    props = await self.get_props(prod, port)
                except TransportError as err:
                    _LOGGER.debug(
                        "Condor get_props(%s/%s) failed: %s", prod, port, err,
                    )
                    continue
                if not props:
                    continue
                merged.update(map_port_props(port, props))
        return merged


    async def start_live_updates(self, on_update: UpdateCallback) -> None:
        """Subscribe to push updates for the default port set.

        Empirically on HX742X only ``Sonicare`` + ``RoutineStatus`` are
        chatty during sessions; ``Battery`` / ``BrushHead`` /
        ``SessionStorage`` fire only on actual change. Per-port subscribe
        failures are logged and skipped — a partially subscribed device
        is still useful.
        """
        if not self._connected:
            raise TransportError("Condor session not established")
        self._live_callback = on_update
        subscribed: list[tuple[str, str]] = []
        for prod, port in DEFAULT_SUBSCRIBE_PORTS:
            try:
                ok = await self._subscribe_port(prod, port)
            except TransportError as err:
                _LOGGER.debug("Subscribe %s/%s raised: %s", prod, port, err)
                continue
            if ok:
                subscribed.append((prod, port))
            else:
                _LOGGER.info("Subscribe %s/%s declined by device", prod, port)
        self._subscribed_ports = subscribed
        _LOGGER.debug("Condor subscribed to %d port(s)", len(subscribed))

    async def stop_live_updates(self) -> None:
        """Unsubscribe every port that successfully subscribed.

        Failures are swallowed — a dropped link already tore the
        subscriptions down device-side, so a best-effort cleanup on our
        end is all that's needed.
        """
        self._live_callback = None
        ports = self._subscribed_ports
        self._subscribed_ports = []
        if not self._connected:
            return
        for prod, port in ports:
            try:
                await self._unsubscribe_port(prod, port)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Unsubscribe %s/%s failed: %s", prod, port, err)

    async def _subscribe_port(self, product_id: str, port: str) -> bool:
        """Send one MSG_SUBSCRIBE. Returns True on ``NoError`` status.

        Payload format (NUL between product/port — asymmetric with the
        ChangeIndication payload the device sends back, which uses a
        slash): ``product\\0port\\0{"timeout":N}``.
        """
        body = json.dumps({"timeout": SUBSCRIBE_TIMEOUT_SECS}).encode("utf-8")
        payload = (
            product_id.encode("ascii") + b"\x00"
            + port.encode("ascii") + b"\x00"
            + body
        )
        resp = await self._send_and_wait(MSG_SUBSCRIBE, payload)
        if not resp:
            return False
        return resp[0] == STATUS_OK

    async def _unsubscribe_port(self, product_id: str, port: str) -> None:
        payload = (
            product_id.encode("ascii") + b"\x00"
            + port.encode("ascii") + b"\x00"
            + b"{}"
        )
        await self._send_and_wait(MSG_UNSUBSCRIBE, payload, timeout=3.0)

    async def set_brushing_mode(self, mode_key: str) -> None:
        raise NotImplementedError("Condor PutProps — implemented in Phase 4")

    async def set_intensity(self, intensity_key: str) -> None:
        raise NotImplementedError("Condor PutProps — implemented in Phase 4")


# --- Response-body parsing ------------------------------------------------


def _status_name(status: int) -> str:
    return f"{STATUS_NAMES.get(status, 'Unknown')}({status})"


def _parse_generic_resp(body: bytes) -> tuple[int, Any]:
    """Decode a GenericResponse body into (status, parsed_payload).

    Wire format is ``<status_byte> <utf8_json_body> 0x00``. The trailing
    NUL terminator is emitted by the OEM firmware regardless of body
    content. When the body isn't valid JSON (empty-port answers,
    unexpected framing), ``parsed_payload`` is ``None``.
    """
    if not body:
        return 255, None
    status = body[0]
    tail = body[1:].rstrip(b"\x00")
    if not tail:
        return status, None
    try:
        return status, json.loads(tail.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return status, None
