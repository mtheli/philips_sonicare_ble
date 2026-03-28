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
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectOptionDict,
)

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection as bleak_establish

from .const import (
    DOMAIN,
    CONF_POLL_INTERVAL,
    CONF_ENABLE_LIVE_UPDATES,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    CONF_ESP_DEVICE_NAME,
    CONF_ESP_DEVICE_ID,
    CONF_NOTIFY_THROTTLE,
    CONF_SENSOR_PRESSURE,
    CONF_SENSOR_TEMPERATURE,
    CONF_SENSOR_GYROSCOPE,
    TRANSPORT_BLEAK,
    TRANSPORT_ESP_BRIDGE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_ENABLE_LIVE_UPDATES,
    DEFAULT_NOTIFY_THROTTLE,
    DEFAULT_SENSOR_PRESSURE,
    DEFAULT_SENSOR_TEMPERATURE,
    DEFAULT_SENSOR_GYROSCOPE,
    MIN_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    MIN_NOTIFY_THROTTLE,
    MAX_NOTIFY_THROTTLE,
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
    SVC_BYTESTREAM,
)
from .transport import EspBridgeTransport
from .exceptions import TransportError

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
    SVC_BYTESTREAM.lower(): "ByteStreaming",
}

# Map each service to one representative characteristic for ESP probing
SERVICE_PROBE_CHARS: dict[str, str] = {
    SVC_BATTERY: CHAR_BATTERY_LEVEL,
    SVC_DEVICE_INFO: CHAR_MODEL_NUMBER,
    SVC_SONICARE: "477ea600-a260-11e4-ae37-0002a5d54010",  # CHAR_HANDLE_STATE
    SVC_ROUTINE: "477ea600-a260-11e4-ae37-0002a5d54080",   # CHAR_BRUSHING_MODE
    SVC_STORAGE: "477ea600-a260-11e4-ae37-0002a5d540d0",   # CHAR_LATEST_SESSION_ID
    SVC_SENSOR: "477ea600-a260-11e4-ae37-0002a5d54120",    # CHAR_SENSOR_ENABLE
    SVC_BRUSHHEAD: "477ea600-a260-11e4-ae37-0002a5d54210",  # CHAR_BRUSHHEAD_NFC_VERSION
    SVC_DIAGNOSTIC: "477ea600-a260-11e4-ae37-0002a5d54310",  # CHAR_ERROR_PERSISTENT
    SVC_EXTENDED: "477ea600-a260-11e4-ae37-0002a5d54420",   # CHAR_SETTINGS
}


class PhilipsSonicareConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Philips Sonicare BLE."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._name: str | None = None
        self._fetched_data: dict[str, Any] | None = None
        self._transport_type: str = TRANSPORT_BLEAK
        self._esp_device_name: str | None = None
        self._esp_device_id: str = ""
        self._esp_device_ids: list[str] = []
        self._bridge_info: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Capabilities fetch (direct BLE)
    # ------------------------------------------------------------------
    async def _async_fetch_capabilities(self, address: str) -> dict[str, Any]:
        """Connect to the device and read capabilities via direct BLE."""
        result: dict[str, Any] = {"services": []}

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

    # ------------------------------------------------------------------
    # Capabilities fetch (ESP bridge)
    # ------------------------------------------------------------------
    async def _async_fetch_capabilities_esp(
        self,
        address: str,
        esp_device_name: str,
        esp_device_id: str = "",
    ) -> dict[str, Any]:
        """Read capabilities and probe services via ESP32 bridge."""
        transport = EspBridgeTransport(self.hass, address, esp_device_name, esp_device_id)
        try:
            await transport.connect()

            found_services: list[str] = []
            model_number: str | None = None
            for svc_uuid, probe_char in SERVICE_PROBE_CHARS.items():
                raw = await transport.read_char(probe_char)
                if raw is not None:
                    found_services.append(svc_uuid)
                    if probe_char == CHAR_MODEL_NUMBER:
                        model_number = raw.decode("utf-8", errors="replace").strip()

            if not found_services:
                raise TransportError(
                    "Could not read any service via ESP bridge - toothbrush may not be connected"
                )

            # Battery
            battery: int | None = None
            raw_bat = await transport.read_char(CHAR_BATTERY_LEVEL)
            if raw_bat:
                battery = raw_bat[0]

            # Serial
            serial: str | None = None
            raw_serial = await transport.read_char(CHAR_SERIAL_NUMBER)
            if raw_serial:
                serial = raw_serial.decode("utf-8", errors="replace").strip()

            # Firmware
            firmware: str | None = None
            raw_fw = await transport.read_char(CHAR_FIRMWARE_REVISION)
            if raw_fw:
                firmware = raw_fw.decode("utf-8", errors="replace").strip()

            return {
                "services": found_services,
                "sonicare_mac": transport.detected_mac,
                "model": model_number,
                "serial": serial,
                "firmware": firmware,
                "battery": battery,
            }

        except TransportError:
            raise
        finally:
            await transport.disconnect()

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------
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

        known_all = _EXPECTED_SERVICES | {SVC_BYTESTREAM.lower()}
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
    # ESP bridge helpers
    # ------------------------------------------------------------------
    def _get_esphome_device_options(self) -> list[SelectOptionDict]:
        """Build a list of available ESPHome devices for the selector."""
        esphome_entries = self.hass.config_entries.async_entries("esphome")
        options: list[SelectOptionDict] = []
        for entry in esphome_entries:
            device_name = entry.data.get("device_name")
            if device_name:
                options.append(
                    SelectOptionDict(
                        value=device_name,
                        label=f"{entry.title} ({device_name})",
                    )
                )
        return options

    def _detect_esp_device_ids(self, esp_device_name: str) -> list[str]:
        """Detect available device_id suffixes on an ESP bridge."""
        # Single device (no suffix)
        if self.hass.services.has_service("esphome", f"{esp_device_name}_ble_read_char"):
            return [""]

        # Multi-device: find suffixed services
        esphome_services = self.hass.services.async_services().get("esphome", {})
        prefix = f"{esp_device_name}_ble_read_char_"
        return [
            svc_name[len(prefix):]
            for svc_name in esphome_services
            if svc_name.startswith(prefix)
        ]

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
                self._transport_type = TRANSPORT_BLEAK
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
    # Manual flow — menu: choose connection type
    # ------------------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user — choose connection type."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["user_bleak", "esp_bridge"],
        )

    # ------------------------------------------------------------------
    # Direct BLE manual setup
    # ------------------------------------------------------------------
    async def async_step_user_bleak(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual MAC address entry for direct BLE."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper()
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()

            self._address = address
            self._name = address

            try:
                self._fetched_data = await self._async_fetch_capabilities(address)
                if self._fetched_data.get("services"):
                    self._transport_type = TRANSPORT_BLEAK
                    return await self.async_step_show_capabilities()
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
                step_id="user_bleak",
                data_schema=vol.Schema(
                    {vol.Required(CONF_ADDRESS): vol.In(sonicare_devices)}
                ),
                errors=errors,
            )

        return self.async_show_form(
            step_id="user_bleak",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): str}
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # ESP32 Bridge setup
    # ------------------------------------------------------------------
    async def async_step_esp_bridge(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle ESP32 bridge configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            esp_device_name = user_input["esp_device_name"].strip().replace("-", "_")

            device_ids = self._detect_esp_device_ids(esp_device_name)
            if not device_ids:
                _LOGGER.error("No philips_sonicare services found on %s", esp_device_name)
                errors["base"] = "cannot_connect"
            else:
                self._esp_device_name = esp_device_name
                self._esp_device_ids = device_ids

                if len(device_ids) > 1:
                    return await self.async_step_esp_select_device()

                self._esp_device_id = device_ids[0]
                return await self._esp_bridge_health_check()

        esp_options = self._get_esphome_device_options()

        if esp_options:
            data_schema = vol.Schema(
                {
                    vol.Required("esp_device_name"): SelectSelector(
                        SelectSelectorConfig(options=esp_options)
                    ),
                }
            )
        else:
            data_schema = vol.Schema(
                {
                    vol.Required("esp_device_name"): str,
                }
            )
            errors["base"] = "no_esphome_devices"

        return self.async_show_form(
            step_id="esp_bridge",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_esp_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let user pick which device on a multi-device ESP bridge."""
        if user_input is not None:
            self._esp_device_id = user_input["esp_device_id"]
            return await self._esp_bridge_health_check()

        options: list[SelectOptionDict] = []
        for did in self._esp_device_ids:
            options.append(SelectOptionDict(value=did, label=did or "default"))

        if not options:
            return self.async_abort(reason="already_configured")

        if len(options) == 1:
            self._esp_device_id = options[0]["value"]
            return await self._esp_bridge_health_check()

        return self.async_show_form(
            step_id="esp_select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("esp_device_id"): SelectSelector(
                        SelectSelectorConfig(options=options)
                    ),
                }
            ),
        )

    async def _esp_bridge_health_check(self) -> FlowResult:
        """Run bridge health check and proceed to status step."""
        if self._bridge_info:
            return await self.async_step_esp_bridge_status()

        transport = EspBridgeTransport(
            self.hass, "", self._esp_device_name, self._esp_device_id
        )
        try:
            await transport.connect()
            self._bridge_info = {
                "version": transport.bridge_version or "?",
                "ble_connected": str(transport.is_device_connected).lower(),
                "mac": transport.detected_mac or "",
            }
        except TransportError:
            _LOGGER.error("ESP bridge not reachable: %s", self._esp_device_name)
            return self.async_show_form(
                step_id="esp_bridge",
                data_schema=vol.Schema({vol.Required("esp_device_name"): str}),
                errors={"base": "cannot_connect"},
            )
        except Exception:
            _LOGGER.exception("Unexpected error checking ESP bridge")
            return self.async_show_form(
                step_id="esp_bridge",
                data_schema=vol.Schema({vol.Required("esp_device_name"): str}),
                errors={"base": "unknown"},
            )
        finally:
            await transport.disconnect()

        return await self.async_step_esp_bridge_status()

    async def async_step_esp_bridge_status(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show ESP bridge status before reading toothbrush capabilities."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                capabilities = await self._async_fetch_capabilities_esp(
                    "", self._esp_device_name, self._esp_device_id,
                )

                sonicare_mac = capabilities.get("sonicare_mac")
                if sonicare_mac:
                    await self.async_set_unique_id(
                        sonicare_mac.upper(), raise_on_progress=False
                    )
                else:
                    await self.async_set_unique_id(f"esp_{self._esp_device_name}")
                self._abort_if_unique_id_configured()

                self._fetched_data = capabilities
                self._address = sonicare_mac
                model = capabilities.get("model")
                self._name = model if model else self._esp_device_name
                self._transport_type = TRANSPORT_ESP_BRIDGE

                return await self.async_step_show_capabilities()

            except TransportError:
                _LOGGER.error(
                    "ESP bridge: unable to read toothbrush capabilities via %s",
                    self._esp_device_name,
                )
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error reading toothbrush capabilities")
                errors["base"] = "unknown"

        # Format bridge status display
        info = self._bridge_info or {}
        if info:
            version = info.get("version", "?")
            ble_connected = info.get("ble_connected") == "true"
            mac = info.get("mac", "")

            ble_status = "\u2705 Connected" if ble_connected else "\u274c Disconnected"

            rows = [
                f"<tr><td><b>Version</b></td><td>v{version}</td></tr>",
                f"<tr><td><b>BLE</b></td><td>{ble_status}</td></tr>",
            ]
            if mac and mac != "00:00:00:00:00:00":
                rows.append(f"<tr><td><b>MAC</b></td><td><code>{mac}</code></td></tr>")

            status_text = "<table>" + "".join(rows) + "</table>"
        else:
            status_text = "Diagnostic details not available."

        return self.async_show_form(
            step_id="esp_bridge_status",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._esp_device_name or "",
                "status": status_text,
            },
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Capabilities dialog (shared by BLE and ESP)
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

            entry_data: dict[str, Any] = {
                CONF_SERVICES: services,
            }

            if self._transport_type == TRANSPORT_ESP_BRIDGE:
                entry_data[CONF_TRANSPORT_TYPE] = TRANSPORT_ESP_BRIDGE
                entry_data[CONF_ESP_DEVICE_NAME] = self._esp_device_name
                if self._esp_device_id:
                    entry_data[CONF_ESP_DEVICE_ID] = self._esp_device_id
                if self._address:
                    entry_data[CONF_ADDRESS] = self._address
            else:
                entry_data[CONF_ADDRESS] = self._address
                entry_data[CONF_TRANSPORT_TYPE] = TRANSPORT_BLEAK

            return self.async_create_entry(
                title=f"Philips Sonicare ({title})",
                data=entry_data,
            )

        device_info_text = self._get_device_info_text(self._fetched_data)
        services_text = self._get_service_status_text(
            self._fetched_data.get("services", [])
        )

        bridge_info = ""
        if self._transport_type == TRANSPORT_ESP_BRIDGE:
            bridge_info = f" via **ESP32 Bridge** ({self._esp_device_name})"
        else:
            bridge_info = " via **Direct Bluetooth**"

        return self.async_show_form(
            step_id="show_capabilities",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": str(self._name),
                "device_info": device_info_text,
                "services": services_text,
                "bridge_info": bridge_info,
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
        is_esp = (
            self._config_entry.data.get(CONF_TRANSPORT_TYPE) == TRANSPORT_ESP_BRIDGE
        )

        if user_input is not None:
            data = {
                CONF_POLL_INTERVAL: user_input[CONF_POLL_INTERVAL],
                CONF_ENABLE_LIVE_UPDATES: user_input[CONF_ENABLE_LIVE_UPDATES],
                CONF_SENSOR_PRESSURE: user_input.get(CONF_SENSOR_PRESSURE, DEFAULT_SENSOR_PRESSURE),
                CONF_SENSOR_TEMPERATURE: user_input.get(CONF_SENSOR_TEMPERATURE, DEFAULT_SENSOR_TEMPERATURE),
                CONF_SENSOR_GYROSCOPE: user_input.get(CONF_SENSOR_GYROSCOPE, DEFAULT_SENSOR_GYROSCOPE),
            }
            if is_esp and CONF_NOTIFY_THROTTLE in user_input:
                data[CONF_NOTIFY_THROTTLE] = int(user_input[CONF_NOTIFY_THROTTLE])
            return self.async_create_entry(title="", data=data)

        options = self._config_entry.options
        schema_fields: dict = {
            vol.Required(
                CONF_POLL_INTERVAL,
                default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)),
            vol.Required(
                CONF_ENABLE_LIVE_UPDATES,
                default=options.get(CONF_ENABLE_LIVE_UPDATES, DEFAULT_ENABLE_LIVE_UPDATES),
            ): bool,
            vol.Required(
                CONF_SENSOR_PRESSURE,
                default=options.get(CONF_SENSOR_PRESSURE, DEFAULT_SENSOR_PRESSURE),
            ): bool,
            vol.Required(
                CONF_SENSOR_TEMPERATURE,
                default=options.get(CONF_SENSOR_TEMPERATURE, DEFAULT_SENSOR_TEMPERATURE),
            ): bool,
            vol.Required(
                CONF_SENSOR_GYROSCOPE,
                default=options.get(CONF_SENSOR_GYROSCOPE, DEFAULT_SENSOR_GYROSCOPE),
            ): bool,
        }

        if is_esp:
            schema_fields[vol.Required(
                CONF_NOTIFY_THROTTLE,
                default=options.get(CONF_NOTIFY_THROTTLE, DEFAULT_NOTIFY_THROTTLE),
            )] = vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_NOTIFY_THROTTLE, max=MAX_NOTIFY_THROTTLE),
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
        )
