# custom_components/philips_sonicare/binary_sensor.py
from __future__ import annotations

import logging
from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PhilipsSonicareCoordinator
from .entity import PhilipsSonicareEntity
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Philips Sonicare binary sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities = [
        SonicareIsBrushingBinarySensor(coordinator, entry),
        SonicareIsChargingBinarySensor(coordinator, entry),
    ]

    async_add_entities(entities)


class SonicareIsBrushingBinarySensor(PhilipsSonicareEntity, BinarySensorEntity):
    """Binary sensor showing if the toothbrush is currently brushing."""

    _attr_translation_key = "is_brushing"
    _attr_icon = "mdi:toothbrush-electric"

    def __init__(
        self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_is_brushing"

    @property
    def is_on(self) -> bool:
        """Return True if the toothbrush is currently brushing.

        True when handle_state_value == 2 (run) OR brushing_state_value == 1 (on).
        """
        if not self.coordinator.data:
            return False

        handle_state = self.coordinator.data.get("handle_state_value")
        brushing_state = self.coordinator.data.get("brushing_state_value")

        return handle_state == 2 or brushing_state == 1


class SonicareIsChargingBinarySensor(PhilipsSonicareEntity, BinarySensorEntity):
    """Binary sensor showing if the toothbrush is charging."""

    _attr_translation_key = "is_charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(
        self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_is_charging"

    @property
    def is_on(self) -> bool:
        """Return True if the toothbrush is currently charging.

        True when handle_state_value == 3 (charge).
        """
        if not self.coordinator.data:
            return False

        return self.coordinator.data.get("handle_state_value") == 3
