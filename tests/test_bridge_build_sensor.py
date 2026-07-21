"""Smoke tests for the ESP Build diagnostic sensor.

The sensor composes its state from the build-environment fields the bridge
reports via info events (firmware >= 1.10.0). Older firmware reports
neither field — the sensor must stay unknown instead of guessing.
"""

from types import SimpleNamespace

from custom_components.philips_sonicare_ble.sensor import SonicareBridgeBuildSensor


def _sensor_with_transport(**fields) -> SonicareBridgeBuildSensor:
    sensor = SonicareBridgeBuildSensor.__new__(SonicareBridgeBuildSensor)
    sensor.coordinator = SimpleNamespace(transport=SimpleNamespace(**fields))
    return sensor


def test_both_fields_compose_state_and_attributes() -> None:
    sensor = _sensor_with_transport(
        esphome_version="2026.7.1", idf_version="v5.5.5"
    )
    assert sensor.native_value == "ESPHome 2026.7.1 / IDF v5.5.5"
    assert sensor.extra_state_attributes == {
        "esphome_version": "2026.7.1",
        "idf_version": "v5.5.5",
    }


def test_partial_fields_render_without_placeholder() -> None:
    sensor = _sensor_with_transport(esphome_version="2026.7.1", idf_version=None)
    assert sensor.native_value == "ESPHome 2026.7.1"
    assert sensor.extra_state_attributes == {"esphome_version": "2026.7.1"}

    sensor = _sensor_with_transport(esphome_version=None, idf_version="v5.5.5")
    assert sensor.native_value == "IDF v5.5.5"
    assert sensor.extra_state_attributes == {"idf_version": "v5.5.5"}


def test_old_firmware_reports_nothing() -> None:
    # Pre-1.10.0 bridges never send the fields; the transport attributes
    # stay None and the sensor must be unknown with no attributes.
    sensor = _sensor_with_transport(esphome_version=None, idf_version=None)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


def test_transport_without_attributes_at_all() -> None:
    # Non-ESP transports don't even have the attributes — getattr fallback.
    sensor = _sensor_with_transport()
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None
