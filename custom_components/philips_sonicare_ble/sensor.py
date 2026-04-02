# custom_components/philips_sonicare/sensor.py
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.const import UnitOfTime, PERCENTAGE

from .coordinator import PhilipsSonicareCoordinator
from homeassistant.components.bluetooth import async_last_service_info

from .const import (
    DOMAIN,
    HANDLE_STATES,
    BRUSHING_MODES,
    BRUSHING_STATES,
    INTENSITIES,
    PRESSURE_ALARM_STATES,
    BRUSHHEAD_TYPES,
    CONF_TRANSPORT_TYPE,
    CONF_SERVICES,
    TRANSPORT_ESP_BRIDGE,
    SVC_BRUSHHEAD,
    SVC_STORAGE,
    SVC_SENSOR,
)
from .entity import PhilipsSonicareEntity, PhilipsBrushHeadEntity, PhilipsBridgeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Philips Sonicare sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    services = {s.lower() for s in entry.data.get(CONF_SERVICES, [])}

    model = entry.data.get("model", "")
    is_kids = model.upper().startswith("HX63")

    entities: list[PhilipsSonicareEntity] = [
        # Toothbrush handle sensors
        SonicareBatterySensor(coordinator, entry),
        SonicareHandleStateSensor(coordinator, entry),
        SonicareBrushingModeSensor(coordinator, entry),
        SonicareIntensitySensor(coordinator, entry),
        SonicareBrushingTimeSensor(coordinator, entry),
        SonicareRoutineLengthSensor(coordinator, entry),
        SonicareRoutineCountdownSensor(coordinator, entry),
        SonicareMotorRuntimeSensor(coordinator, entry),
        SonicareModelNumberSensor(coordinator, entry),
        SonicareFirmwareSensor(coordinator, entry),
        SonicareLastSeenSensor(coordinator, entry),
        SonicareHandleTimeSensor(coordinator, entry),
        SonicareActivitySensor(coordinator, entry),
    ]

    # Not available on Kids devices (HX63xx)
    if not is_kids:
        entities.append(SonicareBrushingStateSensor(coordinator, entry))
        entities.append(SonicareSessionIdSensor(coordinator, entry))

    # Storage service sensors (session history)
    if SVC_STORAGE.lower() in services:
        entities.extend([
            SonicareLatestSessionIdSensor(coordinator, entry),
            SonicareSessionCountSensor(coordinator, entry),
        ])

    # Sensor/IMU service sensors (pressure, temperature)
    if SVC_SENSOR.lower() in services:
        entities.extend([
            SonicarePressureSensor(coordinator, entry),
            SonicarePressureStateSensor(coordinator, entry),
            SonicareTemperatureSensor(coordinator, entry),
        ])

    # Brush head sub-device sensors (NFC brush head detection)
    if SVC_BRUSHHEAD.lower() in services:
        entities.extend([
            SonicareBrushHeadWearSensor(coordinator, entry),
            SonicareBrushHeadUsageSensor(coordinator, entry),
            SonicareBrushHeadLimitSensor(coordinator, entry),
            SonicareBrushHeadSerialSensor(coordinator, entry),
            SonicareBrushHeadDateSensor(coordinator, entry),
            SonicareBrushHeadRingIdSensor(coordinator, entry),
            SonicareBrushHeadNfcVersionSensor(coordinator, entry),
            SonicareBrushHeadTypeSensor(coordinator, entry),
            SonicareBrushHeadPayloadSensor(coordinator, entry),
        ])

    # RSSI sensor (direct BLE only — ESP bridge has no advertisement RSSI)
    if entry.data.get(CONF_TRANSPORT_TYPE) != TRANSPORT_ESP_BRIDGE:
        entities.append(SonicareRssiSensor(coordinator, entry))

    # ESP bridge sub-device sensor (only for ESP transport)
    if entry.data.get(CONF_TRANSPORT_TYPE) == TRANSPORT_ESP_BRIDGE:
        entities.append(SonicareBridgeVersionSensor(coordinator, entry))

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------
class SonicareBatterySensor(PhilipsSonicareEntity, SensorEntity):
    """Battery level sensor."""

    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _data_key = "battery"
    _restore_type = int

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_battery"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("battery")


# ---------------------------------------------------------------------------
# Handle State
# ---------------------------------------------------------------------------
class SonicareHandleStateSensor(PhilipsSonicareEntity, SensorEntity):
    """Handle state sensor (off, standby, run, charge, ...)."""

    _attr_translation_key = "handle_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(HANDLE_STATES.values())
    _data_key = "handle_state"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_handle_state"

    def _restore_from_state(self, state: str) -> None:
        if self.coordinator.data is None:
            self.coordinator.data = {}
        self.coordinator.data["handle_state"] = state
        reverse = {v: k for k, v in HANDLE_STATES.items()}
        if state in reverse:
            self.coordinator.data["handle_state_value"] = reverse[state]

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("handle_state")

    @property
    def icon(self) -> str:
        state = self.coordinator.data.get("handle_state") if self.coordinator.data else None
        if state == "run":
            return "mdi:toothbrush-electric"
        if state == "charge":
            return "mdi:battery-charging"
        if state == "standby":
            return "mdi:toothbrush"
        return "mdi:toothbrush"


# ---------------------------------------------------------------------------
# Brushing Mode
# ---------------------------------------------------------------------------
class SonicareBrushingModeSensor(PhilipsSonicareEntity, SensorEntity):
    """Brushing mode sensor (clean, white+, gum_health, deep_clean+)."""

    _attr_translation_key = "brushing_mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(BRUSHING_MODES.values())
    _data_key = "brushing_mode"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushing_mode"

    def _restore_from_state(self, state: str) -> None:
        if self.coordinator.data is None:
            self.coordinator.data = {}
        self.coordinator.data["brushing_mode"] = state
        reverse = {v: k for k, v in BRUSHING_MODES.items()}
        if state in reverse:
            self.coordinator.data["brushing_mode_value"] = reverse[state]

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushing_mode")


# ---------------------------------------------------------------------------
# Brushing State
# ---------------------------------------------------------------------------
class SonicareBrushingStateSensor(PhilipsSonicareEntity, SensorEntity):
    """Brushing state sensor (off, on, pause)."""

    _attr_translation_key = "brushing_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(BRUSHING_STATES.values())
    _data_key = "brushing_state"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushing_state"

    def _restore_from_state(self, state: str) -> None:
        if self.coordinator.data is None:
            self.coordinator.data = {}
        self.coordinator.data["brushing_state"] = state
        reverse = {v: k for k, v in BRUSHING_STATES.items()}
        if state in reverse:
            self.coordinator.data["brushing_state_value"] = reverse[state]

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushing_state")


# ---------------------------------------------------------------------------
# Intensity
# ---------------------------------------------------------------------------
class SonicareIntensitySensor(PhilipsSonicareEntity, SensorEntity):
    """Intensity sensor (low, medium, high)."""

    _attr_translation_key = "intensity"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(INTENSITIES.values())
    _data_key = "intensity"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_intensity"

    def _restore_from_state(self, state: str) -> None:
        if self.coordinator.data is None:
            self.coordinator.data = {}
        self.coordinator.data["intensity"] = state
        reverse = {v: k for k, v in INTENSITIES.items()}
        if state in reverse:
            self.coordinator.data["intensity_value"] = reverse[state]

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("intensity")


# ---------------------------------------------------------------------------
# Brushing Time
# ---------------------------------------------------------------------------
class SonicareBrushingTimeSensor(PhilipsSonicareEntity, SensorEntity):
    """Current brushing time in seconds."""

    _attr_translation_key = "brushing_time"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 0
    _data_key = "brushing_time"
    _restore_type = int

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushing_time"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushing_time")


# ---------------------------------------------------------------------------
# Routine Length
# ---------------------------------------------------------------------------
class SonicareRoutineLengthSensor(PhilipsSonicareEntity, SensorEntity):
    """Routine length in seconds."""

    _attr_translation_key = "routine_length"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 0
    _data_key = "routine_length"
    _restore_type = int

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_routine_length"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("routine_length")


# ---------------------------------------------------------------------------
# Routine Countdown
# ---------------------------------------------------------------------------
class SonicareRoutineCountdownSensor(PhilipsSonicareEntity, SensorEntity):
    """Remaining brushing time (routine_length - brushing_time)."""

    _attr_translation_key = "routine_countdown"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 0
    _data_key = "brushing_time"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_routine_countdown"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        # Only show countdown during active brushing
        brushing = (
            self.coordinator.data.get("brushing_state") == "on"
            or self.coordinator.data.get("handle_state_value") == 2
        )
        if not brushing:
            return None
        routine = self.coordinator.data.get("routine_length")
        elapsed = self.coordinator.data.get("brushing_time")
        if routine is None or elapsed is None:
            return None
        return max(0, routine - elapsed)


# ---------------------------------------------------------------------------
# Session ID
# ---------------------------------------------------------------------------
class SonicareSessionIdSensor(PhilipsSonicareEntity, SensorEntity):
    """Current session ID."""

    _attr_translation_key = "session_id"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:identifier"
    _data_key = "session_id"
    _restore_type = int

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_session_id"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("session_id")


# ---------------------------------------------------------------------------
# Latest Session ID
# ---------------------------------------------------------------------------
class SonicareLatestSessionIdSensor(PhilipsSonicareEntity, SensorEntity):
    """Latest session ID."""

    _attr_translation_key = "latest_session_id"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:identifier"
    _data_key = "latest_session_id"
    _restore_type = int

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_latest_session_id"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("latest_session_id")


# ---------------------------------------------------------------------------
# Session Count
# ---------------------------------------------------------------------------
class SonicareSessionCountSensor(PhilipsSonicareEntity, SensorEntity):
    """Total session count."""

    _attr_translation_key = "session_count"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"
    _data_key = "session_count"
    _restore_type = int
    _static_sensor = True

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_session_count"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("session_count")


# ---------------------------------------------------------------------------
# Motor Runtime
# ---------------------------------------------------------------------------
class SonicareMotorRuntimeSensor(PhilipsSonicareEntity, SensorEntity):
    """Total motor runtime in seconds."""

    _attr_translation_key = "motor_runtime"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:engine"
    _data_key = "motor_runtime"
    _restore_type = int
    _static_sensor = True

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_motor_runtime"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("motor_runtime")


# ---------------------------------------------------------------------------
# Brush Head Wear %
# ---------------------------------------------------------------------------
class SonicareBrushHeadWearSensor(PhilipsBrushHeadEntity, SensorEntity):
    """Brush head wear percentage."""

    _attr_translation_key = "brushhead_wear"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-donut"
    _data_key = "brushhead_wear_pct"
    _restore_type = float

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushhead_wear"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushhead_wear_pct")


# ---------------------------------------------------------------------------
# Brush Head Usage
# ---------------------------------------------------------------------------
class SonicareBrushHeadUsageSensor(PhilipsBrushHeadEntity, SensorEntity):
    """Brush head lifetime usage (raw value)."""

    _attr_translation_key = "brushhead_usage"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:timer-sand"
    _data_key = "brushhead_lifetime_usage"
    _restore_type = int

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushhead_usage"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushhead_lifetime_usage")


# ---------------------------------------------------------------------------
# Brush Head Limit
# ---------------------------------------------------------------------------
class SonicareBrushHeadLimitSensor(PhilipsBrushHeadEntity, SensorEntity):
    """Brush head lifetime limit (raw value)."""

    _attr_translation_key = "brushhead_limit"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:timer-sand-complete"
    _data_key = "brushhead_lifetime_limit"
    _restore_type = int

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushhead_limit"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushhead_lifetime_limit")


# ---------------------------------------------------------------------------
# Brush Head Serial
# ---------------------------------------------------------------------------
class SonicareBrushHeadSerialSensor(PhilipsBrushHeadEntity, SensorEntity):
    """Brush head serial number."""

    _attr_translation_key = "brushhead_serial"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:barcode"
    _data_key = "brushhead_serial"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushhead_serial"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushhead_serial")


# ---------------------------------------------------------------------------
# Brush Head Date
# ---------------------------------------------------------------------------
class SonicareBrushHeadDateSensor(PhilipsBrushHeadEntity, SensorEntity):
    """Brush head production date."""

    _attr_translation_key = "brushhead_date"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:calendar"
    _data_key = "brushhead_date"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushhead_date"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushhead_date")


# ---------------------------------------------------------------------------
# Brush Head Ring ID
# ---------------------------------------------------------------------------
class SonicareBrushHeadRingIdSensor(PhilipsBrushHeadEntity, SensorEntity):
    """Brush head NFC ring ID."""

    _attr_translation_key = "brushhead_ring_id"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:nfc-variant"
    _data_key = "brushhead_ring_id"
    _restore_type = int

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushhead_ring_id"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushhead_ring_id")


# ---------------------------------------------------------------------------
# Brush Head NFC Version
# ---------------------------------------------------------------------------
class SonicareBrushHeadNfcVersionSensor(PhilipsBrushHeadEntity, SensorEntity):
    """Brush head NFC chip version."""

    _attr_translation_key = "brushhead_nfc_version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:nfc"
    _data_key = "brushhead_nfc_version"
    _restore_type = int

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushhead_nfc_version"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushhead_nfc_version")


# ---------------------------------------------------------------------------
# Brush Head Type
# ---------------------------------------------------------------------------
class SonicareBrushHeadTypeSensor(PhilipsBrushHeadEntity, SensorEntity):
    """Brush head type (adaptive_clean, sensitive, ...)."""

    _attr_translation_key = "brushhead_type"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(BRUSHHEAD_TYPES.values())
    _attr_icon = "mdi:toothbrush"
    _data_key = "brushhead_type"
    _restore_type = str

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushhead_type"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushhead_type")


# ---------------------------------------------------------------------------
# Brush Head NFC Payload (raw hex)
# ---------------------------------------------------------------------------
class SonicareBrushHeadPayloadSensor(PhilipsBrushHeadEntity, SensorEntity):
    """Brush head NFC payload (raw hex data)."""

    _attr_translation_key = "brushhead_payload"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:nfc-variant"
    _data_key = "brushhead_payload"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushhead_payload"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushhead_payload")


# ---------------------------------------------------------------------------
# Model Number
# ---------------------------------------------------------------------------
class SonicareModelNumberSensor(PhilipsSonicareEntity, SensorEntity):
    """Model number."""

    _attr_translation_key = "model_number"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:information-outline"
    _data_key = "model_number"
    _static_sensor = True

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_model_number"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("model_number")


# ---------------------------------------------------------------------------
# Firmware
# ---------------------------------------------------------------------------
class SonicareFirmwareSensor(PhilipsSonicareEntity, SensorEntity):
    """Firmware revision."""

    _attr_translation_key = "firmware"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chip"
    _data_key = "firmware"
    _static_sensor = True

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_firmware"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("firmware")


# ---------------------------------------------------------------------------
# Last Seen
# ---------------------------------------------------------------------------
class SonicareLastSeenSensor(PhilipsSonicareEntity, SensorEntity):
    """Last time the device was seen."""

    _attr_translation_key = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _data_key = "last_seen"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_last_seen"

    def _restore_from_state(self, state: str) -> None:
        if self.coordinator.data is None:
            self.coordinator.data = {}
        try:
            self.coordinator.data["last_seen"] = datetime.fromisoformat(state)
        except (ValueError, TypeError):
            pass

    @property
    def native_value(self) -> datetime | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("last_seen")

    @property
    def available(self) -> bool:
        """Always available so the user can see when the device was last seen."""
        return True


# ---------------------------------------------------------------------------
# Activity (composite state derived from handle_state + brushing_state)
# ---------------------------------------------------------------------------
_ACTIVITY_STATES = ["initializing", "off", "standby", "brushing", "paused", "charging"]

_ACTIVITY_ICONS = {
    "initializing": "mdi:loading",
    "off": "mdi:power-standby",
    "standby": "mdi:toothbrush",
    "brushing": "mdi:toothbrush-electric",
    "paused": "mdi:pause-circle-outline",
    "charging": "mdi:battery-charging-outline",
}


class SonicareActivitySensor(PhilipsSonicareEntity, SensorEntity):
    """Composite activity sensor derived from handle_state + brushing_state."""

    _attr_translation_key = "activity"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = _ACTIVITY_STATES

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_activity"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        data = self.coordinator.data
        if not data or data.get("handle_state") is None:
            return "initializing"

        handle = data.get("handle_state")
        brushing = data.get("brushing_state")

        if handle == "run" or brushing == "on":
            return "brushing"
        if brushing == "pause":
            return "paused"
        if handle == "charge":
            return "charging"
        if handle == "standby":
            return "standby"
        return "off"

    @property
    def icon(self) -> str:
        return _ACTIVITY_ICONS.get(self.native_value, "mdi:help-circle")


# ---------------------------------------------------------------------------
# Pressure (from sensor data stream 0x4130)
# ---------------------------------------------------------------------------
class SonicarePressureSensor(PhilipsSonicareEntity, SensorEntity):
    """Pressure sensor from IMU data stream (value in grams)."""

    _attr_translation_key = "pressure"
    _attr_icon = "mdi:arrow-collapse-down"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "g"
    _data_key = "pressure"
    _restore_type = int

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_pressure"

    @property
    def available(self) -> bool:
        if not self.coordinator.data:
            return False
        return self.coordinator.data.get("brushing_state") == "on"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("pressure")


# ---------------------------------------------------------------------------
# Temperature (from sensor data stream 0x4130)
# ---------------------------------------------------------------------------
class SonicareTemperatureSensor(PhilipsSonicareEntity, SensorEntity):
    """Temperature sensor from IMU data stream."""

    _attr_translation_key = "temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "\u00b0C"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _data_key = "temperature"
    _restore_type = float

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_temperature"

    @property
    def available(self) -> bool:
        if not self.coordinator.data:
            return False
        return self.coordinator.data.get("brushing_state") == "on"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("temperature")


# ---------------------------------------------------------------------------
# Pressure State (from sensor data stream 0x4130)
# ---------------------------------------------------------------------------
class SonicarePressureStateSensor(PhilipsSonicareEntity, SensorEntity):
    """Pressure state enum (no_contact, optimal, too_high)."""

    _attr_translation_key = "pressure_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(PRESSURE_ALARM_STATES.values())
    _attr_icon = "mdi:arrow-collapse-down"
    _data_key = "pressure_state"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_pressure_state"

    @property
    def available(self) -> bool:
        if not self.coordinator.data:
            return False
        return self.coordinator.data.get("brushing_state") == "on"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("pressure_state")


# ---------------------------------------------------------------------------
# Handle Time (total operating time since manufacture)
# ---------------------------------------------------------------------------
class SonicareHandleTimeSensor(PhilipsSonicareEntity, SensorEntity):
    """Total handle operating time in seconds."""

    _attr_translation_key = "handle_time"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 0
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clock-outline"
    _data_key = "handle_time"
    _restore_type = int
    _static_sensor = True

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_handle_time"

    @property
    def native_value(self) -> int | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("handle_time")


# ---------------------------------------------------------------------------
# RSSI (BLE signal strength, direct BLE only)
# ---------------------------------------------------------------------------
class SonicareRssiSensor(PhilipsSonicareEntity, SensorEntity):
    """BLE RSSI signal strength."""

    _attr_translation_key = "rssi"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = "dBm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_rssi"

    @property
    def native_value(self) -> int | None:
        service_info = async_last_service_info(self.hass, self._device_id)
        if service_info is None:
            return None
        return service_info.rssi


# ---------------------------------------------------------------------------
# ESP Bridge Version (on bridge sub-device)
# ---------------------------------------------------------------------------
class SonicareBridgeVersionSensor(PhilipsBridgeEntity, SensorEntity):
    """ESP bridge firmware component version."""

    _attr_translation_key = "bridge_version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_bridge_version"

    @property
    def native_value(self) -> str | None:
        transport = self.coordinator.transport
        return getattr(transport, "bridge_version", None)
