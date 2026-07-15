"""Shared helpers for the Philips Sonicare BLE integration."""

from typing import Any

from .const import SVC_DEVICE_INFO

# Proprietary Sonicare service families: Classic (477ea600…) and the newer
# framed transport (e50ba3c0…). Deliberately narrower than the config flow's
# _EXPECTED_SERVICES, which also lists standard services (0x180A/0x180F)
# that any BLE device may expose — as bond-gate evidence only the
# proprietary families count.
_SONICARE_SERVICE_PREFIXES = ("477ea600", "e50ba3c0")


def is_bond_gated_profile(
    result: dict[str, Any],
    gatt_services: list[str],
    adv_services: list[str],
) -> bool:
    """Detect a brush that hides its GATT table until bonded.

    Some models expose only a minimal bootstrap profile to unbonded
    centrals: Device Information (0x180A) is missing entirely, so the
    capability probe fails with "not found" instead of an auth error and
    the config flow's auth-hint path never fires (HX991X, issue #25). A
    connected device that advertises Sonicare services but hides 0x180A
    means the full table is bond-gated. Every model seen so far exposes
    0x180A without a bond (Condor even keeps its chars open-read), so
    its absence is a safe discriminator.
    """
    # `is not None`, not truthiness: a battery reading of 0 is a successful
    # read and must count as "device answered".
    if any(
        result.get(k) is not None
        for k in ("model", "serial", "firmware", "battery")
    ):
        return False
    if SVC_DEVICE_INFO.lower() in (s.lower() for s in gatt_services):
        return False
    return any(
        s.lower().startswith(_SONICARE_SERVICE_PREFIXES)
        for s in list(adv_services) + list(gatt_services)
    )


def esphome_service_id(device_name: str) -> str:
    """Normalize an ESPHome device name to its Home Assistant service prefix.

    ESPHome stores ``device_name`` in mDNS form (e.g. ``atom-lite``) in
    its config entry, but HA registers each ESPHome user-service with
    underscores (e.g. ``atom_lite_ble_read_char``). Run any name that
    came from ``entry.data["device_name"]``, an mDNS hostname, or
    free-text user input through this helper before using it as a
    prefix for ``hass.services.has_service`` /
    ``hass.services.async_services`` lookups, otherwise the lookup
    silently misses every device whose name contains a hyphen.
    """
    return device_name.replace("-", "_")
