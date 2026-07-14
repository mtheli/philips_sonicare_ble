# custom_components/philips_sonicare/entity.py
from __future__ import annotations

import logging

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.bluetooth import async_last_service_info
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .coordinator import PhilipsSonicareCoordinator
from .const import (
    DOMAIN,
    CONF_ADDRESS,
    CONF_TRANSPORT_TYPE,
    TRANSPORT_ESP_BRIDGE,
    CONF_ESP_DEVICE_NAME,
    CONF_DEVICE_NAME,
    CONF_AREA,
)

_LOGGER = logging.getLogger(__name__)


class PhilipsSonicareEntity(CoordinatorEntity[PhilipsSonicareCoordinator]):
    """Base class for all Philips Sonicare entities.

    State restoration across restarts happens at the coordinator level (a
    ``Store`` with native values), not per entity — see
    ``PhilipsSonicareCoordinator.async_load_stored_data``.
    """

    _attr_has_entity_name = True
    _data_key: str | None = None

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

        stored_name = entry.data.get(CONF_DEVICE_NAME)
        if stored_name:
            name = stored_name
        else:
            # Pre-feature entries — synthesize the old "Philips Sonicare {model}"
            # label so existing installs aren't renamed.
            model = coordinator.data.get("model_number") if coordinator.data else None
            name = f"Philips Sonicare {model}" if model else "Philips Sonicare"

        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            connections={(dr.CONNECTION_BLUETOOTH, self._device_id)},
            manufacturer="Philips",
            name=name,
            suggested_area=entry.data.get(CONF_AREA) or None,
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
        """Return True once the device has ever been seen.

        The brush is a sleepy device — it is out of BLE reach between
        sessions as its normal state, so availability can't hinge on being
        currently reachable (same reasoning as core's Oral-B integration).
        Once it has been seen — live or restored from storage — the last
        known values stay available; live connectivity is exposed on the
        Connection sub-device instead.
        """
        if self.coordinator.data and self.coordinator.data.get("last_seen"):
            return True

        # Never seen (fresh install): fall back to reachability
        if self._is_esp_bridge:
            return self.coordinator.transport.is_connected
        return async_last_service_info(self.hass, self._device_id) is not None


class PhilipsBrushHeadEntity(PhilipsSonicareEntity):
    """Base class for entities on the Brush Head sub-device."""

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        parent_name = self._attr_device_info.get("name") or "Philips Sonicare"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, f"{self._device_id}_brushhead")},
            manufacturer="Philips",
            name=f"{parent_name} Brush Head",
            via_device=(DOMAIN, self._device_id),
        )


class PhilipsConnectionEntity(PhilipsSonicareEntity):
    """Base class for entities on the Connection sub-device.

    Groups transport-level diagnostics (adapter, RSSI, link status) on a
    dedicated device so the main device only shows toothbrush state.

    Identifier is kept as `{device_id}_bridge` (historical) so existing ESP
    Bridge installations keep their registry entries without a migration.
    """

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        parent_name = self._attr_device_info.get("name") or "Philips Sonicare"
        device_name = f"{parent_name} Connection"
        manufacturer = "Espressif" if self._is_esp_bridge else "Home Assistant"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, f"{self._device_id}_bridge")},
            manufacturer=manufacturer,
            name=device_name,
            via_device=(DOMAIN, self._device_id),
        )

    @property
    def available(self) -> bool:
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


# Backwards-compat alias for any out-of-tree imports
PhilipsBridgeEntity = PhilipsConnectionEntity
