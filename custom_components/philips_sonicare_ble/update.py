# custom_components/philips_sonicare/update.py
"""Firmware update entity for the ESP bridge.

Passive (no install) update entity that surfaces when a newer ESP bridge
firmware is available. The "latest" version and the changelog are read
straight from the GitHub repo at runtime, so users are notified of new
bridge firmware without the integration shipping a release.

Flashing itself is done via ESPHome (recompile + OTA) — this entity only
informs. It exists only for ESP-bridge transports; Direct-BLE setups have
no bridge and get no entity.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    BRIDGE_CHANGELOG_URL,
    BRIDGE_RELEASE_URL,
    BRIDGE_VERSION_URL,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    TRANSPORT_ESP_BRIDGE,
)
from .coordinator import PhilipsSonicareCoordinator
from .entity import PhilipsConnectionEntity

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=24)
_FETCH_TIMEOUT = 30


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the ESP bridge firmware update entity (ESP-bridge transport only)."""
    if entry.data.get(CONF_TRANSPORT_TYPE) != TRANSPORT_ESP_BRIDGE:
        # Direct-BLE setups have no bridge to update.
        return

    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([SonicareBridgeUpdate(coordinator, entry)])


class SonicareBridgeUpdate(PhilipsConnectionEntity, UpdateEntity):
    """Passive firmware-update check for the ESP bridge sub-device."""

    _attr_has_entity_name = True
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = UpdateEntityFeature.RELEASE_NOTES
    _attr_translation_key = "bridge_firmware"
    _attr_release_url = BRIDGE_RELEASE_URL

    def __init__(
        self, coordinator: PhilipsSonicareCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._device_id}_bridge_firmware"
        self._latest_version: str | None = None

    @property
    def installed_version(self) -> str | None:
        """Version running on the ESP bridge.

        Falls back to the last-known version persisted on the bridge
        sub-device so a disconnected/sleeping bridge still shows its
        firmware (and whether an update is pending) instead of "Unknown".
        """
        live = self.coordinator.transport.bridge_version
        if live:
            return live
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get_device(
            identifiers={(DOMAIN, f"{self._device_id}_bridge")}
        )
        return device.sw_version if device else None

    @property
    def latest_version(self) -> str | None:
        """Latest bridge firmware version published in the repo.

        Guarded so a bridge flashed from a branch *ahead* of main never
        triggers a bogus "update available" (which would be a downgrade
        prompt). HA's ``state`` is ``@final`` and treats a non-comparable
        version pair as "update available", so we neutralise both cases here:
        if the installed version is newer-or-equal — or can't be compared —
        we report it as the latest, yielding string-equality → up to date.
        """
        installed = self.installed_version
        latest = self._latest_version
        # Before the first successful fetch, mirror installed → no spurious update.
        if not latest:
            return installed
        if installed:
            try:
                if AwesomeVersion(installed) >= AwesomeVersion(latest):
                    return installed
            except AwesomeVersionException:
                # Non-comparable installed version (e.g. a "dev" build) — don't
                # let HA's fallback prompt a phantom update.
                return installed
        return latest

    async def async_added_to_hass(self) -> None:
        """Fetch on load, then schedule a periodic refresh.

        This is a CoordinatorEntity (``should_poll`` is False), so HA never
        calls ``async_update`` on its own — we drive the 24 h refresh with our
        own timer instead.
        """
        await super().async_added_to_hass()
        await self.async_update()

        async def _refresh(_now) -> None:
            await self.async_update()
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_interval(self.hass, _refresh, SCAN_INTERVAL)
        )

    async def async_update(self) -> None:
        """Fetch the latest bridge version from the repo."""
        session = async_get_clientsession(self.hass)
        try:
            resp = await session.get(BRIDGE_VERSION_URL, timeout=_FETCH_TIMEOUT)
            resp.raise_for_status()
            version = (await resp.text()).strip()
        except Exception as err:  # noqa: BLE001 — keep last known value on any failure
            _LOGGER.debug("Failed to fetch latest bridge version: %s", err)
            return

        # Guard against fetching an error page / garbage.
        if version and re.fullmatch(r"\d+\.\d+\.\d+", version):
            self._latest_version = version
            _LOGGER.debug(
                "Bridge firmware check: installed=%s latest=%s",
                self.installed_version,
                version,
            )
        else:
            _LOGGER.debug("Unexpected bridge VERSION payload: %r", version[:64])

    async def async_release_notes(self) -> str | None:
        """Lazily fetch the changelog and return every section the bridge is
        behind — all versions newer than installed, up to and including latest.

        Only called when the user opens the release-notes dialog, so the 24 h
        poll stays lightweight.
        """
        latest = self._latest_version
        if not latest:
            return None

        session = async_get_clientsession(self.hass)
        try:
            resp = await session.get(BRIDGE_CHANGELOG_URL, timeout=_FETCH_TIMEOUT)
            resp.raise_for_status()
            changelog = await resp.text()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to fetch bridge changelog: %s", err)
            return None

        notes = _extract_changelog_sections(changelog, self.installed_version, latest)
        if notes:
            return notes
        return f"See the [full changelog]({BRIDGE_RELEASE_URL}) for details."


def _extract_changelog_sections(
    changelog: str, installed: str | None, latest: str | None
) -> str | None:
    """Return every changelog block in the range ``(installed, latest]``.

    Headings look like ``## v1.6.1 — 2026-06-25``; each section runs until the
    next ``## `` heading. Sections are returned newest-first, exactly as they
    appear in the changelog, so a bridge several versions behind sees every
    entry it skipped. When the installed version can't be parsed (a dev build
    or an offline bridge with no known version) only the latest section is
    returned, so the dialog isn't flooded with the whole history.
    """
    heading_re = re.compile(r"^##\s+v?(\d+\.\d+\.\d+)\b.*$", re.MULTILINE)
    matches = list(heading_re.finditer(changelog))
    if not matches:
        return None

    def _ver(value: str | None) -> AwesomeVersion | None:
        if not value:
            return None
        try:
            return AwesomeVersion(value)
        except AwesomeVersionException:
            return None

    installed_v = _ver(installed)
    latest_v = _ver(latest)

    sections: list[str] = []
    for i, match in enumerate(matches):
        version_v = _ver(match.group(1))
        if version_v is None:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(changelog)
        body = changelog[match.start() : end].strip()

        if latest_v is not None and version_v > latest_v:
            continue  # defensive: newer than what we're offering
        if installed_v is not None:
            if version_v <= installed_v:
                continue  # already installed or older
        elif latest_v is not None and version_v != latest_v:
            continue  # no lower bound → only the latest section

        sections.append(body)

    if not sections:
        return None
    return "\n\n".join(sections)
