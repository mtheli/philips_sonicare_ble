from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PhilipsSonicareCoordinator
from .const import DOMAIN, supports_settings_write
from .entity import PhilipsSonicareEntity

_LOGGER = logging.getLogger(__name__)

SETTINGS_BIT_ADAPTIVE_INTENSITY = 0x1000
SETTINGS_BIT_SCRUBBING_FEEDBACK = 0x0800
SETTINGS_BIT_PRESSURE_FEEDBACK = 0x0200


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Philips Sonicare switch entities."""
    model = entry.data.get("model", "")
    if not supports_settings_write(model):
        return

    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    # Condor devices won't accept settings-bitmask writes until the
    # PutProps flow lands, so skip wiring the switches altogether.
    if not coordinator.supports_writes:
        return

    async_add_entities([
        SonicareSettingsSwitch(
            coordinator, entry,
            "adaptive_intensity",
            SETTINGS_BIT_ADAPTIVE_INTENSITY,
            "mdi:auto-fix",
        ),
        SonicareSettingsSwitch(
            coordinator, entry,
            "scrubbing_feedback",
            SETTINGS_BIT_SCRUBBING_FEEDBACK,
            "mdi:vibrate",
        ),
        SonicareSettingsSwitch(
            coordinator, entry,
            "pressure_feedback",
            SETTINGS_BIT_PRESSURE_FEEDBACK,
            "mdi:gauge",
        ),
    ])


class SonicareSettingsSwitch(PhilipsSonicareEntity, SwitchEntity):
    """Switch entity for a single settings bit on characteristic 0x4420."""

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self.coordinator.data:
            state = self.coordinator.data.get("brushing_state")
            if state == "on":
                return False
        return True

    def __init__(
        self,
        coordinator: PhilipsSonicareCoordinator,
        entry: ConfigEntry,
        key: str,
        bit_mask: int,
        icon: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_{key}"
        self._attr_translation_key = key
        self._attr_icon = icon
        self._bit_mask = bit_mask

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        settings = self.coordinator.data.get("settings_bitmask")
        if settings is None:
            return None
        return bool(settings & self._bit_mask)

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_write_settings_bit(self._bit_mask, True)
        settings = self.coordinator.data.get("settings_bitmask", 0)
        self.coordinator.data["settings_bitmask"] = settings | self._bit_mask
        self.coordinator.async_set_updated_data(self.coordinator.data)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_write_settings_bit(self._bit_mask, False)
        settings = self.coordinator.data.get("settings_bitmask", 0)
        self.coordinator.data["settings_bitmask"] = settings & ~self._bit_mask
        self.coordinator.async_set_updated_data(self.coordinator.data)
