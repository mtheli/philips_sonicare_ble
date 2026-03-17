"""Config flow for Philips Sonicare BLE."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from bleak import BleakClient
from bleak_retry_connector import establish_connection as bleak_establish

from .const import (
    DOMAIN,
    CONF_POLL_INTERVAL,
    CONF_ENABLE_LIVE_UPDATES,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    TRANSPORT_BLEAK,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_ENABLE_LIVE_UPDATES,
    MIN_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    CHAR_MODEL_NUMBER,
    CHAR_SERIAL_NUMBER,
    CHAR_FIRMWARE_REVISION,
)

_LOGGER = logging.getLogger(__name__)


class PhilipsSonicareConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Philips Sonicare BLE."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._name: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle Bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._address = discovery_info.address
        self._name = discovery_info.name or "Philips Sonicare"

        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm Bluetooth discovery."""
        if user_input is None:
            return self.async_show_form(
                step_id="bluetooth_confirm",
                description_placeholders={"name": self._name},
            )

        # Try to connect and read basic info
        device_info = {}
        services = []
        if self._discovery_info:
            try:
                client = await bleak_establish(
                    BleakClient,
                    self._discovery_info.device,
                    "philips_sonicare_ble",
                    timeout=15.0,
                )
                if client and client.is_connected:
                    try:
                        for char, key in [
                            (CHAR_MODEL_NUMBER, "model"),
                            (CHAR_SERIAL_NUMBER, "serial"),
                            (CHAR_FIRMWARE_REVISION, "firmware"),
                        ]:
                            try:
                                raw = await client.read_gatt_char(char)
                                if raw:
                                    device_info[key] = raw.decode("utf-8", "ignore").strip("\x00 ")
                            except Exception:
                                pass

                        services = [
                            str(s.uuid).lower() for s in client.services
                        ]
                    finally:
                        await client.disconnect()
            except Exception as err:
                _LOGGER.warning("Could not connect during setup: %s", err)

        title = device_info.get("model", self._name) or self._name

        return self.async_create_entry(
            title=title,
            data={
                CONF_ADDRESS: self._address,
                CONF_TRANSPORT_TYPE: TRANSPORT_BLEAK,
                CONF_SERVICES: services,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual setup."""
        errors = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper()
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"Philips Sonicare ({address})",
                data={
                    CONF_ADDRESS: address,
                    CONF_TRANSPORT_TYPE: TRANSPORT_BLEAK,
                    CONF_SERVICES: [],
                },
            )

        # Show discovered Sonicare devices
        discovered = async_discovered_service_info(self.hass)
        sonicare_devices = {}
        for info in discovered:
            name = info.name or ""
            if "sonicare" in name.lower() or "philips ohc" in name.lower():
                sonicare_devices[info.address] = f"{name} ({info.address})"

        if sonicare_devices:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {vol.Required(CONF_ADDRESS): vol.In(sonicare_devices)}
                ),
                errors=errors,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): str}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PhilipsSonicareOptionsFlow(config_entry)


class PhilipsSonicareOptionsFlow(OptionsFlow):
    """Options flow for Philips Sonicare BLE."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)),
                    vol.Required(
                        CONF_ENABLE_LIVE_UPDATES,
                        default=options.get(CONF_ENABLE_LIVE_UPDATES, DEFAULT_ENABLE_LIVE_UPDATES),
                    ): bool,
                }
            ),
        )
