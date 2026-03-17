"""Config flow for Philips Sonicare BLE."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from bleak import BleakClient
from bleak.exc import BleakError
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
    CHAR_BATTERY_LEVEL,
    CHAR_MODEL_NUMBER,
    CHAR_SERIAL_NUMBER,
    CHAR_FIRMWARE_REVISION,
    SVC_BATTERY,
    SVC_DEVICE_INFO,
    SVC_SONICARE,
    SVC_ROUTINE,
    SVC_STORAGE,
    SVC_SENSOR,
    SVC_BRUSHHEAD,
    SVC_DIAGNOSTIC,
    SVC_EXTENDED,
    SVC_BYTESTREAM_LEGACY,
)

_LOGGER = logging.getLogger(__name__)

# Standard BLE services to hide from display
_STANDARD_BLE_SERVICES = {
    "00001800-0000-1000-8000-00805f9b34fb",  # Generic Access
    "00001801-0000-1000-8000-00805f9b34fb",  # Generic Attribute
}

# Expected Sonicare services
_EXPECTED_SERVICES = {
    SVC_BATTERY.lower(),
    SVC_DEVICE_INFO.lower(),
    SVC_SONICARE.lower(),
    SVC_ROUTINE.lower(),
    SVC_STORAGE.lower(),
    SVC_SENSOR.lower(),
    SVC_BRUSHHEAD.lower(),
    SVC_DIAGNOSTIC.lower(),
    SVC_EXTENDED.lower(),
}

# Human-readable names for services
SERVICE_NAMES: dict[str, str] = {
    SVC_BATTERY.lower(): "Battery",
    SVC_DEVICE_INFO.lower(): "Device Information",
    SVC_SONICARE.lower(): "Sonicare (Main)",
    SVC_ROUTINE.lower(): "Routine / Session",
    SVC_STORAGE.lower(): "Storage / History",
    SVC_SENSOR.lower(): "Sensor (IMU)",
    SVC_BRUSHHEAD.lower(): "Brush Head",
    SVC_DIAGNOSTIC.lower(): "Diagnostic",
    SVC_EXTENDED.lower(): "Extended / Settings",
    SVC_BYTESTREAM_LEGACY.lower(): "ByteStreaming (Legacy)",
}


class PhilipsSonicareConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Philips Sonicare BLE."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._name: str | None = None
        self._fetched_data: dict[str, Any] | None = None

    async def _async_fetch_capabilities(self, address: str) -> dict[str, Any]:
        """Connect to the device and read capabilities.

        Uses the cached BLEDevice from discovery when available to skip
        the address lookup, and use_services_cache=True to skip GATT
        service discovery on repeated connects.
        """
        result: dict[str, Any] = {"services": []}

        # Prefer the device object we already have from discovery
        if self._discovery_info is not None:
            device = self._discovery_info.device
        else:
            device = async_ble_device_from_address(self.hass, address)

        if not device:
            _LOGGER.warning("Device %s not found in range", address)
            return result

        client: BleakClient | None = None
        try:
            client = await bleak_establish(
                BleakClient, device, "philips_sonicare_ble",
                use_services_cache=True, timeout=15.0,
            )
            if not client or not client.is_connected:
                return result

            result["services"] = [str(s.uuid).lower() for s in client.services]

            # Read device info (sequential — BLE GATT is request-response)
            for char_uuid, key in (
                (CHAR_BATTERY_LEVEL, "battery"),
                (CHAR_MODEL_NUMBER, "model"),
                (CHAR_SERIAL_NUMBER, "serial"),
                (CHAR_FIRMWARE_REVISION, "firmware"),
            ):
                try:
                    raw = await asyncio.wait_for(
                        client.read_gatt_char(char_uuid), timeout=5.0
                    )
                    if raw:
                        if key == "battery":
                            result[key] = raw[0]
                        else:
                            result[key] = raw.decode("utf-8", "ignore").strip("\x00 ")
                except (BleakError, TimeoutError, Exception) as err:
                    _LOGGER.debug("Failed to read %s: %s", key, err)

        except Exception as err:
            _LOGGER.warning("Could not connect during capabilities fetch: %s", err)
        finally:
            if client and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        return result

    @staticmethod
    def _get_service_status_text(fetched_uuids: list[str]) -> str:
        """Format found services as HTML table with checkmarks."""
        fetched_lower = {s.lower() for s in fetched_uuids} - _STANDARD_BLE_SERVICES

        found_rows: list[str] = []
        missing_rows: list[str] = []
        unknown_rows: list[str] = []

        for uuid in sorted(_EXPECTED_SERVICES):
            name = SERVICE_NAMES.get(uuid, "Unknown")
            short = uuid.split("-")[0]
            if uuid in fetched_lower:
                found_rows.append(
                    f"<tr><td>✅</td><td>{name}</td><td><code>{short}</code></td></tr>"
                )
            else:
                missing_rows.append(
                    f"<tr><td>❌</td><td>{name}</td><td><code>{short}</code></td></tr>"
                )

        # Unknown services (found but not expected)
        known_all = _EXPECTED_SERVICES | {SVC_BYTESTREAM_LEGACY.lower()}
        for uuid in sorted(fetched_lower - _EXPECTED_SERVICES):
            name = SERVICE_NAMES.get(uuid, "Unknown")
            short = uuid.split("-")[0]
            if uuid in known_all:
                found_rows.append(
                    f"<tr><td>✅</td><td>{name}</td><td><code>{short}</code></td></tr>"
                )
            else:
                unknown_rows.append(
                    f"<tr><td>❔</td><td>{name}</td><td><code>{short}</code></td></tr>"
                )

        rows = found_rows + missing_rows + unknown_rows
        if not rows:
            return "No services detected"
        return "<table>" + "".join(rows) + "</table>"

    @staticmethod
    def _get_device_info_text(data: dict[str, Any]) -> str:
        """Format device info as HTML table."""
        rows: list[str] = []
        if model := data.get("model"):
            rows.append(f"<tr><td><b>Model</b></td><td>{model}</td></tr>")
        if serial := data.get("serial"):
            rows.append(f"<tr><td><b>Serial</b></td><td><code>{serial}</code></td></tr>")
        if firmware := data.get("firmware"):
            rows.append(f"<tr><td><b>Firmware</b></td><td>{firmware}</td></tr>")
        if (battery := data.get("battery")) is not None:
            rows.append(f"<tr><td><b>Battery</b></td><td>{battery}%</td></tr>")
        if not rows:
            return "Could not read device information"
        return "<table>" + "".join(rows) + "</table>"

    # ------------------------------------------------------------------
    # Discovery flow
    # ------------------------------------------------------------------
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

        errors: dict[str, str] = {}
        try:
            self._fetched_data = await self._async_fetch_capabilities(self._address)
            if self._fetched_data.get("services"):
                return await self.async_step_show_capabilities()
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error during capabilities fetch")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._name},
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Manual flow
    # ------------------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual setup."""
        errors = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper()
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()

            self._address = address
            self._name = address

            try:
                self._fetched_data = await self._async_fetch_capabilities(address)
                if self._fetched_data.get("services"):
                    return await self.async_step_show_capabilities()
                # No services found but no exception — create entry anyway
                return self.async_create_entry(
                    title=f"Philips Sonicare ({address})",
                    data={
                        CONF_ADDRESS: address,
                        CONF_TRANSPORT_TYPE: TRANSPORT_BLEAK,
                        CONF_SERVICES: [],
                    },
                )
            except Exception:
                _LOGGER.exception("Unexpected error during manual setup")
                errors["base"] = "cannot_connect"

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

    # ------------------------------------------------------------------
    # Capabilities dialog
    # ------------------------------------------------------------------
    async def async_step_show_capabilities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show detected device info and services, then create entry."""
        if self._fetched_data is None:
            return await self.async_step_user()

        if user_input is not None:
            services = self._fetched_data.get("services", [])
            title = self._fetched_data.get("model", self._name) or self._name

            return self.async_create_entry(
                title=title,
                data={
                    CONF_ADDRESS: self._address,
                    CONF_TRANSPORT_TYPE: TRANSPORT_BLEAK,
                    CONF_SERVICES: services,
                },
            )

        device_info_text = self._get_device_info_text(self._fetched_data)
        services_text = self._get_service_status_text(
            self._fetched_data.get("services", [])
        )

        return self.async_show_form(
            step_id="show_capabilities",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": str(self._name),
                "device_info": device_info_text,
                "services": services_text,
            },
        )

    # ------------------------------------------------------------------
    # Options flow
    # ------------------------------------------------------------------
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
