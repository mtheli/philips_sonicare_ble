"""Config flow for Philips Sonicare BLE."""
from __future__ import annotations

import asyncio
import logging
import time
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
    SVC_CONDOR,
)
from .helpers import esphome_service_id
from .transport import EspBridgeTransport, describe_connection_path
from .exceptions import NotPairedException, TransportError

_LOGGER = logging.getLogger(__name__)

# Sentinel option in the Direct-BLE picker that switches to free-text entry.
# Picked when the user wants to type a MAC manually (e.g. an RPA-rotating
# brush whose current address is not the freshest one in the discovery list).
_MANUAL_ADDRESS = "__manual__"

# Standard BLE services to hide from display
_STANDARD_BLE_SERVICES = {
    "00001800-0000-1000-8000-00805f9b34fb",  # Generic Access
    "00001801-0000-1000-8000-00805f9b34fb",  # Generic Attribute
}

# Services any supported Sonicare exposes. A device qualifies as a
# Sonicare if *any* of these appear — older models fan out into the
# per-feature Classic services, HX742X / Series 7100 (Condor) collapses
# everything onto a single framed transport service.
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
    SVC_CONDOR.lower(),
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
    SVC_CONDOR.lower(): "Condor (Series 7100+)",
}

# What each service enables in HA — shown as the "Provides" column.
SERVICE_FEATURES: dict[str, str] = {
    SVC_BATTERY.lower(): "Battery level",
    SVC_DEVICE_INFO.lower(): "Model, serial, firmware",
    SVC_SONICARE.lower(): "Handle state, brushing mode",
    SVC_ROUTINE.lower(): "Session timing, current mode",
    SVC_STORAGE.lower(): "Session history",
    SVC_SENSOR.lower(): "Pressure & motion sensors",
    SVC_BRUSHHEAD.lower(): "Brush head NFC, wear tracking",
    SVC_DIAGNOSTIC.lower(): "Error log",
    SVC_EXTENDED.lower(): "Adaptive intensity, feedback toggles",
    SVC_BYTESTREAM.lower(): "Streaming data channel",
    SVC_CONDOR.lower(): "Newer protocol (HX7100+)",
}

# Classic services that the Condor protocol replaces with its single
# framed transport service.
_CLASSIC_SERVICE_UUIDS = {
    SVC_BATTERY.lower(),
    SVC_SONICARE.lower(),
    SVC_ROUTINE.lower(),
    SVC_STORAGE.lower(),
    SVC_SENSOR.lower(),
    SVC_BRUSHHEAD.lower(),
    SVC_DIAGNOSTIC.lower(),
    SVC_EXTENDED.lower(),
}

# Map each service to one representative characteristic for ESP probing.
# The ESP bridge has no "list services" call, so we read one char per
# Classic service and add the service when the read returns data. The
# Condor service has no universally-readable char (e50b0005 is optional
# firmware-side and missing on HX742X FW 1.8.20.0), so Condor is
# inferred by exclusion below — if Device Information answered but no
# Classic service did, the device must be Condor.
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
        self._probed_bridges: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._manual_address_entry: bool = False
        self._configured_bridge_ids: set[str] = set()

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
    @staticmethod
    async def _read_with_auth_retry(
        client: BleakClient,
        char_uuid: str,
        timeout: float = 5.0,
    ) -> bytes | None:
        """Read a GATT characteristic, retrying once on auth errors.

        ESPHome bluetooth_proxy negotiates SMP in the background on the
        first read of a protected characteristic. That read returns
        status=0x05; auth completes ~500-1500 ms later. A single retry
        after a 2s grace period turns the transient failure into a
        success without false-positive "not paired" errors.
        """
        try:
            return await asyncio.wait_for(
                client.read_gatt_char(char_uuid), timeout=timeout
            )
        except (BleakError, TimeoutError) as err:
            err_msg = str(err).lower()
            auth_error = any(
                hint in err_msg
                for hint in (
                    "0x05", "0x0e", "0x0f",
                    "unlikely error",
                    "insufficient auth", "insufficient enc",
                    "not permitted", "authentication", "security",
                )
            )
            if not auth_error or not client.is_connected:
                raise
            _LOGGER.info(
                "Read on %s returned auth error — waiting for SMP to complete",
                char_uuid,
            )
            await asyncio.sleep(2.0)
            return await asyncio.wait_for(
                client.read_gatt_char(char_uuid), timeout=timeout
            )

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

            connection_path = describe_connection_path(self.hass, client, device)
            result["connection_path"] = connection_path
            _LOGGER.info(
                "%s: capabilities probe connected via %s",
                address,
                connection_path,
            )

            # GATT services are more complete than advertisement — use them
            gatt_services = [str(s.uuid).lower() for s in client.services]
            if gatt_services:
                result["services"] = gatt_services

            # Condor brushes (HX742X / Series 7100+) require BLE bonding
            # before the e50b… handshake's first CCCD write is accepted.
            # The probe below only touches Battery / Device-Info chars
            # which are open-read on these devices, so the bond
            # requirement wouldn't surface as an auth error here.
            # Trigger auto-pair preemptively when the Condor service is
            # discovered and no bond exists yet — mirrors the ESP bridge's
            # esp_ble_set_encryption() trigger on Condor detection.
            if SVC_CONDOR.lower() in gatt_services:
                from .dbus_pairing import async_is_device_paired
                if not await async_is_device_paired(address):
                    _LOGGER.info(
                        "%s: Condor service present without a bond — "
                        "requesting auto-pair before continuing",
                        address,
                    )
                    raise NotPairedException("Condor brush requires bonding")

            for char_uuid, key in (
                (CHAR_BATTERY_LEVEL, "battery"),
                (CHAR_MODEL_NUMBER, "model"),
                (CHAR_SERIAL_NUMBER, "serial"),
                (CHAR_FIRMWARE_REVISION, "firmware"),
            ):
                try:
                    raw = await self._read_with_auth_retry(
                        client, char_uuid, timeout=5.0
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

        Probe first without pairing; only pair on auth failure. After a
        successful probe, query BlueZ for the bond state to label the
        device — this distinguishes true open GATT from a device that
        is already bonded (e.g. a stale bond that survived a previous
        config entry removal and lets reads succeed without a new
        handshake).

        Raises NotPairedException if pairing fails or is not possible.
        """
        from .dbus_pairing import async_is_device_paired

        try:
            result = await self._async_fetch_capabilities(address)
            paired = await async_is_device_paired(address)
            result["pairing"] = "bonded" if paired else "open_gatt"
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
            battery: int | None = None
            for svc_uuid, probe_char in SERVICE_PROBE_CHARS.items():
                raw = await transport.read_char(probe_char)
                if raw is not None:
                    found_services.append(svc_uuid)
                    if probe_char == CHAR_MODEL_NUMBER:
                        model_number = raw.decode("utf-8", errors="replace").strip()
                    elif probe_char == CHAR_BATTERY_LEVEL and raw:
                        battery = raw[0]

            if not found_services:
                raise TransportError(
                    "Could not read any service via ESP bridge - toothbrush may not be connected"
                )

            # Condor-by-exclusion: the only readable Condor char (e50b0005)
            # is optional — HX742X FW 1.8.20.0 omits it entirely, so a
            # direct probe misses that device. We're already past the
            # name-based Sonicare discovery, so if the device answered
            # Device Information but none of the Classic feature services,
            # the only supported protocol left is Condor.
            classic_seen = any(
                svc in found_services for svc in (
                    SVC_SONICARE, SVC_ROUTINE, SVC_STORAGE, SVC_SENSOR,
                    SVC_BRUSHHEAD, SVC_DIAGNOSTIC, SVC_EXTENDED, SVC_BATTERY,
                )
            )
            if (
                model_number
                and not classic_seen
                and SVC_CONDOR not in found_services
            ):
                found_services.append(SVC_CONDOR)
                _LOGGER.debug(
                    "ESP bridge: inferred Condor protocol on %s (model=%s, no Classic services)",
                    address, model_number,
                )

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

            connection_path = esp_device_name
            if esp_bridge_id:
                connection_path = f"{esp_device_name} / {esp_bridge_id}"
            return {
                "services": found_services,
                "sonicare_mac": transport.detected_mac,
                "model": model_number,
                "serial": serial,
                "firmware": firmware,
                "battery": battery,
                "connection_path": connection_path,
            }

        except TransportError:
            raise
        finally:
            await transport.disconnect()

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_family(fetched_lower: set[str], model: str) -> str:
        """Return device family: 'condor', 'mode_b', or 'classic'."""
        if SVC_CONDOR.lower() in fetched_lower:
            return "condor"
        # HX63xx / HX64xx are Mode-B brushes with a reduced service set.
        m = (model or "").upper()
        if m.startswith(("HX63", "HX64")):
            return "mode_b"
        return "classic"

    @staticmethod
    def _missing_reason(uuid_lower: str, family: str) -> str:
        """Why a service is absent on this device, if we can explain it."""
        if family == "condor" and uuid_lower in _CLASSIC_SERVICE_UUIDS:
            return "via Condor protocol"
        if uuid_lower == SVC_CONDOR.lower():
            return "for HX7100+ models"
        if family == "mode_b":
            return "not on this model"
        return ""

    @classmethod
    def _get_service_status_text(
        cls, fetched_uuids: list[str], model: str = ""
    ) -> str:
        """Format found and missing services as an HTML table (no header).

        Cells stay terse — the *why* of a missing service collapses into
        a footer below the table so that long reason strings don't wrap
        cells across multiple lines.
        """
        fetched_lower = {s.lower() for s in fetched_uuids} - _STANDARD_BLE_SERVICES
        family = cls._detect_family(fetched_lower, model)

        def _row(icon: str, name: str, provides: str) -> str:
            return (
                f"<tr><td>{icon}</td><td>{name}</td>"
                f"<td>{provides}</td></tr>"
            )

        found_rows: list[str] = []
        missing_rows: list[str] = []
        used_reasons: set[str] = set()

        for uuid in sorted(_EXPECTED_SERVICES):
            name = SERVICE_NAMES.get(uuid)
            if not name:
                continue
            feature = SERVICE_FEATURES.get(uuid, "")
            if uuid in fetched_lower:
                found_rows.append(_row("✅", name, feature))
            else:
                reason = cls._missing_reason(uuid, family)
                if reason:
                    used_reasons.add(reason)
                missing_rows.append(_row("❌", name, feature))

        known_all = _EXPECTED_SERVICES | {SVC_BYTESTREAM.lower()}
        for uuid in sorted(fetched_lower - _EXPECTED_SERVICES):
            name = SERVICE_NAMES.get(uuid)
            if not name or uuid not in known_all:
                # Drop services we can't name or describe — visual noise.
                continue
            found_rows.append(_row("✅", name, SERVICE_FEATURES.get(uuid, "")))

        rows = found_rows + missing_rows
        if not rows:
            return "No services detected"

        table = f"<table><tbody>{''.join(rows)}</tbody></table>"

        footer_for = {
            "not on this model":
                "❌ entries are not available on this model.",
            "for HX7100+ models":
                "Condor protocol is exclusive to HX7100+ (Series 7100+).",
            "via Condor protocol":
                "Classic feature services are replaced by the Condor "
                "protocol on this model.",
        }
        notes = [footer_for[r] for r in sorted(used_reasons) if r in footer_for]
        if notes:
            table += "\n\n" + "\n\n".join(notes)
        return table

    @staticmethod
    def _get_device_info_text(data: dict[str, Any], address: str | None = None) -> str:
        """Format device info as an HTML table (no header)."""
        rows: list[str] = []
        if model := data.get("model"):
            rows.append(f"<tr><td><b>Model</b></td><td>{model}</td></tr>")
        if serial := data.get("serial"):
            rows.append(f"<tr><td><b>Serial</b></td><td><code>{serial}</code></td></tr>")
        if firmware := data.get("firmware"):
            rows.append(f"<tr><td><b>Firmware</b></td><td>{firmware}</td></tr>")
        if (battery := data.get("battery")) is not None:
            rows.append(f"<tr><td><b>Battery</b></td><td>{battery}%</td></tr>")
        if address:
            rows.append(
                f"<tr><td><b>MAC</b></td><td><code>{address.upper()}</code></td></tr>"
            )
        pairing = data.get("pairing")
        if pairing == "bonded":
            rows.append(
                "<tr><td><b>BLE Security</b></td><td>Bonded (encrypted)</td></tr>"
            )
        elif pairing == "open_gatt":
            rows.append(
                "<tr><td><b>BLE Security</b></td><td>Unpaired (no encryption)</td></tr>"
            )
        if not rows:
            return "Could not read device information"
        return f"<table><tbody>{''.join(rows)}</tbody></table>"

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
        if bridge_info:
            return f"✅ Connected{bridge_info}."
        return "✅ Connected."

    # ------------------------------------------------------------------
    # ESP bridge helpers
    # ------------------------------------------------------------------
    async def _get_esphome_device_options(self) -> list[SelectOptionDict]:
        """Build a list of ESPHome devices that host a Sonicare bridge.

        Service-name detection alone is not enough — philips_shaver
        registers the same service names. We probe ble_get_info on each
        candidate and only accept ESPs where at least one bridge replies
        on the Sonicare event channel.
        """
        esphome_entries = self.hass.config_entries.async_entries("esphome")
        options: list[SelectOptionDict] = []
        self._probed_bridges = {}
        for entry in esphome_entries:
            device_name = entry.data.get("device_name")
            if not device_name:
                continue
            device_name = esphome_service_id(device_name)
            bridge_ids = self._detect_esp_bridge_ids(device_name)
            if not bridge_ids:
                continue
            sonicare = await self._probe_sonicare_bridges(device_name, bridge_ids)
            if not sonicare:
                _LOGGER.debug(
                    "Skipping ESP %s: no philips_sonicare bridge responded", device_name
                )
                continue
            self._probed_bridges[device_name] = sonicare

            slot_info = ""
            if len(sonicare) > 1:
                paired_count = sum(
                    1 for _, info in sonicare
                    if info.get("pair_capable") != "true"
                    and info.get("mac", "") not in ("", "00:00:00:00:00:00")
                )
                free_count = len(sonicare) - paired_count
                parts = []
                if paired_count:
                    parts.append(f"{paired_count} paired")
                if free_count:
                    parts.append(f"{free_count} free")
                if parts:
                    slot_info = f"{' / '.join(parts)} slots"

            show_slug = entry.title.lower() != device_name.lower()
            if show_slug and slot_info:
                label = f"{entry.title} ({device_name}, {slot_info})"
            elif show_slug:
                label = f"{entry.title} ({device_name})"
            elif slot_info:
                label = f"{entry.title} ({slot_info})"
            else:
                label = entry.title

            options.append(SelectOptionDict(value=device_name, label=label))
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

    async def _probe_bridge_info(
        self, esp_device_name: str, bridge_id: str, timeout: float = 3.0,
    ) -> dict[str, str] | None:
        """Probe a single bridge via ble_get_info.

        Returns the info-event payload, or ``None`` if the call timed out
        or no Sonicare-bridge response was received. Listening on
        ``philips_sonicare_ble_status`` is the disambiguator versus a
        philips_shaver bridge that happens to share service names.
        """
        svc_name = f"{esp_device_name}_ble_get_info"
        if bridge_id:
            svc_name += f"_{bridge_id}"
        if not self.hass.services.has_service("esphome", svc_name):
            return None

        info_future: asyncio.Future[dict[str, str]] = self.hass.loop.create_future()

        @callback
        def _on_status(event: Event) -> None:
            if (event.data.get("status") == "info"
                    and event.data.get("bridge_id", "") == bridge_id
                    and not info_future.done()):
                info_future.set_result(dict(event.data))

        unsub = self.hass.bus.async_listen(
            "esphome.philips_sonicare_ble_status", _on_status
        )
        try:
            await self.hass.services.async_call(
                "esphome", svc_name, {}, blocking=True
            )
            return await asyncio.wait_for(info_future, timeout=timeout)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — broad catch is intentional
            return None
        finally:
            unsub()

    async def _probe_sonicare_bridges(
        self, esp_device_name: str, bridge_ids: list[str],
    ) -> list[tuple[str, dict[str, str]]]:
        """Probe all bridge_ids on an ESP in parallel; keep responders only."""
        results = await asyncio.gather(
            *(self._probe_bridge_info(esp_device_name, did) for did in bridge_ids)
        )
        return [(did, info) for did, info in zip(bridge_ids, results) if info is not None]

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
        device_name = esphome_service_id(host.rstrip(".").removesuffix(".local"))
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
        # (esp_device_name, esp_bridge_id) tuples that already have a
        # ConfigEntry — used as a probe-independent fallback when the
        # bridge can't supply mac/identity right after boot (NVS-restore
        # race) or while the bridge is actively connecting.
        configured_bridges = {
            (
                entry.data.get(CONF_ESP_DEVICE_NAME, ""),
                entry.data.get(CONF_ESP_BRIDGE_ID, ""),
            )
            for entry in self._async_current_entries()
            if entry.data.get(CONF_TRANSPORT_TYPE) == TRANSPORT_ESP_BRIDGE
        }

        # Probe bridges to check which are ours and which are already configured
        unconfigured = False
        for did in bridge_ids:
            # Direct ConfigEntry match — skip probe.
            if (device_name, did) in configured_bridges:
                continue
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
                # Prefer identity_address (persisted in NVS, used as
                # ConfigEntry.unique_id) over mac (= live remote_bda which
                # is 00:00:… while the brush is disconnected).
                identity = info.get("identity_address", "").upper()
                mac = info.get("mac", "").upper()
                known = {m for m in (identity, mac)
                         if m and m != "00:00:00:00:00:00"}
                if not known or not known.intersection(configured_macs):
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

    async def _find_esp_bridge_for_mac(
        self, target_mac: str
    ) -> tuple[str, str] | None:
        """Locate an ESP bridge slot that already has this MAC bonded.

        Returns (esp_device_name, bridge_id) when an ESP slot reports
        `mac` equal to ``target_mac``; otherwise None. We deliberately
        only match bonded slots — an empty pair-capable slot doesn't
        justify diverting a discovered brush away from Direct BLE.
        """
        target = target_mac.upper()
        esphome_entries = self.hass.config_entries.async_entries("esphome")
        for entry in esphome_entries:
            device_name = entry.data.get("device_name")
            if not device_name:
                continue
            device_name = esphome_service_id(device_name)
            bridge_ids = self._detect_esp_bridge_ids(device_name)
            if not bridge_ids:
                continue
            sonicare = await self._probe_sonicare_bridges(device_name, bridge_ids)
            for bridge_id, info in sonicare:
                mac = info.get("mac", "").upper()
                if mac and mac == target:
                    return (device_name, bridge_id)
        return None

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm Bluetooth discovery."""
        # Auto-route to ESP only when an ESP slot already has this MAC
        # bonded — otherwise fall through to Direct BLE confirm so the
        # user can pick the manual ESP path themselves if needed.
        match = await self._find_esp_bridge_for_mac(self._address or "")
        if match:
            self._esp_device_name, self._esp_bridge_id = match
            self._esp_bridge_ids = self._detect_esp_bridge_ids(self._esp_device_name)
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
            raw = user_input[CONF_ADDRESS]
            if raw == _MANUAL_ADDRESS:
                # User picked the "enter manually" sentinel — re-render the
                # step as a free-text field, keep prior errors empty.
                self._manual_address_entry = True
            else:
                address = raw.upper()
                await self.async_set_unique_id(address)
                self._abort_if_already_configured()

                self._address = address
                self._name = address

                try:
                    self._fetched_data = await self._fetch_with_pair_retry(address)
                    # Refuse to create an entry if the GATT probe never
                    # established a connection. ``connection_path`` is set by
                    # ``_async_fetch_capabilities`` only after a live client
                    # is in hand — its absence means we have no evidence the
                    # device is reachable at this address (out of range,
                    # rotated RPA, no slot). Creating an entry anyway would
                    # leave the user with a permanently "Initializing"
                    # device and no actionable feedback in the UI.
                    if "connection_path" not in self._fetched_data:
                        errors["base"] = "cannot_connect"
                    else:
                        has_device_info = any(
                            self._fetched_data.get(k)
                            for k in ("model", "serial", "firmware", "battery")
                        )
                        if has_device_info and self._has_sonicare_services(self._fetched_data):
                            self._transport_type = TRANSPORT_BLEAK
                            return await self.async_step_show_capabilities()
                        # Connect succeeded but the device didn't expose any
                        # Sonicare service / DeviceInfo we could read. Don't
                        # create an empty entry that would just sit in
                        # "Initializing" forever — surface as an error so
                        # the user can re-try with a different address.
                        errors["base"] = "not_a_sonicare"
                except NotPairedException:
                    return await self.async_step_not_paired()
                except Exception:
                    _LOGGER.exception("Unexpected error during manual setup")
                    errors["base"] = "cannot_connect"

        # Free-text entry path: nothing to discover, or user asked for it.
        if self._manual_address_entry:
            return self.async_show_form(
                step_id="user_bleak",
                data_schema=vol.Schema({
                    vol.Required(
                        CONF_ADDRESS,
                        default=self._address,
                    ) if self._address else vol.Required(CONF_ADDRESS): str,
                }),
                errors=errors,
            )

        # Build the discovered-device picker. Each option label carries the
        # advertisement age and RSSI so RPA-rotating brushes (e.g. HX742X)
        # are picked from the freshest entry instead of a stale RPA whose
        # connect attempt would just time out.
        now_mono = time.monotonic()
        scored: list[tuple[int, SelectOptionDict]] = []
        for info in async_discovered_service_info(self.hass):
            name = info.name or ""
            if "sonicare" not in name.lower() and "philips ohc" not in name.lower():
                continue
            age_s = max(0, int(now_mono - info.time)) if info.time else None
            rssi = info.rssi
            label_parts = [f"{name} ({info.address})"]
            if age_s is not None:
                label_parts.append(f"{age_s}s ago")
            if rssi is not None:
                label_parts.append(f"{rssi} dBm")
            label = label_parts[0] + (" — " + ", ".join(label_parts[1:]) if len(label_parts) > 1 else "")
            scored.append((
                age_s if age_s is not None else 9999,
                SelectOptionDict(value=info.address, label=label),
            ))

        if scored:
            scored.sort(key=lambda t: t[0])  # freshest first
            options: list[SelectOptionDict] = [opt for _, opt in scored]
            options.append(SelectOptionDict(
                value=_MANUAL_ADDRESS,
                label="Other — enter address manually",
            ))
            return self.async_show_form(
                step_id="user_bleak",
                data_schema=vol.Schema({
                    vol.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(options=options)
                    )
                }),
                errors=errors,
            )

        # No discoveries — fall back to free text.
        return self.async_show_form(
            step_id="user_bleak",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_ADDRESS,
                    default=self._address,
                ) if self._address else vol.Required(CONF_ADDRESS): str,
            }),
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
            esp_device_name = esphome_service_id(user_input["esp_device_name"].strip())

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

        esp_options = await self._get_esphome_device_options()

        if not esp_options:
            return self.async_abort(reason="no_esphome_devices")

        if len(esp_options) == 1 and not user_input:
            sole = esp_options[0]["value"]
            self._esp_device_name = sole
            bridge_ids = self._detect_esp_bridge_ids(sole)
            self._esp_bridge_ids = bridge_ids
            if len(bridge_ids) > 1:
                return await self.async_step_esp_select_device()
            self._esp_bridge_id = bridge_ids[0] if bridge_ids else ""
            return await self._esp_bridge_health_check()

        data_schema = vol.Schema(
            {
                vol.Required("esp_device_name"): SelectSelector(
                    SelectSelectorConfig(options=esp_options)
                ),
            }
        )

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
            if selected in self._configured_bridge_ids:
                return self.async_abort(reason="already_configured")
            self._esp_bridge_id = selected
            return await self._esp_bridge_health_check()

        # Collect MACs already configured for this integration
        configured_macs = {
            entry.unique_id.upper()
            for entry in self._async_current_entries()
            if entry.unique_id
        }

        # Reuse probes collected during the device-list step if present;
        # otherwise probe now (e.g. discovery / bluetooth_confirm path).
        cached = self._probed_bridges.get(self._esp_device_name)
        if cached is None:
            cached = await self._probe_sonicare_bridges(
                self._esp_device_name, self._esp_bridge_ids
            )
            self._probed_bridges[self._esp_device_name] = cached

        self._configured_bridge_ids = set()
        options: list[SelectOptionDict] = []
        has_available = False
        shown_states: set[str] = set()

        for did, info in cached:
            mac = info.get("mac", "")
            has_mac = bool(mac) and mac != "00:00:00:00:00:00"
            is_configured = has_mac and mac.upper() in configured_macs
            label = self._format_bridge_label(did, info)

            if is_configured:
                self._configured_bridge_ids.add(did)
                options.append(SelectOptionDict(value=did, label=f"✅ {label}"))
                shown_states.add("already_configured")
            else:
                has_available = True
                options.append(SelectOptionDict(value=did, label=label))

            if info.get("pair_capable") == "true":
                shown_states.add("pair_required")
                continue
            paired = info.get("paired", "")
            if paired == "true":
                shown_states.add("bonded")
            elif paired == "false":
                shown_states.add("open_gatt")
            if info.get("ble_connected") == "true":
                shown_states.add("online")
            else:
                shown_states.add("offline")

        if not cached:
            return self.async_abort(reason="no_devices_found")
        if not has_available:
            return self.async_abort(reason="already_configured")

        # Auto-select if only one unconfigured device and no configured ones shown
        unconfigured = [
            o for o in options if o["value"] not in self._configured_bridge_ids
        ]
        if len(unconfigured) == 1 and len(options) == 1:
            self._esp_bridge_id = unconfigured[0]["value"]
            return await self._esp_bridge_health_check()

        # Default to first unconfigured option so users don't have to deselect
        # the already-configured ✅ entry every time.
        default_value = unconfigured[0]["value"] if unconfigured else options[0]["value"]

        legend_parts: list[str] = []
        if "already_configured" in shown_states:
            legend_parts.append("✅ already configured")
        if "bonded" in shown_states:
            legend_parts.append("🔒 bonded")
        if "open_gatt" in shown_states:
            legend_parts.append("🔓 unpaired")
        if "online" in shown_states:
            legend_parts.append("🟢 online")
        if "offline" in shown_states:
            legend_parts.append("⚪ offline")

        pair_hint = (
            "Entries without status icons are empty bridges in pair-mode. "
            "Switch on the toothbrush you want to bond before submitting."
            if "pair_required" in shown_states
            else ""
        )

        return self.async_show_form(
            step_id="esp_select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("esp_bridge_id", default=default_value): SelectSelector(
                        SelectSelectorConfig(options=options)
                    ),
                }
            ),
            description_placeholders={
                "legend": " · ".join(legend_parts),
                "pair_hint": pair_hint,
            },
        )

    @staticmethod
    def _format_bridge_label(bridge_id: str, info: dict[str, str]) -> str:
        """Human-readable label for a bridge entry in the picker."""
        name = bridge_id or "default"
        if info.get("pair_capable") == "true":
            return name

        mac = info.get("mac", "")
        model = info.get("model", "")
        ble_name = info.get("ble_name", "")
        connected = info.get("ble_connected") == "true"
        paired = info.get("paired", "")

        icons: list[str] = []
        if paired == "true":
            icons.append("🔒")
        elif paired == "false":
            icons.append("🔓")
        icons.append("🟢" if connected else "⚪")

        descriptor = " / ".join(p for p in (model, ble_name) if p)
        body_parts = [name]
        if descriptor:
            body_parts.append(descriptor)
        if mac and mac != "00:00:00:00:00:00":
            body_parts.append(mac.upper())

        return f"{' '.join(icons)} {' — '.join(body_parts)}"

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
            raw = info or {}
            self._bridge_info = {
                "version": raw.get("version") or transport.bridge_version or "?",
                "ble_connected": raw.get("ble_connected", str(transport.is_device_connected).lower()),
                "mac": raw.get("mac") or transport.detected_mac or "",
                "paired": transport.ble_paired or "",
                # Mode B pair-flow signals (absent on older bridges → defaults
                # keep the classic flow).
                "mode": raw.get("mode", "external"),
                "pair_capable": raw.get("pair_capable", "false"),
                "pair_mode_active": raw.get("pair_mode_active", "false"),
                "identity_address": raw.get("identity_address", ""),
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

    async def async_step_esp_bridge_status_connected(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Alias step rendered when the bridge is already BLE-connected.

        HA routes step submissions to async_step_<step_id>; we keep the
        translations split between two step IDs (different action hints)
        but share the implementation here.
        """
        return await self.async_step_esp_bridge_status(user_input)

    async def async_step_esp_bridge_status(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show ESP bridge status before reading toothbrush capabilities."""
        errors: dict[str, str] = {}

        # Mode B with no bound brush → user must arm pair-mode first.
        info = self._bridge_info or {}
        if info.get("pair_capable", "false") == "true":
            return await self.async_step_request_pair()

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
        ble_connected = info.get("ble_connected") == "true" if info else False
        if info:
            version = info.get("version", "?")
            mac = info.get("mac", "")

            ble_status = "\u2705 Connected" if ble_connected else "\u274c Disconnected"

            paired_str = info.get("paired", "")
            if paired_str == "true":
                paired_text = "Bonded (encrypted)"
            elif paired_str == "false":
                paired_text = "Unpaired (no encryption)"
            else:
                paired_text = ""

            rows = [f"<tr><td><b>BLE</b></td><td>{ble_status}</td></tr>"]
            if paired_text:
                rows.append(
                    f"<tr><td><b>Security</b></td><td>{paired_text}</td></tr>"
                )
            if mac and mac != "00:00:00:00:00:00":
                rows.append(
                    f"<tr><td><b>Toothbrush MAC</b></td><td><code>{mac}</code></td></tr>"
                )
            rows.append(f"<tr><td><b>Version</b></td><td>v{version}</td></tr>")

            status_text = f"<table><tbody>{''.join(rows)}</tbody></table>"
        else:
            status_text = "Diagnostic details not available."

        target = self._esp_device_name or ""
        if self._esp_bridge_id:
            target = f"{target} / {self._esp_bridge_id}"

        step_id = (
            "esp_bridge_status_connected" if ble_connected else "esp_bridge_status"
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._esp_device_name or "",
                "target": target,
                "status": status_text,
            },
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Pair-mode flow (Mode B bridges with no bound identity)
    # ------------------------------------------------------------------
    async def async_step_request_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask the user to confirm before arming pair-mode on the bridge."""
        if user_input is not None:
            return await self.async_step_wait_pair()

        return self.async_show_form(
            step_id="request_pair",
            data_schema=vol.Schema({}),
            description_placeholders=self._pair_target_placeholders(),
        )

    def _pair_target_placeholders(self) -> dict[str, str]:
        """Placeholders identifying the bridge being paired/reset."""
        device = self._esp_device_name or ""
        if self._esp_bridge_id:
            target = f"{device} / {self._esp_bridge_id}"
        else:
            target = device
        return {
            "device_name": device,
            "bridge_id": self._esp_bridge_id or "",
            "target": target,
        }

    async def async_step_wait_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Arm pair-mode and wait for pair_complete / pair_timeout event."""
        timeout_s = 60
        bridge_id = self._esp_bridge_id or ""
        pair_future: asyncio.Future[dict[str, str]] = self.hass.loop.create_future()

        @callback
        def _on_status(event: Event) -> None:
            data = event.data
            if data.get("bridge_id", "") != bridge_id:
                return
            status = data.get("status")
            if status not in ("pair_complete", "pair_timeout"):
                return
            if not pair_future.done():
                pair_future.set_result(dict(data))

        unsub = self.hass.bus.async_listen(
            "esphome.philips_sonicare_ble_status", _on_status
        )
        try:
            svc_name = f"{self._esp_device_name}_ble_pair_mode"
            if bridge_id:
                svc_name += f"_{bridge_id}"
            try:
                await self.hass.services.async_call(
                    "esphome",
                    svc_name,
                    {"enabled": True, "timeout_s": str(timeout_s)},
                    blocking=True,
                )
            except Exception as err:
                _LOGGER.error("Failed to arm pair-mode on %s: %s",
                              self._esp_device_name, err)
                return self.async_show_form(
                    step_id="request_pair",
                    data_schema=vol.Schema({}),
                    errors={"base": "cannot_connect"},
                    description_placeholders=self._pair_target_placeholders(),
                )

            try:
                # Wait slightly longer than the bridge's own timeout so the
                # pair_timeout event has a chance to arrive before we give up.
                result = await asyncio.wait_for(pair_future, timeout=timeout_s + 5)
            except asyncio.TimeoutError:
                _LOGGER.warning("Pair-mode wait timed out (no event received)")
                return self.async_show_form(
                    step_id="request_pair",
                    data_schema=vol.Schema({}),
                    errors={"base": "pair_timeout"},
                    description_placeholders=self._pair_target_placeholders(),
                )
        finally:
            unsub()
            # If we leave without a pair_complete/pair_timeout event (HA-side
            # timeout, exception, …), tell the bridge to stand down so a fresh
            # Sonicare in range during the bridge's leftover window doesn't
            # get auto-bonded. Best-effort — the bridge has its own timer.
            if not pair_future.done():
                try:
                    await self.hass.services.async_call(
                        "esphome", svc_name,
                        {"enabled": False, "timeout_s": "0"},
                        blocking=False,
                    )
                except Exception:
                    _LOGGER.debug("Best-effort pair-mode cancel failed (ignoring)")

        if result.get("status") == "pair_timeout":
            return self.async_show_form(
                step_id="request_pair",
                data_schema=vol.Schema({}),
                errors={"base": "pair_timeout"},
                description_placeholders=self._pair_target_placeholders(),
            )

        identity = result.get("identity_address", "").upper()
        if not identity:
            _LOGGER.error("pair_complete received without identity_address")
            return self.async_show_form(
                step_id="request_pair",
                data_schema=vol.Schema({}),
                errors={"base": "unknown"},
                description_placeholders=self._pair_target_placeholders(),
            )

        # Pair succeeded — clear bridge_info so the next health check picks up
        # the freshly-bound state, then run capabilities probe via the
        # existing ESP-bridge path.
        self._bridge_info = None
        self._address = identity
        await self.async_set_unique_id(identity, raise_on_progress=False)
        self._abort_if_already_configured()
        return await self._esp_bridge_health_check()

    async def async_step_reset_bridge(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm + execute unpair on a bound Mode B bridge."""
        if user_input is not None:
            svc_name = f"{self._esp_device_name}_ble_unpair"
            if self._esp_bridge_id:
                svc_name += f"_{self._esp_bridge_id}"
            try:
                await self.hass.services.async_call(
                    "esphome", svc_name, {}, blocking=True,
                )
            except Exception as err:
                _LOGGER.error("Failed to unpair %s: %s",
                              self._esp_device_name, err)
                return self.async_show_form(
                    step_id="reset_bridge",
                    data_schema=vol.Schema({}),
                    errors={"base": "cannot_connect"},
                )
            # Bridge is now pair_capable=true again — refetch info, then re-pair.
            self._bridge_info = None
            return await self._esp_bridge_health_check()

        placeholders = self._pair_target_placeholders()
        placeholders["identity_address"] = (
            (self._bridge_info or {}).get("identity_address", "")
        )
        return self.async_show_form(
            step_id="reset_bridge",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
        )

    # ------------------------------------------------------------------
    # Capabilities dialog (shared by BLE and ESP)
    # ------------------------------------------------------------------
    async def async_step_show_capabilities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show detected device info and services, then create entry."""
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

        device_info_text = self._get_device_info_text(self._fetched_data, self._address)
        services_text = self._get_service_status_text(
            self._fetched_data.get("services", []),
            self._fetched_data.get("model") or "",
        )

        path = self._fetched_data.get("connection_path")
        bridge_info = f" via **{path}**" if path else ""

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
