"""Reference implementation of the gateway side of docs/MQTT_GATEWAY.md.

``CondorMqttGateway`` serves any ``CondorLink`` over MQTT: the requests on
``<base>/cmd`` become link operations, the answers go to ``<base>/resp``,
change indications to ``<base>/ind/<product>/<port>``. With a
``CondorBleLink`` underneath it does what a phone gateway does; the tests
put a fake link underneath.

The MQTT client is abstracted the same way as in ``condor_mqtt_link``
(``publish`` / ``subscribe``); the last will (``<base>/status`` =
``offline``, retained) has to be configured on the client's connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from custom_components.philips_sonicare_ble.condor_link import CondorLink
from custom_components.philips_sonicare_ble.condor_mqtt_link import (
    PROTOCOL_VERSION,
    MqttClient,
)
from custom_components.philips_sonicare_ble.exceptions import TransportError

_LOGGER = logging.getLogger(__name__)

_PORT_OPS = ("get_props", "put_props", "subscribe", "unsubscribe")
_OPS = ("get_prods", "get_ports", *_PORT_OPS)


class CondorMqttGateway:
    def __init__(
        self,
        link: CondorLink,
        client: MqttClient,
        base: str,
        info: dict[str, Any],
        session: int = 0,
    ) -> None:
        self.link = link
        self.client = client
        self.base = base.rstrip("/")
        self.info = {"protocol": PROTOCOL_VERSION, **info}
        self.session = session
        self.connected = False
        self.rssi: int | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._unsub = None

    # --- Lifecycle -------------------------------------------------------

    async def start(self) -> None:
        await self.client.publish(f"{self.base}/status", "online", retain=True)
        await self.client.publish(
            f"{self.base}/info", json.dumps(self.info), retain=True,
        )
        await self._publish_link()
        self._unsub = await self.client.subscribe(f"{self.base}/cmd", self._on_cmd)
        self._worker = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        if self._unsub is not None:
            result = self._unsub()
            if asyncio.iscoroutine(result):
                await result
            self._unsub = None
        await self.client.publish(f"{self.base}/status", "offline", retain=True)

    async def session_up(self) -> None:
        """Call after the link to the brush is open (handshake done)."""
        self.session += 1
        self.connected = True
        self.link.set_change_handler(self._on_change)
        await self._publish_link()

    async def session_down(self) -> None:
        """Call when the brush disconnected."""
        self.connected = False
        self.link.set_change_handler(None)
        self.link.invalidate()
        await self._publish_link()
        while not self._queue.empty():
            req = self._queue.get_nowait()
            await self._error(req.get("id"), "not_connected")

    async def _publish_link(self) -> None:
        state: dict[str, Any] = {"connected": self.connected, "session": self.session}
        if self.rssi is not None:
            state["rssi"] = self.rssi
        await self.client.publish(f"{self.base}/link", json.dumps(state), retain=True)

    # --- Requests --------------------------------------------------------

    def _on_cmd(self, _topic: str, payload: str) -> None:
        try:
            req = json.loads(payload)
        except ValueError:
            req = None
        if not isinstance(req, dict):
            _LOGGER.debug("cmd not a JSON object: %r", payload)
            return
        self._queue.put_nowait(req)

    async def _run(self) -> None:
        while True:
            req = await self._queue.get()
            try:
                await self._handle(req)
            except Exception as err:  # noqa: BLE001
                await self._error(req.get("id"), "failed", str(err))

    async def _handle(self, req: dict[str, Any]) -> None:
        req_id = req.get("id")
        op = req.get("op")
        if not isinstance(req_id, int) or op not in _OPS:
            await self._error(req_id, "bad_request")
            return
        product, port = req.get("product"), req.get("port")
        if op != "get_prods" and not isinstance(product, str):
            await self._error(req_id, "bad_request")
            return
        if op in _PORT_OPS and not isinstance(port, str):
            await self._error(req_id, "bad_request")
            return
        if op == "put_props" and not isinstance(req.get("props"), dict):
            await self._error(req_id, "bad_request")
            return
        if not self.connected or not self.link.is_open:
            await self._error(req_id, "not_connected")
            return
        try:
            if op == "get_prods":
                status, data = await self.link.get_prods()
            elif op == "get_ports":
                status, data = await self.link.get_ports(product)
            elif op == "put_props":
                status, data = await self.link.put_props(product, port, req["props"])
            else:
                status, data = await getattr(self.link, op)(product, port)
        except TransportError as err:
            kind = "timeout" if "timeout" in str(err).lower() else "failed"
            await self._error(req_id, kind, str(err))
            return
        await self.client.publish(
            f"{self.base}/resp",
            json.dumps({"id": req_id, "status": status, "data": data}),
        )

    async def _error(self, req_id: Any, error: str, message: str | None = None) -> None:
        body: dict[str, Any] = {"id": req_id, "error": error}
        if message:
            body["message"] = message
        await self.client.publish(f"{self.base}/resp", json.dumps(body))

    # --- Change indications ---------------------------------------------

    def _on_change(self, product: str, port: str, props: dict[str, Any] | bytes) -> None:
        if isinstance(props, bytes):
            payload, qos = json.dumps({"hex": props.hex()}), 0
        else:
            payload, qos = json.dumps(props), 1
        asyncio.get_running_loop().create_task(
            self.client.publish(f"{self.base}/ind/{product}/{port}", payload, qos=qos)
        )
