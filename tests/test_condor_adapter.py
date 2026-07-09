"""Tests for the Condor (newer protocol) JSON → coordinator-data adapter.

The inputs come from ``tests/fixtures/condor_hx742x.json``, a real probe
snapshot captured with ``scripts/sonicare_scan.py --json``. Each port's
``props`` in that file is exactly what the device returns from GetProps /
ChangeIndication, so these assertions pin the mapping against real firmware
output (HX742X, firmware 1.8.20.0).
"""

from __future__ import annotations

import pytest

from custom_components.philips_sonicare_ble.condor_adapter import map_port_props

# Golden mapping of every port present in the fixture to the flat
# coordinator-data keys the entities consume. Derived from the captured
# device state; update alongside the fixture if a new capture replaces it.
EXPECTED_BY_PORT: dict[str, dict] = {
    "firmware": {"model_number": "HX742X", "firmware": "1.8.20"},
    "Sonicare": {
        "handle_state_value": 2,
        "handle_state": "run",
        "brushing_state": "on",
        "brushing_state_value": 1,
        "handle_time": 18058312,
    },
    "RoutineStatus": {
        "session_id": 7,
        "brushing_mode_value": 0,
        "brushing_mode": "clean",
        "brushing_time": 23,
        "routine_length": 120,
        "intensity_value": 0,
        "intensity": "low",
    },
    "SensorData": {},
    "BrushHead": {
        "brushhead_serial": "04:50:6D:D2:AE:22:91",
        "brushhead_lifetime_limit": 21600,
        "brushhead_lifetime_usage": 1962,
        "brushhead_ring_id": 2,
        "brushhead_nfc_version": "2.1",
    },
    "SessionStorage": {"latest_session_id": 6, "session_count": 7},
    "Diagnostics": {
        "error_persistent": 1073741888,
        "error_volatile": 1073741824,
    },
    "Extended": {"settings_bitmask": 512},
    "Battery": {"battery": 67},
}


def _all_ports(snapshot: dict) -> dict[str, dict]:
    """Flatten every product's ports into one {port: props} mapping."""
    ports: dict[str, dict] = {}
    for product in snapshot["condor"].values():
        ports.update(product["ports"])
    return ports


@pytest.mark.parametrize("port, expected", EXPECTED_BY_PORT.items())
def test_map_port_props_matches_golden(condor_hx742x, port, expected):
    """Each captured port maps to the expected coordinator-data keys."""
    props = _all_ports(condor_hx742x)[port]
    assert props is not None, f"fixture has no props for {port}"
    assert map_port_props(port, props) == expected


def test_fixture_covers_every_json_port(condor_hx742x):
    """Guard against a re-capture silently adding a port we don't assert on."""
    json_ports = {
        port
        for port, props in _all_ports(condor_hx742x).items()
        if props is not None
    }
    assert json_ports == set(EXPECTED_BY_PORT)


def test_binary_ports_have_no_props(condor_hx742x):
    """Binary streaming ports (*.b) return an empty body — recorded as null."""
    ports = _all_ports(condor_hx742x)
    assert ports["SensorData.b"] is None
    assert ports["SessionStorage.b"] is None


def test_unknown_port_returns_empty(condor_hx742x):
    """An unmapped port name is ignored, not an error."""
    assert map_port_props("NoSuchPort", {"whatever": 1}) == {}


def test_partial_props_merge_cleanly(condor_hx742x):
    """A ChangeIndication delta carries only changed keys; the mapper must
    emit only those, so callers can merge it over prior state.
    """
    out = map_port_props("Battery", {"BatteryPercent": 42})
    assert out == {"battery": 42}
