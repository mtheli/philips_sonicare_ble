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
    BRUSHHEAD_TYPE_FAMILY,
    CONF_TRANSPORT_TYPE,
    CONF_SERVICES,
    TRANSPORT_ESP_BRIDGE,
    SVC_BRUSHHEAD,
    SVC_CONDOR,
    SVC_STORAGE,
    SVC_SENSOR,
    SECTORS_PREMIUM,
    number_of_sectors_for_model,
    current_sector,
)
from .condor_adapter import CONDOR_BRUSHING_MODES
from .entity import PhilipsSonicareEntity, PhilipsBrushHeadEntity, PhilipsConnectionEntity

# Union of every brushing-mode label the integration may receive. Classic
# (Prestige) and Condor (HX742X+) share the 0..5 ordinal but use different
# labels for modes 1, 2 and 4 (e.g. ``white_plus`` vs ``white``). The
# read-only sensor accepts both label sets so SensorDeviceClass.ENUM
# validation passes regardless of which protocol drives the device.
ALL_BRUSHING_MODE_LABELS: list[str] = list(
    dict.fromkeys(
        list(BRUSHING_MODES.values()) + list(CONDOR_BRUSHING_MODES.values())
    )
)
BRUSHING_MODE_VALUE_BY_LABEL: dict[str, int] = {
    **{v: k for k, v in BRUSHING_MODES.items()},
    **{v: k for k, v in CONDOR_BRUSHING_MODES.items()},
}

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Philips Sonicare sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    services = {s.lower() for s in entry.data.get(CONF_SERVICES, [])}

    model = entry.data.get("model", "")
    is_kids = model.upper().startswith("HX63")
    is_condor = SVC_CONDOR.lower() in services

    entities: list[PhilipsSonicareEntity] = [
        # Toothbrush handle sensors
        SonicareBatterySensor(coordinator, entry),
        SonicareHandleStateSensor(coordinator, entry),
        SonicareBrushingModeSensor(coordinator, entry),
        SonicareIntensitySensor(coordinator, entry),
        SonicareBrushingTimeSensor(coordinator, entry),
        SonicareRoutineLengthSensor(coordinator, entry),
        SonicareRoutineCountdownSensor(coordinator, entry),
        SonicareNumberOfSectorsSensor(coordinator, entry),
        SonicareSectorSensor(coordinator, entry),
        SonicareModelNumberSensor(coordinator, entry),
        SonicareFirmwareSensor(coordinator, entry),
        SonicareLastSeenSensor(coordinator, entry),
        SonicareActivitySensor(coordinator, entry),
        SonicareAdapterSensor(coordinator, entry),
        SonicareAdapterTypeSensor(coordinator, entry),
    ]

    # Classic-only handle counters with no Condor equivalent:
    #  - Motor Runtime: the Condor protocol exposes no cumulative
    #    motor-runtime field, so the sensor could only ever read Unknown
    #    (Issue #23).
    #  - Handle Time: on Classic this is a Uint32 operating-seconds counter;
    #    on Condor the same name is the handle's real-time clock (a wall-clock
    #    timestamp), which this integration never sets — so it free-runs and
    #    reads as an implausible multi-hundred-day "duration" (Issue #23).
    #    Not a duration, not telemetry.
    # Stale entities created by earlier versions on Condor devices are
    # cleaned up once by async_migrate_entry (config-entry minor_version 2).
    if not is_condor:
        entities.append(SonicareMotorRuntimeSensor(coordinator, entry))
        entities.append(SonicareHandleTimeSensor(coordinator, entry))

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
    elif is_condor:
        # Condor exposes the same pressure-state and temperature telemetry
        # through the SensorData.b port, but not the raw pressure grams value
        # (its firmware streams the on-change state frame only), so the raw
        # pressure sensor is omitted here.
        entities.extend([
            SonicarePressureStateSensor(coordinator, entry),
            SonicareTemperatureSensor(coordinator, entry),
        ])

    # Brush head sub-device sensors (NFC brush head detection).
    #
    # Classic devices expose a dedicated GATT service (SVC_BRUSHHEAD) and
    # populate all nine NFC chars. Condor (HX742X+) delivers a subset via
    # the BrushHead JSON port — Serial / Limit / Usage / RingId /
    # NfcTagVersion confirmed live on FW 1.8.20.0; Date / Type / Payload
    # are Classic-only and the sensors for them would stay unavailable,
    # so they're omitted on Condor.
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
    elif SVC_CONDOR.lower() in services:
        entities.extend([
            SonicareBrushHeadWearSensor(coordinator, entry),
            SonicareBrushHeadUsageSensor(coordinator, entry),
            SonicareBrushHeadLimitSensor(coordinator, entry),
            SonicareBrushHeadSerialSensor(coordinator, entry),
            SonicareBrushHeadRingIdSensor(coordinator, entry),
            SonicareBrushHeadNfcVersionSensor(coordinator, entry),
        ])

    # RSSI sensor (direct BLE only — ESP bridge has no advertisement RSSI)
    if entry.data.get(CONF_TRANSPORT_TYPE) != TRANSPORT_ESP_BRIDGE:
        entities.append(SonicareRssiSensor(coordinator, entry))

    # ESP bridge sub-device sensor (only for ESP transport)
    if entry.data.get(CONF_TRANSPORT_TYPE) == TRANSPORT_ESP_BRIDGE:
        entities.append(SonicareBridgeVersionSensor(coordinator, entry))
        entities.append(SonicareBridgeBootTimeSensor(coordinator, entry))

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
    """Brushing mode sensor — accepts both Classic and Condor labels."""

    _attr_translation_key = "brushing_mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ALL_BRUSHING_MODE_LABELS
    _data_key = "brushing_mode"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushing_mode"

    def _restore_from_state(self, state: str) -> None:
        if self.coordinator.data is None:
            self.coordinator.data = {}
        self.coordinator.data["brushing_mode"] = state
        if state in BRUSHING_MODE_VALUE_BY_LABEL:
            self.coordinator.data["brushing_mode_value"] = (
                BRUSHING_MODE_VALUE_BY_LABEL[state]
            )

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
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 0
    _data_key = "brushing_time"

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
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 0
    _data_key = "routine_length"

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
    _attr_state_class = SensorStateClass.MEASUREMENT
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
# Number of Sectors (static per model)
# ---------------------------------------------------------------------------
class SonicareNumberOfSectorsSensor(PhilipsSonicareEntity, SensorEntity):
    """Number of brushing sectors (zones) for this model."""

    _attr_translation_key = "number_of_sectors"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:grid"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_number_of_sectors"
        self._sectors = number_of_sectors_for_model(entry.data.get("model", ""))

    @property
    def native_value(self) -> int:
        return self._sectors


# ---------------------------------------------------------------------------
# Sector (current, derived from brushing time)
# ---------------------------------------------------------------------------
class SonicareSectorSensor(PhilipsSonicareEntity, SensorEntity):
    """Current brushing sector, derived from elapsed time and the
    mode-specific visit sequence (so White+/Gum Health revisits are
    reported as the original anatomical sector, not a 7th/8th step)."""

    _attr_translation_key = "sector"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        f"sector_{i}" for i in range(1, SECTORS_PREMIUM + 1)
    ] + ["success", "no_sector"]
    _attr_icon = "mdi:map-marker"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_sector"
        self._model = entry.data.get("model", "")

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        brushing = (
            self.coordinator.data.get("brushing_state") == "on"
            or self.coordinator.data.get("handle_state_value") == 2
        )
        elapsed = self.coordinator.data.get("brushing_time")
        routine = self.coordinator.data.get("routine_length")
        mode = self.coordinator.data.get("brushing_mode")
        if not brushing or elapsed is None or elapsed <= 0:
            return "no_sector"
        if routine is None or routine <= 0:
            return "no_sector"
        if elapsed >= routine:
            return "success"
        sector = current_sector(self._model, mode, elapsed, routine)
        if sector is None:
            return "no_sector"
        return f"sector_{sector}"


# ---------------------------------------------------------------------------
# Session ID
# ---------------------------------------------------------------------------
class SonicareSessionIdSensor(PhilipsSonicareEntity, SensorEntity):
    """Current session ID."""

    _attr_translation_key = "session_id"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:identifier"
    _data_key = "session_id"

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

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushhead_type"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("brushhead_type")

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        family = BRUSHHEAD_TYPE_FAMILY.get(self.native_value)
        if not family:
            return None
        letter, name = family
        return {"family_letter": letter, "family_name": name}


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
class SonicareLastSeenSensor(PhilipsConnectionEntity, SensorEntity):
    """Last time the device was seen."""

    _attr_translation_key = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _data_key = "last_seen"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_last_seen"

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
        if not data:
            return "off"

        if data.get("_connecting"):
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
    """Pressure state enum (ok, optimal, too_high)."""

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
class SonicareRssiSensor(PhilipsConnectionEntity, SensorEntity):
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
        # When actively connected, prefer the RSSI from the scanner carrying
        # the link — the global advert cache may show a stronger signal on a
        # different scanner that isn't serving the connection.
        live_rssi = self.coordinator.transport.connection_rssi
        if live_rssi is not None:
            return live_rssi
        service_info = async_last_service_info(self.hass, self._device_id)
        if service_info is None or service_info.rssi is None:
            return None
        # -127 is habluetooth/BlueZ sentinel for "no fresh advertisement"
        if service_info.rssi <= -127:
            return None
        return service_info.rssi


# ---------------------------------------------------------------------------
# Adapter (host hciN / ESPHome proxy name / ESP bridge name)
# ---------------------------------------------------------------------------
class SonicareAdapterSensor(PhilipsConnectionEntity, SensorEntity):
    """Adapter currently carrying the BLE connection."""

    _attr_translation_key = "adapter"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_adapter"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.transport.connection_path


class SonicareAdapterTypeSensor(PhilipsConnectionEntity, SensorEntity):
    """Classification of the active BLE transport.

    Surfaces the same enum the coordinator uses internally to decide
    whether an eager-SMP probe-read is required before the subscribe
    burst. Useful as a diagnostic when troubleshooting connection
    quality across mixed adapter setups.
    """

    _attr_translation_key = "adapter_type"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["direct_ble", "esp_bridge", "stock_proxy", "unknown"]
    _attr_icon = "mdi:transit-connection-variant"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_adapter_type"

    @property
    def native_value(self) -> str:
        return self.coordinator.adapter_type


# ---------------------------------------------------------------------------
# ESP Bridge Version (on bridge sub-device)
# ---------------------------------------------------------------------------
class SonicareBridgeVersionSensor(PhilipsConnectionEntity, SensorEntity):
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


# ---------------------------------------------------------------------------
# ESP Bridge Last Boot (on bridge sub-device)
# ---------------------------------------------------------------------------
class SonicareBridgeBootTimeSensor(PhilipsConnectionEntity, SensorEntity):
    """ESP bridge boot timestamp (refreshed only on detected restart)."""

    _attr_translation_key = "bridge_boot_time"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_bridge_boot_time"

    @property
    def native_value(self) -> datetime | None:
        transport = self.coordinator.transport
        return getattr(transport, "bridge_boot_time", None)
