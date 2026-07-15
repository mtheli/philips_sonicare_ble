# custom_components/philips_sonicare/coordinator.py
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.config_entries import ConfigEntry
from homeassistant.components import bluetooth as ha_bluetooth
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
from .condor_adapter import resolve_brushing_mode
from .const import (
    DOMAIN,
    SVC_CONDOR,
    SVC_BRUSHHEAD,
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
    CONF_WARN_COUNTERFEIT,
    DEFAULT_WARN_COUNTERFEIT,
    COUNTERFEIT_DETECTION_DELAY,
)

_LOGGER = logging.getLogger(__name__)
_RAW_LOGGER = logging.getLogger(__name__ + ".raw")
_RAW_LOGGER.setLevel(logging.WARNING)  # silent unless explicitly enabled

STORAGE_VERSION = 1
# Debounced: brushing sessions update data every second, so the actual disk
# write lands once, shortly after the burst ends. Store flushes any pending
# save on HA shutdown by itself.
STORAGE_SAVE_DELAY = 10

# Live session state is not persisted — the brush is asleep again by the time
# HA comes back up, so restoring "brushing" would be wrong and would unlock
# the session-gated sensors (pressure/temperature) with stale values.
UNPERSISTED_KEYS = {
    "handle_state",
    "handle_state_value",
    "brushing_state",
    "brushing_state_value",
    "pressure",
    "pressure_alarm",
    "pressure_state",
    "temperature",
}


def _storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}"


async def async_remove_stored_data(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the persisted device data of a removed config entry."""
    await Store(hass, STORAGE_VERSION, _storage_key(entry_id)).async_remove()


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
        # its transport service is in the discovered set, Classic (direct
        # GATT-per-property) otherwise. The two protocols produce the
        # same ``coordinator.data`` shape through their respective
        # adapters, so entity code stays protocol-agnostic.
        discovered = {s.lower() for s in entry.data.get(CONF_SERVICES, [])}
        self._use_condor = SVC_CONDOR.lower() in discovered
        # Counterfeit detection only applies to devices that actually expose a
        # brush-head NFC service (Classic ``SVC_BRUSHHEAD`` or Condor). Models
        # without it — e.g. the HX63xx Kids — never report a serial, so the
        # check would otherwise flag every brushing session as a fake.
        self._has_brushhead = SVC_BRUSHHEAD.lower() in discovered or self._use_condor
        if self._use_condor:
            from .condor_protocol import CondorProtocol
            self._protocol = CondorProtocol(transport)
        else:
            from .classic_protocol import ClassicProtocol
            self._protocol = ClassicProtocol(transport)

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
        # The protocol needs the model family to pick the right brushing-mode
        # decode table (0x4022 mode-id on HX9996/HX999X vs 0x4080 sequential
        # index elsewhere); the firmware model-number is stable for a paired
        # device.
        self._protocol.model = model
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
        # transport.disconnect_count as of the last live setup — a later
        # mismatch means a disconnect/reconnect happened that the monitor
        # loop never observed (see the connected wait loop).
        self._setup_disconnect_count = 0
        self._full_read_done = False
        self._last_adapter_type: str | None = None
        self._unsub_adv_debug = None
        self._dbus_bus: MessageBus | None = None
        self._counterfeit_timer_task: asyncio.Task | None = None
        self._counterfeit_detected: bool = False
        self._counterfeit_cleanup_done: bool = False
        # HA >= 2026.5 exposes async_clear_advertisement_history — preferred over
        # the BlueZ D-Bus RSSI listener for waking on static-ADV devices.
        self._use_adv_clear = hasattr(
            ha_bluetooth, "async_clear_advertisement_history"
        )
        self._wake_event = asyncio.Event()
        # Wake REASON for _wake_event: True only when a real ADV/RSSI signal
        # arrived (_handle_wake). The disconnect callback sets the event too
        # (to nudge the poll loop), so the reconnect loop must not treat a
        # bare set event as "device is awake".
        self._adv_wake = False
        self._sensor_subscribed = False
        self._brushhead_read_pending = False
        self._live_cb: Callable | None = None
        # Persists the last known device data across HA restarts — the brush
        # sleeps between sessions, so without this every entity would stay
        # empty until the next time it is used (mirrors what core's
        # bluetooth.passive_update_processor store does for passive devices).
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, _storage_key(entry.entry_id)
        )

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
            "brushhead_counterfeit": False,
        }

    # ------------------------------------------------------------------
    # Persisted device data
    # ------------------------------------------------------------------

    async def async_load_stored_data(self) -> None:
        """Merge the persisted device data into the initial dataset.

        Called once during setup, before live monitoring starts, so the
        entities come up with the last known values instead of empty ones.
        Live reads overwrite these as soon as the brush is next seen.
        """
        stored = await self._store.async_load()
        if not stored:
            return
        restored = {k: v for k, v in stored.items() if k not in UNPERSISTED_KEYS}
        last_seen = restored.get("last_seen")
        if isinstance(last_seen, str):
            try:
                restored["last_seen"] = datetime.fromisoformat(last_seen)
            except ValueError:
                restored.pop("last_seen")
        self.data = {**(self.data or {}), **restored}
        _LOGGER.debug(
            "Restored %d stored values for %s", len(restored), self.address
        )

    @callback
    def async_set_updated_data(self, data: dict[str, Any]) -> None:
        """Publish new data and schedule a debounced save to disk."""
        super().async_set_updated_data(data)
        self._store.async_delay_save(self._data_to_save, STORAGE_SAVE_DELAY)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        """Serialize the persistable subset of ``self.data`` for storage."""
        data = self.data or {}
        out = {
            k: v
            for k, v in data.items()
            if not k.startswith("_") and k not in UNPERSISTED_KEYS
        }
        if isinstance(out.get("last_seen"), datetime):
            out["last_seen"] = out["last_seen"].isoformat()
        return out

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
            if not self._use_adv_clear:
                # Fallback for HA < 2026.5 without async_clear_advertisement_history.
                await self._start_dbus_rssi_listener()
        self._live_task = self.entry.async_create_background_task(
            self.hass, self._start_live_monitoring(), "philips_sonicare_monitoring"
        )

    def _handle_wake(self) -> None:
        """Handle device wake — set activity to initializing and trigger connect."""
        if not self.transport.is_connected and self.data:
            self.data["_connecting"] = True
            self.async_set_updated_data(self.data)
        self._adv_wake = True
        self._wake_event.set()

    def _consume_wake(self) -> None:
        """Consume a pending wake: event and wake-reason flag together.

        The two form one unit of state — clearing only one of them would
        desync the reconnect gate.
        """
        self._wake_event.clear()
        self._adv_wake = False

    @callback
    def _clear_adv_history(self) -> None:
        """Re-arm advertisement-based wake detection for static-ADV devices.

        habluetooth deduplicates identical advertisements (core#141662), so the
        registered advertisement callback stops firing after the first packet.
        Clearing the dedup history makes the next (identical) ADV reach the
        callback again. Called right before each ADV wait so every reconnect
        cycle re-opens the guard; no periodic timer needed.

        Replaces the BlueZ D-Bus RSSI listener on HA >= 2026.5. No-op on older
        HA, which uses _start_dbus_rssi_listener instead.
        """
        if self._use_adv_clear:
            ha_bluetooth.async_clear_advertisement_history(self.hass, self.address)

    def _start_advertisement_callback(self) -> None:
        """Register HA bluetooth callback for advertisement detection.

        Note: habluetooth filters identical advertisements, so this only fires
        when advertisement DATA changes (manufacturer_data, service_data,
        service_uuids, or name). For devices like the Sonicare that send
        static data, _clear_adv_history re-arms it before each ADV wait (or the
        D-Bus RSSI listener provides the fallback on HA < 2026.5).
        """

        @callback
        def _advertisement_callback(service_info, change):
            # Ignore stale/cached history data (fires on registration, and on
            # BlueZ RSSI-invalidation events for cached devices). Such an
            # event still re-populated habluetooth's dedup history, spending
            # the one-shot _clear_adv_history arm — re-open the guard, or the
            # next real (identical-payload) ADV would be deduplicated away
            # and this callback would never fire again.
            if service_info.rssi is not None and service_info.rssi <= -127:
                _LOGGER.debug("%s: ADV ignored (stale RSSI %s) — re-arming dedup guard", service_info.address, service_info.rssi)
                self._clear_adv_history()
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
        """Classic path: decode GATT bytes then apply shared post-processing.

        Wire-format decoding lives in :meth:`ClassicProtocol.parse_results`;
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

        Layers on top of the raw merge: sensor-stream gating for Classic
        (keyed off ``brushing_state``), brush-head wear derivation,
        last-seen bookkeeping, and device-registry sync on model/firmware
        changes. Callers pass the output back through
        :meth:`async_set_updated_data` once they've confirmed a state
        transition is worth publishing.
        """
        old = self.data or {}
        new_data = old.copy()
        new_data.update(parsed)

        # Condor RoutineStatus.Mode is a position into the device's static
        # RoutineIDs list, not a routine id — resolve it against the list we
        # learned from the Sonicare port (persisted in new_data). Translate
        # only a freshly-arrived position from ``parsed`` so we never re-index
        # an already-resolved routine id on a later delta.
        if self._use_condor and "brushing_mode_value" in parsed:
            resolved = resolve_brushing_mode(
                new_data.get("routine_ids"), parsed["brushing_mode_value"]
            )
            if resolved is not None:
                new_data["brushing_mode_value"], new_data["brushing_mode"] = resolved

        # Sensor stream gates on brushing_state for both protocols. Classic
        # toggles the CCCD subscribe on the sensor char; Condor toggles the
        # enable register plus a Subscribe on the ``SensorData.b`` port — both
        # behind the same session gate via ``start_sensor_stream``.
        if "brushing_state" in parsed:
            old_state = old.get("brushing_state")
            new_state = parsed["brushing_state"]
            if new_state != old_state:
                if new_state == "on" and not self._sensor_subscribed:
                    self.hass.async_create_task(self._subscribe_sensor_data())
                elif old_state == "on" and self._sensor_subscribed:
                    self.hass.async_create_task(self._unsubscribe_sensor_data())

        # Counterfeit brush head detection
        self._update_counterfeit(old, new_data)

        # Derived: brush head wear percentage
        limit = new_data.get("brushhead_lifetime_limit")
        usage = new_data.get("brushhead_lifetime_usage")
        if limit and usage is not None and limit > 0:
            new_data["brushhead_wear_pct"] = min(round(usage / limit * 100, 1), 100.0)
        elif usage == 0 and self._is_valid_serial(
            new_data.get("brushhead_serial")
        ):
            # usage 0 only means "brand-new head" while a head is actually
            # attached (valid serial); a bare handle reports usage 0 too.
            new_data["brushhead_wear_pct"] = 0.0

        # Change detection: only update last_seen when data actually changed
        # or every 30s as heartbeat for availability tracking
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
        # Keep the protocol's mode-decode table in sync if we learn the model
        # from a live read (covers fresh pairs whose entry had no model yet).
        if model and not self._use_condor and self._protocol.model != model:
            self._protocol.model = model
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

    # ------------------------------------------------------------------
    # Counterfeit brush head detection
    # ------------------------------------------------------------------

    def _is_valid_serial(self, serial: str | None) -> bool:
        """True when the serial represents a successfully-read NFC chip.

        An all-zero serial means no chip was read — either no head is
        attached or the head carries no readable chip. Its byte length
        varies per model (7 or 8 bytes), so match the pattern, not a
        fixed string.
        """
        return serial is not None and any(c not in "0:" for c in serial)

    def _looks_counterfeit(self, data: dict) -> bool:
        """True when the serial indicates no valid NFC chip was read.

        Type is intentionally not checked — non-genuine heads sometimes
        report a plausible-looking type byte even without a valid serial.
        """
        return not self._is_valid_serial(data.get("brushhead_serial"))

    def _update_counterfeit(self, old: dict, new_data: dict) -> None:
        """Manage the counterfeit detection timer and issue state."""
        # Devices without a brush-head NFC service (e.g. HX63xx Kids) never
        # report a serial — skip detection entirely so we don't flag every
        # session. Drop any issue an earlier build may have raised, once.
        if not self._has_brushhead:
            if not self._counterfeit_cleanup_done:
                self._counterfeit_cleanup_done = True
                self._clear_counterfeit_issue()
            new_data["brushhead_counterfeit"] = False
            return

        serial = new_data.get("brushhead_serial")

        # Valid serial arrived — cancel timer and clear the alert. The issue
        # persists in the registry across restarts while _counterfeit_detected
        # resets to False, so clear once at startup too (cleanup flag) to drop
        # a stale warning, without spamming async_delete_issue every notify.
        if self._is_valid_serial(serial):
            self._cancel_counterfeit_timer()
            if self._counterfeit_detected or not self._counterfeit_cleanup_done:
                self._clear_counterfeit_issue()
            self._counterfeit_detected = False
            self._counterfeit_cleanup_done = True
            new_data["brushhead_counterfeit"] = False
            return

        # Propagate currently-detected state into the new snapshot
        new_data["brushhead_counterfeit"] = self._counterfeit_detected

        # Only run detection while the serial looks suspect (no valid NFC read)
        if not self._looks_counterfeit(new_data):
            self._cancel_counterfeit_timer()
            return

        brushing_now = (
            new_data.get("brushing_state") == "on"
            or new_data.get("handle_state_value") == 2
        )
        old_brushing = (
            old.get("brushing_state") == "on"
            or old.get("handle_state_value") == 2
        )

        if brushing_now and not old_brushing:
            # Brushing just started — begin 30 s countdown
            self._start_counterfeit_timer()
        elif not brushing_now and old_brushing:
            # Brushing stopped before the timer fired — cancel without alerting
            self._cancel_counterfeit_timer()
        elif brushing_now and self._counterfeit_timer_task is None and not self._counterfeit_detected:
            # Already brushing when we (re)connected — start timer if not running
            self._start_counterfeit_timer()

    def _start_counterfeit_timer(self) -> None:
        """Start (or restart) the counterfeit detection countdown."""
        self._cancel_counterfeit_timer()
        self._counterfeit_timer_task = self.entry.async_create_background_task(
            self.hass,
            self._counterfeit_timer_fired(),
            "philips_sonicare_counterfeit_timer",
        )

    def _cancel_counterfeit_timer(self) -> None:
        """Cancel any pending counterfeit detection timer."""
        if self._counterfeit_timer_task and not self._counterfeit_timer_task.done():
            self._counterfeit_timer_task.cancel()
        self._counterfeit_timer_task = None

    async def _counterfeit_timer_fired(self) -> None:
        """Fired after COUNTERFEIT_DETECTION_DELAY seconds of continuous brushing."""
        await asyncio.sleep(COUNTERFEIT_DETECTION_DELAY)
        if not self.data:
            return
        # Re-check conditions at fire time
        if not self._looks_counterfeit(self.data):
            return
        brushing_now = (
            self.data.get("brushing_state") == "on"
            or self.data.get("handle_state_value") == 2
        )
        if not brushing_now:
            return
        _LOGGER.warning(
            "%s: no valid brush head serial after %ds with the handle running "
            "— possible counterfeit or missing brush head",
            self.address,
            COUNTERFEIT_DETECTION_DELAY,
        )
        self._counterfeit_detected = True
        self.data["brushhead_counterfeit"] = True
        self._create_counterfeit_issue()
        self.async_set_updated_data(self.data)

    def _create_counterfeit_issue(self) -> None:
        """Raise an HA repair issue for the counterfeit brush head."""
        if not self.entry.options.get(CONF_WARN_COUNTERFEIT, DEFAULT_WARN_COUNTERFEIT):
            return
        from homeassistant.helpers import issue_registry as ir
        device_name = self.entry.data.get("device_name") or self.address
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"brushhead_counterfeit_{self.address}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="brushhead_counterfeit",
            translation_placeholders={"device": device_name},
        )

    def _clear_counterfeit_issue(self) -> None:
        """Remove the counterfeit repair issue."""
        from homeassistant.helpers import issue_registry as ir
        ir.async_delete_issue(
            self.hass, DOMAIN, f"brushhead_counterfeit_{self.address}"
        )

    async def _start_live_monitoring(self) -> None:
        """Persistent live connection with notifications.

        Direct BLE: waits for advertisement (ADV callback; dedup history is
        cleared before each wait, or D-Bus RSSI fallback on HA < 2026.5).
        ESP bridge: waits for ESP "ready" event (device auto-connected).

        Both modes are event-driven — no blind retries.
        """
        MAX_QUICK_RETRIES = 2  # quick retries after unexpected disconnect (Direct BLE only)

        while True:
            # ---- Wait for device to be available ----
            # Direct BLE: wait for ADV/D-Bus signal
            # ESP bridge: skips — connect() sets up listeners, then we wait below
            if not self._is_esp_bridge and not self.transport.is_connected:
                    if self._wake_event.is_set() and self._adv_wake:
                        self._consume_wake()
                        _LOGGER.info("Advertisement already pending — connecting to %s", self.address)
                    else:
                        # A set wake event without _adv_wake is the disconnect
                        # nudge, not a wake. Connecting on it blind-hammers a
                        # brush that just went to sleep (20 s timeouts) while
                        # the dedup guard stays closed — so consume the event
                        # and arm the ADV path instead.
                        self._consume_wake()
                        # Re-open the dedup guard so the next ADV wakes us even
                        # though the payload is identical to the last one seen.
                        self._clear_adv_history()
                        _LOGGER.debug("Waiting for advertisement from %s...", self.address)
                        await self._wake_event.wait()
                        self._consume_wake()
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
                        _LOGGER.info("%s: ESP bridge requires resubscription", self.address)
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
                            # ADVs seen before the drop no longer prove the
                            # brush is awake — it keeps advertising right up
                            # to the link drop when falling asleep (live-
                            # verified), so require a fresh ADV to reconnect.
                            self._adv_wake = False
                            # Wake the loop so it observes the disconnect
                            # before the brush reconnects — otherwise the
                            # 5 s poll below can miss the transition
                            # entirely and never re-run live setup.
                            self._wake_event.set()
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
                        # Stamp the disconnect counter BEFORE the reads: a
                        # disconnect landing anywhere after this point makes
                        # the wait loop below re-run the whole setup, even
                        # when the brush reconnected too fast for
                        # is_connected to ever read False here.
                        self._setup_disconnect_count = (
                            self.transport.disconnect_count
                        )

                    # Initial refresh + live subscriptions. The two protocols
                    # diverge here — Classic polls char-by-char then starts
                    # CCCD notifications; Condor runs its handshake, does a
                    # framed refresh_all, and subscribes named JSON ports.
                    if self._use_condor:
                        sub_count = await self._setup_condor_session()
                    else:
                        sub_count = await self._setup_classic_session()
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
                        _LOGGER.info("%s: ESP bridge rebooted — forcing re-setup", self.address)
                        break

                    # A disconnect we never saw as is_connected == False:
                    # the brush dropped and reconnected between two wakes
                    # of this loop. The bridge restored its subscriptions
                    # itself, but HA still needs the fresh read batch (and
                    # the "_connecting" flag cleared) — re-run live setup.
                    if (
                        self._is_esp_bridge
                        and isinstance(self.transport, EspBridgeTransport)
                        and self.transport.disconnect_count
                        != self._setup_disconnect_count
                    ):
                        _LOGGER.info(
                            "%s: reconnect detected — forcing re-setup",
                            self.address,
                        )
                        break

                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass

            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.error("%s: unexpected error in live monitoring: %s", self.address, err)
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
                "%s: ESP bridge v%s is outdated (minimum: v%s) — "
                "rebuild and flash your ESPHome device",
                self.address,
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
        self.data["brushhead_serial"] = None
        self.data["brushhead_nfc_version"] = None
        self.data["brushhead_type"] = None
        self.data["brushhead_date"] = None
        self.data["brushhead_lifetime_limit"] = None
        self.data["brushhead_lifetime_usage"] = None
        self.data["brushhead_wear_pct"] = None
        self.data["brushhead_ring_id"] = None
        self.data["brushhead_payload"] = None
        _LOGGER.info("%s: brush head removed — data cleared", self.address)

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
            _LOGGER.info("%s: brush head detected — reading NFC data", self.address)
            # Short delay to let the handle finish processing the NFC chip
            await asyncio.sleep(1)
            results = await self._protocol.read_chars(self._BRUSHHEAD_REREAD_CHARS)
            if any(v is not None for v in results.values()):
                new_data = self._process_results(results)
                self.async_set_updated_data(new_data)
                _LOGGER.info("%s: brush head data updated", self.address)
        finally:
            self._brushhead_read_pending = False

    @property
    def adapter_type(self) -> str:
        """Classify the active BLE transport.

        Returned values:

        - ``esp_bridge`` — our custom ESPHome component (proactive
          ``esp_ble_set_encryption()`` on bonded devices)
        - ``direct_ble`` — host BlueZ adapter via bleak (BlueZ encrypts
          proactively when a bond exists)
        - ``stock_proxy`` — stock ESPHome ``bluetooth_proxy`` reached
          via the habluetooth wrapper (Bluedroid lazy encryption)
        - ``unknown`` — not connected, or backend type not recognised

        The distinction matters for setup-time SMP behaviour (Issue #6,
        doff-1): only ``stock_proxy`` needs the eager probe-read before
        the subscribe burst.
        """
        if self._is_esp_bridge:
            return "esp_bridge"
        if self._last_adapter_type is not None and not self.transport.is_connected:
            return self._last_adapter_type
        client = getattr(self.transport, "_client", None)
        backend = getattr(client, "_backend", None) if client else None
        if backend is None:
            return self._last_adapter_type or "unknown"
        mod = type(backend).__module__ or ""
        if "bluezdbus" in mod:
            self._last_adapter_type = "direct_ble"
        elif "esphome" in mod:
            self._last_adapter_type = "stock_proxy"
        else:
            return self._last_adapter_type or "unknown"
        return self._last_adapter_type

    def _scanner_needs_eager_smp(self) -> bool:
        """True when the active transport is a stock ``bluetooth_proxy``."""
        return self.adapter_type == "stock_proxy"

    async def _eager_smp_probe(self) -> None:
        """Poll-read ``CHAR_HANDLE_STATE`` until it succeeds, signalling
        that SMP has finished and the link is encrypted.

        The first attempt triggers SMP on lazy-encrypt stacks (Bluedroid
        in stock ``bluetooth_proxy``). Each subsequent attempt costs
        one ATT round-trip (~50–100 ms) and either fails again with
        Insufficient-auth (SMP still in flight) or succeeds (SMP done).

        Returning only after a successful read means the regular read
        burst that follows runs against an already-encrypted link, so
        none of the user-facing chars need to retry — and the subscribe
        burst after that is unconditionally safe.

        Capped at a 3 s deadline so a genuinely broken bond doesn't
        hang setup; if we time out we proceed anyway (the ``_setup``
        path's existing per-char failures still apply).
        """
        if not self.transport.is_connected:
            return
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        deadline = t0 + 3.0
        poll_interval = 0.2
        attempt = 0
        while True:
            attempt += 1
            if not self.transport.is_connected:
                _LOGGER.debug(
                    "%s: SMP probe aborted — transport disconnected after "
                    "%d attempt(s)",
                    self.address, attempt,
                )
                return
            value = await self.transport.read_char(CHAR_HANDLE_STATE)
            elapsed_ms = (loop.time() - t0) * 1000
            if value is not None:
                _LOGGER.info(
                    "%s: SMP ready after %d probe(s) in %.0f ms — "
                    "link is encrypted",
                    self.address, attempt, elapsed_ms,
                )
                return
            if loop.time() >= deadline:
                err_text = (
                    self.transport.pop_read_error(CHAR_HANDLE_STATE)
                    or "no response"
                )
                _LOGGER.warning(
                    "%s: SMP probe didn't succeed within 3 s after %d "
                    "attempt(s) (last error: %s) — proceeding anyway, "
                    "subscribes may fail",
                    self.address, attempt, err_text,
                )
                return
            await asyncio.sleep(poll_interval)

    async def _setup_classic_session(self) -> int:
        """Classic protocol setup: batch reads then CCCD notifications.

        First connect reads every char we know about (model, firmware,
        brush head, …); subsequent reconnects stick to the dynamic
        subset. Returns the count of successful subscriptions so the
        caller can fail the session if the device answered nothing.
        """
        if self._scanner_needs_eager_smp():
            _LOGGER.info(
                "%s: stock bluetooth_proxy detected — polling SMP probe "
                "until link is encrypted",
                self.address,
            )
            await self._eager_smp_probe()
        else:
            _LOGGER.debug(
                "%s: transport handles encryption proactively — skipping "
                "eager SMP probe",
                self.address,
            )

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
        callers treat zero as a fatal session error just like Classic's
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
        if self._sensor_subscribed or not self.transport.is_connected:
            return
        # Classic delivers the stream through the char callback; Condor routes
        # it through the change-indication callback set at connect, so it does
        # not need ``_live_cb``.
        if not self._use_condor and not self._live_cb:
            return
        mask = self._compute_sensor_enable_mask()
        if mask == 0:
            _LOGGER.debug("%s: all sensors disabled in options — skipping sensor subscribe", self.address)
            return
        if await self._protocol.start_sensor_stream(mask, self._live_cb):
            self._sensor_subscribed = True
            _LOGGER.info("%s: sensor data stream subscribed (session active)", self.address)

    async def _unsubscribe_sensor_data(self) -> None:
        """Unsubscribe from sensor data stream and disable sensors."""
        if not self._sensor_subscribed:
            return
        await self._protocol.stop_sensor_stream()
        self._sensor_subscribed = False
        _LOGGER.info("%s: sensor data stream unsubscribed (session ended)", self.address)

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
        self._cancel_counterfeit_timer()

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
