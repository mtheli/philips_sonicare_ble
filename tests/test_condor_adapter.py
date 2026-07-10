"""Tests for the Condor (newer protocol) JSON → coordinator-data adapter.

The inputs come from ``tests/fixtures/condor_hx742x.json``, a real probe
snapshot captured with ``scripts/sonicare_scan.py --json``. Each port's
``props`` in that file is exactly what the device returns from GetProps /
ChangeIndication, so these assertions pin the mapping against real firmware
output (HX742X, firmware 1.8.20.0).
"""

from __future__ import annotations

import pytest

from custom_components.philips_sonicare_ble.condor_adapter import (
    map_port_props,
    resolve_brushing_mode,
)

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
        "routine_ids": [0, 5, 1, 2, 0],
    },
    "RoutineStatus": {
        # Mode is a raw slot position here; the label is resolved against
        # routine_ids at the coordinator, so the port mapper leaves it unset.
        "session_id": 7,
        "brushing_mode_value": 0,
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


@pytest.mark.parametrize(
    "props",
    [
        {"Sensors": 7, "Types": 3, "Control": 1},  # SenseIQ sensors enabled
        {"Sensors": 0, "Types": 3, "Control": 1},  # sensors disabled
    ],
)
def test_sensordata_props_stay_unmapped(props):
    """SensorData JSON props are control state, not telemetry (see mapper
    table) — both enable-mask variants observed on HX742X handles in the
    field (issues #13, #23) must map to no coordinator keys."""
    assert map_port_props("SensorData", props) == {}


def test_partial_props_merge_cleanly(condor_hx742x):
    """A ChangeIndication delta carries only changed keys; the mapper must
    emit only those, so callers can merge it over prior state.
    """
    out = map_port_props("Battery", {"BatteryPercent": 42})
    assert out == {"battery": 42}


def test_routine_status_mode_is_raw_position(condor_hx742x):
    """RoutineStatus exposes only the raw slot position — the label is
    resolved against routine_ids at the coordinator, not in the port mapper.
    """
    out = map_port_props("RoutineStatus", {"Mode": 1})
    assert out == {"brushing_mode_value": 1}
    assert "brushing_mode" not in out


def test_sonicare_exposes_routine_ids(condor_hx742x):
    """The Sonicare port carries the device's static slot→routine-id list."""
    out = map_port_props("Sonicare", {"RoutineIDs": [0, 5, 1, 2, 0]})
    assert out["routine_ids"] == [0, 5, 1, 2, 0]


def test_resolve_brushing_mode_indexes_routine_ids():
    """Mode is a position into RoutineIDs, not a routine id. For the HX742X
    slot list [0, 5, 1, 2, 0] the four physical slots resolve to
    clean / sensitive / white / gum_care.
    """
    routine_ids = [0, 5, 1, 2, 0]
    assert resolve_brushing_mode(routine_ids, 0) == (0, "clean")
    assert resolve_brushing_mode(routine_ids, 1) == (5, "sensitive")
    assert resolve_brushing_mode(routine_ids, 2) == (1, "white")
    assert resolve_brushing_mode(routine_ids, 3) == (2, "gum_care")


def test_resolve_brushing_mode_unresolvable():
    """Unknown list or out-of-range position resolves to None so the caller
    leaves the label unset rather than guessing.
    """
    assert resolve_brushing_mode(None, 1) is None
    assert resolve_brushing_mode([], 0) is None
    assert resolve_brushing_mode([0, 5, 1, 2, 0], 9) is None
    assert resolve_brushing_mode([0, 5, 1, 2, 0], None) is None


# --- SensorData.b binary telemetry decode ---------------------------------
# Golden inputs are real frames captured live from an HX742A over the
# SensorData.b port (little-endian). Synthetic 7-byte frames cover the
# value-carrying variant that on-change firmwares don't emit.

from custom_components.philips_sonicare_ble.condor_adapter import map_sensor_frame


@pytest.mark.parametrize(
    "frame_hex, expected",
    [
        # Temperature (type 2): whole degrees in byte 5. Real captures.
        ("02000000001b", {"temperature": 27}),
        ("020012000_1c".replace("_", "0"), {"temperature": 28}),
        ("02002d00001e", {"temperature": 30}),
        # Signed temperature — a sub-zero reading stays negative.
        ("0200000000ec", {"temperature": -20}),
        # Pressure on-change frame (5 bytes): state at byte 4, no value.
        # 0x02 is the over-pressure ("too hard") signal.
        ("0100010002", {"pressure_alarm": 2, "pressure_state": "too_high"}),
        ("0100330002", {"pressure_alarm": 2, "pressure_state": "too_high"}),
        # Pressure value frame (7 bytes): value at 4-5, state at 6.
        ("0100000_2c0102".replace("_", "0"), {"pressure": 300, "pressure_alarm": 2, "pressure_state": "too_high"}),
        # A 7-byte "ok" frame ending in 0x00 keeps its length and reads ok.
        ("01000000050000", {"pressure": 5, "pressure_alarm": 0, "pressure_state": "ok"}),
        # Counter-only 3-byte heartbeat — discarded.
        ("010002", {}),
        # Too short to carry a type.
        ("01", {}),
        ("", {}),
        # IMU (type 4) is not surfaced as a HA sensor.
        ("0400" + "00" * 14, {}),
        # Unknown frame type.
        ("0900abcd", {}),
    ],
)
def test_map_sensor_frame(frame_hex, expected):
    assert map_sensor_frame(bytes.fromhex(frame_hex)) == expected
