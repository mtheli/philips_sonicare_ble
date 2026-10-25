"""Smoke tests for the repair issue on bridges built with a bond-losing ESP-IDF.

ESP-IDF 5.5.6 (ESPHome 2026.10.0) forgets a bond after the first reconnect
(espressif/esp-idf#19286). The bridge reports its IDF version in info
events; the coordinator raises one issue per ESP device while it is
affected and clears it once the bridge reports a different version.
"""

from types import SimpleNamespace

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.philips_sonicare_ble.coordinator import (
    PhilipsSonicareCoordinator,
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


def _coordinator(idf_version):
    transport = EspBridgeTransport.__new__(EspBridgeTransport)
    transport._idf_version = idf_version
    coord = PhilipsSonicareCoordinator.__new__(PhilipsSonicareCoordinator)
    coord.hass = None
    coord.transport = transport
    coord.address = "AA:BB:CC:DD:EE:FF"
    coord.entry = SimpleNamespace(data={"esp_device_name": "Atom-S3R"})
    return coord


@pytest.mark.parametrize("idf", ["v5.5.6", "v5.5.6-dirty", "5.5.6"])
def test_affected_idf_raises_issue_per_esp_device(calls, idf) -> None:
    _coordinator(idf)._check_bridge_idf()
    assert calls["delete"] == []
    [(issue_id, kw)] = calls["create"]
    assert issue_id == "esp_bridge_idf_bond_loss_atom-s3r"
    assert kw["translation_key"] == "esp_bridge_idf_bond_loss"
    assert kw["translation_placeholders"] == {
        "esp_device": "Atom-S3R",
        "idf_version": idf,
    }


@pytest.mark.parametrize("idf", ["v5.5.5", "v5.5.7", "v6.0.1", "v5.5.60"])
def test_other_idf_clears_issue(calls, idf) -> None:
    _coordinator(idf)._check_bridge_idf()
    assert calls["create"] == []
    assert calls["delete"] == ["esp_bridge_idf_bond_loss_atom-s3r"]


def test_unknown_idf_leaves_issue_alone(calls) -> None:
    # Firmware before 1.10.0 does not report the IDF version at all.
    _coordinator(None)._check_bridge_idf()
    assert calls == {"create": [], "delete": []}
