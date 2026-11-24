"""Reference implementation of the gateway side of docs/MQTT_GATEWAY.md.

``CondorMqttGateway`` runs a Condor session over any ``CondorLink`` and
publishes it on MQTT: on every new session it subscribes the default ports,
reads every port into ``<base>/state/<product>/<port>`` (retained, kept up
to date from the change indications) and reports the snapshot complete in
``<base>/link``; change indications also go to ``<base>/ind/...``. Requests
on ``<base>/cmd`` cover the sensor stream and single reads and writes.

With a ``CondorBleLink`` underneath it does what a phone gateway does; the
tests put a fake link underneath. The MQTT client is abstracted the same
way as in ``condor_mqtt_link`` (``publish`` / ``subscribe``); the last will
(``<base>/status`` = ``offline``, retained) has to be configured on the
client's connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

# Run as a script from anywhere: the integration package lives one level up.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from custom_components.philips_sonicare_ble.condor_link import (  # noqa: E402
    STATUS_OK,
    CondorLink,
)
from custom_components.philips_sonicare_ble.condor_mqtt_link import (  # noqa: E402
    PROTOCOL_VERSION,
    MqttClient,
)
from custom_components.philips_sonicare_ble.condor_protocol import (  # noqa: E402
    DEFAULT_SUBSCRIBE_PORTS,
)
from custom_components.philips_sonicare_ble.exceptions import (  # noqa: E402
    TransportError,
)

_LOGGER = logging.getLogger(__name__)

_OPS = ("get_props", "put_props", "sensor_stream")
_DEDUP = 64  # remembered (client, id) pairs


class CondorMqttGateway:
    def __init__(
        self,
        link: CondorLink,
        client: MqttClient,
        base: str,
        info: dict[str, Any],
    ) -> None:
        self.link = link
        self.client = client
        self.base = base.rstrip("/")
        self.info = {"protocol": PROTOCOL_VERSION, **info}
        # Never repeat a session number, not even after a restart: start
        # from the clock.
        self.session = int(time.time())
        self.connected = False
        self.snapshot = False
        self.sensor_mask = 0
        self.rssi: int | None = None
        # Full properties per port of the current session.
        self.state: dict[tuple[str, str], dict[str, Any]] = {}
        self._reading = False
        self._read_inds: list[tuple[str, str, dict[str, Any]]] = []
        self._published_ports: set[tuple[str, str]] = set()
        self._answers: OrderedDict[tuple[Any, Any], str] = OrderedDict()
        self._in_flight: set[tuple[Any, Any]] = set()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # Everything goes out through one queue, in the order it happened:
        # an indication must never overtake the state it belongs after.
        self._outbox: asyncio.Queue[tuple[str, str, int, bool]] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._unsub = None

    # --- Publishing ------------------------------------------------------

    def _send(self, topic: str, payload: str, qos: int = 1, retain: bool = False) -> None:
        self._outbox.put_nowait((f"{self.base}/{topic}", payload, qos, retain))

    async def _sender(self) -> None:
        while True:
            topic, payload, qos, retain = await self._outbox.get()
            try:
                await self.client.publish(topic, payload, qos=qos, retain=retain)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("publish %s failed: %s", topic, err)
            finally:
                self._outbox.task_done()

    async def flush(self) -> None:
        await self._outbox.join()

    def _send_link(self) -> None:
        state: dict[str, Any] = {
            "connected": self.connected,
            "session": self.session,
            "snapshot": self.snapshot,
            "sensor_mask": self.sensor_mask,
        }
        if self.snapshot:
            state["ports"] = [f"{p}/{port}" for p, port in self.state]
        if self.rssi is not None:
            state["rssi"] = self.rssi
        self._send("link", json.dumps(state), retain=True)

    def _send_state(self, key: tuple[str, str]) -> None:
        body = {"session": self.session, "props": self.state[key]}
        self._send(f"state/{key[0]}/{key[1]}", json.dumps(body), retain=True)
        self._published_ports.add(key)

    def send_info(self) -> None:
        self._send("info", json.dumps(self.info), retain=True)

    # --- Lifecycle -------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._sender()), loop.create_task(self._run())]
        # link first: until it is replaced, a retained link from before the
        # restart may still say "connected".
        self._send_link()
        self.send_info()
        self._send("status", "online", retain=True)
        self._unsub = await self.client.subscribe(f"{self.base}/cmd", self._on_cmd)
        await self.flush()

    async def stop(self) -> None:
        if self._unsub is not None:
            result = self._unsub()
            if asyncio.iscoroutine(result):
                await result
            self._unsub = None
        self._send("status", "offline", retain=True)
        await self.flush()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def session_up(self) -> None:
        """Call after the link to the brush is open (handshake done).

        Announces the session, subscribes the default ports, reads every
        port and reports the snapshot complete.
        """
        self.session = max(self.session + 1, int(time.time()))
        self.connected = True
        self.snapshot = False
        self.sensor_mask = 0
        self.state = {}
        self.link.set_change_handler(self._on_change)
        self._send_link()

        await self.link.subscribe_ports(DEFAULT_SUBSCRIBE_PORTS)
        self._reading, self._read_inds = True, []
        try:
            ports = await self.link.read_all_ports()
        finally:
            self._reading = False
        for prod, port, props in ports:
            key = (prod, port)
            # Changes that came in while reading win over the read: they
            # are at least as new.
            for ind_prod, ind_port, delta in self._read_inds:
                if (ind_prod, ind_port) == key:
                    props.update(delta)
            self.state[key] = props
            self._send_state(key)
        # Ports from an earlier snapshot that this one does not have.
        for key in self._published_ports - set(self.state):
            self._send(f"state/{key[0]}/{key[1]}", "", retain=True)
        self._published_ports &= set(self.state)
        self.snapshot = True
        self._send_link()
        await self.flush()

    async def session_down(self) -> None:
        """Call when the brush disconnected."""
        self.connected = False
        self.snapshot = False
        self.sensor_mask = 0
        self.link.set_change_handler(None)
        self.link.invalidate()
        self._send_link()
        while not self._queue.empty():
            req = self._queue.get_nowait()
            self._answer(req, {"error": "not_connected"})
        await self.flush()

    # --- Requests --------------------------------------------------------

    def _on_cmd(self, _topic: str, payload: str) -> None:
        try:
            req = json.loads(payload)
        except ValueError:
            req = None
        if not isinstance(req, dict):
            _LOGGER.debug("cmd not a JSON object: %r", payload)
            return
        key = (req.get("client"), req.get("id"))
        # QoS 1 may deliver a request twice: run it once, answer again.
        if key in self._answers:
            self._send("resp", self._answers[key])
            return
        if key in self._in_flight:
            return
        self._in_flight.add(key)
        self._queue.put_nowait(req)

    def _answer(self, req: dict[str, Any], body: dict[str, Any]) -> None:
        resp = {"id": req.get("id"), **body}
        if req.get("client") is not None:
            resp["client"] = req["client"]
        payload = json.dumps(resp)
        key = (req.get("client"), req.get("id"))
        self._in_flight.discard(key)
        self._answers[key] = payload
        while len(self._answers) > _DEDUP:
            self._answers.popitem(last=False)
        self._send("resp", payload)

    async def _run(self) -> None:
        while True:
            req = await self._queue.get()
            try:
                self._answer(req, await self._handle(req))
            except Exception as err:  # noqa: BLE001
                self._answer(req, {"error": "failed", "message": str(err)})

    async def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        if not isinstance(req.get("id"), int) or op not in _OPS:
            return {"error": "bad_request"}
        product, port = req.get("product"), req.get("port")
        if op == "sensor_stream":
            if not isinstance(req.get("mask"), int):
                return {"error": "bad_request"}
        elif not isinstance(product, str) or not isinstance(port, str):
            return {"error": "bad_request"}
        if op == "put_props" and not isinstance(req.get("props"), dict):
            return {"error": "bad_request"}
        if not self.connected or not self.link.is_open:
            return {"error": "not_connected"}
        try:
            if op == "sensor_stream":
                mask = req["mask"]
                if mask == self.sensor_mask:
                    status = STATUS_OK  # already as requested
                elif mask and self.sensor_mask:
                    # Running: only the enable register changes.
                    status, _ = await self.link.put_props(
                        "1", "SensorData", {"Sensors": mask},
                    )
                elif mask:
                    ok = await self.link.start_sensor_stream(mask)
                    status = STATUS_OK if ok else 1
                else:
                    await self.link.stop_sensor_stream()
                    status = STATUS_OK
                if status == STATUS_OK and mask != self.sensor_mask:
                    self.sensor_mask = mask
                    self._send_link()
                return {"status": status, "data": None}
            if op == "get_props":
                status, data = await self.link.get_props(product, port)
            else:
                status, data = await self.link.put_props(product, port, req["props"])
        except TransportError as err:
            kind = "timeout" if "timeout" in str(err).lower() else "failed"
            return {"error": kind, "message": str(err)}
        return {"status": status, "data": data}

    # --- Change indications ---------------------------------------------

    def _on_change(self, product: str, port: str, props: dict[str, Any] | bytes) -> None:
        if isinstance(props, bytes):
            self._send(f"ind/{product}/{port}", json.dumps({"hex": props.hex()}), qos=0)
            return
        self._send(f"ind/{product}/{port}", json.dumps(props))
        key = (product, port)
        if self._reading:
            self._read_inds.append((product, port, dict(props)))
        elif self.snapshot and key in self.state:
            self.state[key].update(props)
            self._send_state(key)


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
                gateway.send_info()
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
