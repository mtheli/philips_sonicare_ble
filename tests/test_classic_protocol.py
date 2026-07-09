"""Tests for the Classic (legacy protocol) GATT → coordinator-data decoder.

Inputs are real snapshots captured with ``scripts/sonicare_scan.py --json``:

* ``classic_hx6340_kids.json`` — Sonicare for Kids HX6340 (firmware 4.2.2), a
  minimal brush: no brush-head NFC, no diagnostics.
* ``classic_hx992x.json`` — DiamondClean HX992X (firmware 2.15.1), a premium
  brush exercising brush-head NFC, diagnostics and settings.

Each characteristic's ``value_hex`` is the raw GATT payload, so these
assertions pin ``parse_results`` against real device bytes.
"""

from __future__ import annotations

import pytest

from custom_components.philips_sonicare_ble.classic_protocol import ClassicProtocol
from custom_components.philips_sonicare_ble.const import uses_routine_id_mode

from .conftest import chars_as_bytes, load_json_fixture

# Golden decodes keyed by fixture file. Derived from the captures; update
# alongside a fixture if a new capture replaces it.
EXPECTED_HX6340 = {
    "battery": 24,
    "brushing_mode": "clean",
    "brushing_mode_value": 0,
    "brushing_time": 3,
    "firmware": "4.2.2",
    "handle_state": "run",
    "handle_state_value": 2,
    "handle_time": 23118353,
    "intensity": "medium",
    "intensity_value": 1,
    "latest_session_id": 286,
    "manufacturer_name": "Philips OHC",
    "model_number": "HX6340",
    "motor_runtime": 25171,
    "routine_length": 120,
    "serial_number": "0000000000",
    "session_id": 287,
}

EXPECTED_HX992X = {
    "battery": 32,
    "brushhead_date": "241211 72M",
    "brushhead_lifetime_limit": 21600,
    "brushhead_lifetime_usage": 5737,
    "brushhead_nfc_version": 258,
    "brushhead_payload": "https://www.philips.com/nfcbrushheadtap",
    "brushhead_ring_id": 20,
    "brushhead_serial": "04:09:1C:72:B8:1B:91",
    "brushhead_type": "sensitive",
    "brushing_mode": "white_plus",
    "brushing_mode_value": 1,
    "brushing_state": "on",
    "brushing_state_value": 1,
    "brushing_time": 7,
    "error_persistent": 4096,
    "error_volatile": 0,
    "firmware": "2.15.1 0.6.1.0",
    "handle_state": "run",
    "handle_state_value": 2,
    "handle_time": 1783615540,
    "intensity": "medium",
    "intensity_value": 1,
    "latest_session_id": 484,
    "manufacturer_name": "Philips OHC",
    "model_number": "HX992X",
    "motor_runtime": 44444,
    "routine_length": 160,
    "serial_number": "0000000000",
    "session_count": 485,
    "session_id": 485,
    "settings_bitmask": 1,
}

EXPECTED_HX999X = {
    "battery": 46,
    "brushhead_date": "260322 43M",
    "brushhead_lifetime_limit": 21600,
    "brushhead_lifetime_usage": 1911,
    "brushhead_nfc_version": 258,
    "brushhead_payload": "https://www.philips.com/nfcbrushheadtap",
    "brushhead_ring_id": 2,
    "brushhead_serial": "04:50:6D:D2:AE:22:91",
    "brushhead_type": "adaptive_clean",
    "brushing_mode": "clean",
    "brushing_mode_value": 0,
    "brushing_state": "on",
    "brushing_state_value": 1,
    "brushing_time": 27,
    "error_persistent": 1073750052,
    "error_volatile": 1073741828,
    "firmware": "1.15.4",
    "handle_state": "run",
    "handle_state_value": 2,
    "handle_time": 5913072,
    "intensity": "low",
    "intensity_value": 0,
    "latest_session_id": 245,
    "manufacturer_name": "Philips OHC",
    "model_number": "HX999X",
    "motor_runtime": 19795,
    "routine_length": 120,
    "serial_number": "0000000000",
    "session_count": 246,
    "session_id": 246,
    "settings_bitmask": 6656,
}

EXPECTED_HX960X = {
    "battery": 42,
    "brushhead_date": "260322 43M",
    "brushhead_lifetime_limit": 21600,
    "brushhead_lifetime_usage": 1942,
    "brushhead_nfc_version": 258,
    "brushhead_payload": "https://www.philips.com/nfcbrushheadtap",
    "brushhead_ring_id": 2,
    "brushhead_serial": "04:50:6D:D2:AE:22:91",
    "brushhead_type": "adaptive_clean",
    "brushing_mode": "clean",
    "brushing_mode_value": 0,
    "brushing_state": "on",
    "brushing_state_value": 1,
    "brushing_time": 18,
    "error_persistent": 1073743944,
    "error_volatile": 1073741824,
    "firmware": "1.4.3",
    "handle_state": "run",
    "handle_state_value": 2,
    "handle_time": 1161581,
    "intensity": "low",
    "intensity_value": 0,
    "latest_session_id": 3773,
    "manufacturer_name": "Philips OHC",
    "model_number": "HX960X",
    "motor_runtime": 404400,
    "routine_length": 120,
    "serial_number": "0000000000",
    "session_count": 446,
    "session_id": 3774,
    "settings_bitmask": 0,
}

GOLDEN = {
    "classic_hx6340_kids.json": EXPECTED_HX6340,
    "classic_hx992x.json": EXPECTED_HX992X,
    "classic_hx999x_prestige.json": EXPECTED_HX999X,
    "classic_hx960x_expertclean.json": EXPECTED_HX960X,
}


def _protocol_for(snapshot: dict) -> ClassicProtocol:
    """Build a transport-less ClassicProtocol whose model matches the capture.

    ``parse_results`` is a pure transform that never touches the transport, so
    ``None`` is enough; only ``model`` matters (it selects the mode-decode
    table).
    """
    proto = ClassicProtocol(transport=None)
    proto.model = snapshot["device_info"]["Model Number"].strip()
    return proto


def _decode(fixture_file: str) -> dict:
    snapshot = load_json_fixture(fixture_file)
    return _protocol_for(snapshot).parse_results(chars_as_bytes(snapshot))


@pytest.mark.parametrize("fixture_file, expected", GOLDEN.items())
def test_parse_results_matches_golden(fixture_file, expected):
    """Every captured brush decodes to the expected coordinator-data keys."""
    assert _decode(fixture_file) == expected


def test_mode_decoded_via_4080_per_model():
    """The Kids and DiamondClean report mode on 0x4080 (not routine-id mode).
    Same raw scheme, different per-model labels: HX6340 index 0 → clean,
    HX992X index 1 → white_plus.
    """
    assert not uses_routine_id_mode("HX6340")
    assert not uses_routine_id_mode("HX992X")
    assert not uses_routine_id_mode("HX960X")
    assert _decode("classic_hx6340_kids.json")["brushing_mode"] == "clean"
    assert _decode("classic_hx992x.json")["brushing_mode"] == "white_plus"
    assert _decode("classic_hx960x_expertclean.json")["brushing_mode"] == "clean"


def test_mode_decoded_via_4022_routine_id():
    """The HX999X Prestige is a routine-id-mode model: its selected mode comes
    from 0x4022 (Available Routine IDs), decoded with the routine-id table —
    the branch the other two fixtures never exercise.
    """
    assert uses_routine_id_mode("HX999X")
    out = _decode("classic_hx999x_prestige.json")
    assert out["brushing_mode"] == "clean"
    assert out["brushing_mode_value"] == 0


def test_uint16_le_decoding():
    """Session id 0x1f01 little-endian == 287, routine length 0x7800 == 120."""
    out = _decode("classic_hx6340_kids.json")
    assert out["session_id"] == 287
    assert out["routine_length"] == 120
    assert out["latest_session_id"] == 286


def test_premium_brushhead_nfc_and_settings():
    """The HX992X exposes brush-head NFC identity and a settings bitmask that
    the minimal Kids brush does not.
    """
    out = _decode("classic_hx992x.json")
    assert out["brushhead_type"] == "sensitive"
    assert out["brushhead_serial"] == "04:09:1C:72:B8:1B:91"
    assert out["brushhead_payload"] == "https://www.philips.com/nfcbrushheadtap"
    assert out["settings_bitmask"] == 1
    assert out["motor_runtime"] == 44444


def test_minimal_brush_has_no_brushhead_fields():
    """The Kids brush has no brush-head service, so no NFC keys appear."""
    out = _decode("classic_hx6340_kids.json")
    assert "brushhead_type" not in out
    assert "brushhead_serial" not in out
    assert "settings_bitmask" not in out


def test_empty_and_missing_chars_are_skipped():
    """Empty payloads (hardware/software revision) and absent characteristics
    produce no keys rather than blank or crashing entries.
    """
    out = _decode("classic_hx6340_kids.json")
    assert "hardware_revision" not in out
    assert "software_revision" not in out
    assert "brushhead_lifetime_usage" not in out


def test_partial_results_decode_independently():
    """A subset of characteristics decodes without needing the rest — mirrors
    a notification carrying a single changed value.
    """
    snapshot = load_json_fixture("classic_hx6340_kids.json")
    proto = _protocol_for(snapshot)
    battery_uuid = "00002a19-0000-1000-8000-00805f9b34fb"
    all_chars = chars_as_bytes(snapshot)
    assert proto.parse_results({battery_uuid: all_chars[battery_uuid]}) == {
        "battery": 24
    }
