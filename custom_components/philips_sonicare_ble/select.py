# custom_components/philips_sonicare/select.py
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PhilipsSonicareCoordinator
from .const import DOMAIN, BRUSHING_MODES
from .entity import PhilipsSonicareEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Philips Sonicare select entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    async_add_entities([
        SonicareBrushingModeSelect(coordinator, entry),
    ])


class SonicareBrushingModeSelect(PhilipsSonicareEntity, SelectEntity):
    """Select entity to set the brushing mode for the next session."""

    _attr_translation_key = "brushing_mode_select"
    _attr_icon = "mdi:toothbrush-electric"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushing_mode_select"

    @property
    def options(self) -> list[str]:
        """Return available brushing modes from the device."""
        available_ids = (
            self.coordinator.data.get("available_mode_ids")
            if self.coordinator.data
            else None
        )
        if available_ids:
            return [
                BRUSHING_MODES[mid]
                for mid in available_ids
                if mid in BRUSHING_MODES
            ]
        # Fallback: all known modes
        return list(BRUSHING_MODES.values())

    @property
    def current_option(self) -> str | None:
        """Return the currently selected mode."""
        if not self.coordinator.data:
            return None
        # During active session, show the active brushing mode
        selected = self.coordinator.data.get("selected_mode")
        if selected:
            return selected
        # Fallback: last known brushing_mode from notification
        return self.coordinator.data.get("brushing_mode")

    async def async_select_option(self, option: str) -> None:
        """Set the brushing mode."""
        await self.coordinator.async_set_brushing_mode(option)
