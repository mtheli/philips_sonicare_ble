# custom_components/philips_sonicare/coordinator.py
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
    async_register_callback,
)

try:
    from dbus_fast.aio import MessageBus
    from dbus_fast import BusType, Message, MessageType
    HAS_DBUS_FAST = True
except ImportError:
    HAS_DBUS_FAST = False

from .transport import BleakTransport, EspBridgeTransport, SonicareTransport
from .exceptions import TransportError
from .const import (
    DOMAIN,
    SVC_CONDOR,
    CHAR_BATTERY_LEVEL,
    CHAR_MODEL_NUMBER,
    CHAR_SERIAL_NUMBER,
    CHAR_FIRMWARE_REVISION,
    CHAR_HARDWARE_REVISION,
    CHAR_SOFTWARE_REVISION,
    CHAR_MANUFACTURER_NAME,
    CHAR_HANDLE_STATE,
    CHAR_AVAILABLE_ROUTINE_IDS,
    CHAR_MOTOR_RUNTIME,
    CHAR_SESSION_ID,
    CHAR_BRUSHING_MODE,
    CHAR_BRUSHING_STATE,
    CHAR_BRUSHING_TIME,
    CHAR_ROUTINE_LENGTH,
    CHAR_INTENSITY,
    CHAR_LATEST_SESSION_ID,
    CHAR_SESSION_COUNT,
    CHAR_BRUSHHEAD_SERIAL,
    CHAR_BRUSHHEAD_DATE,
    CHAR_BRUSHHEAD_LIFETIME_LIMIT,
    CHAR_BRUSHHEAD_LIFETIME_USAGE,
    CHAR_BRUSHHEAD_NFC_VERSION,
    CHAR_BRUSHHEAD_TYPE,
    CHAR_BRUSHHEAD_PAYLOAD,
    CHAR_BRUSHHEAD_RING_ID,
    CHAR_ERROR_PERSISTENT,
    CHAR_ERROR_VOLATILE,
    CHAR_HANDLE_TIME,
    SENSOR_ENABLE_PRESSURE,
    SENSOR_ENABLE_TEMPERATURE,
    SENSOR_ENABLE_GYROSCOPE,
    CONF_SENSOR_PRESSURE,
    CONF_SENSOR_TEMPERATURE,
    CONF_SENSOR_GYROSCOPE,
    DEFAULT_SENSOR_PRESSURE,
    DEFAULT_SENSOR_TEMPERATURE,
    DEFAULT_SENSOR_GYROSCOPE,
    supports_mode_write,
    CHAR_SERVICE_MAP,
    NOTIFICATION_CHARS,
    POLL_READ_CHARS,
    LIVE_READ_CHARS,
    CONF_ADDRESS,
    CONF_TRANSPORT_TYPE,
    CONF_SERVICES,
    TRANSPORT_ESP_BRIDGE,
    MIN_BRIDGE_VERSION,
    CONF_NOTIFY_THROTTLE,
    DEFAULT_NOTIFY_THROTTLE,
)

_LOGGER = logging.getLogger(__name__)
_RAW_LOGGER = logging.getLogger(__name__ + ".raw")
_RAW_LOGGER.setLevel(logging.WARNING)  # silent unless explicitly enabled


class PhilipsSonicareCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Data update coordinator for Philips Sonicare."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        transport: SonicareTransport,
    ) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        self.address = entry.data.get("address", "unknown")
        self.transport = transport
        self._is_esp_bridge = (
            entry.data.get(CONF_TRANSPORT_TYPE) == TRANSPORT_ESP_BRIDGE
        )

        # Protocol selection — Condor (framed, push-based, HX742X+) when
        # its transport service is in the discovered set, Legacy (direct
        # GATT-per-property) otherwise. The two protocols produce the
        # same ``coordinator.data`` shape through their respective
        # adapters, so entity code stays protocol-agnostic.
        discovered = {s.lower() for s in entry.data.get(CONF_SERVICES, [])}
        self._use_condor = SVC_CONDOR.lower() in discovered
        if self._use_condor:
            from .condor_protocol import CondorProtocol
            self._protocol = CondorProtocol(transport)
        else:
            from .legacy_protocol import LegacyProtocol
            self._protocol = LegacyProtocol(transport)

        # Read options
        options = entry.options
        self._poll_chars = list(POLL_READ_CHARS)
        self._live_chars = list(LIVE_READ_CHARS)
        self._notify_chars = list(NOTIFICATION_CHARS)

        # Filter by service availability (from setup discovery)
        services = {s.lower() for s in entry.data.get(CONF_SERVICES, [])}
        for char, svc in CHAR_SERVICE_MAP.items():
            if svc.lower() not in services:
                if char in self._poll_chars:
                    self._poll_chars.remove(char)
                if char in self._live_chars:
                    self._live_chars.remove(char)
                if char in self._notify_chars:
                    self._notify_chars.remove(char)

        # Remove 0x4022 for models without mode write — they use 0x4080 for mode
        model = entry.data.get("model", "")
        if not supports_mode_write(model):
            for charlist in (self._poll_chars, self._live_chars, self._notify_chars):
                if CHAR_AVAILABLE_ROUTINE_IDS in charlist:
                    charlist.remove(CHAR_AVAILABLE_ROUTINE_IDS)

        # Kids devices (HX63xx) have fewer chars within available services
        if model.upper().startswith("HX63"):
            for char in (CHAR_AVAILABLE_ROUTINE_IDS, CHAR_BRUSHING_STATE):
                if char in self._poll_chars:
                    self._poll_chars.remove(char)
                if char in self._live_chars:
                    self._live_chars.remove(char)
                if char in self._notify_chars:
                    self._notify_chars.remove(char)
            # Session ID exists but doesn't support notify on Kids firmware
            if CHAR_SESSION_ID in self._notify_chars:
                self._notify_chars.remove(CHAR_SESSION_ID)

        self._connection_lock = asyncio.Lock()
        self._live_task: asyncio.Task | None = None
        self._live_setup_done = False
        self._full_read_done = False
        self._unsub_adv_debug = None
        self._dbus_bus: MessageBus | None = None
        self._wake_event = asyncio.Event()
        self._sensor_subscribed = False
        self._brushhead_read_pending = False
        self._live_cb: Callable | None = None

        _LOGGER.debug(
            "Initializing coordinator for %s (transport: %s)",
            self.address,
            "ESP" if self._is_esp_bridge else "Direct BLE",
        )
        # Event-driven: no polling. Connect on ADV/D-Bus (Direct BLE)
        # or ESP "ready" event (ESP bridge).
        super().__init__(
            hass,
            _LOGGER,
            name=f"Philips Sonicare {self.address}",
            update_interval=None,
        )

        # Initial empty dataset
        self.data = {
            "battery": None,
            "firmware": None,
            "hardware_revision": None,
            "software_revision": None,
            "model_number": None,
            "serial_number": None,
            "manufacturer_name": None,
            "available_mode_ids": None,
            "selected_mode": None,
            "handle_state": "off",
            "handle_state_value": None,
            "brushing_mode": None,
            "brushing_mode_value": None,
            "brushing_state": None,
            "brushing_state_value": None,
            "intensity": None,
            "intensity_value": None,
            "brushing_time": None,
            "routine_length": None,
            "session_id": None,
            "latest_session_id": None,
            "session_count": None,
            "motor_runtime": None,
            "brushhead_lifetime_limit": None,
            "brushhead_lifetime_usage": None,
            "brushhead_wear_pct": None,
            "brushhead_serial": None,
            "brushhead_date": None,
            "brushhead_nfc_version": None,
            "brushhead_type": None,
            "brushhead_payload": None,
            "brushhead_ring_id": None,
            "error_persistent": None,
            "error_volatile": None,
            "pressure": None,
            "pressure_alarm": None,
            "pressure_state": None,
            "temperature": None,
            "handle_time": None,
            "last_seen": None,
        }

    @property
    def supports_writes(self) -> bool:
        """True when the active protocol exposes mode / intensity / settings writes.

        Condor's ``PutProps`` lands in a later phase — until then the
        write-capable select and switch entities stay hidden so a user
        can't trigger a ``NotImplementedError`` from the UI.
        """
        return not self._use_condor

    async def async_start(self) -> None:
        """Start live monitoring. Call after setup is complete."""
        if not self._is_esp_bridge:
            self._start_advertisement_callback()
            await self._start_dbus_rssi_listener()
        self._live_task = self.entry.async_create_background_task(
            self.hass, self._start_live_monitoring(), "philips_sonicare_monitoring"
        )

    def _handle_wake(self) -> None:
        """Handle device wake — set activity to initializing and trigger connect."""
        if not self.transport.is_connected and self.data:
            self.data["_connecting"] = True
            self.async_set_updated_data(self.data)
        self._wake_event.set()

    def _start_advertisement_callback(self) -> None:
        """Register HA bluetooth callback for advertisement detection.

        Note: habluetooth filters identical advertisements, so this only fires
        when advertisement DATA changes (manufacturer_data, service_data,
        service_uuids, or name). For devices like the Sonicare that send
        static data, the D-Bus RSSI listener provides the fallback.
        """

        @callback
        def _advertisement_callback(service_info, change):
            # Ignore stale/cached history data (fires on registration)
            if service_info.rssi is not None and service_info.rssi <= -127:
                _LOGGER.debug("ADV ignored (stale RSSI %s)", service_info.rssi)
                return
            if not self.transport.is_connected:
                _LOGGER.info(
                    "Wake via ADV: %s | RSSI: %s dBm",
                    service_info.address,
                    service_info.rssi,
                )
            else:
                _LOGGER.debug(
                    "ADV while connected: %s | RSSI: %s dBm",
                    service_info.address,
                    service_info.rssi,
                )
            self._handle_wake()

        self._unsub_adv_debug = async_register_callback(
            self.hass,
            _advertisement_callback,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.ACTIVE,
        )

    async def _start_dbus_rssi_listener(self) -> None:
        """Listen for BlueZ D-Bus RSSI changes to detect device advertisements.

        habluetooth deduplicates advertisements with identical data
        (home-assistant/core#141662). Devices like the Sonicare that send
        unchanged ADV content never trigger HA callbacks after first discovery.

        RSSI changes with every advertisement packet due to signal fluctuation,
        so BlueZ emits PropertiesChanged even when the ADV payload is identical.
        This listener catches those RSSI updates as a wake signal.
        """
        if not HAS_DBUS_FAST:
            _LOGGER.debug("dbus-fast not available — D-Bus RSSI listener disabled")
            return

        try:
            self._dbus_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as err:
            _LOGGER.warning("D-Bus not available for RSSI wake detection: %s", err)
            return

        # Find the device path dynamically (supports hci0, hci1, etc.)
        from .dbus_pairing import _find_device_path
        device_path = await _find_device_path(self._dbus_bus, self.address)
        if not device_path:
            mac_path = self.address.upper().replace(":", "_")
            device_path = f"/org/bluez/hci0/dev_{mac_path}"
            _LOGGER.debug("Device not in BlueZ ObjectManager, using default path: %s", device_path)

        def _on_message(msg: Message) -> None:
            if msg.message_type != MessageType.SIGNAL:
                return
            if msg.member != "PropertiesChanged":
                return
            if msg.path != device_path:
                return
            body = msg.body
            if len(body) >= 2 and "RSSI" in body[1]:
                rssi = body[1]["RSSI"].value
                if not self.transport.is_connected:
                    _LOGGER.info("Wake via D-Bus RSSI: %s from %s", rssi, self.address)
                else:
                    _LOGGER.debug("D-Bus RSSI while connected: %s from %s", rssi, self.address)
                self._handle_wake()

        self._dbus_bus.add_message_handler(_on_message)

        await self._dbus_bus.call(Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            member="AddMatch",
            signature="s",
            body=[
                f"type='signal',"
                f"interface='org.freedesktop.DBus.Properties',"
                f"member='PropertiesChanged',"
                f"path='{device_path}'"
            ],
        ))

        _LOGGER.info(
            "D-Bus RSSI listener active for %s (%s)",
            self.address, device_path,
        )

    # ------------------------------------------------------------------
    # Called automatically by the coordinator (polling — ESP bridge only)
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data via polling (ESP bridge) or return cached data (Direct BLE)."""

        # No polling — all data comes from live monitoring
        return self.data or {}

    # ------------------------------------------------------------------
    # Shared processing for poll + live
    # ------------------------------------------------------------------
    def _process_results(self, results: dict[str, bytes | None]) -> dict[str, Any]:
        """Legacy path: decode GATT bytes then apply shared post-processing.

        Wire-format decoding lives in :meth:`LegacyProtocol.parse_results`;
        the Condor path produces the same parsed shape through its own
        adapter, so both call into :meth:`_apply_parsed` for the shared
        bookkeeping.
        """
        if not any(v is not None for v in results.values()):
            return self.data
        parsed = self._protocol.parse_results(results)
        return self._apply_parsed(parsed)

    def _apply_parsed(self, parsed: dict[str, Any]) -> dict[str, Any]:
        """Merge a parsed partial dict into ``self.data`` with side-effects.

        Layers on top of the raw merge: sensor-stream gating for Legacy
        (keyed off ``brushing_state``), brush-head wear derivation,
        last-seen bookkeeping, and device-registry sync on model/firmware
        changes. Callers pass the output back through
        :meth:`async_set_updated_data` once they've confirmed a state
        transition is worth publishing.
        """
        new_data = self.data.copy() if self.data else {}
        new_data.update(parsed)

        # Sensor stream gates on brushing_state — Legacy only, since
        # Condor never emits this key (its sensor stream rides a separate
        # Subscribe on the ``SensorData.b`` port instead of CCCD).
        if not self._use_condor and "brushing_state" in parsed:
            old_state = (self.data or {}).get("brushing_state")
            new_state = parsed["brushing_state"]
            if new_state != old_state:
                if new_state == "on" and not self._sensor_subscribed:
                    self.hass.async_create_task(self._subscribe_sensor_data())
                elif old_state == "on" and self._sensor_subscribed:
                    self.hass.async_create_task(self._unsubscribe_sensor_data())

        # Derived: brush head wear percentage
        limit = new_data.get("brushhead_lifetime_limit")
        usage = new_data.get("brushhead_lifetime_usage")
        if limit and usage is not None and limit > 0:
            new_data["brushhead_wear_pct"] = min(round(usage / limit * 100, 1), 100.0)
        elif usage == 0:
            new_data["brushhead_wear_pct"] = 0.0

        # Change detection: only update last_seen when data actually changed
        # or every 30s as heartbeat for availability tracking
        old = self.data or {}
        changed = any(
            new_data.get(k) != old.get(k)
            for k in new_data
            if k != "last_seen"
        )

        now = datetime.now(timezone.utc)
        last = old.get("last_seen")
        if changed or last is None or (now - last).total_seconds() >= 30:
            new_data["last_seen"] = now
        else:
            new_data["last_seen"] = last

        # Device registry: only update when model or firmware actually changed
        model = new_data.get("model_number")
        firmware = new_data.get("firmware")
        if changed and (model or firmware):
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get_device(
                identifiers={(DOMAIN, self.address)}
            )
            if device and (device.model != model or device.sw_version != firmware):
                dev_reg.async_update_device(
                    device.id,
                    model=model or "Philips Sonicare",
                    sw_version=firmware,
                )

        return new_data

    async def _start_live_monitoring(self) -> None:
        """Persistent live connection with notifications.

        Direct BLE: waits for advertisement (ADV callback or D-Bus RSSI).
        ESP bridge: waits for ESP "ready" event (device auto-connected).

        Both modes are event-driven — no blind retries.
        """
        MAX_QUICK_RETRIES = 2  # quick retries after unexpected disconnect (Direct BLE only)

        while True:
            # ---- Wait for device to be available ----
            # Direct BLE: wait for ADV/D-Bus signal
            # ESP bridge: skips — connect() sets up listeners, then we wait below
            if not self._is_esp_bridge and not self.transport.is_connected:
                    if self._wake_event.is_set():
                        self._wake_event.clear()
                        _LOGGER.info("Advertisement already pending — connecting to %s", self.address)
                    else:
                        _LOGGER.debug("Waiting for advertisement from %s...", self.address)
                        await self._wake_event.wait()
                        self._wake_event.clear()
                        _LOGGER.info("Advertisement received — connecting to %s", self.address)

            # ---- Connect and set up live monitoring ----
            async with self._connection_lock:
                try:
                    # Check if ESP bridge needs resubscription (after ESP restart)
                    if (
                        self.transport.is_connected
                        and self._live_setup_done
                        and isinstance(self.transport, EspBridgeTransport)
                        and self.transport.needs_resubscribe
                    ):
                        _LOGGER.info("ESP bridge requires resubscription")
                        self.transport.acknowledge_resubscribe()
                        self._live_setup_done = False

                    if self.transport.is_connected and self._live_setup_done:
                        await asyncio.sleep(5)
                        continue

                    def _on_state_change():
                        if self.transport.is_connected:
                            _LOGGER.info("%s: connected", self.address)
                            # Show "initializing" while reading data
                            if self.data:
                                self.data["_connecting"] = True
                            # Wake the loop to set up live monitoring
                            self._wake_event.set()
                        else:
                            _LOGGER.info("%s: disconnected", self.address)
                            # Clear brushing state so Activity shows "off"
                            # Keep handle_state_value so Charging sensor
                            # retains last known state (still on charger)
                            if self.data:
                                self.data["handle_state"] = "off"
                                self.data["brushing_state"] = None
                                self.data["brushing_state_value"] = None
                                self.data.pop("_connecting", None)
                        self.async_set_updated_data(self.data)

                    self.transport.set_disconnect_callback(_on_state_change)

                    _LOGGER.info("Establishing live connection to %s...", self.address)
                    await self.transport.connect()

                    # ESP bridge: wait for BLE device to actually connect
                    if self._is_esp_bridge and not self.transport.is_connected:
                        _LOGGER.debug(
                            "ESP bridge alive, waiting for BLE device connection for %s...",
                            self.address,
                        )
                        self._wake_event.clear()
                        await self._wake_event.wait()
                        self._wake_event.clear()
                        _LOGGER.info("BLE device connected via ESP bridge for %s", self.address)

                    # Set notification throttle for ESP bridge
                    if self._is_esp_bridge:
                        throttle_ms = self.entry.options.get(
                            CONF_NOTIFY_THROTTLE, DEFAULT_NOTIFY_THROTTLE
                        )
                        await self.transport.set_notify_throttle(throttle_ms)

                    # Initial refresh + live subscriptions. The two protocols
                    # diverge here — Legacy polls char-by-char then starts
                    # CCCD notifications; Condor runs its handshake, does a
                    # framed refresh_all, and subscribes named JSON ports.
                    if self._use_condor:
                        sub_count = await self._setup_condor_session()
                    else:
                        sub_count = await self._setup_legacy_session()
                    if sub_count == 0:
                        raise TransportError("No notifications could be subscribed")
                    self._live_setup_done = True
                    if self.data is None:
                        self.data = {}
                    self.data.pop("_connecting", None)
                    path = self.transport.connection_path
                    if path and self.data.get("connection_path") != path:
                        self.data["connection_path"] = path
                        self.async_set_updated_data(self.data)
                    _LOGGER.info("%s: live monitoring active (%d subscriptions)", self.address, sub_count)

                    if self._is_esp_bridge:
                        self._update_bridge_device_version()
                        self._check_bridge_version()
                        if self.transport.needs_resubscribe:
                            self.transport.acknowledge_resubscribe()

                except Exception as err:
                    err_msg = str(err).lower()
                    is_unreachable = (
                        "no longer reachable" in err_msg
                        or "connection slot" in err_msg
                        or "timeout" in err_msg
                    )
                    if is_unreachable:
                        _LOGGER.debug(
                            "%s: device not reachable: %s", self.address, err
                        )
                    else:
                        _LOGGER.warning(
                            "%s: live monitoring error: %s", self.address, err
                        )
                    try:
                        await self.transport.disconnect()
                    except Exception:
                        pass

                    if not self._is_esp_bridge:
                        # Direct BLE: quick retries, then wait for ADV
                        for attempt in range(MAX_QUICK_RETRIES):
                            await asyncio.sleep(5)
                            if self._wake_event.is_set():
                                break
                            _LOGGER.debug(
                                "Quick retry %d/%d for %s...",
                                attempt + 1, MAX_QUICK_RETRIES, self.address,
                            )
                            try:
                                await self.transport.connect()
                                break  # success — fall through to setup on next loop
                            except Exception:
                                try:
                                    await self.transport.disconnect()
                                except Exception:
                                    pass
                        # If still not connected, loop back to ADV wait
                    else:
                        # ESP bridge: wait for next ready event before retrying
                        self._wake_event.clear()
                        _LOGGER.debug("Waiting for ESP bridge ready event for %s...", self.address)
                        try:
                            await asyncio.wait_for(self._wake_event.wait(), timeout=60)
                        except asyncio.TimeoutError:
                            pass
                    continue

            # ---- Connected: wait until disconnect (or ESP reboot) ----
            try:
                while self.transport.is_connected:
                    if (
                        isinstance(self.transport, EspBridgeTransport)
                        and self.transport.needs_resubscribe
                    ):
                        self.transport.acknowledge_resubscribe()
                        _LOGGER.info("ESP bridge rebooted — forcing re-setup")
                        break

                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass

            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.error("Unexpected error in live monitoring: %s", err)
            finally:
                self._live_setup_done = False
                self._sensor_subscribed = False
                if self._use_condor:
                    try:
                        await self._protocol.stop_live_updates()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        await self._protocol.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    await self._protocol.unsubscribe_all()
                _LOGGER.info("%s: live connection ended", self.address)

    def _update_bridge_device_version(self) -> None:
        """Update sw_version on the ESP bridge sub-device."""
        version = self.transport.bridge_version
        if not version:
            return
        device_id = self.entry.data.get(CONF_ADDRESS) or self.entry.data.get(
            "esp_device_name", ""
        )
        dev_reg = dr.async_get(self.hass)
        bridge_device = dev_reg.async_get_device(
            identifiers={(DOMAIN, f"{device_id}_bridge")}
        )
        if bridge_device:
            dev_reg.async_update_device(bridge_device.id, sw_version=version)

    def _check_bridge_version(self) -> None:
        """Create or clear a HA repair issue if the ESP bridge firmware is outdated."""
        assert isinstance(self.transport, EspBridgeTransport)
        version = self.transport.bridge_version
        if not version:
            return
        from packaging.version import Version
        try:
            outdated = Version(version) < Version(MIN_BRIDGE_VERSION)
        except Exception:
            _LOGGER.debug("Cannot parse bridge version '%s'", version)
            return
        from homeassistant.helpers import issue_registry as ir
        if outdated:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "esp_bridge_outdated",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="esp_bridge_outdated",
                translation_placeholders={
                    "version": version,
                    "min_version": MIN_BRIDGE_VERSION,
                },
            )
            _LOGGER.warning(
                "ESP bridge v%s is outdated (minimum: v%s) — "
                "rebuild and flash your ESPHome device",
                version,
                MIN_BRIDGE_VERSION,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, "esp_bridge_outdated")


    def _make_live_callback(self):
        """Create a single notification callback for all subscribed characteristics."""

        @callback
        def _callback(char_uuid: str, data: bytes):
            if not data:
                return

            # Brush head serial notification: detect attach/detach
            if char_uuid == CHAR_BRUSHHEAD_SERIAL:
                if any(b != 0 for b in data):
                    # Non-zero = brush head attached → re-read NFC data
                    if not self._brushhead_read_pending:
                        self._brushhead_read_pending = True
                        self.hass.async_create_task(self._read_brushhead_chars())
                else:
                    # All zeros = brush head removed → clear data (like OEM app)
                    self._clear_brushhead_data()

            new_data = self._process_results({char_uuid: data})
            new_data.pop("_connecting", None)

            if new_data == self.data:
                return  # nothing changed

            if _RAW_LOGGER.isEnabledFor(logging.DEBUG):
                old = self.data or {}
                delta = {
                    k: (old.get(k), v)
                    for k, v in new_data.items()
                    if old.get(k) != v
                }
                if delta:
                    _RAW_LOGGER.debug(
                        "%s: notify %s delta %s",
                        self.address,
                        char_uuid,
                        ", ".join(f"{k}: {ov!r}→{nv!r}" for k, (ov, nv) in delta.items()),
                    )

            self.async_set_updated_data(new_data)

        return _callback

    def _clear_brushhead_data(self) -> None:
        """Clear all brush head data when head is removed (like OEM app)."""
        if not self.data:
            return
        self.data["brushhead_nfc_version"] = None
        self.data["brushhead_type"] = None
        self.data["brushhead_date"] = None
        self.data["brushhead_lifetime_limit"] = None
        self.data["brushhead_lifetime_usage"] = None
        self.data["brushhead_wear_pct"] = None
        self.data["brushhead_ring_id"] = None
        self.data["brushhead_payload"] = None
        _LOGGER.info("Brush head removed — data cleared")

    # Chars to re-read after brush head attach notification.
    # OEM app reads only 4 (version, limit, usage, ring_id) — but their
    # initial read succeeds because NFC is already scanned by then.
    # With ESP bridge timing, the initial read often happens before NFC
    # data is ready, so we also re-read payload, brush head type, and date.
    # Serial is excluded (already in the notification, re-reading loops).
    _BRUSHHEAD_REREAD_CHARS = [
        CHAR_BRUSHHEAD_NFC_VERSION,     # 0x4210
        CHAR_BRUSHHEAD_TYPE,    # 0x4220
        CHAR_BRUSHHEAD_DATE,            # 0x4240
        CHAR_BRUSHHEAD_LIFETIME_LIMIT,  # 0x4280
        CHAR_BRUSHHEAD_LIFETIME_USAGE,  # 0x4290
        CHAR_BRUSHHEAD_PAYLOAD,         # 0x42B0
        CHAR_BRUSHHEAD_RING_ID,         # 0x42C0
    ]

    async def _read_brushhead_chars(self) -> None:
        """Re-read brush head characteristics after NFC scan (like OEM app)."""
        try:
            if not self.transport.is_connected:
                return
            _LOGGER.info("Brush head detected — reading NFC data")
            # Short delay to let the handle finish processing the NFC chip
            await asyncio.sleep(1)
            results = await self._protocol.read_chars(self._BRUSHHEAD_REREAD_CHARS)
            if any(v is not None for v in results.values()):
                new_data = self._process_results(results)
                self.async_set_updated_data(new_data)
                _LOGGER.info("Brush head data updated")
        finally:
            self._brushhead_read_pending = False

    async def _setup_legacy_session(self) -> int:
        """Legacy protocol setup: batch reads then CCCD notifications.

        First connect reads every char we know about (model, firmware,
        brush head, …); subsequent reconnects stick to the dynamic
        subset. Returns the count of successful subscriptions so the
        caller can fail the session if the device answered nothing.
        """
        read_chars = (
            self._poll_chars
            if not self._full_read_done
            else self._live_chars
        )
        results = await self._protocol.read_chars(read_chars)

        if any(v is not None for v in results.values()):
            new_data = self._process_results(results)
            new_data.pop("_connecting", None)
            self.async_set_updated_data(new_data)
            if not self._full_read_done:
                self._full_read_done = True
                _LOGGER.info(
                    "%s: full initial data read complete (%d chars)",
                    self.address, len(results),
                )
            else:
                _LOGGER.info("%s: initial data read complete", self.address)

        return await self._start_all_notifications()

    async def _setup_condor_session(self) -> int:
        """Condor protocol setup: run the framed handshake, pull a full
        state snapshot, then subscribe named ports for push deltas.

        Returns the number of ports that successfully subscribed —
        callers treat zero as a fatal session error just like Legacy's
        subscription count.
        """
        await self._protocol.connect()

        initial = await self._protocol.refresh_all()
        if initial:
            new_data = self._apply_parsed(initial)
            new_data.pop("_connecting", None)
            self.async_set_updated_data(new_data)
            if not self._full_read_done:
                self._full_read_done = True
                _LOGGER.info(
                    "%s: Condor refresh_all complete (%d keys)",
                    self.address, len(initial),
                )
            else:
                _LOGGER.info("%s: Condor refresh_all complete", self.address)

        await self._protocol.start_live_updates(self._on_condor_delta)
        return len(getattr(self._protocol, "_subscribed_ports", []))

    @callback
    def _on_condor_delta(self, delta: dict[str, Any]) -> None:
        """Route a Condor ChangeIndication delta into ``coordinator.data``.

        Runs in the HA event loop from the BLE notification callback —
        safe to call ``async_set_updated_data`` inline.
        """
        if not delta:
            return
        new_data = self._apply_parsed(delta)
        new_data.pop("_connecting", None)
        if new_data == self.data:
            return
        self.async_set_updated_data(new_data)

    async def _start_all_notifications(self) -> int:
        """Start GATT notifications for live updates. Returns number of successful subscriptions."""
        if not self.transport.is_connected:
            return 0

        self._live_cb = self._make_live_callback()
        self._sensor_subscribed = False
        count = await self._protocol.subscribe_notifications(
            self._notify_chars, self._live_cb
        )

        # If brush is already in an active session, subscribe sensor data now
        if (self.data or {}).get("brushing_state") == "on":
            await self._subscribe_sensor_data()

        return count

    def _compute_sensor_enable_mask(self) -> int:
        """Compute sensor enable bitmask from options."""
        options = self.entry.options
        mask = 0
        if options.get(CONF_SENSOR_PRESSURE, DEFAULT_SENSOR_PRESSURE):
            mask |= SENSOR_ENABLE_PRESSURE
        if options.get(CONF_SENSOR_TEMPERATURE, DEFAULT_SENSOR_TEMPERATURE):
            mask |= SENSOR_ENABLE_TEMPERATURE
        if options.get(CONF_SENSOR_GYROSCOPE, DEFAULT_SENSOR_GYROSCOPE):
            mask |= SENSOR_ENABLE_GYROSCOPE
        return mask

    async def _subscribe_sensor_data(self) -> None:
        """Enable sensors and subscribe to sensor data stream."""
        if self._sensor_subscribed or not self.transport.is_connected or not self._live_cb:
            return
        mask = self._compute_sensor_enable_mask()
        if mask == 0:
            _LOGGER.debug("All sensors disabled in options — skipping sensor subscribe")
            return
        if await self._protocol.start_sensor_stream(mask, self._live_cb):
            self._sensor_subscribed = True
            _LOGGER.info("Sensor data stream subscribed (session active)")

    async def _unsubscribe_sensor_data(self) -> None:
        """Unsubscribe from sensor data stream and disable sensors."""
        if not self._sensor_subscribed:
            return
        await self._protocol.stop_sensor_stream()
        self._sensor_subscribed = False
        _LOGGER.info("Sensor data stream unsubscribed (session ended)")

    async def _stop_all_notifications(self) -> None:
        """Stop all GATT notifications."""
        await self._protocol.unsubscribe_all()

    async def async_set_brushing_mode(self, mode_key: str) -> None:
        """Write the selected brushing mode to the toothbrush."""
        await self._protocol.set_brushing_mode(mode_key)
        self.data["selected_mode"] = mode_key
        self.data["brushing_mode"] = mode_key
        self.async_set_updated_data(self.data)

    async def async_set_intensity(self, intensity_key: str) -> None:
        """Write the selected intensity to the toothbrush."""
        await self._protocol.set_intensity(intensity_key)
        self.data["intensity"] = intensity_key
        self.async_set_updated_data(self.data)

    async def async_read_settings(self) -> int:
        """Read the settings bitmask."""
        return await self._protocol.read_settings_bitmask()

    async def async_write_settings_bit(self, bit_mask: int, enabled: bool) -> None:
        """Toggle a single bit in the settings bitmask."""
        await self._protocol.write_settings_bit(bit_mask, enabled)

    async def async_shutdown(self) -> None:
        """Called on unload - clean up everything."""
        if self._unsub_adv_debug:
            self._unsub_adv_debug()
            self._unsub_adv_debug = None

        if self._dbus_bus:
            self._dbus_bus.disconnect()
            self._dbus_bus = None

        if self._use_condor:
            try:
                await self._protocol.stop_live_updates()
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._protocol.disconnect()
            except Exception:  # noqa: BLE001
                pass
        else:
            await self._protocol.unsubscribe_all()

        if self._live_task:
            self._live_task.cancel()
            try:
                await self._live_task
            except asyncio.CancelledError:
                pass

        await self.transport.disconnect()
