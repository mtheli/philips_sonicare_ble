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
    CHAR_SENSOR_DATA,
    CHAR_SENSOR_ENABLE,
    CHAR_HANDLE_TIME,
    SENSOR_FRAME_PRESSURE,
    SENSOR_FRAME_TEMPERATURE,
    SENSOR_FRAME_GYROSCOPE,
    SENSOR_ENABLE_PRESSURE,
    SENSOR_ENABLE_TEMPERATURE,
    SENSOR_ENABLE_GYROSCOPE,
    CONF_SENSOR_PRESSURE,
    CONF_SENSOR_TEMPERATURE,
    CONF_SENSOR_GYROSCOPE,
    DEFAULT_SENSOR_PRESSURE,
    DEFAULT_SENSOR_TEMPERATURE,
    DEFAULT_SENSOR_GYROSCOPE,
    HANDLE_STATES,
    PRESSURE_ALARM_STATES,
    BRUSHING_MODES,
    BRUSHING_STATES,
    INTENSITIES,
    BRUSHHEAD_TYPES,
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

        # Kids devices (HX63xx) have fewer chars within available services
        model = entry.data.get("model", "")
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
        """Process raw GATT values into coordinator data."""
        if not any(v is not None for v in results.values()):
            return self.data

        new_data = self.data.copy() if self.data else {}

        # === Standard GATT Characteristics ===
        if raw := results.get(CHAR_BATTERY_LEVEL):
            new_data["battery"] = raw[0]

        if raw := results.get(CHAR_FIRMWARE_REVISION):
            new_data["firmware"] = raw.decode("utf-8", "ignore").strip()

        if raw := results.get(CHAR_HARDWARE_REVISION):
            new_data["hardware_revision"] = raw.decode("utf-8", "ignore").strip()

        if raw := results.get(CHAR_SOFTWARE_REVISION):
            new_data["software_revision"] = raw.decode("utf-8", "ignore").strip()

        if raw := results.get(CHAR_MODEL_NUMBER):
            new_data["model_number"] = raw.decode("utf-8", "ignore").strip()

        if raw := results.get(CHAR_SERIAL_NUMBER):
            new_data["serial_number"] = raw.decode("utf-8", "ignore").strip()

        if raw := results.get(CHAR_MANUFACTURER_NAME):
            new_data["manufacturer_name"] = raw.decode("utf-8", "ignore").strip()

        # === Sonicare-specific Characteristics ===

        # Available Routine IDs (byte array of mode IDs)
        if raw := results.get(CHAR_AVAILABLE_ROUTINE_IDS):
            mode_ids = list(raw)
            new_data["available_mode_ids"] = mode_ids
            # Also read the currently selected mode (first byte = selected)
            # The first ID in the list is NOT the selected one — it's just
            # the list of available modes. Selected mode comes from a
            # separate read (see selected_mode handling below).

        # Handle State (uint8)
        if raw := results.get(CHAR_HANDLE_STATE):
            state_byte = raw[0]
            new_data["handle_state_value"] = state_byte
            mapped = HANDLE_STATES.get(state_byte)
            if mapped is None:
                _LOGGER.warning("Unknown handle_state value: %d (raw: %s)", state_byte, raw.hex())
            new_data["handle_state"] = mapped

        # Brushing Mode (uint8, some devices use uint16 LE)
        if raw := results.get(CHAR_BRUSHING_MODE):
            if len(raw) >= 2:
                mode_value = int.from_bytes(raw[:2], "little")
            else:
                mode_value = raw[0]
            new_data["brushing_mode_value"] = mode_value
            mapped = BRUSHING_MODES.get(mode_value)
            if mapped is None:
                _LOGGER.warning("Unknown brushing_mode value: %d (raw: %s)", mode_value, raw.hex())
            new_data["brushing_mode"] = mapped

        # Brushing State (uint8)
        if raw := results.get(CHAR_BRUSHING_STATE):
            state_value = raw[0]
            new_data["brushing_state_value"] = state_value
            mapped = BRUSHING_STATES.get(state_value)
            if mapped is None:
                _LOGGER.warning("Unknown brushing_state value: %d (raw: %s)", state_value, raw.hex())
            new_data["brushing_state"] = mapped

            # Dynamic sensor subscription: subscribe during active sessions only
            old_state = (self.data or {}).get("brushing_state")
            if mapped != old_state:
                if mapped == "on" and not self._sensor_subscribed:
                    self.hass.async_create_task(self._subscribe_sensor_data())
                elif old_state == "on" and self._sensor_subscribed:
                    self.hass.async_create_task(self._unsubscribe_sensor_data())

        # Intensity (uint8)
        if raw := results.get(CHAR_INTENSITY):
            intensity_value = raw[0]
            new_data["intensity_value"] = intensity_value
            mapped = INTENSITIES.get(intensity_value)
            if mapped is None:
                _LOGGER.warning("Unknown intensity value: %d (raw: %s)", intensity_value, raw.hex())
            new_data["intensity"] = mapped

        # Brushing Time (uint16 LE, seconds)
        if raw := results.get(CHAR_BRUSHING_TIME):
            new_data["brushing_time"] = int.from_bytes(raw[:2], "little")

        # Routine Length (uint16 LE, seconds)
        if raw := results.get(CHAR_ROUTINE_LENGTH):
            new_data["routine_length"] = int.from_bytes(raw[:2], "little")

        # Session ID (uint16 LE)
        if raw := results.get(CHAR_SESSION_ID):
            new_data["session_id"] = int.from_bytes(raw[:2], "little")

        # Latest Session ID (uint16 LE)
        if raw := results.get(CHAR_LATEST_SESSION_ID):
            new_data["latest_session_id"] = int.from_bytes(raw[:2], "little")

        # Session Count (uint16 LE)
        if raw := results.get(CHAR_SESSION_COUNT):
            new_data["session_count"] = int.from_bytes(raw[:2], "little")

        # Motor Runtime (uint32 LE, seconds)
        if raw := results.get(CHAR_MOTOR_RUNTIME):
            new_data["motor_runtime"] = int.from_bytes(raw[:4], "little")

        # Handle Time (uint32 LE, seconds)
        if raw := results.get(CHAR_HANDLE_TIME):
            new_data["handle_time"] = int.from_bytes(raw[:4], "little")

        # Brush Head Lifetime Limit (uint16 LE)
        if raw := results.get(CHAR_BRUSHHEAD_LIFETIME_LIMIT):
            new_data["brushhead_lifetime_limit"] = int.from_bytes(raw[:2], "little")

        # Brush Head Lifetime Usage (uint16 LE)
        if raw := results.get(CHAR_BRUSHHEAD_LIFETIME_USAGE):
            new_data["brushhead_lifetime_usage"] = int.from_bytes(raw[:2], "little")

        # Brush Head Wear % (computed)
        limit = new_data.get("brushhead_lifetime_limit")
        usage = new_data.get("brushhead_lifetime_usage")
        if limit and usage is not None and limit > 0:
            new_data["brushhead_wear_pct"] = min(round(usage / limit * 100, 1), 100.0)
        elif usage == 0:
            new_data["brushhead_wear_pct"] = 0.0

        # Brush Head Serial (7-byte NFC UID, displayed as colon-separated hex)
        if raw := results.get(CHAR_BRUSHHEAD_SERIAL):
            new_data["brushhead_serial"] = ":".join(f"{b:02X}" for b in raw)

        # Brush Head Date (UTF-8 string)
        if raw := results.get(CHAR_BRUSHHEAD_DATE):
            new_data["brushhead_date"] = raw.decode("utf-8", "ignore").strip()

        # Brush Head NFC Version (uint16 LE)
        if raw := results.get(CHAR_BRUSHHEAD_NFC_VERSION):
            new_data["brushhead_nfc_version"] = int.from_bytes(raw[:2], "little")

        # Brush Head Type (uint8)
        if raw := results.get(CHAR_BRUSHHEAD_TYPE):
            new_data["brushhead_type"] = BRUSHHEAD_TYPES.get(raw[0], f"unknown_{raw[0]}")

        # Brush Head Payload (NFC NDEF data — usually a URL, fallback to hex)
        if raw := results.get(CHAR_BRUSHHEAD_PAYLOAD):
            try:
                text = raw.decode("utf-8")
                if text.isprintable():
                    new_data["brushhead_payload"] = text
                else:
                    new_data["brushhead_payload"] = raw.hex()
            except (UnicodeDecodeError, ValueError):
                new_data["brushhead_payload"] = raw.hex()

        # Brush Head Ring ID (uint16 LE)
        if raw := results.get(CHAR_BRUSHHEAD_RING_ID):
            new_data["brushhead_ring_id"] = int.from_bytes(raw[:2], "little")

        # Error Persistent (uint32 LE)
        if raw := results.get(CHAR_ERROR_PERSISTENT):
            new_data["error_persistent"] = int.from_bytes(raw[:4], "little")

        # Error Volatile (uint32 LE)
        if raw := results.get(CHAR_ERROR_VOLATILE):
            new_data["error_volatile"] = int.from_bytes(raw[:4], "little")

        # Sensor Data Stream (0x4130) — pressure, temperature, gyroscope frames
        if raw := results.get(CHAR_SENSOR_DATA):
            if len(raw) >= 4:
                import struct
                frame_type = struct.unpack("<H", raw[:2])[0]
                if frame_type == SENSOR_FRAME_PRESSURE and len(raw) >= 7:
                    new_data["pressure"] = struct.unpack("<h", raw[4:6])[0]
                    alarm_value = raw[6]
                    new_data["pressure_alarm"] = alarm_value
                    new_data["pressure_state"] = PRESSURE_ALARM_STATES.get(alarm_value)
                elif frame_type == SENSOR_FRAME_TEMPERATURE and len(raw) >= 6:
                    new_data["temperature"] = round(struct.unpack("<H", raw[4:6])[0] / 256, 1)

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
            # ESP: skip — connect() below waits for "ready" event internally
            if not self._is_esp_bridge and not self.transport.is_connected:
                    if self._wake_event.is_set():
                        # ADV already received (e.g., during startup or quick retry)
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

                    # Set notification throttle for ESP bridge
                    if self._is_esp_bridge:
                        throttle_ms = self.entry.options.get(
                            CONF_NOTIFY_THROTTLE, DEFAULT_NOTIFY_THROTTLE
                        )
                        await self.transport.set_notify_throttle(throttle_ms)

                    # Read characteristics first, then subscribe.
                    # First connect: read ALL chars (incl. static data like
                    # brush head, model, firmware). Subsequent: dynamic only.
                    read_chars = (
                        self._poll_chars
                        if not self._full_read_done
                        else self._live_chars
                    )
                    results = {}
                    for uuid in read_chars:
                        if not self.transport.is_connected:
                            break
                        try:
                            value = await self.transport.read_char(uuid)
                            results[uuid] = value
                        except Exception as e:
                            _LOGGER.debug("Live initial read failed for %s: %s", uuid, e)

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

                    # Subscribe after reads are done
                    sub_count = await self._start_all_notifications()
                    if sub_count == 0:
                        raise TransportError("No notifications could be subscribed")
                    self._live_setup_done = True
                    if self.data:
                        self.data.pop("_connecting", None)
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
                await self.transport.unsubscribe_all()
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
            results = {}
            for uuid in self._BRUSHHEAD_REREAD_CHARS:
                if not self.transport.is_connected:
                    break
                try:
                    value = await self.transport.read_char(uuid)
                    results[uuid] = value
                except Exception as e:
                    _LOGGER.debug("Brush head read failed for %s: %s", uuid, e)
            if any(v is not None for v in results.values()):
                new_data = self._process_results(results)
                self.async_set_updated_data(new_data)
                _LOGGER.info("Brush head data updated")
        finally:
            self._brushhead_read_pending = False

    async def _start_all_notifications(self) -> int:
        """Start GATT notifications for live updates. Returns number of successful subscriptions."""
        if not self.transport.is_connected:
            return 0

        self._live_cb = self._make_live_callback()
        self._sensor_subscribed = False
        count = 0
        for char_uuid in self._notify_chars:
            try:
                await self.transport.subscribe(char_uuid, self._live_cb)
                count += 1
                _LOGGER.debug("%s: subscribed to %s", self.address, char_uuid)
            except Exception as e:
                _LOGGER.warning("Failed to subscribe %s: %s", char_uuid, e)

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
        """Enable sensors and subscribe to sensor data stream (0x4130)."""
        if self._sensor_subscribed or not self.transport.is_connected or not self._live_cb:
            return
        mask = self._compute_sensor_enable_mask()
        if mask == 0:
            _LOGGER.debug("All sensors disabled in options — skipping sensor subscribe")
            return
        try:
            await self.transport.write_char(
                CHAR_SENSOR_ENABLE,
                bytes([mask]),
            )
            _LOGGER.debug("Sensor enable written: 0x%02X", mask)
        except Exception as e:
            _LOGGER.warning("Failed to write sensor enable: %s", e)
        try:
            await self.transport.subscribe(CHAR_SENSOR_DATA, self._live_cb)
            self._sensor_subscribed = True
            _LOGGER.info("Sensor data stream subscribed (session active)")
        except Exception as e:
            _LOGGER.warning("Failed to subscribe sensor data: %s", e)

    async def _unsubscribe_sensor_data(self) -> None:
        """Unsubscribe from sensor data stream and disable sensors."""
        if not self._sensor_subscribed:
            return
        try:
            await self.transport.unsubscribe(CHAR_SENSOR_DATA)
        except Exception:
            pass
        try:
            # Disable all sensors (write 0x00 to 0x4120)
            await self.transport.write_char(CHAR_SENSOR_ENABLE, bytes([0x00]))
        except Exception:
            pass
        self._sensor_subscribed = False
        _LOGGER.info("Sensor data stream unsubscribed (session ended)")

    async def _stop_all_notifications(self) -> None:
        """Stop all GATT notifications."""
        await self.transport.unsubscribe_all()

    async def async_set_brushing_mode(self, mode_key: str) -> None:
        """Write the selected brushing mode to the toothbrush (0x4022)."""
        # Reverse-lookup: mode string → mode ID
        mode_id = None
        for mid, mname in BRUSHING_MODES.items():
            if mname == mode_key:
                mode_id = mid
                break
        if mode_id is None:
            raise ValueError(f"Unknown brushing mode: {mode_key}")

        available = self.data.get("available_mode_ids") or []
        if available and mode_id not in available:
            raise ValueError(
                f"Mode {mode_key} (id={mode_id}) not available on this device"
            )

        await self.transport.write_char(
            CHAR_AVAILABLE_ROUTINE_IDS, bytes([mode_id])
        )
        self.data["selected_mode"] = mode_key
        self.async_set_updated_data(self.data)
        _LOGGER.info("Brushing mode set to %s (id=%d)", mode_key, mode_id)

    async def async_shutdown(self) -> None:
        """Called on unload - clean up everything."""
        if self._unsub_adv_debug:
            self._unsub_adv_debug()
            self._unsub_adv_debug = None

        if self._dbus_bus:
            self._dbus_bus.disconnect()
            self._dbus_bus = None

        await self.transport.unsubscribe_all()

        if self._live_task:
            self._live_task.cancel()
            try:
                await self._live_task
            except asyncio.CancelledError:
                pass

        await self.transport.disconnect()
