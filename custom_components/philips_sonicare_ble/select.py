# custom_components/philips_sonicare/select.py
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PhilipsSonicareCoordinator
from .const import DOMAIN, BRUSHING_MODES, INTENSITIES, supports_mode_write
from .entity import PhilipsSonicareEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Philips Sonicare select entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    model = entry.data.get("model", "")
    entities: list[SelectEntity] = []
    if supports_mode_write(model):
        entities.append(SonicareBrushingModeSelect(coordinator, entry))
        entities.append(SonicareIntensitySelect(coordinator, entry))

    async_add_entities(entities)


class SonicareWriteSelectEntity(PhilipsSonicareEntity, SelectEntity):
    """Base class for writable select entities — unavailable during brushing."""

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self.coordinator.data:
            state = self.coordinator.data.get("brushing_state")
            if state == "on":
                return False
        return True


class SonicareBrushingModeSelect(SonicareWriteSelectEntity):
    """Select entity to set the brushing mode for the next session."""

    _attr_translation_key = "brushing_mode_select"
    _attr_icon = "mdi:toothbrush-electric"

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_brushing_mode_select"

    @property
    def options(self) -> list[str]:
        """Return all known brushing modes."""
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


class SonicareIntensitySelect(SonicareWriteSelectEntity):
    """Select entity to set the brushing intensity."""

    _attr_translation_key = "intensity_select"
    _attr_icon = "mdi:speedometer"

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_intensity_select"

    @property
    def options(self) -> list[str]:
        return list(INTENSITIES.values())

    @property
    def current_option(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("intensity")

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_intensity(option)
