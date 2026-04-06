"""BLE transport abstraction for Philips Sonicare.

Two implementations:
- BleakTransport: Direct BLE via bleak
- EspBridgeTransport: Via ESP32 ESPHome bridge (service calls + events)
"""
from __future__ import annotations

import abc
import asyncio
import logging
import time
from datetime import timedelta
from typing import Callable

from bleak import BleakClient
from bleak_retry_connector import establish_connection as bleak_establish

from homeassistant.components.bluetooth import async_last_service_info
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval

from .const import CHAR_SERVICE_MAP
from .exceptions import TransportError

_LOGGER = logging.getLogger(__name__)
TRACE = 5

ESP_EVENT_NAME = "esphome.philips_sonicare_ble_data"
ESP_STATUS_EVENT_NAME = "esphome.philips_sonicare_ble_status"
ESP_READ_TIMEOUT = 5.0
ESP_HEARTBEAT_TIMEOUT = 45.0


class SonicareTransport(abc.ABC):
    """Abstract BLE transport for Philips Sonicare."""

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish persistent connection for live monitoring."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Disconnect and clean up."""

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        """Return True if the transport has an active connection."""

    @property
    def is_bridge_alive(self) -> bool:
        return self.is_connected

    @property
    def is_device_connected(self) -> bool:
        return self.is_connected

    @abc.abstractmethod
    async def read_char(self, char_uuid: str) -> bytes | None:
        """Read a single GATT characteristic."""

    @abc.abstractmethod
    async def read_chars(self, char_uuids: list[str]) -> dict[str, bytes | None]:
        """Read multiple GATT characteristics (polling pattern)."""

    @abc.abstractmethod
    async def write_char(self, char_uuid: str, data: bytes) -> None:
        """Write data to a GATT characteristic."""

    @abc.abstractmethod
    async def subscribe(self, char_uuid: str, cb: Callable[[str, bytes], None]) -> None:
        """Subscribe to notifications on a characteristic."""

    @abc.abstractmethod
    async def unsubscribe(self, char_uuid: str) -> None:
        """Unsubscribe from notifications on a characteristic."""

    @abc.abstractmethod
    async def unsubscribe_all(self) -> None:
        """Unsubscribe from all active notification subscriptions."""

    async def set_notify_throttle(self, ms: int) -> None:
        """Set the notification throttle on the bridge (no-op for direct BLE)."""

    @abc.abstractmethod
    def set_disconnect_callback(self, cb: Callable[[], None]) -> None:
        """Register a callback invoked when the connection drops."""


class BleakTransport(SonicareTransport):
    """Direct BLE transport using bleak."""

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self._hass = hass
        self._address = address
        self._client: BleakClient | None = None
        self._disconnect_cb: Callable[[], None] | None = None
        self._last_read_errors: dict[str, str] = {}

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        service_info = async_last_service_info(self._hass, self._address)
        if not service_info:
            raise TransportError(f"Device {self._address} not in range")

        def _on_disconnect(_client):
            _LOGGER.info("%s: connection lost", self._address)
            self._client = None
            if self._disconnect_cb:
                self._disconnect_cb()

        self._client = await bleak_establish(
            BleakClient,
            service_info.device,
            "philips_sonicare",
            disconnected_callback=_on_disconnect,
            timeout=30.0,
        )

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    def pop_read_error(self, char_uuid: str) -> str | None:
        return self._last_read_errors.pop(char_uuid, None)

    async def read_char(self, char_uuid: str) -> bytes | None:
        if not self.is_connected:
            return None
        try:
            value = await self._client.read_gatt_char(char_uuid)
            return bytes(value) if value else None
        except Exception as e:
            _LOGGER.debug("Read failed for %s: %s", char_uuid, e)
            self._last_read_errors[char_uuid] = str(e)
            return None

    async def read_chars(self, char_uuids: list[str]) -> dict[str, bytes | None]:
        results: dict[str, bytes | None] = {u: None for u in char_uuids}
        service_info = async_last_service_info(self._hass, self._address)
        if not service_info:
            _LOGGER.debug("Device %s not in range", self._address)
            return results

        client: BleakClient | None = None
        try:
            client = await bleak_establish(
                BleakClient, service_info.device, "philips_sonicare", timeout=30.0
            )
            if not client or not client.is_connected:
                return results

            for uuid in char_uuids:
                try:
                    value = await client.read_gatt_char(uuid)
                    if value:
                        results[uuid] = bytes(value)
                except Exception as e:
                    _LOGGER.debug("Read failed for %s: %s", uuid, e)
        except Exception as err:
            _LOGGER.debug("BLE poll error (device likely sleeping): %s", err)
        finally:
            if client and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        return results

    async def write_char(self, char_uuid: str, data: bytes) -> None:
        if not self.is_connected:
            raise TransportError("Not connected")
        await self._client.write_gatt_char(char_uuid, data)

    async def subscribe(self, char_uuid: str, cb: Callable[[str, bytes], None]) -> None:
        if not self.is_connected:
            raise TransportError("Not connected")

        def _bleak_cb(_sender, data):
            cb(char_uuid, data)

        await self._client.start_notify(char_uuid, _bleak_cb)

    async def unsubscribe(self, char_uuid: str) -> None:
        if not self.is_connected:
            return
        try:
            await self._client.stop_notify(char_uuid)
        except Exception:
            pass

    async def unsubscribe_all(self) -> None:
        pass

    def set_disconnect_callback(self, cb: Callable[[], None]) -> None:
        self._disconnect_cb = cb


class EspBridgeTransport(SonicareTransport):
    """BLE transport via ESP32 ESPHome bridge."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        esphome_device_name: str,
        esp_bridge_id: str = "",
    ) -> None:
        self._hass = hass
        self._address = address
        self._device_name = esphome_device_name
        self._esp_bridge_id = esp_bridge_id
        self._setup_done = False
        self._device_connected = False
        self._esp_alive = False
        self._last_heartbeat: float = 0.0
        self._disconnect_cb: Callable[[], None] | None = None
        self._event_unsub: Callable | None = None
        self._status_unsub: Callable | None = None
        self._heartbeat_check_unsub: Callable | None = None
        self._pending_reads: dict[str, asyncio.Future[bytes | None]] = {}
        self._last_read_errors: dict[str, str] = {}
        self._notify_callbacks: dict[str, Callable[[str, bytes], None]] = {}
        self._detected_mac: str | None = address if ":" in address else None
        self._bridge_version: str | None = None
        self._pending_info: asyncio.Future[dict[str, str]] | None = None
        self._ble_paired: str | None = None
        self._needs_resubscribe = False

    def _svc_name(self, action: str) -> str:
        base = f"{self._device_name}_{action}"
        if self._esp_bridge_id:
            return f"{base}_{self._esp_bridge_id}"
        return base

    @staticmethod
    def _get_service_uuid(char_uuid: str) -> str:
        svc = CHAR_SERVICE_MAP.get(char_uuid)
        if not svc:
            raise TransportError(f"No service UUID mapping for characteristic {char_uuid}")
        return svc

    def _cancel_pending_reads(self) -> None:
        if not self._pending_reads:
            return
        for future in self._pending_reads.values():
            if not future.done():
                future.set_result(None)
        self._pending_reads.clear()

    @property
    def detected_mac(self) -> str | None:
        return self._detected_mac

    @property
    def bridge_version(self) -> str | None:
        return self._bridge_version

    @property
    def ble_paired(self) -> str | None:
        return self._ble_paired

    @property
    def is_bridge_alive(self) -> bool:
        if not self._setup_done:
            return False
        if not self._hass.services.has_service("esphome", self._svc_name("ble_read_char")):
            return False
        return self._esp_alive

    @property
    def is_device_connected(self) -> bool:
        return self._device_connected

    @property
    def is_connected(self) -> bool:
        return self.is_bridge_alive and self._device_connected

    @property
    def needs_resubscribe(self) -> bool:
        return self._needs_resubscribe

    def acknowledge_resubscribe(self) -> None:
        self._needs_resubscribe = False

    async def connect(self) -> None:
        svc = self._svc_name("ble_read_char")
        if not self._hass.services.has_service("esphome", svc):
            raise TransportError(f"ESPHome service esphome.{svc} not available yet")

        if self._event_unsub:
            self._setup_done = True
            # Wait for bridge to report alive if not yet seen
            if not self._esp_alive:
                await self._wait_for_bridge()
            return

        @callback
        def _handle_event(event: Event) -> None:
            data = event.data
            mac = data.get("mac", "")
            if mac and self._detected_mac and mac.upper() != self._detected_mac.upper():
                return

            uuid = data.get("uuid", "")
            payload_hex = data.get("payload", "")
            error = data.get("error", "")

            if error and uuid and uuid in self._pending_reads:
                self._last_read_errors[uuid] = error
                future = self._pending_reads.pop(uuid)
                if not future.done():
                    future.set_result(None)
                return

            if not uuid or not payload_hex:
                return

            if mac and not self._detected_mac:
                self._detected_mac = mac

            try:
                payload = bytes.fromhex(payload_hex)
            except ValueError:
                return

            if uuid in self._pending_reads:
                future = self._pending_reads.pop(uuid)
                if not future.done():
                    future.set_result(payload)

            if uuid in self._notify_callbacks:
                self._notify_callbacks[uuid](uuid, payload)

        self._event_unsub = self._hass.bus.async_listen(ESP_EVENT_NAME, _handle_event)

        @callback
        def _handle_status_event(event: Event) -> None:
            mac = event.data.get("mac", "")
            if mac and self._detected_mac and mac.upper() != self._detected_mac.upper():
                return

            status = event.data.get("status", "")
            version = event.data.get("version")
            if version:
                self._bridge_version = version

            self._last_heartbeat = time.monotonic()
            was_alive = self._esp_alive
            was_connected = self._device_connected

            if not self._esp_alive:
                self._esp_alive = True

            if status == "info":
                # Filter by bridge_id if present (multi-device ESP)
                event_bridge_id = event.data.get("bridge_id", "")
                if event_bridge_id and self._esp_bridge_id and event_bridge_id != self._esp_bridge_id:
                    return
                # Only set _detected_mac from info events (bridge_id filtered)
                # to avoid cross-contamination from other instances' heartbeats
                if mac and not self._detected_mac:
                    self._detected_mac = mac
                paired = event.data.get("paired")
                if paired is not None:
                    self._ble_paired = paired
                ble_connected = event.data.get("ble_connected")
                if ble_connected is not None:
                    self._device_connected = ble_connected == "true"
                if self._pending_info and not self._pending_info.done():
                    self._pending_info.set_result(dict(event.data))
            elif status == "heartbeat":
                ble_connected = event.data.get("ble_connected") == "true"
                self._device_connected = ble_connected
                if not ble_connected:
                    self._cancel_pending_reads()
            elif status == "ready":
                # Only mark device connected after GATT discovery is
                # complete ("ready"), not on "connected" (GATT_OPEN_EVT)
                # which fires before the characteristic table is populated.
                self._device_connected = True
                # Only flag resubscribe if HA has no active subscriptions.
                # The bridge re-fires "ready" on every heartbeat when it
                # has no subscriptions — ignore if HA already subscribed.
                if not self._notify_callbacks:
                    self._needs_resubscribe = True
            elif status == "connected":
                pass  # GATT discovery still in progress

            elif status == "disconnected":
                self._device_connected = False
                self._cancel_pending_reads()

            # Only fire callback when actual state changed
            now_connected = self.is_connected
            was_fully_connected = was_alive and was_connected
            if now_connected != was_fully_connected and self._disconnect_cb:
                self._disconnect_cb()
            elif status == "disconnected" and self._disconnect_cb:
                        self._disconnect_cb()

        self._status_unsub = self._hass.bus.async_listen(ESP_STATUS_EVENT_NAME, _handle_status_event)

        @callback
        def _check_heartbeat(now=None) -> None:
            if not self._setup_done or self._last_heartbeat == 0:
                return
            elapsed = time.monotonic() - self._last_heartbeat
            if elapsed > ESP_HEARTBEAT_TIMEOUT and self._esp_alive:
                self._esp_alive = False
                self._cancel_pending_reads()
                if self._disconnect_cb:
                    self._disconnect_cb()

        self._heartbeat_check_unsub = async_track_time_interval(
            self._hass, _check_heartbeat, timedelta(seconds=15)
        )

        self._setup_done = True

        # Wait for bridge to report alive and device connected
        await self._wait_for_bridge()

    async def _wait_for_bridge(self) -> None:
        """Wait until the ESP bridge reports alive and BLE device connected."""
        if self.is_connected:
            return
        _LOGGER.debug("Waiting for ESP bridge status events...")
        for _ in range(30):  # max 30s
            await asyncio.sleep(1)
            if self.is_connected:
                _LOGGER.info(
                    "ESP bridge ready (mac=%s, version=%s)",
                    self._detected_mac,
                    self._bridge_version,
                )
                return
        if not self._esp_alive:
            raise TransportError("ESP bridge did not respond within 30s")
        if not self._device_connected:
            _LOGGER.warning("ESP bridge alive but BLE device not connected (yet)")
            # Don't raise — device may connect later

    async def disconnect(self) -> None:
        if self._event_unsub:
            self._event_unsub()
            self._event_unsub = None
        if self._status_unsub:
            self._status_unsub()
            self._status_unsub = None
        if self._heartbeat_check_unsub:
            self._heartbeat_check_unsub()
            self._heartbeat_check_unsub = None
        self._setup_done = False
        self._device_connected = False
        self._esp_alive = False
        self._pending_reads.clear()
        self._notify_callbacks.clear()

    def pop_read_error(self, char_uuid: str) -> str | None:
        return self._last_read_errors.pop(char_uuid, None)

    async def read_char(self, char_uuid: str) -> bytes | None:
        if not self._setup_done:
            return None
        service_uuid = self._get_service_uuid(char_uuid)
        self._last_read_errors.pop(char_uuid, None)
        future: asyncio.Future[bytes | None] = self._hass.loop.create_future()
        self._pending_reads[char_uuid] = future

        try:
            await self._hass.services.async_call(
                "esphome",
                self._svc_name("ble_read_char"),
                {"service_uuid": service_uuid, "char_uuid": char_uuid},
                blocking=True,
            )
        except HomeAssistantError as err:
            self._pending_reads.pop(char_uuid, None)
            _LOGGER.debug("Service call failed for %s: %s", char_uuid, err)
            return None

        try:
            return await asyncio.wait_for(future, timeout=ESP_READ_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending_reads.pop(char_uuid, None)
            _LOGGER.debug("Read timeout for %s (other reads continue)", char_uuid)
            return None

    async def read_chars(self, char_uuids: list[str]) -> dict[str, bytes | None]:
        if not self._setup_done:
            await self.connect()
        results: dict[str, bytes | None] = {}
        for uuid in char_uuids:
            if not self.is_connected:
                results[uuid] = None
                continue
            results[uuid] = await self.read_char(uuid)
        return results

    async def write_char(self, char_uuid: str, data: bytes) -> None:
        if not self._setup_done:
            raise TransportError("Not connected")
        service_uuid = self._get_service_uuid(char_uuid)
        try:
            await self._hass.services.async_call(
                "esphome",
                self._svc_name("ble_write_char"),
                {"service_uuid": service_uuid, "char_uuid": char_uuid, "data": data.hex()},
                blocking=True,
            )
        except HomeAssistantError as err:
            raise TransportError(f"ESP write_char failed: {err}") from err

    async def subscribe(self, char_uuid: str, cb: Callable[[str, bytes], None]) -> None:
        if not self._setup_done:
            raise TransportError("Not connected")
        service_uuid = self._get_service_uuid(char_uuid)
        self._notify_callbacks[char_uuid] = cb
        try:
            await self._hass.services.async_call(
                "esphome",
                self._svc_name("ble_subscribe"),
                {"service_uuid": service_uuid, "char_uuid": char_uuid},
                blocking=True,
            )
        except HomeAssistantError as err:
            self._notify_callbacks.pop(char_uuid, None)
            raise TransportError(f"ESP subscribe failed: {err}") from err

    async def unsubscribe(self, char_uuid: str) -> None:
        self._notify_callbacks.pop(char_uuid, None)
        if not self._setup_done or not self._device_connected:
            return
        service_uuid = self._get_service_uuid(char_uuid)
        try:
            await self._hass.services.async_call(
                "esphome",
                self._svc_name("ble_unsubscribe"),
                {"service_uuid": service_uuid, "char_uuid": char_uuid},
                blocking=True,
            )
        except Exception:
            pass

    async def unsubscribe_all(self) -> None:
        if not self._device_connected:
            # Device already gone — just clear local callbacks
            self._notify_callbacks.clear()
            return
        for char_uuid in list(self._notify_callbacks.keys()):
            await self.unsubscribe(char_uuid)

    async def get_bridge_info(self) -> dict[str, str] | None:
        """Request diagnostic info from ESP bridge via ble_get_info service."""
        if not self._setup_done:
            return None

        self._pending_info = self._hass.loop.create_future()

        try:
            await self._hass.services.async_call(
                "esphome",
                self._svc_name("ble_get_info"),
                {},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("ESP get_bridge_info failed: %s", err)
            self._pending_info = None
            return None

        try:
            return await asyncio.wait_for(self._pending_info, timeout=5.0)
        except asyncio.TimeoutError:
            _LOGGER.warning("ESP get_bridge_info timeout")
            self._pending_info = None
            return None

    async def set_notify_throttle(self, ms: int) -> None:
        if not self.is_connected:
            return
        try:
            await self._hass.services.async_call(
                "esphome",
                self._svc_name("ble_set_throttle"),
                {"throttle_ms": str(ms)},
                blocking=True,
            )
        except HomeAssistantError:
            pass

    def set_disconnect_callback(self, cb: Callable[[], None]) -> None:
        self._disconnect_cb = cb
