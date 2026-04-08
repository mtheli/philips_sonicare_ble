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
from homeassistant.core import Event, callback
from homeassistant.data_entry_flow import AbortFlow, FlowResult
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
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
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    CONF_ESP_DEVICE_NAME,
    CONF_ESP_BRIDGE_ID,
    CONF_NOTIFY_THROTTLE,
    CONF_SENSOR_PRESSURE,
    CONF_SENSOR_TEMPERATURE,
    CONF_SENSOR_GYROSCOPE,
    TRANSPORT_BLEAK,
    TRANSPORT_ESP_BRIDGE,
    DEFAULT_NOTIFY_THROTTLE,
    DEFAULT_SENSOR_PRESSURE,
    DEFAULT_SENSOR_TEMPERATURE,
    DEFAULT_SENSOR_GYROSCOPE,
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
from .exceptions import NotPairedException, TransportError

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
        self._esp_bridge_id: str = ""
        self._esp_bridge_ids: list[str] = []
        self._bridge_info: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Duplicate check
    # ------------------------------------------------------------------
    def _abort_if_already_configured(self) -> None:
        """Abort with detailed message if this unique_id is already configured."""
        for entry in self._async_current_entries():
            if entry.unique_id and entry.unique_id == self.unique_id:
                transport = entry.data.get(CONF_TRANSPORT_TYPE, TRANSPORT_BLEAK)
                transport_label = (
                    "ESP32 Bridge" if transport == TRANSPORT_ESP_BRIDGE
                    else "Direct Bluetooth"
                )
                disabled = entry.disabled_by is not None
                status = "disabled" if disabled else "active"
                raise AbortFlow(
                    "already_configured_detail",
                    description_placeholders={
                        "transport": transport_label,
                        "status": status,
                    },
                )

    # ------------------------------------------------------------------
    # Capabilities fetch (direct BLE)
    # ------------------------------------------------------------------
    async def _async_fetch_capabilities(self, address: str) -> dict[str, Any]:
        """Connect to the device and read capabilities via direct BLE."""
        # Pre-fill services from advertisement data (available before connect)
        adv_services: list[str] = []
        if self._discovery_info is not None:
            adv_services = [
                u.lower() for u in (self._discovery_info.service_uuids or [])
            ]

        result: dict[str, Any] = {"services": list(adv_services)}

        # Always get the freshest BLEDevice reference — the discovery_info
        # device may be stale if the user waited before clicking Submit.
        device = async_ble_device_from_address(self.hass, address)
        if not device and self._discovery_info is not None:
            device = self._discovery_info.device

        if not device:
            _LOGGER.warning("Device %s not found in range", address)
            return result

        client: BleakClient | None = None
        try:
            client = await bleak_establish(
                BleakClient, device, "philips_sonicare_ble",
                use_services_cache=True, timeout=30.0,
            )
            if not client or not client.is_connected:
                return result

            # GATT services are more complete than advertisement — use them
            gatt_services = [str(s.uuid).lower() for s in client.services]
            if gatt_services:
                result["services"] = gatt_services

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
                    err_msg = str(err).lower()
                    if any(
                        hint in err_msg
                        for hint in (
                            "0x05", "0x0e", "0x0f",
                            "unlikely error",
                            "insufficient auth", "insufficient enc",
                            "not permitted", "authentication", "security",
                        )
                    ) or (client and client.is_connected):
                        raise NotPairedException from err
                    _LOGGER.debug("Failed to read %s: %s", key, err)

        except NotPairedException:
            raise
        except (BleakError, TimeoutError) as err:
            err_msg = str(err).lower()
            if "failed to discover services" in err_msg:
                _LOGGER.warning("Service discovery failed (stale bond?): %s", err)
                raise NotPairedException from err
            _LOGGER.warning("Could not connect during capabilities fetch: %s", err)
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
    # BLE pairing helpers
    # ------------------------------------------------------------------
    async def _try_auto_pair(self, address: str) -> bool:
        """Attempt D-Bus auto-pairing. Returns True on success."""
        from .dbus_pairing import PairingError, async_pair_and_trust, is_dbus_available

        if not is_dbus_available():
            _LOGGER.debug("D-Bus not available — cannot auto-pair")
            return False

        try:
            _LOGGER.info("Auto-pairing %s via D-Bus ...", address)
            await async_pair_and_trust(address)
            await asyncio.sleep(2)  # let BlueZ key distribution settle
            return True
        except PairingError as err:
            _LOGGER.warning("Auto-pairing failed for %s: %s", address, err)
            return False

    async def _fetch_with_pair_retry(self, address: str) -> dict[str, Any]:
        """Fetch capabilities, auto-pairing on auth errors.

        Uses a D-Bus pre-check to skip the slow connect attempt when the
        device is known to be unpaired.  Falls back to probe-read error
        detection if the pre-check is inconclusive.

        Raises NotPairedException if pairing fails or is not possible.
        """
        from .dbus_pairing import async_is_device_paired, is_dbus_available

        # Always try connecting without pairing first — many devices
        # (including Sonicare For Kids) have open GATT even though
        # BlueZ reports them as "not paired".
        try:
            result = await self._async_fetch_capabilities(address)
            result["pairing"] = "open_gatt"
            return result
        except NotPairedException:
            pass

        # Connection failed due to auth — try auto-pairing
        if await self._try_auto_pair(address):
            try:
                result = await self._async_fetch_capabilities(address)
                result["pairing"] = "bonded"
                return result
            except NotPairedException:
                pass
        raise NotPairedException

    # ------------------------------------------------------------------
    # Capabilities fetch (ESP bridge)
    # ------------------------------------------------------------------
    async def _async_fetch_capabilities_esp(
        self,
        address: str,
        esp_device_name: str,
        esp_bridge_id: str = "",
    ) -> dict[str, Any]:
        """Read capabilities and probe services via ESP32 bridge."""
        transport = EspBridgeTransport(self.hass, address, esp_device_name, esp_bridge_id)
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

        rows = found_rows + unknown_rows
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
        pairing = data.get("pairing")
        if pairing == "bonded":
            rows.append("<tr><td><b>BLE Security</b></td><td>Paired (bonded)</td></tr>")
        elif pairing == "open_gatt":
            rows.append("<tr><td><b>BLE Security</b></td><td>Open GATT (no pairing)</td></tr>")
        if not rows:
            return "Could not read device information"
        return "<table>" + "".join(rows) + "</table>"

    @staticmethod
    def _has_sonicare_services(data: dict[str, Any]) -> bool:
        """Check if any Sonicare-specific GATT services were discovered."""
        services = data.get("services", [])
        fetched_lower = {s.lower() for s in services} - _STANDARD_BLE_SERVICES
        return bool(fetched_lower & _EXPECTED_SERVICES)

    @staticmethod
    def _get_connection_status_text(
        name: str, bridge_info: str, data: dict[str, Any]
    ) -> str:
        """Return connection status message based on fetched data."""
        return f"✅ Successfully connected to **{name}**{bridge_info}."

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

    def _detect_esp_bridge_ids(self, esp_device_name: str) -> list[str]:
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
    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> FlowResult:
        """Handle Zeroconf discovery of ESPHome devices.

        Checks if the discovered ESPHome device has our Sonicare bridge
        services registered. If not, aborts silently.
        """
        # Extract device name from zeroconf hostname (e.g. "atom-lite" from "atom-lite.local.")
        host = discovery_info.hostname or ""
        device_name = host.rstrip(".").removesuffix(".local").replace("-", "_")
        if not device_name:
            return self.async_abort(reason="not_supported")

        # Wait for ESPHome to register services (may not be ready yet)
        for _ in range(10):
            bridge_ids = self._detect_esp_bridge_ids(device_name)
            if bridge_ids:
                break
            await asyncio.sleep(3)
        else:
            return self.async_abort(reason="not_supported")

        # Found bridges — check if ALL are already configured
        self._esp_device_name = device_name
        self._esp_bridge_ids = bridge_ids

        configured_macs = {
            entry.unique_id.upper()
            for entry in self._async_current_entries()
            if entry.unique_id
        }

        # Probe bridges to check which are ours and which are already configured
        unconfigured = False
        for did in bridge_ids:
            svc_name = f"{device_name}_ble_get_info"
            if did:
                svc_name += f"_{did}"
            info_future: asyncio.Future[dict[str, str]] = self.hass.loop.create_future()

            @callback
            def _on_status(event: Event, _did=did) -> None:
                if (event.data.get("status") == "info"
                        and event.data.get("bridge_id", "") == _did
                        and not info_future.done()):
                    info_future.set_result(dict(event.data))

            unsub = self.hass.bus.async_listen(
                "esphome.philips_sonicare_ble_status", _on_status
            )
            try:
                await self.hass.services.async_call(
                    "esphome", svc_name, {}, blocking=True
                )
                info = await asyncio.wait_for(info_future, timeout=3.0)
                mac = info.get("mac", "")
                if not mac or mac.upper() not in configured_macs:
                    unconfigured = True
                    break
            except (asyncio.TimeoutError, Exception):
                pass  # Not our bridge type — skip
            finally:
                unsub()

        if not unconfigured:
            return self.async_abort(reason="already_configured")

        _LOGGER.info("Zeroconf: found Sonicare bridge on ESP device '%s'", device_name)
        self._name = device_name.replace("_", "-")
        self.context["title_placeholders"] = {"name": self._name}

        if len(bridge_ids) > 1:
            return await self.async_step_esp_select_device()
        self._esp_bridge_id = bridge_ids[0]
        return await self._esp_bridge_health_check()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle Bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_already_configured()

        self._discovery_info = discovery_info
        self._address = discovery_info.address
        self._name = discovery_info.name or "Philips Sonicare"

        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_bluetooth_confirm()

    def _find_esp_bridge(self) -> str | None:
        """Find an ESP device running the philips_sonicare component."""
        esphome_entries = self.hass.config_entries.async_entries("esphome")
        for entry in esphome_entries:
            device_name = entry.data.get("device_name")
            if device_name and self._detect_esp_bridge_ids(device_name):
                return device_name
        return None

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm Bluetooth discovery."""
        # If an ESP bridge with our component is available, skip Direct BLE
        # and go straight to the ESP Bridge flow.
        esp_device = self._find_esp_bridge()
        if esp_device:
            self._esp_device_name = esp_device
            self._esp_bridge_ids = self._detect_esp_bridge_ids(esp_device)
            if len(self._esp_bridge_ids) > 1:
                return await self.async_step_esp_select_device()
            self._esp_bridge_id = self._esp_bridge_ids[0]
            return await self._esp_bridge_health_check()

        if user_input is None:
            return self.async_show_form(
                step_id="bluetooth_confirm",
                description_placeholders={
                    "name": self._name,
                    "address": self._address,
                },
            )

        errors: dict[str, str] = {}
        try:
            self._fetched_data = await self._fetch_with_pair_retry(self._address)
            has_device_info = any(
                self._fetched_data.get(k)
                for k in ("model", "serial", "firmware", "battery")
            )
            if has_device_info and self._has_sonicare_services(self._fetched_data):
                self._transport_type = TRANSPORT_BLEAK
                return await self.async_step_show_capabilities()
            errors["base"] = "cannot_connect"
        except NotPairedException:
            return await self.async_step_not_paired()
        except Exception:
            _LOGGER.exception("Unexpected error during capabilities fetch")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._name,
                "address": self._address,
            },
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
            self._abort_if_already_configured()

            self._address = address
            self._name = address

            try:
                self._fetched_data = await self._fetch_with_pair_retry(address)
                has_device_info = any(
                    self._fetched_data.get(k)
                    for k in ("model", "serial", "firmware", "battery")
                )
                if has_device_info and self._has_sonicare_services(self._fetched_data):
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
            except NotPairedException:
                return await self.async_step_not_paired()
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

            bridge_ids = self._detect_esp_bridge_ids(esp_device_name)
            if not bridge_ids:
                _LOGGER.error("No philips_sonicare services found on %s", esp_device_name)
                errors["base"] = "cannot_connect"
            else:
                self._esp_device_name = esp_device_name
                self._esp_bridge_ids = bridge_ids

                if len(bridge_ids) > 1:
                    return await self.async_step_esp_select_device()

                self._esp_bridge_id = bridge_ids[0]
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
            selected = user_input["esp_bridge_id"]
            if selected.startswith("✅ "):
                return self.async_abort(reason="already_configured")
            self._esp_bridge_id = selected
            return await self._esp_bridge_health_check()

        # Collect MACs already configured for this integration
        configured_macs = {
            entry.unique_id.upper()
            for entry in self._async_current_entries()
            if entry.unique_id
        }

        # Probe all bridge_ids in parallel — filter by bridge_id in response
        async def _probe(did: str) -> tuple[str, dict[str, str] | None]:
            svc_name = f"{self._esp_device_name}_ble_get_info"
            if did:
                svc_name += f"_{did}"
            info_future: asyncio.Future[dict[str, str]] = self.hass.loop.create_future()

            @callback
            def _on_status(event: Event) -> None:
                if (event.data.get("status") == "info"
                        and event.data.get("bridge_id", "") == did
                        and not info_future.done()):
                    info_future.set_result(dict(event.data))

            unsub = self.hass.bus.async_listen(
                "esphome.philips_sonicare_ble_status", _on_status
            )
            try:
                await self.hass.services.async_call(
                    "esphome", svc_name, {}, blocking=True
                )
                return did, await asyncio.wait_for(info_future, timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                return did, None
            finally:
                unsub()

        results = await asyncio.gather(*[_probe(did) for did in self._esp_bridge_ids])

        options: list[SelectOptionDict] = []
        has_available = False
        found_any = False
        for did, info in results:
            # Skip bridges that don't respond (e.g. Shaver bridges
            # fire a different event type that our transport never receives)
            if info is None:
                continue

            found_any = True
            mac = info.get("mac", "")
            mac_suffix = f" — {mac}" if mac and mac != "00:00:00:00:00:00" else ""

            if mac and mac.upper() in configured_macs:
                options.append(SelectOptionDict(
                    value=f"✅ {did}",
                    label=f"✅ {did}{mac_suffix}",
                ))
            else:
                has_available = True
                options.append(SelectOptionDict(
                    value=did,
                    label=f"{did}{mac_suffix}" if did else mac or "default",
                ))

        if not found_any:
            return self.async_abort(reason="no_devices_found")
        if not has_available:
            return self.async_abort(reason="already_configured")

        # Auto-select if only one unconfigured device and no configured ones shown
        unconfigured = [o for o in options if not o["value"].startswith("✅ ")]
        if len(unconfigured) == 1 and len(options) == 1:
            self._esp_bridge_id = unconfigured[0]["value"]
            return await self._esp_bridge_health_check()

        return self.async_show_form(
            step_id="esp_select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("esp_bridge_id"): SelectSelector(
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
            self.hass, "", self._esp_device_name, self._esp_bridge_id
        )
        try:
            await transport.connect()
            info = await transport.get_bridge_info()
            self._bridge_info = {
                "version": (info or {}).get("version") or transport.bridge_version or "?",
                "ble_connected": (info or {}).get("ble_connected", str(transport.is_device_connected).lower()),
                "mac": (info or {}).get("mac") or transport.detected_mac or "",
                "paired": transport.ble_paired or "",
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
                    "", self._esp_device_name, self._esp_bridge_id,
                )

                sonicare_mac = capabilities.get("sonicare_mac")
                if sonicare_mac:
                    await self.async_set_unique_id(
                        sonicare_mac.upper(), raise_on_progress=False
                    )
                else:
                    await self.async_set_unique_id(f"esp_{self._esp_device_name}")
                self._abort_if_already_configured()

                # Add pairing status from bridge info
                paired_str = (self._bridge_info or {}).get("paired", "")
                if paired_str == "true":
                    capabilities["pairing"] = "bonded"
                elif paired_str == "false":
                    capabilities["pairing"] = "open_gatt"

                self._fetched_data = capabilities
                self._address = sonicare_mac
                model = capabilities.get("model")
                self._name = model if model else self._esp_device_name
                self._transport_type = TRANSPORT_ESP_BRIDGE

                return await self.async_step_show_capabilities()

            except AbortFlow:
                raise
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

            paired_str = info.get("paired", "")
            if paired_str == "true":
                paired_text = "Paired (bonded)"
            elif paired_str == "false":
                paired_text = "Open GATT (no pairing)"
            else:
                paired_text = ""

            rows = [
                f"<tr><td><b>Version</b></td><td>v{version}</td></tr>",
                f"<tr><td><b>BLE</b></td><td>{ble_status}</td></tr>",
            ]
            if mac and mac != "00:00:00:00:00:00":
                rows.append(f"<tr><td><b>MAC</b></td><td><code>{mac}</code></td></tr>")
            if paired_text:
                rows.append(f"<tr><td><b>Security</b></td><td>{paired_text}</td></tr>")

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
                "model": self._fetched_data.get("model", ""),
            }

            if self._transport_type == TRANSPORT_ESP_BRIDGE:
                entry_data[CONF_TRANSPORT_TYPE] = TRANSPORT_ESP_BRIDGE
                entry_data[CONF_ESP_DEVICE_NAME] = self._esp_device_name
                if self._esp_bridge_id:
                    entry_data[CONF_ESP_BRIDGE_ID] = self._esp_bridge_id
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

        connection_status = self._get_connection_status_text(
            str(self._name), bridge_info, self._fetched_data
        )

        return self.async_show_form(
            step_id="show_capabilities",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": str(self._name),
                "connection_status": connection_status,
                "device_info": device_info_text,
                "services": services_text,
            },
        )

    # ------------------------------------------------------------------
    # Pairing fallback (manual instructions)
    # ------------------------------------------------------------------
    async def async_step_not_paired(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show manual pairing instructions when auto-pairing failed."""
        if user_input is not None:
            # User clicked retry after manual pairing
            errors: dict[str, str] = {}
            try:
                self._fetched_data = await self._async_fetch_capabilities(
                    self._address
                )
                has_device_info = any(
                    self._fetched_data.get(k)
                    for k in ("model", "serial", "firmware", "battery")
                )
                if has_device_info and self._has_sonicare_services(
                    self._fetched_data
                ):
                    self._transport_type = TRANSPORT_BLEAK
                    return await self.async_step_show_capabilities()
                errors["base"] = "cannot_connect"
            except NotPairedException:
                errors["base"] = "pairing_failed"
            except Exception:
                _LOGGER.exception("Error after manual pairing retry")
                errors["base"] = "unknown"

            return self.async_show_form(
                step_id="not_paired",
                description_placeholders={
                    "address": self._address or "",
                },
                errors=errors,
            )

        return self.async_show_form(
            step_id="not_paired",
            description_placeholders={
                "address": self._address or "",
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
                CONF_SENSOR_PRESSURE: user_input.get(CONF_SENSOR_PRESSURE, DEFAULT_SENSOR_PRESSURE),
                CONF_SENSOR_TEMPERATURE: user_input.get(CONF_SENSOR_TEMPERATURE, DEFAULT_SENSOR_TEMPERATURE),
                CONF_SENSOR_GYROSCOPE: user_input.get(CONF_SENSOR_GYROSCOPE, DEFAULT_SENSOR_GYROSCOPE),
            }
            if is_esp:
                if CONF_NOTIFY_THROTTLE in user_input:
                    data[CONF_NOTIFY_THROTTLE] = int(user_input[CONF_NOTIFY_THROTTLE])
            return self.async_create_entry(title="", data=data)

        options = self._config_entry.options
        schema_fields: dict = {}
        schema_fields[vol.Required(
                CONF_SENSOR_PRESSURE,
                default=options.get(CONF_SENSOR_PRESSURE, DEFAULT_SENSOR_PRESSURE),
            )] = bool
        schema_fields[vol.Required(
                CONF_SENSOR_TEMPERATURE,
                default=options.get(CONF_SENSOR_TEMPERATURE, DEFAULT_SENSOR_TEMPERATURE),
            )] = bool
        schema_fields[vol.Required(
                CONF_SENSOR_GYROSCOPE,
                default=options.get(CONF_SENSOR_GYROSCOPE, DEFAULT_SENSOR_GYROSCOPE),
            )] = bool

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
