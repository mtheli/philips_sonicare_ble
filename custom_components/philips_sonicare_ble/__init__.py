# custom_components/philips_sonicare/__init__.py
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    CONF_ADDRESS,
    CONF_TRANSPORT_TYPE,
    TRANSPORT_ESP_BRIDGE,
    CONF_ESP_DEVICE_NAME,
    CONF_ESP_BRIDGE_ID,
    CHAR_SERVICE_MAP,
)
from .coordinator import PhilipsSonicareCoordinator
from .helpers import esphome_service_id
from .transport import BleakTransport, EspBridgeTransport

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT, Platform.SWITCH]

SERVICE_READ_CHARACTERISTIC = "read_characteristic"
SERVICE_WRITE_CHARACTERISTIC = "write_characteristic"


def _get_coordinator(hass: HomeAssistant, entry_id: str | None):
    """Resolve coordinator from entry_id or use first available."""
    if entry_id and entry_id in hass.data[DOMAIN]:
        return hass.data[DOMAIN][entry_id]["coordinator"]
    first = next(iter(hass.data[DOMAIN].values()), None)
    return first["coordinator"] if first else None


def _async_link_via_esp_device(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Link the Sonicare device to its ESP32 bridge in the device registry."""
    esp_device_name = entry.data[CONF_ESP_DEVICE_NAME]
    dev_reg = dr.async_get(hass)

    # Find the ESPHome config entry matching our bridge device name.
    # ESPHome stores device_name in mDNS form (atom-lite); we store it
    # in service-id form (atom_lite). Normalize the entry side and compare.
    esp_mac: str | None = None
    target = esphome_service_id(esp_device_name)
    for esphome_entry in hass.config_entries.async_entries("esphome"):
        entry_name = esphome_service_id(esphome_entry.data.get("device_name", ""))
        if entry_name == target:
            esp_mac = esphome_entry.unique_id
            break

    if not esp_mac:
        _LOGGER.debug("ESPHome config entry for '%s' not found", esp_device_name)
        return

    esp_device = dev_reg.async_get_device(
        connections={(dr.CONNECTION_NETWORK_MAC, esp_mac)}
    )
    if not esp_device:
        _LOGGER.debug("ESPHome device for '%s' not in registry", esp_device_name)
        return

    device_id = entry.data.get(CONF_ADDRESS) or esp_device_name

    # Link Sonicare toothbrush device → ESPHome device
    sonicare_device = dev_reg.async_get_device(
        identifiers={(DOMAIN, device_id)}
    )
    if sonicare_device:
        dev_reg.async_update_device(sonicare_device.id, via_device_id=esp_device.id)
        _LOGGER.info("Linked Sonicare device to ESP bridge '%s'", esp_device_name)

    # Link ESP Bridge sub-device → ESPHome device
    bridge_device = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{device_id}_bridge")}
    )
    if bridge_device:
        dev_reg.async_update_device(bridge_device.id, via_device_id=esp_device.id)
        _LOGGER.info("Linked Bridge sub-device to ESP '%s'", esp_device_name)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Philips Sonicare from a config entry."""
    address = entry.data.get("address", "")
    transport_type = entry.data.get(CONF_TRANSPORT_TYPE)

    if transport_type == TRANSPORT_ESP_BRIDGE:
        esp_device_name = entry.data[CONF_ESP_DEVICE_NAME]
        esp_bridge_id = entry.data.get(CONF_ESP_BRIDGE_ID, "")
        transport = EspBridgeTransport(hass, address, esp_device_name, esp_bridge_id)
    else:
        transport = BleakTransport(hass, address)

    coordinator = PhilipsSonicareCoordinator(hass, entry, transport)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}

    # Non-blocking first refresh — the toothbrush sleeps most of the time,
    # so blocking startup for a device that may not be reachable is not worth it.
    # Sensors will show "Unknown" briefly until the device wakes up.
    coordinator.async_set_updated_data(coordinator.data or {})

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Link device to ESP bridge in device registry
    if transport_type == TRANSPORT_ESP_BRIDGE:
        _async_link_via_esp_device(hass, entry)

    # Start polling/live monitoring after platforms are registered
    await coordinator.async_start()

    # Register debug services (only once)
    _char_schema = vol.Schema({
        vol.Required("characteristic_uuid"): vol.Any(str, [str]),
        vol.Optional("entry_id"): str,
    })

    async def _read_uuids(coord, raw_input) -> tuple[str, dict[str, dict]]:
        """Read one or more characteristics."""
        if isinstance(raw_input, list):
            uuids = [u.strip().lower() for u in raw_input]
        else:
            uuids = [u.strip().lower() for u in raw_input.split(",")]

        if not coord.transport.is_connected:
            return "not_connected", {u: {"value": None, "bytes": 0} for u in uuids}

        results = {}
        for char_uuid in uuids:
            try:
                raw = await coord.transport.read_char(char_uuid)
            except Exception as e:
                _LOGGER.error("Failed to read characteristic %s: %s", char_uuid, e)
                results[char_uuid] = {"value": None, "bytes": 0, "error": str(e)}
                continue
            if raw is None:
                entry_result: dict[str, Any] = {"value": None, "bytes": 0}
                error = getattr(coord.transport, "pop_read_error", lambda u: None)(char_uuid)
                if error:
                    entry_result["error"] = error
                results[char_uuid] = entry_result
            else:
                results[char_uuid] = {"value": raw.hex(), "bytes": len(raw), "_raw": raw}
        has_errors = any("error" in r for r in results.values())
        has_data = any(r.get("value") is not None for r in results.values())
        if has_errors:
            status = "partial" if has_data else "error"
        else:
            status = "ok"
        return status, results

    if not hass.services.has_service(DOMAIN, SERVICE_READ_CHARACTERISTIC):
        async def handle_read_characteristic(call: ServiceCall) -> ServiceResponse:
            """Read GATT characteristics and return parsed values."""
            coord = _get_coordinator(hass, call.data.get("entry_id"))
            if not coord:
                return {"status": "no_device", "results": {}, "parsed": {}}

            status, results = await _read_uuids(coord, call.data["characteristic_uuid"])

            # Parse requested characteristics in isolation
            to_parse = {uuid: r["_raw"] for uuid, r in results.items() if "_raw" in r}
            parsed = {}
            if to_parse:
                saved_data = coord.data
                try:
                    coord.data = {}
                    parsed_data = coord._process_results(to_parse)
                finally:
                    coord.data = saved_data
                for key, val in parsed_data.items():
                    if key == "last_seen":
                        continue
                    parsed[key] = val

            clean = {uuid: {k: v for k, v in r.items() if k != "_raw"} for uuid, r in results.items()}
            return {"status": status, "results": clean, "parsed": parsed}

        hass.services.async_register(
            DOMAIN, SERVICE_READ_CHARACTERISTIC, handle_read_characteristic,
            schema=_char_schema, supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_WRITE_CHARACTERISTIC):
        async def handle_write_characteristic(call: ServiceCall) -> ServiceResponse:
            """Write a hex value to a BLE GATT characteristic."""
            coord = _get_coordinator(hass, call.data.get("entry_id"))
            if not coord:
                return {"status": "no_device"}

            raw_uuid = call.data["characteristic_uuid"]
            char_uuid = raw_uuid.strip().lower() if isinstance(raw_uuid, str) else raw_uuid
            hex_value = call.data["value"].replace(" ", "")

            if not coord.transport.is_connected:
                return {"status": "not_connected", "characteristic": char_uuid}

            try:
                payload = bytes.fromhex(hex_value)
            except ValueError:
                return {"status": "error", "error": f"Invalid hex value: {hex_value}"}

            try:
                await coord.transport.write_char(char_uuid, payload)
            except Exception as e:
                _LOGGER.error("Failed to write characteristic %s: %s", char_uuid, e)
                return {"status": "error", "characteristic": char_uuid, "error": str(e)}

            return {
                "status": "ok",
                "characteristic": char_uuid,
                "written": hex_value,
                "bytes": len(payload),
            }

        hass.services.async_register(
            DOMAIN, SERVICE_WRITE_CHARACTERISTIC, handle_write_characteristic,
            schema=vol.Schema({
                vol.Required("characteristic_uuid"): str,
                vol.Required("value"): str,
                vol.Optional("entry_id"): str,
            }),
            supports_response=SupportsResponse.ONLY,
        )

    _LOGGER.info("Philips Sonicare integration loaded - device: %s", address)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Philips Sonicare config entry."""
    _LOGGER.info("Unloading Philips Sonicare integration started")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    coordinator = hass.data[DOMAIN].pop(entry.entry_id)["coordinator"]
    await coordinator.async_shutdown()

    # Remove services if no more entries
    if not hass.data[DOMAIN]:
        for svc in (SERVICE_READ_CHARACTERISTIC, SERVICE_WRITE_CHARACTERISTIC):
            if hass.services.has_service(DOMAIN, svc):
                hass.services.async_remove(DOMAIN, svc)

    # Allow re-discovery for direct BLE devices
    if entry.data.get(CONF_TRANSPORT_TYPE) != TRANSPORT_ESP_BRIDGE:
        from homeassistant.components.bluetooth import async_rediscover_address
        async_rediscover_address(hass, entry.data["address"])

    _LOGGER.info("Unloading Philips Sonicare integration finished")
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Release the device-side bond when the entry is permanently removed.

    Distinct from ``async_unload_entry`` (which fires on every reload /
    restart). Only ``async_remove_entry`` reflects the user's intent to
    delete the entry for good — the right hook for un-bonding the brush
    so the next setup attempt sees a clean slate.

    Two transport-specific paths:

    - **ESP bridge**: call the bridge's ``ble_unpair`` ESPHome service.
      That wipes the BLE bond and the NVS-persisted identity on the
      ESP, returning the bridge to ``pair_capable=true``.
    - **Direct BLE**: drop the host-side BlueZ bond via the same
      ``async_pair_and_trust``-companion D-Bus call we already use for
      stale-bond cleanup during pairing. Symmetric to the auto-pair path.

    Both branches are best-effort. If the ESP is offline or D-Bus is
    unreachable we log and return quietly — HA will delete the entry
    regardless of what this returns.
    """
    transport = entry.data.get(CONF_TRANSPORT_TYPE)

    if transport == TRANSPORT_ESP_BRIDGE:
        esp_device_name = entry.data.get(CONF_ESP_DEVICE_NAME)
        if not esp_device_name:
            return
        esp_device_name = esphome_service_id(esp_device_name)
        bridge_id = entry.data.get(CONF_ESP_BRIDGE_ID, "")

        svc_name = f"{esp_device_name}_ble_unpair"
        if bridge_id:
            svc_name += f"_{bridge_id}"

        if not hass.services.has_service("esphome", svc_name):
            _LOGGER.info(
                "ESP bridge %s offline at remove time — skipping ble_unpair "
                "(bond on bridge stays)",
                esp_device_name,
            )
            return

        # Listen for the bridge's `unpaired` confirmation before returning.
        # Bridge v1.3.2+ defers the event by ~2 s so the BLE stack can settle;
        # we wait up to 4 s. If the event doesn't arrive the bridge may have
        # wedged and need a manual reboot — log a warning, don't block the
        # entry removal (HA deletes the entry regardless of what we return).
        unpair_done = asyncio.Event()

        @callback
        def _on_status(event) -> None:
            data = event.data
            if data.get("status") != "unpaired":
                return
            if data.get("bridge_id", "") != bridge_id:
                return
            unpair_done.set()

        unsub = hass.bus.async_listen(
            "esphome.philips_sonicare_ble_status", _on_status
        )

        try:
            await hass.services.async_call(
                "esphome", svc_name, {}, blocking=True,
            )
            try:
                await asyncio.wait_for(unpair_done.wait(), timeout=4.0)
                _LOGGER.info(
                    "Removed bond on ESP bridge %s for %s",
                    esp_device_name,
                    entry.unique_id or entry.data.get(CONF_ADDRESS, "<unknown>"),
                )
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "ble_unpair on %s did not confirm within 4s — bridge may "
                    "need a manual reboot to recover",
                    esp_device_name,
                )
        except Exception as err:  # noqa: BLE001 — removal must not fail
            _LOGGER.warning(
                "ble_unpair on %s failed during entry removal: %s",
                esp_device_name,
                err,
            )
        finally:
            unsub()
        return

    # Direct BLE — release host-side BlueZ bond.
    address = entry.data.get(CONF_ADDRESS)
    if not address:
        return
    from .dbus_pairing import async_remove_device, is_dbus_available
    if not is_dbus_available():
        _LOGGER.debug(
            "D-Bus unavailable — skipping host-side unpair for %s", address
        )
        return
    try:
        await async_remove_device(address)
    except Exception as err:  # noqa: BLE001 — removal must not fail
        _LOGGER.warning(
            "Host-side unpair failed during entry removal for %s: %s",
            address,
            err,
        )
