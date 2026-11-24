"""Condor link through an MQTT gateway (see docs/MQTT_GATEWAY.md).

A gateway near the brush holds the BLE connection and does everything
below the message level (handshake, framing, acks). This link sends the
port operations to it as JSON requests on ``<base>/cmd`` and reads the
answers from ``<base>/resp``; change indications arrive on
``<base>/ind/<product>/<port>``.

The MQTT connection itself is abstracted as ``MqttClient`` so the link does
not depend on a particular client; inside Home Assistant it is backed by
the MQTT integration.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .condor_link import STATUS_NO_RESPONSE, CondorLink
from .exceptions import TransportError

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
TOPIC_PREFIX = "sonicare_gateway"
# How long to wait for an answer. The gateway itself gives up on the brush
# after 5 s and answers with "timeout"; this leaves room for the broker
# round trip on top.
REQUEST_TIMEOUT = 10.0
# After subscribing, retained status/link messages arrive almost at once.
_RETAINED_WAIT = 2.0

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


class CondorMqttLink(CondorLink):
    """Port operations over MQTT to a gateway that holds the BLE link."""

    def __init__(
        self,
        client: MqttClient,
        base: str,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        super().__init__()
        self._client = client
        self._base = base.rstrip("/")
        self._request_timeout = request_timeout

        self._unsubscribers: list[Callable[[], Awaitable[None] | None]] = []
        self._online = False
        self._connected = False
        self._session: int | None = None
        self._opened_session: int | None = None
        self._state_event = asyncio.Event()

        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        # One request at a time, like the BLE link: the gateway queues
        # anyway, and the message layer never needs more.
        self._request_lock = asyncio.Lock()

        # Called with no arguments whenever ``link_up`` or the session
        # changes — gateway online/offline, brush connected/disconnected, new
        # session. The owner (the transport) turns this into its connection
        # events.
        self.on_state_change: Callable[[], None] | None = None

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

    # --- Lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to the gateway's topics. Idempotent."""
        if self._unsubscribers:
            return
        subs = (
            (f"{self._base}/status", self._on_status, 1),
            (f"{self._base}/link", self._on_link, 1),
            (f"{self._base}/resp", self._on_resp, 1),
            (f"{self._base}/ind/#", self._on_ind, 0),
        )
        for topic, cb, qos in subs:
            self._unsubscribers.append(await self._client.subscribe(topic, cb, qos))

    async def open(self) -> None:
        """Take over the gateway's current session.

        The gateway runs the BLE handshake itself; opening only means the
        brush is connected through it and requests can go out.
        """
        await self.start()
        if self.is_open:
            return
        if not self.link_up:
            self._state_event.clear()
            try:
                await asyncio.wait_for(self._wait_link_up(), _RETAINED_WAIT)
            except asyncio.TimeoutError as err:
                raise TransportError(
                    "MQTT gateway: "
                    + ("brush not connected" if self._online else "gateway offline")
                ) from err
        self._opened_session = self._session
        _LOGGER.debug("Condor MQTT session %s open on %s", self._session, self._base)

    async def _wait_link_up(self) -> None:
        while not self.link_up:
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
        unsubscribers, self._unsubscribers = self._unsubscribers, []
        for unsub in unsubscribers:
            try:
                result = unsub()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                pass

    # --- Port operations -------------------------------------------------

    async def get_prods(self) -> tuple[int, Any]:
        return await self._request({"op": "get_prods"})

    async def get_ports(self, product: str) -> tuple[int, Any]:
        return await self._request({"op": "get_ports", "product": product})

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

    async def subscribe(self, product: str, port: str) -> tuple[int, Any]:
        return await self._request(
            {"op": "subscribe", "product": product, "port": port}
        )

    async def unsubscribe(self, product: str, port: str) -> tuple[int, Any]:
        return await self._request(
            {"op": "unsubscribe", "product": product, "port": port}
        )

    async def _request(self, request: dict[str, Any]) -> tuple[int, Any]:
        async with self._request_lock:
            if not self.link_up:
                raise TransportError("MQTT gateway: brush not connected")
            req_id = next(self._ids)
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = future
            try:
                await self._client.publish(
                    f"{self._base}/cmd", json.dumps({"id": req_id, **request}),
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
        self._connected = connected
        self._session = session
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

    def _on_resp(self, _topic: str, payload: str) -> None:
        try:
            resp = json.loads(payload)
        except ValueError:
            _LOGGER.debug("MQTT gateway: resp not JSON: %r", payload)
            return
        if not isinstance(resp, dict):
            return
        future = self._pending.get(resp.get("id"))
        if future is None or future.done():
            # Late answer after a timeout, or someone else's request.
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
        if self._change_handler is None:
            return
        rest = topic[len(self._base) + len("/ind/"):]
        product, _, port = rest.partition("/")
        if not product or not port:
            return
        try:
            body = json.loads(payload)
        except ValueError:
            _LOGGER.debug("MQTT gateway: ind %s not JSON: %r", rest, payload)
            return
        if not isinstance(body, dict):
            return
        if port.endswith(".b"):
            try:
                data = bytes.fromhex(body.get("hex", ""))
            except (TypeError, ValueError):
                _LOGGER.debug("MQTT gateway: ind %s bad hex: %r", rest, payload)
                return
            self._change_handler(product, port, data)
            return
        self._change_handler(product, port, body)
