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
from datetime import datetime, timedelta, timezone
from typing import Callable

from bleak import BleakClient
from bleak_retry_connector import establish_connection as bleak_establish

from homeassistant.components.bluetooth import (
    async_last_service_info,
    async_scanner_by_source,
    async_scanner_devices_by_address,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval

from packaging.version import Version

from .const import BRIDGE_PIPELINED_READS_VERSION, CHAR_SERVICE_MAP
from .exceptions import TransportError

_LOGGER = logging.getLogger(__name__)
_RAW_LOGGER = logging.getLogger(__name__ + ".raw")
_RAW_LOGGER.setLevel(logging.WARNING)  # silent unless explicitly enabled
TRACE = 5

ESP_EVENT_NAME = "esphome.philips_sonicare_ble_data"


def _scanner_name_by_source(hass: HomeAssistant, source: str) -> str | None:
    """Look up a scanner by source MAC — works for ESPHome proxies."""
    try:
        scanner = async_scanner_by_source(hass, source)
        if scanner is not None:
            return getattr(scanner, "name", None)
    except Exception:  # noqa: BLE001
        return None
    return None


def _host_scanner_name_by_adapter(
    hass: HomeAssistant, address: str, adapter_id: str
) -> str | None:
    """Find the HA Host scanner bound to a BlueZ adapter (e.g. "hci0").

    The HA scanner registry keys Host scanners by their BT-MAC, not by the
    adapter name, so we look the scanner up indirectly via the devices it can
    see and match on the `adapter` attribute.
    """
    try:
        for sd in async_scanner_devices_by_address(hass, address, connectable=True):
            if getattr(sd.scanner, "adapter", None) == adapter_id:
                return getattr(sd.scanner, "name", None)
    except Exception:  # noqa: BLE001
        return None
    return None


def describe_connection_path(
    hass: HomeAssistant, client: BleakClient, device
) -> str:
    """Return the adapter label used for a BleakClient connection.

    Matches the name shown in Settings -> Devices -> Bluetooth. Uses private
    Bleak attributes to identify the backend; any upstream rename falls back
    to a best-effort label instead of breaking the connect flow.
    """
    try:
        # Primary: habluetooth's HaBleakClientWrapper sets _connected_scanner
        # on the client after a successful connect. This is the scanner that
        # actually carried the connection — more reliable than backend
        # introspection, and works for both host (BlueZ) and remote (ESPHome)
        # scanners without branching on backend type.
        connected_scanner = getattr(client, "_connected_scanner", None)
        if connected_scanner is not None:
            name = getattr(connected_scanner, "name", None)
            if name:
                return name
            source = getattr(connected_scanner, "source", None)
            if source:
                return source

        backend = getattr(client, "_backend", None)
        if backend is None:
            return "unknown"
        mod = type(backend).__module__ or ""
        address = getattr(device, "address", None) or "?"

        if "bluezdbus" in mod:
            adapter_id = "?"
            # Primary: live _device_info from the BlueZ backend
            try:
                info = getattr(backend, "_device_info", None)
                if info and "Adapter" in info:
                    adapter_id = info["Adapter"].rsplit("/", 1)[-1]
            except Exception:  # noqa: BLE001
                pass
            # Fallback 1: BLEDevice.details["path"] -> /org/bluez/hciN/dev_...
            if adapter_id == "?":
                try:
                    details = getattr(device, "details", None)
                    path = details.get("path") if isinstance(details, dict) else None
                    if isinstance(path, str) and path.startswith("/org/bluez/"):
                        adapter_id = path.split("/")[3]
                except Exception:  # noqa: BLE001
                    pass
            # Fallback 2: _adapter attr set in BleakClient constructor
            if adapter_id == "?":
                try:
                    adapter_attr = getattr(backend, "_adapter", None)
                    if isinstance(adapter_attr, str) and adapter_attr:
                        adapter_id = adapter_attr
                except Exception:  # noqa: BLE001
                    pass
            name = _host_scanner_name_by_adapter(hass, address, adapter_id)
            return name or adapter_id

        if "esphome" in mod:
            source = "?"
            try:
                details = getattr(device, "details", None)
                if isinstance(details, dict):
                    source = details.get("source") or "?"
            except Exception:  # noqa: BLE001
                pass
            name = _scanner_name_by_source(hass, source) if source != "?" else None
            return name or source

        return type(backend).__name__
    except Exception as err:  # noqa: BLE001
        return f"unknown ({err})"


def is_local_bluez_connection(client: BleakClient) -> bool:
    """Whether a BleakClient connection is carried by the local BlueZ stack.

    Bond state is per-controller: a bond in BlueZ says nothing about an
    ESPHome proxy's NVS and vice versa. habluetooth routes connects by
    RSSI, so even a "Direct BLE" probe may ride a remote scanner — any
    conclusion drawn from an auth error about the *BlueZ* bond is only
    valid when the connection actually went through BlueZ.
    """
    try:
        backend = getattr(client, "_backend", None)
        if backend is None:
            return False
        return "bluezdbus" in (type(backend).__module__ or "")
    except Exception:  # noqa: BLE001
        return False
ESP_STATUS_EVENT_NAME = "esphome.philips_sonicare_ble_status"
ESP_SERVICES_EVENT_NAME = "esphome.philips_sonicare_ble_services"
ESP_READ_TIMEOUT = 5.0
# Base budget for a pipelined poll batch: covers one bridge-side ATT
# watchdog stall (10 s) with margin; per-read time is added on top.
BATCH_READ_TIMEOUT_BASE = 15.0
ESP_HEARTBEAT_TIMEOUT = 45.0

# Return values of async_unpair_bridge_slot.
UNPAIR_OK = "unpaired"
UNPAIR_UNCONFIRMED = "unconfirmed"
UNPAIR_UNAVAILABLE = "unavailable"
UNPAIR_FAILED = "failed"


async def async_unpair_bridge_slot(
    hass: HomeAssistant,
    esp_device_name: str,
    bridge_id: str,
    timeout: float = 4.0,
) -> str:
    """Clear a bridge slot's bond and wait for the bridge to confirm.

    Fires the slot's ``ble_unpair`` ESPHome service, then waits for the
    bridge's ``unpaired`` status event (deferred ~2 s on v1.3.2+ so the
    BLE stack can settle). Shared by the config-flow reset step and entry
    removal so both treat a silent failure the same way.

    Returns one of ``UNPAIR_OK`` (bridge confirmed), ``UNPAIR_UNCONFIRMED``
    (call succeeded but no event within ``timeout`` — the bridge may have
    wedged), ``UNPAIR_UNAVAILABLE`` (service missing, bridge offline), or
    ``UNPAIR_FAILED`` (the service call raised).
    """
    svc_name = f"{esp_device_name}_ble_unpair"
    if bridge_id:
        svc_name += f"_{bridge_id}"

    if not hass.services.has_service("esphome", svc_name):
        return UNPAIR_UNAVAILABLE

    unpair_done = asyncio.Event()

    @callback
    def _on_status(event: Event) -> None:
        data = event.data
        if data.get("status") != "unpaired":
            return
        # bridge_id compared case-insensitively (HA lowercases service names)
        if data.get("bridge_id", "").lower() != bridge_id.lower():
            return
        unpair_done.set()

    unsub = hass.bus.async_listen(ESP_STATUS_EVENT_NAME, _on_status)
    try:
        try:
            await hass.services.async_call(
                "esphome", svc_name, {}, blocking=True
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("ble_unpair on %s failed: %s", esp_device_name, err)
            return UNPAIR_FAILED

        try:
            await asyncio.wait_for(unpair_done.wait(), timeout=timeout)
            return UNPAIR_OK
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "ble_unpair on %s did not confirm within %.0fs",
                esp_device_name, timeout,
            )
            return UNPAIR_UNCONFIRMED
    finally:
        unsub()


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

    @property
    def connection_path(self) -> str | None:
        """Label of the adapter/bridge currently carrying the connection."""
        return None

    @property
    def connection_rssi(self) -> int | None:
        """RSSI seen by the scanner currently carrying the connection.

        Distinct from ``async_last_service_info`` which returns the RSSI from
        whichever scanner has the freshest advertisement — that scanner may
        differ from the one serving the active link when multiple scanners
        see the device with different RSSI.
        """
        return None

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

    @property
    def auto_tx_ack(self) -> bool:
        """Whether the transport itself echoes Condor TX_ACK on e50b0003 notifies.

        Default False — direct-BLE keeps acking from the HA event loop, since
        BlueZ ↔ adapter latency is sub-millisecond and well within the brush's
        ~250 ms patience window. Overridden by EspBridgeTransport when the
        ESP bridge handles auto-ack itself (v1.6.0+).
        """
        return False

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
        self._connection_path: str | None = None
        self._connected_scanner = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def connection_path(self) -> str | None:
        return self._connection_path if self.is_connected else None

    @property
    def connection_rssi(self) -> int | None:
        if not self.is_connected or self._connected_scanner is None:
            return None
        try:
            result = self._connected_scanner.get_discovered_device_advertisement_data(
                self._address
            )
        except Exception:  # noqa: BLE001
            return None
        if not result:
            return None
        _device, adv = result
        rssi = getattr(adv, "rssi", None)
        if rssi is None or rssi <= -127:
            return None
        return int(rssi)

    async def connect(self) -> None:
        service_info = async_last_service_info(self._hass, self._address)
        if not service_info:
            raise TransportError(f"Device {self._address} not in range")

        def _on_disconnect(_client):
            _LOGGER.info("%s: connection lost", self._address)
            self._client = None
            self._connection_path = None
            self._connected_scanner = None
            if self._disconnect_cb:
                self._disconnect_cb()

        self._client = await bleak_establish(
            BleakClient,
            service_info.device,
            "philips_sonicare",
            disconnected_callback=_on_disconnect,
            timeout=30.0,
        )
        self._connected_scanner = getattr(self._client, "_connected_scanner", None)
        self._connection_path = describe_connection_path(
            self._hass, self._client, service_info.device
        )
        _LOGGER.info("%s: connected via %s", self._address, self._connection_path)

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
            if _RAW_LOGGER.isEnabledFor(logging.DEBUG):
                _RAW_LOGGER.debug(
                    "%s: notify %s %s",
                    self._address,
                    char_uuid,
                    data.hex() if data else "",
                )
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
        pipelined_reads_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._hass = hass
        self._address = address
        # User opt-out from the options flow. A callable, not a bool —
        # Sonicare options apply live without an entry reload, so the
        # toggle is evaluated per poll batch.
        self._pipelined_reads_enabled = pipelined_reads_enabled or (lambda: True)
        self._device_name = esphome_device_name
        # HA's ServiceRegistry lowercases service names, so the bridge_id suffix
        # is always lowercase on the wire — canonicalize here so service-name
        # building and bridge_id event filtering stay consistent.
        self._esp_bridge_id = (esp_bridge_id or "").lower()
        self._setup_done = False
        self._device_connected = False
        self._esp_alive = False
        self._last_heartbeat: float = 0.0
        self._disconnect_cb: Callable[[], None] | None = None
        self._event_unsub: Callable | None = None
        self._status_unsub: Callable | None = None
        self._heartbeat_check_unsub: Callable | None = None
        # Waiters per characteristic. A list, not a single slot: with the
        # pipelined poll cycle a concurrent entity/service read of the
        # same uuid must not clobber the poll's future (both waiters get
        # resolved by the one bridge reply).
        self._pending_reads: dict[str, list[asyncio.Future[bytes | None]]] = {}
        # True once the bridge reported a firmware that serialises
        # overlapping GATT reads (>= BRIDGE_PIPELINED_READS_VERSION).
        # Computed once per version report, not per poll.
        self._pipelined_reads = False
        self._last_read_errors: dict[str, str] = {}
        self._notify_callbacks: dict[str, Callable[[str, bytes], None]] = {}
        self._detected_mac: str | None = address if ":" in address else None
        self._bridge_version: str | None = None
        self._pending_info: asyncio.Future[dict[str, str]] | None = None
        self._ble_paired: str | None = None
        self._needs_resubscribe = False
        self._ready_event = asyncio.Event()
        # Counts "disconnected" status events. The coordinator compares
        # this against the count it saw when live setup ran to detect
        # reconnects it never observed: a brush can drop the link and be
        # back within well under the monitor loop's poll interval, so
        # is_connected never reads False from the loop's perspective and
        # the fresh read batch never runs. "disconnected" fires exactly
        # once per real disconnect — unlike "ready", which the bridge
        # heartbeat re-fires while no subscriptions exist and which would
        # oscillate with a teardown/re-setup cycle.
        self._disconnect_count = 0
        self._last_uptime: int | None = None
        self._boot_time: datetime | None = None

    @property
    def connection_path(self) -> str | None:
        return self._device_name if self._esp_alive else None

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
        for futures in self._pending_reads.values():
            for future in futures:
                if not future.done():
                    future.set_result(None)
        self._pending_reads.clear()

    def _resolve_pending_reads(self, uuid: str, result: bytes | None) -> bool:
        """Resolve every waiter registered for ``uuid``.

        Returns True if at least one waiter was resolved.
        """
        futures = self._pending_reads.pop(uuid, None)
        if not futures:
            return False
        for future in futures:
            if not future.done():
                future.set_result(result)
        return True

    def _discard_pending_read(
        self, uuid: str, future: asyncio.Future[bytes | None]
    ) -> None:
        """Remove one waiter without touching others for the same uuid."""
        futures = self._pending_reads.get(uuid)
        if not futures:
            return
        try:
            futures.remove(future)
        except ValueError:
            pass
        if not futures:
            self._pending_reads.pop(uuid, None)

    @property
    def detected_mac(self) -> str | None:
        return self._detected_mac

    @property
    def bridge_version(self) -> str | None:
        return self._bridge_version

    @property
    def auto_tx_ack(self) -> bool:
        # Bridge v1.6.0+ echoes the seq from e50b0003 → e50b0004 on its own BLE
        # thread (~10 ms vs. 30-300 ms via Wi-Fi+HA), which keeps us inside the
        # brush's ~250 ms TX_ACK window during sustained brushing sessions.
        # Older bridges don't, so HA must keep sending the ack itself.
        if not self._bridge_version:
            return False
        try:
            return Version(self._bridge_version) >= Version("1.6.0")
        except Exception:
            return False

    @property
    def bridge_boot_time(self) -> datetime | None:
        """Return the ESP bridge boot timestamp.

        Computed from uptime on first sighting and refreshed only on
        detected restart (uptime regression) — stable during runtime.
        """
        return self._boot_time

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
    def disconnect_count(self) -> int:
        """Number of BLE "disconnected" status events seen so far."""
        return self._disconnect_count

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
                self._resolve_pending_reads(uuid, None)
                return

            if not uuid:
                return

            if not payload_hex:
                # A successful read of an empty value — some models report
                # blank Hardware/Software Revision strings (0-byte reads,
                # no error). Resolve the waiters instead of dropping the
                # event, which would leave them running into the batch
                # timeout (observed: 3 empty chars stalled a 3 s poll
                # batch to the full 48 s ceiling).
                self._resolve_pending_reads(uuid, None)
                return

            if mac and not self._detected_mac:
                self._detected_mac = mac

            try:
                payload = bytes.fromhex(payload_hex)
            except ValueError:
                return

            self._resolve_pending_reads(uuid, payload)

            if uuid in self._notify_callbacks:
                if _RAW_LOGGER.isEnabledFor(logging.DEBUG):
                    _RAW_LOGGER.debug(
                        "%s: notify %s %s",
                        self._address,
                        uuid,
                        payload.hex() if payload else "",
                    )
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
                # Defensive: normalise stray surrounding quotes/whitespace so a
                # firmware that reports e.g. '"1.6.1"' still parses as 1.6.1.
                if isinstance(version, str):
                    version = version.strip().strip("\"'").strip()
                if version != self._bridge_version:
                    self._bridge_version = version
                    try:
                        self._pipelined_reads = Version(version) >= Version(
                            BRIDGE_PIPELINED_READS_VERSION
                        )
                    except Exception:  # noqa: BLE001 — unparseable (dev build)
                        self._pipelined_reads = False

            self._last_heartbeat = time.monotonic()
            was_alive = self._esp_alive
            was_connected = self._device_connected

            if not self._esp_alive:
                self._esp_alive = True

            # Detect ESP restart via uptime regression.  After reboot the
            # bridge loses all BLE subscriptions, but HA's notify_callbacks
            # still hold stale entries.  Clear them so the "ready" handler
            # below flags a resubscribe.  Fires on info/heartbeat/ready
            # events (all include uptime_s since ESP v1.2.3).
            uptime_str = event.data.get("uptime_s")
            if uptime_str is not None:
                try:
                    new_uptime = int(uptime_str)
                    is_restart = (
                        self._last_uptime is not None
                        and new_uptime < self._last_uptime
                    )
                    if is_restart:
                        _LOGGER.info(
                            "ESP bridge restarted (uptime %ds → %ds) — "
                            "clearing stale subscriptions",
                            self._last_uptime, new_uptime,
                        )
                        self._notify_callbacks.clear()
                        self._needs_resubscribe = True
                    # Set boot_time on first sighting and on every restart —
                    # keeps the timestamp stable during normal runtime.
                    if is_restart or self._boot_time is None:
                        self._boot_time = datetime.now(timezone.utc) - timedelta(
                            seconds=new_uptime
                        )
                    self._last_uptime = new_uptime
                except ValueError:
                    pass

            if status == "info":
                # Filter by bridge_id if present (multi-device ESP).
                # Lowercase to match the canonicalized self._esp_bridge_id.
                event_bridge_id = event.data.get("bridge_id", "").lower()
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
                self._ready_event.set()
                # Only flag resubscribe if HA has no active subscriptions.
                # The bridge re-fires "ready" on every heartbeat when it
                # has no subscriptions — ignore if HA already subscribed.
                if not self._notify_callbacks:
                    self._needs_resubscribe = True
            elif status == "connected":
                pass  # GATT discovery still in progress

            elif status == "disconnected":
                self._device_connected = False
                self._disconnect_count += 1
                self._cancel_pending_reads()

            # Fire callback when any component of state changed
            if self._disconnect_cb and (
                was_alive != self._esp_alive
                or was_connected != self._device_connected
            ):
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
        self._ready_event.clear()
        _LOGGER.debug("%s: Waiting for ESP bridge ready event...", self._address)
        # Trigger immediate info event instead of waiting for next heartbeat
        try:
            await self._hass.services.async_call(
                "esphome", self._svc_name("ble_get_info"), {}, blocking=True,
            )
        except Exception:
            pass
        # Wait up to 10s for ESP to report alive
        for _ in range(10):
            await asyncio.sleep(1)
            if self._esp_alive:
                break
        if not self._esp_alive:
            raise TransportError("ESP bridge did not respond within 10s")
        if self.is_connected:
            _LOGGER.info(
                "ESP bridge ready (mac=%s, version=%s)",
                self._detected_mac,
                self._bridge_version,
            )
            return
        # ESP alive but device not connected — wait for "ready" event
        _LOGGER.debug(
            "%s: ESP bridge alive, waiting for BLE device to connect...",
            self._address,
        )
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            if self.is_connected:
                _LOGGER.info("ESP bridge ready after timeout (mac=%s)", self._detected_mac)
                return
            _LOGGER.debug("ESP bridge alive but BLE device not connected (yet)")
            return
        _LOGGER.info(
            "ESP bridge ready (mac=%s, version=%s)",
            self._detected_mac,
            self._bridge_version,
        )

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
        self._ready_event.clear()
        self._pending_reads.clear()
        self._notify_callbacks.clear()

    def pop_read_error(self, char_uuid: str) -> str | None:
        return self._last_read_errors.pop(char_uuid, None)

    async def read_char(
        self, char_uuid: str, timeout: float = ESP_READ_TIMEOUT
    ) -> bytes | None:
        if not self._setup_done:
            return None
        service_uuid = self._get_service_uuid(char_uuid)
        self._last_read_errors.pop(char_uuid, None)
        future: asyncio.Future[bytes | None] = self._hass.loop.create_future()
        self._pending_reads.setdefault(char_uuid, []).append(future)

        try:
            await self._hass.services.async_call(
                "esphome",
                self._svc_name("ble_read_char"),
                {"service_uuid": service_uuid, "char_uuid": char_uuid},
                blocking=True,
            )
        except HomeAssistantError as err:
            self._discard_pending_read(char_uuid, future)
            _LOGGER.debug("Service call failed for %s: %s", char_uuid, err)
            return None

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._discard_pending_read(char_uuid, future)
            _LOGGER.debug("Read timeout for %s (other reads continue)", char_uuid)
            return None

    async def read_chars(self, char_uuids: list[str]) -> dict[str, bytes | None]:
        if not self._setup_done:
            await self.connect()
        if not char_uuids:
            return {}
        if not self.is_connected:
            return {u: None for u in char_uuids}

        if self._pipelined_reads and self._pipelined_reads_enabled():
            # Fire all reads at once — the bridge serialises GATT ops via its
            # pending-calls queue (drained one at a time on each completion),
            # so we get back-to-back BLE reads without HA-loop overhead
            # between them. read_char never raises (returns None on failure)
            # so a bare gather is safe.
            #
            # The timeout must cover the whole queue, not one read: with N
            # reads queued behind each other, the last one legitimately waits
            # N × read-time. Budget for one bridge-side ATT watchdog stall
            # (10 s) plus a slow connection interval — bridge error events
            # (read_timeout, queue_full, not_found) still resolve futures
            # early, so a failure-free ceiling costs nothing in wall-clock.
            batch_timeout = BATCH_READ_TIMEOUT_BASE + 1.0 * len(char_uuids)
            started = time.monotonic()
            offsets: dict[str, float] = {}

            async def _timed_read(u: str) -> bytes | None:
                value = await self.read_char(u, timeout=batch_timeout)
                offsets[u] = time.monotonic() - started
                return value

            results = await asyncio.gather(
                *(_timed_read(u) for u in char_uuids)
            )
            batch = dict(zip(char_uuids, results))
            self._log_batch_timing("pipelined", batch, started)
            # Completion timeline: uniform ~x-ms gaps mean the link paces the
            # queue (connection interval); a burst at the end means delivery
            # stalls elsewhere.
            if _LOGGER.isEnabledFor(logging.DEBUG):
                timeline = " ".join(
                    f"{u[:8].lstrip('0') or '0'}={t:.2f}"
                    for u, t in sorted(offsets.items(), key=lambda kv: kv[1])
                )
                _LOGGER.debug(
                    "Read batch timeline for %s [%s]: %s",
                    self._address,
                    self._esp_bridge_id or self._device_name,
                    timeline,
                )
            return batch

        # Serial fallback for bridges without the single-ATT-op scheduler
        # (or with the pipelined-reads option switched off).
        started = time.monotonic()
        sequential: dict[str, bytes | None] = {}
        for uuid in char_uuids:
            sequential[uuid] = await self.read_char(uuid)
        self._log_batch_timing("sequential", sequential, started)
        return sequential

    def _log_batch_timing(
        self, mode: str, results: dict[str, bytes | None], started: float
    ) -> None:
        """One line per poll batch so pipelined vs. sequential is comparable."""
        elapsed = time.monotonic() - started
        ok = sum(1 for v in results.values() if v is not None)
        slot = self._esp_bridge_id or self._device_name
        _LOGGER.info(
            "Read batch (%s) for %s [%s]: %d/%d chars in %.2f s (bridge %s)",
            mode,
            self._address,
            slot,
            ok,
            len(results),
            elapsed,
            self._bridge_version or "unknown",
        )
        failed = [u for u, v in results.items() if v is None]
        if failed:
            _LOGGER.debug(
                "Read batch for %s [%s]: no data for %s",
                self._address,
                slot,
                ", ".join(failed),
            )

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

    @staticmethod
    def _canonical_uuid(uuid: str) -> str:
        """Return a UUID in canonical 128-bit lowercase form.

        The bridge emits short-form UUIDs for assigned-number services
        (``"0x180F"`` for Battery, ``"0x180A"`` for Device Information)
        and full 128-bit strings for custom services. Our constants are
        always 128-bit lowercase, so string equality against the
        ``SVC_*`` values needs the short forms expanded to the standard
        Bluetooth Base UUID before comparing.
        """
        u = uuid.strip().lower()
        if u.startswith("0x"):
            u = u[2:]
        if len(u) == 4:
            return f"0000{u}-0000-1000-8000-00805f9b34fb"
        if len(u) == 8:
            return f"{u}-0000-1000-8000-00805f9b34fb"
        return u

    async def list_services(self, timeout: float = 5.0) -> list[str]:
        """Enumerate the connected device's GATT services via the bridge.

        Calls ``ble_list_services`` and collects the resulting per-service
        events (one ``esphome.philips_sonicare_ble_services`` event per
        service, keyed by ``service_index`` / ``service_count``).

        Returns the discovered service UUIDs in discovery order, each
        normalised to the canonical 128-bit lowercase form so callers can
        compare directly against ``SVC_*`` constants. Returns an empty
        list on timeout or service-call failure — callers should treat
        that as "couldn't determine" rather than "no services present",
        since older bridges and disconnected sessions both look the same
        from here.
        """
        if not self.is_connected:
            return []

        loop = self._hass.loop
        result_future: asyncio.Future[list[str]] = loop.create_future()
        collected: dict[int, str] = {}
        expected_count: int | None = None

        @callback
        def _handler(event: Event) -> None:
            nonlocal expected_count
            data = event.data
            mac = data.get("mac", "")
            if (
                mac
                and self._detected_mac
                and mac.upper() != self._detected_mac.upper()
            ):
                return
            try:
                count = int(data.get("service_count", "0"))
                index = int(data.get("service_index", "0"))
            except (TypeError, ValueError):
                return
            uuid = data.get("service_uuid") or ""
            if uuid:
                collected[index] = self._canonical_uuid(uuid)
            if expected_count is None:
                expected_count = count
            if (
                expected_count
                and len(collected) >= expected_count
                and not result_future.done()
            ):
                ordered = [collected[i] for i in sorted(collected)]
                result_future.set_result(ordered)
            elif expected_count == 0 and not result_future.done():
                result_future.set_result([])

        unsub = self._hass.bus.async_listen(ESP_SERVICES_EVENT_NAME, _handler)
        try:
            await self._hass.services.async_call(
                "esphome",
                self._svc_name("ble_list_services"),
                {},
                blocking=True,
            )
            return await asyncio.wait_for(result_future, timeout=timeout)
        except (HomeAssistantError, asyncio.TimeoutError) as err:
            _LOGGER.debug("ESP list_services failed: %s", err)
            return []
        finally:
            unsub()

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

    async def set_pair_mode(self, enabled: bool, timeout_s: int = 60) -> None:
        """Arm (enabled=True) or cancel (enabled=False) pair-mode on the bridge.

        Mode B only — Mode A bridges accept the call but have nothing to do.
        On enable, the bridge will scan via service-UUID and pair the first
        Sonicare it finds, then emit a `pair_complete` status event with the
        identity_address. On timeout: `pair_timeout`. Events are filtered by
        bridge_id on the HA side.
        """
        if not self._setup_done:
            raise TransportError("Not connected")
        await self._hass.services.async_call(
            "esphome",
            self._svc_name("ble_pair_mode"),
            {"enabled": enabled, "timeout_s": str(timeout_s)},
            blocking=True,
        )

    async def request_unpair(self) -> None:
        """Remove the BLE bond and clear the persisted identity on the bridge.

        Bridge ends up with `pair_capable=true` again so the user can re-pair
        another Sonicare.
        """
        if not self._setup_done:
            raise TransportError("Not connected")
        await self._hass.services.async_call(
            "esphome",
            self._svc_name("ble_unpair"),
            {},
            blocking=True,
        )

    def set_disconnect_callback(self, cb: Callable[[], None]) -> None:
        self._disconnect_cb = cb
