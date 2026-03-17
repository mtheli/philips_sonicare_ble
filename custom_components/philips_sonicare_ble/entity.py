# custom_components/philips_sonicare/entity.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.bluetooth import async_last_service_info
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .coordinator import PhilipsSonicareCoordinator
from .const import DOMAIN, CONF_ADDRESS

_LOGGER = logging.getLogger(__name__)


class PhilipsSonicareEntity(CoordinatorEntity[PhilipsSonicareCoordinator]):
    """Base class for all Philips Sonicare entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entry = entry
        self._device_id = entry.data["address"]

        # Build device name from coordinator data
        model = coordinator.data.get("model_number") if coordinator.data else None
        name = f"Philips Sonicare {model}" if model else "Philips Sonicare"

        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            connections={(dr.CONNECTION_BLUETOOTH, self._device_id)},
            manufacturer="Philips",
            name=name,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Dynamic icon update
        if hasattr(self, "icon"):
            try:
                new_icon = self.icon
                if getattr(self, "_attr_icon", None) != new_icon:
                    self._attr_icon = new_icon
            except Exception as err:
                _LOGGER.debug(
                    "Failed to update dynamic icon for %s: %s",
                    self.entity_id or self.__class__.__name__,
                    err,
                )

        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return True if the device is reachable."""
        # Check if device is advertising via BLE
        service_info = async_last_service_info(self.hass, self._device_id)
        if service_info is not None:
            return True

        # Fallback: check last_seen freshness (10 min timeout)
        last_seen = self.coordinator.data.get("last_seen") if self.coordinator.data else None
        if last_seen:
            return (datetime.now(timezone.utc) - last_seen).total_seconds() < 600

        return False
