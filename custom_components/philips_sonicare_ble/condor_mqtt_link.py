"""Condor link through an MQTT gateway (see docs/MQTT_GATEWAY.md).

A gateway near the brush holds the BLE connection and runs the session on
its own: handshake, framing and acks, reading every port, subscribing the
default ports. It publishes

- ``<base>/status``  online / offline (last will), retained
- ``<base>/link``    brush connected, session number, snapshot complete,
                     retained
- ``<base>/state/<product>/<port>``  full properties of a port, tagged with
                     the session, retained and updated on every change
- ``<base>/ind/<product>/<port>``    each change indication as it arrives

Home Assistant mostly listens. Requests on ``<base>/cmd`` (answered on
``<base>/resp``) remain for what depends on Home Assistant's own state:
the sensor stream and, later, writes.

The MQTT connection itself is abstracted as ``MqttClient`` so the link does
not depend on a particular client; inside Home Assistant it is backed by
the MQTT integration.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

from .condor_link import STATUS_NO_RESPONSE, STATUS_OK, CondorLink
from .exceptions import TransportError

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
TOPIC_PREFIX = "sonicare_gateway"
# How long to wait for an answer. The gateway itself gives up on the brush
# after 5 s and answers with "timeout"; this leaves room for the broker
# round trip on top.
REQUEST_TIMEOUT = 10.0
# How long the gateway may take to read every port after a session starts.
SNAPSHOT_TIMEOUT = 30.0
# After subscribing, retained status/link messages arrive almost at once.
_RETAINED_WAIT = 2.0
# Change indications held while nobody listens yet (between session start
# and the end of setup). Bounded, a brushing session sends about one per
# second.
_MAX_HELD_INDICATIONS = 500

MessageCallback = Callable[[str, str], None]


class MqttClient(Protocol):
    """The two MQTT operations the link needs."""

    async def publish(
        self, topic: str, payload: str, qos: int = 1, retain: bool = False,
    ) -> None: ...

    async def subscribe(
        self, topic: str, callback: MessageCallback, qos: int = 1,
    ) -> Callable[[], Awaitable[None] | None]:
        """Subscribe; returns a function that unsubscribes again."""


def base_topic(address: str) -> str:
    """``sonicare_gateway/<address without colons, lower case>``."""
    return f"{TOPIC_PREFIX}/{address.replace(':', '').lower()}"


def _port_key(topic_rest: str) -> tuple[str, str] | None:
    product, _, port = topic_rest.partition("/")
    return (product, port) if product and port else None


class CondorMqttLink(CondorLink):
    """Condor session that an MQTT gateway runs; this side listens."""

    def __init__(
        self,
        client: MqttClient,
        base: str,
        request_timeout: float = REQUEST_TIMEOUT,
        snapshot_timeout: float = SNAPSHOT_TIMEOUT,
    ) -> None:
        super().__init__()
        self._client = client
        self._base = base.rstrip("/")
        self._request_timeout = request_timeout
        self._snapshot_timeout = snapshot_timeout
        # Answers on the shared resp topic are told apart by this.
        self._client_id = f"ha-{uuid.uuid4().hex[:8]}"

        self._unsubscribers: list[Callable[[], Awaitable[None] | None]] = []
        self._online = False
        self._connected = False
        self._session: int | None = None
        self._snapshot = False
        self._snapshot_ports: list[tuple[str, str]] = []
        self._sensor_mask = 0
        self._opened_session: int | None = None
        self._state_event = asyncio.Event()

        # Retained port state: (product, port) -> (session, props).
        self._state: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}
        # Indications of the current session that arrived while no handler
        # was set, replayed in order once one is.
        self._held: list[tuple[str, str, dict[str, Any] | bytes]] = []

        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._request_lock = asyncio.Lock()

        # Called with no arguments whenever ``link_up`` or the session
        # changes — gateway online/offline, brush connected/disconnected, new
        # session. The owner (the transport) turns this into its connection
        # events.
        self.on_state_change: Callable[[], None] | None = None
        self._info: dict[str, Any] = {}

    # --- State -----------------------------------------------------------

    @property
    def link_up(self) -> bool:
        return self._online and self._connected

    @property
    def is_open(self) -> bool:
        return (
            self._opened_session is not None
            and self.link_up
            and self._session == self._opened_session
        )

    @property
    def session(self) -> int | None:
        return self._session

    @property
    def gateway_online(self) -> bool:
        return self._online

    @property
    def sensor_mask(self) -> int:
        return self._sensor_mask

    @property
    def gateway_info(self) -> dict[str, Any]:
        """The gateway's last ``<base>/info`` (empty until it arrived)."""
        return self._info

    def set_change_handler(self, handler) -> None:
        super().set_change_handler(handler)
        if handler is None:
            return
        held, self._held = self._held, []
        for product, port, props in held:
            if self._change_handler is None:
                break
            self._change_handler(product, port, props)

    # --- Lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to the gateway's topics. Idempotent."""
        if self._unsubscribers:
            return
        subs = (
            (f"{self._base}/status", self._on_status),
            (f"{self._base}/info", self._on_info),
            (f"{self._base}/link", self._on_link),
            (f"{self._base}/state/#", self._on_state),
            (f"{self._base}/resp", self._on_resp),
            # QoS 1: the delivered QoS is the lower of publish and subscribe;
            # binary ports published at 0 stay at 0.
            (f"{self._base}/ind/#", self._on_ind),
        )
        for topic, cb in subs:
            self._unsubscribers.append(await self._client.subscribe(topic, cb, 1))

    async def open(self) -> None:
        """Take over the gateway's current session.

        The gateway runs the BLE handshake itself; opening only means the
        brush is connected through it.
        """
        await self.start()
        if self.is_open:
            return
        if not self.link_up:
            try:
                await asyncio.wait_for(
                    self._wait_for(lambda: self.link_up), _RETAINED_WAIT
                )
            except asyncio.TimeoutError as err:
                raise TransportError(
                    "MQTT gateway: "
                    + ("brush not connected" if self._online else "gateway offline")
                ) from err
        self._opened_session = self._session
        _LOGGER.debug("Condor MQTT session %s open on %s", self._session, self._base)

    async def _wait_for(self, condition: Callable[[], bool]) -> None:
        while not condition():
            self._state_event.clear()
            await self._state_event.wait()

    def invalidate(self) -> None:
        self._opened_session = None

    async def close(self) -> None:
        """End the session. The topic subscriptions stay: they belong to the
        connection to the gateway, which outlives a session (see ``stop``)."""
        self.invalidate()
        self._fail_pending("session closed")

    async def stop(self) -> None:
        """Close and drop the topic subscriptions."""
        await self.close()
        self._online = False
        self._connected = False
        self._session = None
        self._snapshot = False
        self._held = []
        unsubscribers, self._unsubscribers = self._unsubscribers, []
        for unsub in unsubscribers:
            try:
                result = unsub()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                pass

    # --- Session-level operations (the gateway does the work) -----------

    async def read_all_ports(self) -> list[tuple[str, str, dict[str, Any]]]:
        """The gateway's snapshot of the current session.

        Waits until the gateway reports it complete, then returns the
        retained state of every port it lists — as it stands now, with the
        changes since the read already applied by the gateway.
        """
        session = self._opened_session
        if session is None:
            raise TransportError("MQTT gateway: no session")

        def complete() -> bool:
            return (not self.is_open) or (
                self._snapshot and self._session == session
            )

        try:
            await asyncio.wait_for(self._wait_for(complete), self._snapshot_timeout)
        except asyncio.TimeoutError as err:
            raise TransportError(
                f"MQTT gateway: no snapshot within {self._snapshot_timeout:.0f} s"
            ) from err
        if not self.is_open:
            raise TransportError("MQTT gateway: brush disconnected")
        result = []
        for key in self._snapshot_ports:
            entry = self._state.get(key)
            if entry is None or entry[0] != session or not entry[1]:
                continue
            result.append((key[0], key[1], dict(entry[1])))
        return result

    async def subscribe_ports(
        self, ports: Iterable[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """The gateway subscribes the default ports itself."""
        return list(ports)

    async def unsubscribe_ports(self, ports: Iterable[tuple[str, str]]) -> None:
        """The gateway owns its subscriptions; never cancel them from here."""
        return None

    async def start_sensor_stream(self, enable_mask: int) -> bool:
        try:
            status, _ = await self._request(
                {"op": "sensor_stream", "mask": enable_mask}
            )
        except TransportError as err:
            _LOGGER.warning("Condor sensor stream request failed: %s", err)
            return False
        return status == STATUS_OK

    async def stop_sensor_stream(self) -> None:
        try:
            await self._request({"op": "sensor_stream", "mask": 0})
        except Exception:  # noqa: BLE001
            pass

    # --- Port operations -------------------------------------------------

    async def get_props(
        self, product: str, port: str, timeout: float = REQUEST_TIMEOUT,
    ) -> tuple[int, Any]:
        return await self._request(
            {"op": "get_props", "product": product, "port": port}
        )

    async def put_props(
        self, product: str, port: str, props: dict[str, Any],
    ) -> tuple[int, Any]:
        return await self._request(
            {"op": "put_props", "product": product, "port": port, "props": props}
        )

    # Discovery and subscriptions are the gateway's job (see above).

    async def get_prods(self) -> tuple[int, Any]:
        raise TransportError("MQTT gateway: product list comes with the snapshot")

    async def get_ports(self, product: str) -> tuple[int, Any]:
        raise TransportError("MQTT gateway: port list comes with the snapshot")

    async def subscribe(self, product: str, port: str) -> tuple[int, Any]:
        raise TransportError("MQTT gateway: subscriptions belong to the gateway")

    async def unsubscribe(self, product: str, port: str) -> tuple[int, Any]:
        raise TransportError("MQTT gateway: subscriptions belong to the gateway")

    async def _request(self, request: dict[str, Any]) -> tuple[int, Any]:
        async with self._request_lock:
            if not self.link_up:
                raise TransportError("MQTT gateway: brush not connected")
            req_id = next(self._ids)
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = future
            try:
                await self._client.publish(
                    f"{self._base}/cmd",
                    json.dumps({"id": req_id, "client": self._client_id, **request}),
                )
                return await asyncio.wait_for(future, self._request_timeout)
            except asyncio.TimeoutError as err:
                raise TransportError(
                    f"MQTT gateway: no answer to {request['op']} "
                    f"within {self._request_timeout:.0f} s"
                ) from err
            finally:
                self._pending.pop(req_id, None)

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(TransportError(f"MQTT gateway: {reason}"))

    # --- Incoming messages ----------------------------------------------

    def _on_status(self, _topic: str, payload: str) -> None:
        online = payload.strip() == "online"
        if online == self._online:
            return
        self._online = online
        self._state_event.set()
        if not online:
            self._fail_pending("gateway offline")
        self._notify_state_change()

    def _on_info(self, _topic: str, payload: str) -> None:
        try:
            info = json.loads(payload)
        except ValueError:
            return
        if isinstance(info, dict) and info != self._info:
            self._info = info
            self._notify_state_change()

    def _on_link(self, _topic: str, payload: str) -> None:
        try:
            state = json.loads(payload)
        except ValueError:
            _LOGGER.debug("MQTT gateway: link payload not JSON: %r", payload)
            return
        if not isinstance(state, dict):
            return
        connected = bool(state.get("connected"))
        session = state.get("session")
        changed = connected != self._connected or session != self._session
        if session != self._session:
            self._held = []
        self._connected = connected
        self._session = session
        self._snapshot = bool(state.get("snapshot")) and connected
        ports = state.get("ports") if self._snapshot else None
        self._snapshot_ports = [
            key for key in (
                _port_key(p) for p in (ports or []) if isinstance(p, str)
            ) if key is not None
        ]
        mask = state.get("sensor_mask")
        self._sensor_mask = mask if isinstance(mask, int) else 0
        self._state_event.set()
        if not changed:
            return
        if not connected:
            self._fail_pending("brush disconnected")
        self._notify_state_change()

    def _notify_state_change(self) -> None:
        if self.on_state_change is not None:
            try:
                self.on_state_change()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("MQTT gateway state callback failed")

    def _on_state(self, topic: str, payload: str) -> None:
        key = _port_key(topic[len(self._base) + len("/state/"):])
        if key is None:
            return
        if not payload:
            self._state.pop(key, None)  # cleared by the gateway
            return
        try:
            body = json.loads(payload)
        except ValueError:
            _LOGGER.debug("MQTT gateway: state %s not JSON: %r", key, payload)
            return
        if not isinstance(body, dict) or not isinstance(body.get("props"), dict):
            return
        self._state[key] = (body.get("session"), body["props"])

    def _on_resp(self, _topic: str, payload: str) -> None:
        try:
            resp = json.loads(payload)
        except ValueError:
            _LOGGER.debug("MQTT gateway: resp not JSON: %r", payload)
            return
        if not isinstance(resp, dict):
            return
        if resp.get("client") not in (None, self._client_id):
            return  # answer to another listener
        future = self._pending.get(resp.get("id"))
        if future is None or future.done():
            # Late answer after a timeout.
            return
        if "error" in resp:
            message = resp.get("message")
            future.set_exception(TransportError(
                f"MQTT gateway error: {resp['error']}"
                + (f" ({message})" if message else "")
            ))
            return
        status = resp.get("status")
        if not isinstance(status, int):
            status = STATUS_NO_RESPONSE
        future.set_result((status, resp.get("data")))

    def _on_ind(self, topic: str, payload: str) -> None:
        key = _port_key(topic[len(self._base) + len("/ind/"):])
        if key is None or not self._connected:
            return
        product, port = key
        try:
            body = json.loads(payload)
        except ValueError:
            _LOGGER.debug("MQTT gateway: ind %s/%s not JSON: %r", product, port, payload)
            return
        if not isinstance(body, dict):
            return
        props: dict[str, Any] | bytes = body
        if port.endswith(".b"):
            try:
                props = bytes.fromhex(body.get("hex", ""))
            except (TypeError, ValueError):
                _LOGGER.debug("MQTT gateway: ind %s/%s bad hex: %r", product, port, payload)
                return
        if self._change_handler is not None and self.is_open:
            self._change_handler(product, port, props)
        elif len(self._held) < _MAX_HELD_INDICATIONS:
            self._held.append((product, port, props))
