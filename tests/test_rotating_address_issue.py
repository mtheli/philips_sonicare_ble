"""Repair issue and build sensor for bridges that cannot follow a rotating address.

Bridge firmware 1.14.2+ reports its chip and where it resolves rotating
peer addresses (``addr_res``). With ``ctrl`` a brush bonded under a
resolvable private address never reconnects, so the coordinator raises a
repair issue for that entry; ``host`` (classic ESP32 or the build option)
and ``ctrl_priv`` clear it. Changes to these fields re-run the checks.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.philips_sonicare_ble.coordinator import (
    PhilipsSonicareCoordinator,
)
from custom_components.philips_sonicare_ble.sensor import (
    SonicareBridgeBuildSensor,
    SonicareBridgeChipSensor,
)
from custom_components.philips_sonicare_ble.transport import EspBridgeTransport


@pytest.fixture
def calls(monkeypatch):
    seen = {"create": [], "delete": []}
    monkeypatch.setattr(
        ir, "async_create_issue",
        lambda hass, domain, issue_id, **kw: seen["create"].append((issue_id, kw)),
    )
    monkeypatch.setattr(
        ir, "async_delete_issue",
        lambda hass, domain, issue_id: seen["delete"].append(issue_id),
    )
    return seen


def _coordinator(address, addr_res, idf="v5.5.5", bond=None):
    transport = EspBridgeTransport.__new__(EspBridgeTransport)
    transport._idf_version = idf
    transport._addr_res = addr_res
    transport._detected_mac = address
    transport._bond_address = bond
    coord = PhilipsSonicareCoordinator.__new__(PhilipsSonicareCoordinator)
    coord.hass = None
    coord.transport = transport
    coord.address = address
    coord.entry = SimpleNamespace(
        data={"esp_device_name": "atom_s3r", "device_name": "Sonicare 7100"}
    )
    return coord


def test_controller_resolution_with_rotating_bond_raises_issue(calls) -> None:
    _coordinator("4E:FB:C3:DD:DF:11", "ctrl")._check_bridge_address_resolution()
    [(issue_id, kw)] = calls["create"]
    assert issue_id == "esp_bridge_rotating_address_4e:fb:c3:dd:df:11"
    assert kw["translation_key"] == "esp_bridge_rotating_address"
    assert kw["translation_placeholders"] == {
        "device": "Sonicare 7100",
        "esp_device": "atom_s3r",
    }


@pytest.mark.parametrize(
    ("address", "addr_res"),
    [
        ("4E:FB:C3:DD:DF:11", "host"),
        ("4E:FB:C3:DD:DF:11", "ctrl_priv"),
        ("24:E5:AA:92:32:F9", "ctrl"),  # public / identity address
        ("C4:12:34:56:78:9A", "ctrl"),  # static random address
    ],
)
def test_other_constellations_clear_issue(calls, address, addr_res) -> None:
    _coordinator(address, addr_res)._check_bridge_address_resolution()
    assert calls["create"] == []
    assert calls["delete"] == [f"esp_bridge_rotating_address_{address.lower()}"]


def test_old_firmware_leaves_issue_alone(calls) -> None:
    _coordinator("4E:FB:C3:DD:DF:11", None)._check_bridge_address_resolution()
    assert calls == {"create": [], "delete": []}


def test_build_change_reruns_checks_and_refreshes_entities(calls) -> None:
    coord = _coordinator("4E:FB:C3:DD:DF:11", "ctrl", idf="v5.5.6")
    coord.async_update_listeners = MagicMock()
    coord._on_bridge_build_changed()
    created = {issue_id for issue_id, _ in calls["create"]}
    assert "esp_bridge_idf_bond_loss_atom_s3r" in created
    assert "esp_bridge_rotating_address_4e:fb:c3:dd:df:11" in created
    coord.async_update_listeners.assert_called_once()


def test_transport_notifies_only_on_change() -> None:
    transport = EspBridgeTransport.__new__(EspBridgeTransport)
    for attr in ("_esphome_version", "_idf_version", "_chip", "_addr_res"):
        setattr(transport, attr, None)
    cb = MagicMock()
    transport.set_build_info_callback(cb)
    # Mirror the event handler's update loop on a plain dict.
    def feed(data):
        changed = False
        for key, attr in (("esphome_version", "_esphome_version"),
                          ("idf_version", "_idf_version"),
                          ("chip", "_chip"), ("addr_res", "_addr_res")):
            value = data.get(key)
            if value and value != getattr(transport, attr):
                setattr(transport, attr, value)
                changed = True
        if changed:
            transport._build_info_cb()
    feed({"chip": "esp32s3", "addr_res": "host"})
    feed({"chip": "esp32s3", "addr_res": "host"})
    assert cb.call_count == 1
    assert transport.chip == "esp32s3"
    assert transport.addr_res == "host"


def test_build_sensor_leaves_chip_to_chip_sensor() -> None:
    sensor = SonicareBridgeBuildSensor.__new__(SonicareBridgeBuildSensor)
    sensor.coordinator = SimpleNamespace(transport=SimpleNamespace(
        esphome_version="2026.10.0", idf_version="v5.5.5",
        chip="esp32s3", addr_res="host",
    ))
    assert sensor.native_value == "ESPHome 2026.10.0 / IDF v5.5.5"
    assert sensor.extra_state_attributes == {
        "esphome_version": "2026.10.0",
        "idf_version": "v5.5.5",
    }


def _chip_sensor(**fields) -> SonicareBridgeChipSensor:
    sensor = SonicareBridgeChipSensor.__new__(SonicareBridgeChipSensor)
    sensor.coordinator = SimpleNamespace(transport=SimpleNamespace(**fields))
    return sensor


def test_chip_sensor_shows_chip_and_resolution() -> None:
    sensor = _chip_sensor(chip="esp32s3", addr_res="ctrl")
    assert sensor.native_value == "ESP32-S3"
    assert sensor.extra_state_attributes == {"address_resolution": "controller"}
    sensor = _chip_sensor(chip="esp32", addr_res="host")
    assert sensor.native_value == "ESP32"
    assert sensor.extra_state_attributes == {"address_resolution": "host"}


def test_chip_sensor_unknown_on_old_firmware() -> None:
    sensor = _chip_sensor(chip=None, addr_res=None)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None
    sensor = _chip_sensor()
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


def test_identity_entry_judges_by_bond_address(calls) -> None:
    """An entry keyed by the identity looks static; its bond address does not."""
    coord = _coordinator(
        "24:E5:AA:92:32:F9", "ctrl", bond="76:99:0A:0D:D3:32"
    )
    coord._check_bridge_address_resolution()
    assert [issue_id for issue_id, _ in calls["create"]] == [
        "esp_bridge_rotating_address_24:e5:aa:92:32:f9"
    ]
