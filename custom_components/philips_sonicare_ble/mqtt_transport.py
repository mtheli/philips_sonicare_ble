"""Transport for a Condor brush behind an MQTT gateway.

The gateway holds the BLE link (see docs/MQTT_GATEWAY.md); Home Assistant
only exchanges port operations with it. There is no GATT access from here,
so this transport exists to give the coordinator the connection state it
works with — gateway alive, brush connected, reconnects — while the data
goes through ``CondorMqttLink`` (``self.link``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .condor_mqtt_link import CondorMqttLink, MqttClient, base_topic
from .exceptions import TransportError
from .transport import SonicareTransport

_LOGGER = logging.getLogger(__name__)

# How long connect() waits for the gateway's retained "online" status.
GATEWAY_WAIT = 10.0


class HaMqttClient:
    """``MqttClient`` on top of Home Assistant's MQTT integration."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def ready(self) -> bool:
        from homeassistant.components import mqtt

        try:
            return await mqtt.async_wait_for_mqtt_client(self.hass)
        except Exception:  # noqa: BLE001 — MQTT not set up at all
            return False

    async def publish(
        self, topic: str, payload: str, qos: int = 1, retain: bool = False,
    ) -> None:
        from homeassistant.components import mqtt

        await mqtt.async_publish(self.hass, topic, payload, qos, retain)

    async def subscribe(
        self, topic: str, cb: Callable[[str, str], None], qos: int = 1,
    ):
        from homeassistant.components import mqtt

        @callback
        def _on_message(msg: Any) -> None:
            payload = msg.payload
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            cb(msg.topic, payload)

        return await mqtt.async_subscribe(self.hass, topic, _on_message, qos)


class MqttGatewayTransport(SonicareTransport):
    """Connection state of a brush behind an MQTT gateway."""

    def __init__(
        self,
        hass: HomeAssistant | None,
        address: str,
        client: MqttClient | None = None,
    ) -> None:
        self._address = address
        self._client: MqttClient = client if client is not None else HaMqttClient(hass)
        self.link = CondorMqttLink(self._client, base_topic(address))
        self.link.on_state_change = self._on_link_state
        self._state_cb: Callable[[], None] | None = None
        self._last_up = False
        self._last_session: int | None = None
        self._online_event = asyncio.Event()
        # Like the ESP bridge: counts link losses, including a new session
        # that started before a disconnect was ever observed.
        self.disconnect_count = 0

    # --- State -------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self.link.link_up

    @property
    def is_bridge_alive(self) -> bool:
        return self.link.gateway_online

    @property
    def is_device_connected(self) -> bool:
        return self.link.link_up

    @property
    def connection_path(self) -> str | None:
        return "MQTT gateway"

    def set_disconnect_callback(self, cb: Callable[[], None]) -> None:
        self._state_cb = cb

    @callback
    def _on_link_state(self) -> None:
        if self.link.gateway_online:
            self._online_event.set()
        else:
            self._online_event.clear()
        up, session = self.link.link_up, self.link.session
        if self._last_up and (not up or session != self._last_session):
            self.disconnect_count += 1
        changed = up != self._last_up or (up and session != self._last_session)
        self._last_up, self._last_session = up, session
        if changed and self._state_cb is not None:
            self._state_cb()

    # --- Lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Listen to the gateway; returns once it is online.

        Like the ESP bridge, the brush itself may still be away — the
        coordinator waits for the connected event after this.
        """
        ready = getattr(self._client, "ready", None)
        if ready is not None and not await ready():
            raise TransportError("MQTT is not available")
        await self.link.start()
        try:
            await asyncio.wait_for(self._online_event.wait(), GATEWAY_WAIT)
        except asyncio.TimeoutError as err:
            raise TransportError(
                f"MQTT gateway did not respond within {GATEWAY_WAIT:.0f}s"
            ) from err

    async def disconnect(self) -> None:
        await self.link.stop()
        self._online_event.clear()
        self._last_up = False
        self._last_session = None

    # --- No GATT access through a gateway ---------------------------------

    async def read_char(self, char_uuid: str) -> bytes | None:
        raise TransportError("No GATT access through an MQTT gateway")

    async def read_chars(self, char_uuids: list[str]) -> dict[str, bytes | None]:
        raise TransportError("No GATT access through an MQTT gateway")

    async def write_char(self, char_uuid: str, data: bytes) -> None:
        raise TransportError("No GATT access through an MQTT gateway")

    async def subscribe(self, char_uuid: str, cb: Callable[[str, bytes], None]) -> None:
        raise TransportError("No GATT access through an MQTT gateway")

    async def unsubscribe(self, char_uuid: str) -> None:
        return None

    async def unsubscribe_all(self) -> None:
        return None
