# custom_components/philips_sonicare/coordinator.py
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
    async_register_callback,
)

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
    CHAR_BRUSHHEAD_PAYLOAD,
    CHAR_BRUSHHEAD_RING_ID,
    CHAR_ERROR_PERSISTENT,
    CHAR_ERROR_VOLATILE,
    CHAR_SENSOR_DATA,
    SENSOR_FRAME_PRESSURE,
    SENSOR_FRAME_TEMPERATURE,
    SENSOR_FRAME_GYROSCOPE,
    HANDLE_STATES,
    BRUSHING_MODES,
    BRUSHING_STATES,
    INTENSITIES,
    NOTIFICATION_CHARS,
    POLL_READ_CHARS,
    LIVE_READ_CHARS,
    CONF_POLL_INTERVAL,
    CONF_ENABLE_LIVE_UPDATES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_ENABLE_LIVE_UPDATES,
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

        # Read options
        options = entry.options
        poll_interval = options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        self.poll_interval_seconds = poll_interval
        self.enable_live_updates = options.get(
            CONF_ENABLE_LIVE_UPDATES, DEFAULT_ENABLE_LIVE_UPDATES
        )

        self._poll_chars = list(POLL_READ_CHARS)
        self._live_chars = list(LIVE_READ_CHARS)
        self._notify_chars = list(NOTIFICATION_CHARS)

        self._connection_lock = asyncio.Lock()
        self._live_task: asyncio.Task | None = None
        self._live_setup_done = False
        self._unsub_adv_debug = None
        self._device_seen = asyncio.Event()

        _LOGGER.debug(
            "Initializing coordinator for %s with poll interval %s seconds (live updates: %s)",
            self.address,
            self.poll_interval_seconds,
            self.enable_live_updates,
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"Philips Sonicare {self.address}",
            update_interval=timedelta(seconds=self.poll_interval_seconds),
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
            "handle_state": None,
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
            "brushhead_payload": None,
            "brushhead_ring_id": None,
            "error_persistent": None,
            "error_volatile": None,
            "pressure": None,
            "pressure_alarm": None,
            "temperature": None,
            "last_seen": None,
        }

    async def async_start(self) -> None:
        """Start live monitoring. Call after setup is complete."""
        self._start_advertisement_logging()
        if self.enable_live_updates:
            self._live_task = self.entry.async_create_background_task(
                self.hass, self._start_live_monitoring(), "philips_sonicare_monitoring"
            )
        else:
            _LOGGER.info("Live updates disabled - polling only")

    def _start_advertisement_logging(self) -> None:
        """Log every BLE advertisement from the Sonicare."""

        @callback
        def _advertisement_callback(service_info, change):
            adv = service_info.advertisement
            svc_short = [u[-8:] for u in (adv.service_uuids or [])]
            mfr = {k: v.hex() for k, v in adv.manufacturer_data.items()} if adv.manufacturer_data else None
            _LOGGER.info(
                "ADV %s | RSSI: %s dBm | Services: %s%s",
                service_info.address,
                service_info.rssi,
                svc_short,
                f" | MfrData: {mfr}" if mfr else "",
            )
            # Wake up live monitoring thread to attempt reconnect immediately
            self._device_seen.set()

        self._unsub_adv_debug = async_register_callback(
            self.hass,
            _advertisement_callback,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.PASSIVE,
        )

    # ------------------------------------------------------------------
    # Called automatically by the coordinator (polling)
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data via polling fallback."""

        # 1. Live connection active -> skip polling
        if self.transport.is_connected:
            _LOGGER.debug("Live connection active - polling skipped")
            data = self.data or {}
            data["last_seen"] = datetime.now(timezone.utc)
            return data

        # 2. Live updates enabled -> let live thread handle it, don't compete
        if self.enable_live_updates and self._device_seen.is_set():
            _LOGGER.debug("ADV received, live thread will handle reconnect - polling skipped")
            return self.data or {}

        if self.data is None:
            self.data = {}

        # 3. Recent data within poll interval -> skip
        last_seen = self.data.get("last_seen")
        if last_seen:
            age = (datetime.now(timezone.utc) - last_seen).total_seconds()
            if age < self.poll_interval_seconds:
                _LOGGER.debug("Recent data (%.0fs old) - polling skipped", age)
                return self.data or {}

        _LOGGER.info("Polling: connecting to read data...")
        async with self._connection_lock:
            try:
                results = await self.transport.read_chars(self._poll_chars)
                return self._process_results(results)
            except Exception as err:
                raise UpdateFailed(f"Error communicating with device: {err}") from err

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

        # Brush Head Serial (UTF-8 string)
        if raw := results.get(CHAR_BRUSHHEAD_SERIAL):
            new_data["brushhead_serial"] = raw.decode("utf-8", "ignore").strip()

        # Brush Head Date (UTF-8 string)
        if raw := results.get(CHAR_BRUSHHEAD_DATE):
            new_data["brushhead_date"] = raw.decode("utf-8", "ignore").strip()

        # Brush Head Payload (UTF-8 string)
        if raw := results.get(CHAR_BRUSHHEAD_PAYLOAD):
            new_data["brushhead_payload"] = raw.decode("utf-8", "ignore").strip()

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
                    new_data["pressure_alarm"] = raw[6]
                elif frame_type == SENSOR_FRAME_TEMPERATURE and len(raw) >= 6:
                    new_data["temperature"] = round(raw[4] / 256 + raw[5], 1)

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
        """Persistent live connection with notifications."""
        backoff = 5
        max_backoff = 300

        while True:
            _LOGGER.debug("Live loop: waiting for connection lock...")
            async with self._connection_lock:
                _LOGGER.debug("Live loop: lock acquired")
                try:
                    if self.transport.is_connected and self._live_setup_done:
                        await asyncio.sleep(5)
                        continue

                    def _on_state_change():
                        if self.transport.is_connected:
                            _LOGGER.info("Transport state: connected")
                        else:
                            _LOGGER.info("Transport state: disconnected")
                        self.async_set_updated_data(self.data)

                    self.transport.set_disconnect_callback(_on_state_change)

                    _LOGGER.info("Establishing live connection to %s...", self.address)
                    await self.transport.connect()

                    # Subscribe FIRST to keep connection alive
                    sub_count = await self._start_all_notifications()
                    if sub_count == 0:
                        raise TransportError("No notifications could be subscribed")
                    self._live_setup_done = True
                    _LOGGER.info("Live monitoring active (%d subscriptions)", sub_count)

                    # Reset backoff after successful subscribe
                    backoff = 5

                    # Then read all characteristics in background
                    results = {}
                    for uuid in self._live_chars:
                        if not self.transport.is_connected:
                            break
                        try:
                            value = await self.transport.read_char(uuid)
                            results[uuid] = value
                        except Exception as e:
                            _LOGGER.debug("Live initial read failed for %s: %s", uuid, e)

                    if any(v is not None for v in results.values()):
                        new_data = self._process_results(results)
                        self.async_set_updated_data(new_data)
                        _LOGGER.info("Initial data read complete")

                except TransportError as err:
                    _LOGGER.debug(
                        "Transport error: %s - retrying in %ds (or on next ADV)", err, backoff
                    )
                    self._device_seen.clear()
                    try:
                        await asyncio.wait_for(self._device_seen.wait(), timeout=backoff)
                        _LOGGER.info("Advertisement received - attempting immediate reconnect")
                        backoff = 5
                    except asyncio.TimeoutError:
                        backoff = min(backoff * 2, max_backoff)
                    continue

                except Exception as err:
                    _LOGGER.error(
                        "Live monitoring error: %s - retrying in %ds (or on next ADV)", err, backoff
                    )
                    try:
                        await self.transport.disconnect()
                    except Exception:
                        pass
                    self._device_seen.clear()
                    try:
                        await asyncio.wait_for(self._device_seen.wait(), timeout=backoff)
                        _LOGGER.info("Advertisement received - attempting immediate reconnect")
                        backoff = 5
                    except asyncio.TimeoutError:
                        backoff = min(backoff * 2, max_backoff)
                    continue

            # Outside the lock: wait until disconnect
            try:
                while self.transport.is_connected:
                    await asyncio.sleep(5)

            except asyncio.CancelledError:
                _LOGGER.error("Live connection was cancelled")
                raise  # the task was cancelled from outside
            except Exception as err:
                _LOGGER.error("Unexpected error in live monitoring: %s", err)
            finally:
                self._live_setup_done = False
                await self.transport.unsubscribe_all()
                if self._device_seen.is_set():
                    _LOGGER.info("Live connection ended - ADV already received, reconnecting immediately")
                    self._device_seen.clear()
                else:
                    _LOGGER.info("Live connection ended - waiting for next advertisement (or 5s)")
                    try:
                        await asyncio.wait_for(self._device_seen.wait(), timeout=5)
                        _LOGGER.info("ADV received during wait - reconnecting immediately")
                    except asyncio.TimeoutError:
                        _LOGGER.debug("No ADV during 5s wait - going to top of loop")

    def _make_live_callback(self):
        """Create a single notification callback for all subscribed characteristics."""

        @callback
        def _callback(char_uuid: str, data: bytes):
            if not data:
                return

            new_data = self._process_results({char_uuid: data})

            if new_data == self.data:
                return  # nothing changed

            self.async_set_updated_data(new_data)

        return _callback

    async def _start_all_notifications(self) -> int:
        """Start GATT notifications for live updates. Returns number of successful subscriptions."""
        if not self.transport.is_connected:
            return 0

        cb = self._make_live_callback()
        count = 0
        for char_uuid in self._notify_chars:
            try:
                await self.transport.subscribe(char_uuid, cb)
                count += 1
                _LOGGER.debug("Subscribed to %s", char_uuid)
            except Exception as e:
                _LOGGER.warning("Failed to subscribe %s: %s", char_uuid, e)
        return count

    async def _stop_all_notifications(self) -> None:
        """Stop all GATT notifications."""
        await self.transport.unsubscribe_all()

    async def async_shutdown(self) -> None:
        """Called on unload - clean up everything."""
        if self._unsub_adv_debug:
            self._unsub_adv_debug()
            self._unsub_adv_debug = None

        await self.transport.unsubscribe_all()

        if self._live_task:
            self._live_task.cancel()
            try:
                await self._live_task
            except asyncio.CancelledError:
                pass

        await self.transport.disconnect()
