"""Tests for the bond-gated GATT profile detection (issue #25).

Some models (HX991X) expose only a minimal bootstrap profile to unbonded
centrals — Device Information (0x180A) is missing entirely, so the
capability probe fails with "not found" instead of an auth error. The
config flow must route that to the pairing path rather than reporting
"no characteristics found".
"""

from __future__ import annotations

from custom_components.philips_sonicare_ble.const import (
    SVC_DEVICE_INFO,
    SVC_SONICARE,
)
from custom_components.philips_sonicare_ble.helpers import (
    is_bond_gated_profile,
)

from .conftest import load_json_fixture

GENERIC_ACCESS = "00001800-0000-1000-8000-00805f9b34fb"
GENERIC_ATTRIBUTE = "00001801-0000-1000-8000-00805f9b34fb"
BATTERY = "0000180f-0000-1000-8000-00805f9b34fb"

# What an unbonded HX991X shows: the mandatory services plus a Sonicare
# bootstrap service, but no Device Information.
REDUCED_GATT = [GENERIC_ACCESS, GENERIC_ATTRIBUTE, SVC_SONICARE]


def test_reduced_profile_triggers_pairing() -> None:
    """No readable data + no 0x180A + Sonicare advertisement → bond-gated."""
    assert is_bond_gated_profile({}, REDUCED_GATT, [SVC_SONICARE])


def test_sonicare_evidence_from_advertisement_alone() -> None:
    """The Sonicare proof may come from the ADV when the reduced GATT
    table contains none of the proprietary services."""
    assert is_bond_gated_profile(
        {}, [GENERIC_ACCESS, GENERIC_ATTRIBUTE], [SVC_SONICARE]
    )


def test_device_info_present_is_not_bond_gated() -> None:
    """0x180A visible means the profile is not bond-gated, even when
    every probe read failed."""
    assert not is_bond_gated_profile(
        {}, REDUCED_GATT + [SVC_DEVICE_INFO], [SVC_SONICARE]
    )


def test_partial_read_is_not_bond_gated() -> None:
    """Any successfully read value disqualifies the bond-gate verdict."""
    assert not is_bond_gated_profile(
        {"battery": 87}, REDUCED_GATT, [SVC_SONICARE]
    )


def test_battery_zero_is_a_successful_read() -> None:
    """A battery reading of 0 (empty brush) is a successful read and must
    not be mistaken for 'no data' by a truthiness check."""
    assert not is_bond_gated_profile(
        {"battery": 0}, REDUCED_GATT, [SVC_SONICARE]
    )


def test_no_sonicare_evidence_is_not_bond_gated() -> None:
    """A foreign device without Device Information (manual address entry)
    must not be dragged into the pairing path — a standard battery
    service alone is not Sonicare evidence."""
    assert not is_bond_gated_profile(
        {}, [GENERIC_ACCESS, GENERIC_ATTRIBUTE, BATTERY], []
    )


def test_full_hx991x_profile_is_not_bond_gated() -> None:
    """The bonded HX991X capture exposes the full table incl. 0x180A."""
    snapshot = load_json_fixture("classic_hx991x_lightblue.json")
    gatt = [s["uuid"].lower() for s in snapshot["gatt_services"]]
    assert not is_bond_gated_profile({}, gatt, [SVC_SONICARE])
