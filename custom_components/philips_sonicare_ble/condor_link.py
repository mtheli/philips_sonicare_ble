"""Link layer for the Condor protocol.

The Condor protocol has two halves:

- **Link:** everything needed to exchange whole messages with the brush —
  over BLE that is the V4 handshake, the 1-byte transport header with its
  sequence numbers, fragmenting and reassembling frames, and acking every
  incoming packet within about 250 ms.
- **Messages:** what those messages mean — products, ports, properties and
  change indications, and how they map onto the integration's data keys.
  That half lives in ``condor_protocol.CondorProtocol``.

``CondorLink`` is the boundary between the two. Its operations work on
parsed port data (``product``, ``port``, JSON properties) and return the
device status with the parsed answer, so a link does not have to speak the
Condor wire format at all: a gateway that already holds the BLE connection
can offer the same operations over another channel. ``CondorBleLink`` is the
implementation on top of a ``SonicareTransport`` (direct BlueZ or ESP
bridge).

Wire format of ``CondorBleLink`` (on top of GATT service ``e50ba3c0-…``):

- Version negotiation and channel-config exchange on ``…0006`` / ``…0007``
- Framed messages on ``…0001`` (RX, app→device) and ``…0003`` (TX,
  device→app), each fragment carrying a 1-byte transport header. Frame
  structure: ``FE FF`` + 1-byte type + 2-byte big-endian length + body.
- Properties are addressed as ``product_id/port_name`` (e.g. ``1/Battery``,
  ``1/RoutineStatus``). Bodies are UTF-8 JSON for named ports, raw bytes
  for ``*.b`` streaming ports.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import struct
import time
from collections.abc import Callable, Iterable
from typing import Any

from .const import (
    CHAR_CLIENT_CFG,
    CHAR_RX,
    CHAR_RX_ACK,
    CHAR_SERVER_CFG,
    CHAR_TX,
    CHAR_TX_ACK,
)
from .exceptions import TransportError
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
# Returned when the device answered with an empty body — no status byte.
STATUS_NO_RESPONSE = 255
STATUS_NAMES = {
    0: "NoError", 1: "NotUnderstood", 2: "OutOfMemory", 3: "NoSuchPort",
    4: "NotImplemented", 5: "VersionNotSupported", 6: "NoSuchProperty",
    7: "NoSuchOperation", 8: "NoSuchProduct", 9: "PropertyAlreadyExists",
    10: "NoSuchMethod", 11: "WrongParameters", 12: "InvalidParameter",
    13: "NotSubscribed", 14: "ProtocolViolation",
}

# Subscribe timeout: ports stay subscribed for a year unless explicitly
# unsubscribed.
SUBSCRIBE_TIMEOUT_SECS = 31_536_000

RESPONSE_TIMEOUT = 5.0
UNSUBSCRIBE_TIMEOUT = 3.0

# A change indication: product id, port name, and the port's properties —
# a dict for JSON ports, the raw bytes for binary ``*.b`` ports.
ChangeCallback = Callable[[str, str, "dict[str, Any] | bytes"], None]

# Transport header bits (1-byte header before each BLE chunk)
_BIT_CHANNEL = 0x80  # 0 = data, 1 = binary
_BIT_START = 0x40    # set only on the channel-open handshake packet
_MASK_SEQ = 0x3F     # seq 0..63, wraps

# Safe default packet size before Phase-2 channel-config lands. BlueZ keeps
# ATT MTU at 23 unless negotiated, which leaves 20 bytes of payload.
_DEFAULT_PACKET_SIZE = 20
_HANDSHAKE_TIMEOUT = 5.0
# Some hosts hand every notification to the program many times although the
# brush sent it once (issue #45: ~60 copies within microseconds). A copy is
# the same bytes on the same characteristic as the delivery before it; a
# new TX packet cannot look like that because its sequence number moves on.
# A copy later than this is taken as the brush sending the packet again
# (its ack went missing) and acked once more — but never reassembled twice.
_COPY_WINDOW = 0.1
_monotonic = time.monotonic


def status_name(status: int) -> str:
    return f"{STATUS_NAMES.get(status, 'Unknown')}({status})"


class CondorLink(abc.ABC):
    """Exchanges Condor messages with one device.

    Requests are serialized: at most one is in flight at a time. Change
    indications arrive out of band through the handler set with
    ``set_change_handler``; acknowledging them is the link's job.

    Errors: a request that gets no answer in time, or a link that fails,
    raises ``TransportError``. A device that answers with an error status is
    not an exception — the status is returned and the message layer decides.
    """

    def __init__(self) -> None:
        self._change_handler: ChangeCallback | None = None

    def set_change_handler(self, handler: ChangeCallback | None) -> None:
        """Receive every change indication as ``(product, port, props)``."""
        self._change_handler = handler

    @property
    @abc.abstractmethod
    def is_open(self) -> bool:
        """Whether a session is established and requests can be sent."""

    @property
    @abc.abstractmethod
    def link_up(self) -> bool:
        """Whether the underlying connection to the device still exists.

        Can be False while ``is_open`` is still True, for the short time
        between a dropped connection and the owner invalidating the session.
        """

    @abc.abstractmethod
    async def open(self) -> None:
        """Establish the session. A no-op when it is already open."""

    @abc.abstractmethod
    def invalidate(self) -> None:
        """Forget the session synchronously, without touching the device."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Invalidate the session and release what the link holds."""

    @abc.abstractmethod
    async def get_prods(self) -> tuple[int, Any]:
        """List the products: ``{"<id>": {...}, ...}``."""

    @abc.abstractmethod
    async def get_ports(self, product: str) -> tuple[int, Any]:
        """List the port names of one product: ``["Battery", ...]``."""

    @abc.abstractmethod
    async def get_props(
        self, product: str, port: str, timeout: float = RESPONSE_TIMEOUT,
    ) -> tuple[int, Any]:
        """Read all properties of one port."""

    @abc.abstractmethod
    async def put_props(
        self, product: str, port: str, props: dict[str, Any],
    ) -> tuple[int, Any]:
        """Write properties of one port."""

    @abc.abstractmethod
    async def subscribe(self, product: str, port: str) -> tuple[int, Any]:
        """Ask the device to send change indications for one port."""

    @abc.abstractmethod
    async def unsubscribe(self, product: str, port: str) -> tuple[int, Any]:
        """Stop change indications for one port."""

    # --- Session-level operations -----------------------------------------
    # What the message layer needs once per session or per brushing session.
    # The defaults run them as single port operations, the way they go over
    # BLE; a link whose other end does them by itself (an MQTT gateway)
    # overrides them.

    async def read_all_ports(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Read every JSON port of every product.

        Returns ``(product, port, props)`` in device order. Binary ``*.b``
        ports are skipped, so are ports that fail or come back empty — one
        flaky port never blocks the rest. A failing product list raises.
        """
        status, data = await self.get_prods()
        if status != STATUS_OK:
            raise TransportError(f"GetProds failed: {status_name(status)}")
        products = list(data.keys()) if isinstance(data, dict) else []
        result: list[tuple[str, str, dict[str, Any]]] = []
        for prod in products:
            try:
                status, data = await self.get_ports(prod)
                if status != STATUS_OK:
                    raise TransportError(
                        f"GetPorts({prod}) failed: {status_name(status)}"
                    )
            except TransportError as err:
                _LOGGER.debug("Condor discover_ports(%s) failed: %s", prod, err)
                continue
            ports = [p for p in data if isinstance(p, str)] if isinstance(data, list) else []
            for port in ports:
                if port.endswith(".b"):
                    # Binary streams are subscribe-only.
                    continue
                try:
                    status, props = await self.get_props(prod, port)
                    if status != STATUS_OK:
                        raise TransportError(
                            f"GetProps({prod}/{port}) failed: {status_name(status)}"
                        )
                except TransportError as err:
                    _LOGGER.debug(
                        "Condor get_props(%s/%s) failed: %s", prod, port, err,
                    )
                    continue
                if isinstance(props, dict) and props:
                    result.append((prod, port, props))
        return result

    async def subscribe_ports(
        self, ports: Iterable[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """Subscribe each port; returns the ones the device accepted.

        Per-port failures are logged and skipped — a partially subscribed
        device is still useful.
        """
        subscribed: list[tuple[str, str]] = []
        for prod, port in ports:
            try:
                status, _ = await self.subscribe(prod, port)
            except TransportError as err:
                _LOGGER.debug("Subscribe %s/%s raised: %s", prod, port, err)
                continue
            if status == STATUS_OK:
                subscribed.append((prod, port))
            else:
                _LOGGER.info("Subscribe %s/%s declined by device", prod, port)
        return subscribed

    async def unsubscribe_ports(self, ports: Iterable[tuple[str, str]]) -> None:
        """Best-effort unsubscribe of each port."""
        for prod, port in ports:
            try:
                await self.unsubscribe(prod, port)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Unsubscribe %s/%s failed: %s", prod, port, err)

    async def start_sensor_stream(self, enable_mask: int) -> bool:
        """Subscribe ``SensorData.b``, then set the enable register.

        Subscribe first so no frame is emitted before we listen. The enable
        write is best-effort (some firmwares stream regardless of a stale
        register), a failed subscribe means no telemetry: returns whether
        the subscribe succeeded.
        """
        try:
            status, _ = await self.subscribe("1", "SensorData.b")
        except TransportError as err:
            _LOGGER.warning("Condor SensorData.b subscribe failed: %s", err)
            return False
        try:
            put_status, _ = await self.put_props(
                "1", "SensorData", {"Sensors": enable_mask},
            )
            if put_status != STATUS_OK:
                _LOGGER.debug(
                    "Condor PutProps(1/SensorData) status=%s",
                    status_name(put_status),
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Condor sensor enable write failed: %s", err)
        return status == STATUS_OK

    async def stop_sensor_stream(self) -> None:
        """Unsubscribe ``SensorData.b``, best-effort. The enable register is
        left as-is; the handle powers its sensor frontend down on its own when
        the session ends."""
        try:
            await self.unsubscribe("1", "SensorData.b")
        except Exception:  # noqa: BLE001
            pass


class CondorBleLink(CondorLink):
    """Condor link over GATT: V4 handshake, framing, acks."""

    def __init__(self, transport: SonicareTransport) -> None:
        super().__init__()
        self._transport = transport
        self._max_packet_size: int = _DEFAULT_PACKET_SIZE
        self._connected: bool = False

        # Handshake sync
        self._server_cfg_event = asyncio.Event()
        self._server_cfg_data = b""
        self._handshake_ack_event = asyncio.Event()

        # Frame reassembly + request/response demux
        self._rx_buffer = bytearray()
        self._response_event = asyncio.Event()
        self._response_data = b""

        # Outgoing data-channel sequence: starts at 1 after the channel-open
        # START frame (which alone carries BIT_START + seq 0), then advances
        # mod-64. See _next_seq for the wrap invariant.
        self._next_data_seq = 1

        # Serializes outbound RX writes so chunks of different frames never
        # interleave. Distinct from _response_lock, which serializes the
        # full request/response cycle (only one in flight at a time).
        self._send_lock = asyncio.Lock()
        self._response_lock = asyncio.Lock()

        # Last delivery per characteristic: (bytes, monotonic time), and
        # how many copies of earlier deliveries were dropped this session.
        self._last_delivery: dict[str, tuple[bytes, float]] = {}
        self._copies_dropped = 0
        self._resends = 0

    @property
    def is_open(self) -> bool:
        return self._connected

    @property
    def link_up(self) -> bool:
        return self._transport.is_connected

    # --- Session lifecycle -------------------------------------------------

    async def open(self) -> None:
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

    def invalidate(self) -> None:
        """Forget the open session without touching the link.

        The handshake state lives here, the notification registrations live
        in the transport. When a link drops, the transport drops its half on
        its own — this drops ours, synchronously, so ``open()`` cannot
        short-circuit on a session that ended with the previous link. Safe to
        call at any time, including when no session was ever open.
        """
        self._connected = False
        self._max_packet_size = _DEFAULT_PACKET_SIZE
        self._rx_buffer = bytearray()
        self._next_data_seq = 1
        self._server_cfg_data = b""
        self._server_cfg_event.clear()
        self._handshake_ack_event.clear()
        if self._copies_dropped or self._resends:
            _LOGGER.debug(
                "Condor: dropped %d host copies and %d resent packets of "
                "notifications this session",
                self._copies_dropped, self._resends,
            )
        self._last_delivery = {}
        self._copies_dropped = 0
        self._resends = 0

    async def close(self) -> None:
        """Tear down the notification subscriptions. The BLE link itself is
        the transport's concern — close is idempotent."""
        self.invalidate()
        for uuid in (CHAR_SERVER_CFG, CHAR_TX, CHAR_RX_ACK):
            try:
                await self._transport.unsubscribe(uuid)
            except Exception:  # noqa: BLE001
                pass

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

    # --- Port operations ---------------------------------------------------

    async def get_prods(self) -> tuple[int, Any]:
        return _parse_generic_resp(await self._send_and_wait(MSG_GET_PRODS))

    async def get_ports(self, product: str) -> tuple[int, Any]:
        payload = product.encode("ascii") + b"\x00"
        return _parse_generic_resp(
            await self._send_and_wait(MSG_GET_PORTS, payload)
        )

    async def get_props(
        self, product: str, port: str, timeout: float = RESPONSE_TIMEOUT,
    ) -> tuple[int, Any]:
        resp = await self._send_and_wait(
            MSG_GET_PROPS, _port_address(product, port), timeout=timeout,
        )
        return _parse_generic_resp(resp)

    async def put_props(
        self, product: str, port: str, props: dict[str, Any],
    ) -> tuple[int, Any]:
        """Payload mirrors GetProps with a compact JSON body appended:
        ``product\\0port\\0{json}``."""
        body = json.dumps(props, separators=(",", ":")).encode("utf-8")
        resp = await self._send_and_wait(
            MSG_PUT_PROPS, _port_address(product, port) + body,
        )
        return _parse_generic_resp(resp)

    async def subscribe(self, product: str, port: str) -> tuple[int, Any]:
        """Payload format (NUL between product/port — asymmetric with the
        ChangeIndication payload the device sends back, which uses a
        slash): ``product\\0port\\0{"timeout":N}``."""
        body = json.dumps({"timeout": SUBSCRIBE_TIMEOUT_SECS}).encode("utf-8")
        resp = await self._send_and_wait(
            MSG_SUBSCRIBE, _port_address(product, port) + body,
        )
        return _parse_generic_resp(resp)

    async def unsubscribe(self, product: str, port: str) -> tuple[int, Any]:
        resp = await self._send_and_wait(
            MSG_UNSUBSCRIBE, _port_address(product, port) + b"{}",
            timeout=UNSUBSCRIBE_TIMEOUT,
        )
        return _parse_generic_resp(resp)

    # --- Notification callbacks -------------------------------------------

    def _repeat_age(self, uuid: str, data: bytes) -> float | None:
        """Seconds since the identical previous delivery, None if new.

        Records this delivery either way.
        """
        now = _monotonic()
        last = self._last_delivery.get(uuid)
        self._last_delivery[uuid] = (bytes(data), now)
        if last is None or last[0] != data:
            return None
        age = now - last[1]
        if age >= _COPY_WINDOW:
            # The brush sent it again, most likely because an ack was lost.
            self._resends += 1
            _LOGGER.debug(
                "Condor: packet resent after %.0f ms on %s", age * 1000, uuid
            )
            return age
        self._copies_dropped += 1
        if self._copies_dropped == 1:
            _LOGGER.info(
                "Condor: notification delivered again after %.3f ms on %s — "
                "the host repeats notifications; dropping the copies",
                age * 1000, uuid,
            )
        return age

    def _on_server_cfg(self, uuid: str, data: bytes) -> None:
        # A late copy of the 1-byte version reply must not answer the
        # 6-byte channel-config request that follows it.
        if self._repeat_age(uuid, data) is not None:
            return
        self._server_cfg_data = bytes(data)
        self._server_cfg_event.set()

    def _on_rx_ack(self, _uuid: str, _data: bytes) -> None:
        # First RX_ACK after the channel-open write completes the handshake.
        # Subsequent ones ack our own outbound chunks; we don't gate on them.
        if not self._handshake_ack_event.is_set():
            self._handshake_ack_event.set()

    def _on_tx(self, uuid: str, data: bytes) -> None:
        if len(data) < 1:
            return
        seq = data[0] & _MASK_SEQ
        payload = bytes(data[1:])

        age = self._repeat_age(uuid, data)
        if age is not None:
            # Already reassembled. Ack only a resend — acking every copy
            # floods TX_ACK and the brush disconnects.
            if age >= _COPY_WINDOW and not self._transport.auto_tx_ack:
                asyncio.get_running_loop().create_task(self._send_tx_ack(seq))
            return

        # Every incoming data packet must be acked on TX_ACK — otherwise
        # the device's send window fills up and notifications stall, and
        # if we're slow enough (>250 ms) the brush tears the link down with
        # reason=0x13 mid-session. ESP bridges v1.6.0+ echo the ack on the
        # BLE thread already, so skip the HA-loop round-trip there.
        if not self._transport.auto_tx_ack:
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
            # streaming, then route the decoded delta to the handler.
            asyncio.get_running_loop().create_task(self._send_change_ind_ack())
            self._route_change_indication(body)
            return

        # Everything else is a response to the pending request. If no one
        # is waiting, drop it — spurious responses after a timeout are
        # harmless, and we don't want to leak state into the next cycle.
        self._response_data = body
        self._response_event.set()

    def _route_change_indication(self, body: bytes) -> None:
        """Parse a ChangeIndication payload and hand it to the handler.

        Payload layout (device → app): ``<product>/<port>\\0<json_body>\\0``.
        Note the product/port separator is a slash, **not** a NUL — that
        differs from the Subscribe request payload, where both boundaries
        are NUL. Binary ``*.b`` ports are passed on as raw bytes.
        """
        if self._change_handler is None:
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
        if port.endswith(".b"):
            # The body runs from the byte after the header NUL to the exact
            # end of the frame — the frame's own length field delimits it, so
            # there is no trailing terminator to strip (stripping one would
            # corrupt a frame ending in 0x00).
            self._change_handler(prod, port, parts[1])
            return
        raw_body = parts[1].rstrip(b"\x00")
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
        self._change_handler(prod, port, props)

    async def _send_tx_ack(self, seq: int) -> None:
        try:
            await self._transport.write_char(
                CHAR_TX_ACK, bytes([seq & _MASK_SEQ])
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Condor TX_ACK write failed: %s", e)

    async def _send_change_ind_ack(self) -> None:
        # The ChangeIndicationResponse payload is two bytes: a status byte
        # (NoError) followed by a reserved zero. The handle tolerates a
        # single status byte, but matching the exact two-byte payload keeps
        # us spec-conformant.
        try:
            await self._send_msg(MSG_CHANGE_IND_RESP, bytes([STATUS_OK, 0]))
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Condor ChangeInd ACK send failed: %s", e)

    # --- Frame layer ------------------------------------------------------

    def _next_seq(self) -> int:
        # Pure mod-64: the brush enforces strict monotonic seq matching
        # and tears the link down on a gap. Seq 0 is reused after the
        # wrap; the channel-open frame is distinguished by BIT_START in
        # the high bits, not by reserving seq 0.
        seq = self._next_data_seq & _MASK_SEQ
        self._next_data_seq = (self._next_data_seq + 1) % 64
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
        timeout: float = RESPONSE_TIMEOUT,
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


# --- Payload helpers -------------------------------------------------------


def _port_address(product: str, port: str) -> bytes:
    return product.encode("ascii") + b"\x00" + port.encode("ascii") + b"\x00"


def _parse_generic_resp(body: bytes) -> tuple[int, Any]:
    """Decode a GenericResponse body into (status, parsed_payload).

    Wire format is ``<status_byte> <utf8_json_body> 0x00``. The trailing
    NUL terminator is emitted by the handle's firmware regardless of body
    content. When the body isn't valid JSON (empty-port answers,
    unexpected framing), ``parsed_payload`` is ``None``.
    """
    if not body:
        return STATUS_NO_RESPONSE, None
    status = body[0]
    tail = body[1:].rstrip(b"\x00")
    if not tail:
        return status, None
    try:
        return status, json.loads(tail.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return status, None
