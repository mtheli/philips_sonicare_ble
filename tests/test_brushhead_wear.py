"""Tests for the brush-head wear derivation in ``_apply_parsed``.

A bare handle answers the lifetime characteristics with zeros and an
all-zero serial, which must not be mistaken for a brand-new head at 0 %
wear — only a valid (non-zero) serial proves a head is attached. The
serial's byte length varies per model (7 or 8 bytes), so the validity
check has to be pattern-based rather than compare a fixed string.
"""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.philips_sonicare_ble.const import (
    CONF_ADDRESS,
    CONF_ESP_DEVICE_NAME,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    TRANSPORT_ESP_BRIDGE,
)
from custom_components.philips_sonicare_ble.coordinator import (
    PhilipsSonicareCoordinator,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
SERIAL_VALID = "04:A1:5C:32:F0:80:11:D0"
SERIAL_ZERO_8 = "00:00:00:00:00:00:00:00"
SERIAL_ZERO_7 = "00:00:00:00:00:00:00"


class StubTransport:
    """Just enough transport for coordinator construction."""

    is_connected = False
    disconnect_count = 0


def make_coordinator(hass) -> PhilipsSonicareCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_TRANSPORT_TYPE: TRANSPORT_ESP_BRIDGE,
            CONF_ESP_DEVICE_NAME: "sonicare-bridge",
            CONF_SERVICES: [],
            "model": "HX9996",
        },
    )
    entry.add_to_hass(hass)
    return PhilipsSonicareCoordinator(hass, entry, StubTransport())


def test_bare_handle_reports_no_wear(hass) -> None:
    """No head attached: zeros all around must not become 0 % wear."""
    coordinator = make_coordinator(hass)

    new_data = coordinator._apply_parsed(
        {
            "brushhead_serial": SERIAL_ZERO_8,
            "brushhead_lifetime_limit": 0,
            "brushhead_lifetime_usage": 0,
        }
    )

    assert new_data["brushhead_wear_pct"] is None


def test_bare_handle_short_serial_reports_no_wear(hass) -> None:
    """Models with a 7-byte serial must be caught by the same check."""
    coordinator = make_coordinator(hass)

    new_data = coordinator._apply_parsed(
        {
            "brushhead_serial": SERIAL_ZERO_7,
            "brushhead_lifetime_limit": 0,
            "brushhead_lifetime_usage": 0,
        }
    )

    assert new_data["brushhead_wear_pct"] is None


def test_brand_new_head_reports_zero_wear(hass) -> None:
    """A real head with usage 0 is a brand-new head at 0 % (issue #12)."""
    coordinator = make_coordinator(hass)

    new_data = coordinator._apply_parsed(
        {
            "brushhead_serial": SERIAL_VALID,
            "brushhead_lifetime_limit": 226800,
            "brushhead_lifetime_usage": 0,
        }
    )

    assert new_data["brushhead_wear_pct"] == 0.0


def test_brand_new_head_without_limit_reports_zero_wear(hass) -> None:
    """Bridge timing can deliver usage before limit; the serial decides."""
    coordinator = make_coordinator(hass)

    new_data = coordinator._apply_parsed(
        {
            "brushhead_serial": SERIAL_VALID,
            "brushhead_lifetime_usage": 0,
        }
    )

    assert new_data["brushhead_wear_pct"] == 0.0


def test_used_head_reports_percentage(hass) -> None:
    coordinator = make_coordinator(hass)

    new_data = coordinator._apply_parsed(
        {
            "brushhead_serial": SERIAL_VALID,
            "brushhead_lifetime_limit": 226800,
            "brushhead_lifetime_usage": 56700,
        }
    )

    assert new_data["brushhead_wear_pct"] == 25.0
    # No routine length known yet: 120 s default → (226800-56700)/120
    assert new_data["brushhead_sessions_left"] == 1417


def test_sessions_left_uses_handle_routine_length(hass) -> None:
    coordinator = make_coordinator(hass)

    new_data = coordinator._apply_parsed(
        {
            "brushhead_serial": SERIAL_VALID,
            "brushhead_lifetime_limit": 43200,
            "brushhead_lifetime_usage": 21600,
            "routine_length": 180,
        }
    )

    assert new_data["brushhead_sessions_left"] == 120


def test_sessions_left_never_negative(hass) -> None:
    """An overused head (usage past the limit) reports 0, not a negative."""
    coordinator = make_coordinator(hass)

    new_data = coordinator._apply_parsed(
        {
            "brushhead_serial": SERIAL_VALID,
            "brushhead_lifetime_limit": 43200,
            "brushhead_lifetime_usage": 50000,
        }
    )

    assert new_data["brushhead_sessions_left"] == 0


def test_bare_handle_reports_no_sessions(hass) -> None:
    coordinator = make_coordinator(hass)

    new_data = coordinator._apply_parsed(
        {
            "brushhead_serial": SERIAL_ZERO_8,
            "brushhead_lifetime_limit": 0,
            "brushhead_lifetime_usage": 0,
        }
    )

    assert new_data["brushhead_sessions_left"] is None


def test_bare_handle_type_zero_is_not_adaptive_clean(hass) -> None:
    """The type char reads 0x00 on a bare handle — 0 is also Adaptive Clean."""
    coordinator = make_coordinator(hass)

    new_data = coordinator._apply_parsed(
        {
            "brushhead_serial": SERIAL_ZERO_8,
            "brushhead_type": "adaptive_clean",
            "brushhead_lifetime_limit": 0,
            "brushhead_lifetime_usage": 0,
        }
    )

    assert new_data["brushhead_type"] is None


def test_real_adaptive_clean_head_keeps_type(hass) -> None:
    coordinator = make_coordinator(hass)

    new_data = coordinator._apply_parsed(
        {
            "brushhead_serial": SERIAL_VALID,
            "brushhead_type": "adaptive_clean",
            "brushhead_lifetime_limit": 226800,
            "brushhead_lifetime_usage": 0,
        }
    )

    assert new_data["brushhead_type"] == "adaptive_clean"


def test_other_type_codes_survive_missing_serial(hass) -> None:
    """Only the ambiguous zero code is gated on the serial."""
    coordinator = make_coordinator(hass)

    new_data = coordinator._apply_parsed(
        {
            "brushhead_serial": SERIAL_ZERO_8,
            "brushhead_type": "non_rfid",
        }
    )

    assert new_data["brushhead_type"] == "non_rfid"


def test_usage_zero_after_head_removal_stays_cleared(hass) -> None:
    """After removal the serial is cleared; later zero reads change nothing."""
    coordinator = make_coordinator(hass)
    coordinator.data.update(
        {
            "brushhead_serial": SERIAL_VALID,
            "brushhead_lifetime_limit": 226800,
            "brushhead_lifetime_usage": 56700,
            "brushhead_wear_pct": 25.0,
        }
    )
    coordinator._clear_brushhead_data()

    new_data = coordinator._apply_parsed({"brushhead_lifetime_usage": 0})

    assert new_data["brushhead_wear_pct"] is None
