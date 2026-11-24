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
import sys
import time
from pathlib import Path
from typing import Any

# Run as a script from anywhere: the integration package lives one level up.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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
        session: int | None = None,
    ) -> None:
        self.link = link
        self.client = client
        self.base = base.rstrip("/")
        self.info = {"protocol": PROTOCOL_VERSION, **info}
        # Never repeat a session number, not even after a restart: start
        # from the clock.
        self.session = int(time.time()) if session is None else session
        self.connected = False
        self.rssi: int | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._unsub = None

    # --- Lifecycle -------------------------------------------------------

    async def start(self) -> None:
        # link first: until it is replaced, a retained link from before the
        # restart may still say "connected".
        await self._publish_link()
        await self.client.publish(
            f"{self.base}/info", json.dumps(self.info), retain=True,
        )
        await self.client.publish(f"{self.base}/status", "online", retain=True)
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


# --- Command line: a gateway on this machine (bleak + paho-mqtt) ----------
#
#   python scripts/condor_mqtt_gateway.py --address AA:BB:CC:DD:EE:FF \
#       [--adapter hci0] [--broker 127.0.0.1] [--port 1883]
#
# Runs from the repository root. Stands in for a phone gateway in tests.

_DIS = {
    "model": "00002a24-0000-1000-8000-00805f9b34fb",
    "serial": "00002a25-0000-1000-8000-00805f9b34fb",
    "firmware": "00002a26-0000-1000-8000-00805f9b34fb",
}


class PahoClient:
    """``MqttClient`` on paho-mqtt; callbacks run on the asyncio loop."""

    def __init__(self, host: str, port: int, will_topic: str) -> None:
        import paho.mqtt.client as mqtt

        self._loop = asyncio.get_running_loop()
        self._subs: dict[str, Any] = {}
        self._mqtt = mqtt
        client_id = f"condor-gw-{will_topic.split('/')[1]}"
        if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt 2.x
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        else:
            self._client = mqtt.Client(client_id=client_id)
        self._client.will_set(will_topic, "offline", qos=1, retain=True)
        self._client.on_message = self._on_message
        self._connected = asyncio.Event()
        self._client.on_connect = lambda *_: self._loop.call_soon_threadsafe(
            self._connected.set
        )
        self._client.connect(host, port, keepalive=15)
        self._client.loop_start()

    async def wait_connected(self) -> None:
        await asyncio.wait_for(self._connected.wait(), 10)

    def _on_message(self, _client, _userdata, msg) -> None:
        payload = msg.payload.decode("utf-8", errors="replace")
        for pattern, cb in list(self._subs.items()):
            if self._mqtt.topic_matches_sub(pattern, msg.topic):
                self._loop.call_soon_threadsafe(cb, msg.topic, payload)

    async def publish(self, topic, payload, qos=1, retain=False) -> None:
        self._client.publish(topic, payload, qos=qos, retain=retain)

    async def subscribe(self, topic, callback, qos=1):
        self._subs[topic] = callback
        self._client.subscribe(topic, qos)

        def unsubscribe() -> None:
            self._subs.pop(topic, None)
            self._client.unsubscribe(topic)

        return unsubscribe

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


def _bleak_transport_class():
    from bleak import BleakClient

    from custom_components.philips_sonicare_ble.transport import SonicareTransport

    class BleakGattTransport(SonicareTransport):
        """Plain bleak GATT access, no Home Assistant."""

        def __init__(self, address: str, adapter: str | None) -> None:
            self.address = address
            self.adapter = adapter
            self.client: BleakClient | None = None
            self.disconnected = asyncio.Event()
            self._cb = None

        @property
        def is_connected(self) -> bool:
            return self.client is not None and self.client.is_connected

        async def connect(self) -> None:
            self.disconnected.clear()
            kwargs = {"adapter": self.adapter} if self.adapter else {}
            self.client = BleakClient(
                self.address,
                disconnected_callback=lambda _c: self.disconnected.set(),
                timeout=20.0,
                **kwargs,
            )
            await self.client.connect()

        async def disconnect(self) -> None:
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:  # noqa: BLE001
                    pass

        async def read_char(self, char_uuid: str) -> bytes | None:
            return bytes(await self.client.read_gatt_char(char_uuid))

        async def read_chars(self, char_uuids):
            return {u: await self.read_char(u) for u in char_uuids}

        async def write_char(self, char_uuid: str, data: bytes) -> None:
            await self.client.write_gatt_char(char_uuid, data)

        async def subscribe(self, char_uuid, cb) -> None:
            await self.client.start_notify(
                char_uuid, lambda _c, data: cb(char_uuid, bytes(data))
            )

        async def unsubscribe(self, char_uuid) -> None:
            if self.is_connected:
                await self.client.stop_notify(char_uuid)

        async def unsubscribe_all(self) -> None:
            return None

        def set_disconnect_callback(self, cb) -> None:
            self._cb = cb

    return BleakGattTransport


async def _main(args) -> None:
    from custom_components.philips_sonicare_ble.condor_link import CondorBleLink
    from custom_components.philips_sonicare_ble.condor_mqtt_link import base_topic

    base = base_topic(args.address)
    mqtt = PahoClient(args.broker, args.port, f"{base}/status")
    await mqtt.wait_connected()
    transport = _bleak_transport_class()(args.address, args.adapter)
    link = CondorBleLink(transport)
    info = {
        "gateway": "condor-mqtt-gateway (reference)",
        "gateway_version": "1",
        "address": args.address.upper(),
        "name": "Philips Sonicare",
    }
    gateway = CondorMqttGateway(link, mqtt, base, info)
    await gateway.start()
    _LOGGER.info("gateway online on %s", base)
    try:
        while True:
            try:
                await transport.connect()
                for key, uuid in _DIS.items():
                    try:
                        value = (await transport.read_char(uuid)).decode(errors="replace")
                        gateway.info[key] = value.strip("\x00 ")
                    except Exception:  # noqa: BLE001
                        pass
                await mqtt.publish(f"{base}/info", json.dumps(gateway.info), retain=True)
                await link.open()
                await gateway.session_up()
                _LOGGER.info("brush connected, session %d", gateway.session)
                await transport.disconnected.wait()
                _LOGGER.info("brush disconnected")
            except Exception as err:  # noqa: BLE001
                _LOGGER.info("connect failed: %s", err)
                await transport.disconnect()
            if gateway.connected:
                await gateway.session_down()
            else:
                link.invalidate()
            await asyncio.sleep(2)
    finally:
        await transport.disconnect()
        await gateway.stop()
        mqtt.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--address", required=True)
    parser.add_argument("--adapter", default=None)
    parser.add_argument("--broker", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("-v", "--verbose", action="store_true")
    a = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_main(a))
    except KeyboardInterrupt:
        pass
