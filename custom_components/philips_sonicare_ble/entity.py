# custom_components/philips_sonicare/entity.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.components.bluetooth import async_last_service_info
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .coordinator import PhilipsSonicareCoordinator
from .const import DOMAIN, CONF_ADDRESS, CONF_TRANSPORT_TYPE, TRANSPORT_ESP_BRIDGE, CONF_ESP_DEVICE_NAME

_LOGGER = logging.getLogger(__name__)


class PhilipsSonicareEntity(CoordinatorEntity[PhilipsSonicareCoordinator], RestoreEntity):
    """Base class for all Philips Sonicare entities."""

    _attr_has_entity_name = True
    _data_key: str | None = None
    _restore_type: type = str  # int, float, or str

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entry = entry
        self._device_id = entry.data.get("address", entry.data.get("esp_device_name", "unknown"))
        self._is_esp_bridge = (
            entry.data.get(CONF_TRANSPORT_TYPE) == TRANSPORT_ESP_BRIDGE
        )

        # Build device name from coordinator data
        model = coordinator.data.get("model_number") if coordinator.data else None
        name = f"Philips Sonicare {model}" if model else "Philips Sonicare"

        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            connections={(dr.CONNECTION_BLUETOOTH, self._device_id)},
            manufacturer="Philips",
            name=name,
        )

    async def async_added_to_hass(self) -> None:
        """Restore last known state into coordinator data."""
        await super().async_added_to_hass()
        if self._data_key is None:
            return
        if self.coordinator.data and self.coordinator.data.get(self._data_key) is not None:
            return
        last_state = await self.async_get_last_state()
        if not last_state or last_state.state in (None, "unknown", "unavailable"):
            return
        self._restore_from_state(last_state.state)

    def _restore_from_state(self, state: str) -> None:
        """Restore a coordinator data key from a state string."""
        if self.coordinator.data is None:
            self.coordinator.data = {}
        try:
            if self._restore_type is int:
                self.coordinator.data[self._data_key] = int(state)
            elif self._restore_type is float:
                self.coordinator.data[self._data_key] = float(state)
            else:
                self.coordinator.data[self._data_key] = state
        except (ValueError, TypeError):
            _LOGGER.debug("Could not restore %s from '%s'", self._data_key, state)

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
        # ESP bridge: use transport state and last_seen, no BLE advertisement check
        if self._is_esp_bridge:
            if self.coordinator.transport.is_connected:
                return True
            last_seen = self.coordinator.data.get("last_seen") if self.coordinator.data else None
            if last_seen:
                return (datetime.now(timezone.utc) - last_seen).total_seconds() < 600
            return False

        # Direct BLE: check if device is advertising
        service_info = async_last_service_info(self.hass, self._device_id)
        if service_info is not None:
            return True

        # Fallback: check last_seen freshness (10 min timeout)
        last_seen = self.coordinator.data.get("last_seen") if self.coordinator.data else None
        if last_seen:
            return (datetime.now(timezone.utc) - last_seen).total_seconds() < 600

        return False


class PhilipsBrushHeadEntity(PhilipsSonicareEntity):
    """Base class for entities on the Brush Head sub-device."""

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        # Override device_info to register on the brush head sub-device
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, f"{self._device_id}_brushhead")},
            manufacturer="Philips",
            name="Brush Head",
            via_device=(DOMAIN, self._device_id),
        )


class PhilipsBridgeEntity(PhilipsSonicareEntity):
    """Base class for entities on the ESP Bridge sub-device."""

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        # Override device_info to register on the bridge sub-device
        # Linking to the ESPHome parent device is done in __init__.py
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, f"{self._device_id}_bridge")},
            manufacturer="Espressif",
            name="ESP Bridge",
        )

    @property
    def available(self) -> bool:
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
