"""Tests for the connection-path preview on bluetooth_confirm.

habluetooth routes connects through the strongest scanner, so a "Direct
Bluetooth" discovery may in fact ride a stock ESPHome bluetooth_proxy.
``_transport_preview`` names the likely carrier up front and warns when
it is a standard proxy (pairing over one is model-dependent).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)

ADDRESS = "24:E5:AA:BE:9C:1B"


def _flow() -> PhilipsSonicareConfigFlow:
    flow = PhilipsSonicareConfigFlow()
    flow.flow_id = "test-flow"
    flow.handler = "philips_sonicare_ble"
    flow._address = ADDRESS
    flow._name = "Philips Sonicare"
    flow.hass = SimpleNamespace()
    return flow


def _patch_paths(monkeypatch, paths) -> None:
    monkeypatch.setattr(
        "custom_components.philips_sonicare_ble.config_flow."
        "describe_available_paths",
        MagicMock(return_value=paths),
    )


def test_no_paths_yields_empty_lines(monkeypatch) -> None:
    _patch_paths(monkeypatch, [])
    assert _flow()._transport_lines() == ("", "")


def test_local_adapter_via_direct_bluetooth(monkeypatch) -> None:
    _patch_paths(monkeypatch, [
        {"name": "hci0 (00:0A:CD:46:B2:2D)", "rssi": -76, "is_local": True},
    ])
    via, warning = _flow()._transport_lines()
    # Same "via <class> (<detail>)" framing as the capabilities dialog.
    assert via == " via **Direct Bluetooth** (hci0, -76 dBm)"
    assert warning == ""


def test_proxy_via_and_warning(monkeypatch) -> None:
    _patch_paths(monkeypatch, [
        # Remote scanner names may carry a MAC suffix (e.g. the S3R) —
        # the label must show the bare name, no doubled parens.
        {"name": "atom-s3r (98:88:E0:0E:DA:D2)", "rssi": -61, "is_local": False},
    ])
    via, warning = _flow()._transport_lines()
    assert via == " via **Bluetooth proxy** (atom-s3r, -61 dBm)"
    assert "98:88" not in via
    assert 'ha-alert alert-type="warning"' in warning
    assert "<b>atom-s3r</b> (-61 dBm)" in warning
    # Paragraph breaks via <br> — markdown isn't parsed inside the alert.
    assert warning.count("<br><br>") == 2
    assert "can be unreliable" in warning
    assert "ESP32 bridge" in warning


def test_proxy_preferred_with_local_fallback_hint(monkeypatch) -> None:
    _patch_paths(monkeypatch, [
        {"name": "atom-lite", "rssi": -64, "is_local": False},
        {"name": "hci0 (00:0A:CD:46:B2:2D)", "rssi": -82, "is_local": True},
    ])
    via, warning = _flow()._transport_lines()
    assert via == " via **Bluetooth proxy** (atom-lite, -64 dBm)"
    assert 'ha-alert alert-type="warning"' in warning
    assert "<b>hci0</b>" in warning
    assert "strongest signal" in warning


def test_local_strongest_wins_over_weaker_proxy(monkeypatch) -> None:
    # Sorting happens in describe_available_paths; the lines trust entry
    # order — strongest first.
    _patch_paths(monkeypatch, [
        {"name": "hci0 (00:0A:CD:46:B2:2D)", "rssi": -60, "is_local": True},
        {"name": "atom-lite", "rssi": -85, "is_local": False},
    ])
    via, warning = _flow()._transport_lines()
    assert via == " via **Direct Bluetooth** (hci0, -60 dBm)"
    assert warning == ""


async def test_picker_labels_name_the_carrying_scanner(monkeypatch) -> None:
    import time as time_mod

    flow = _flow()
    flow._address = None

    info = SimpleNamespace(
        name="Philips Sonicare",
        address=ADDRESS,
        time=time_mod.monotonic() - 3,
        rssi=-64,
    )
    monkeypatch.setattr(
        "custom_components.philips_sonicare_ble.config_flow."
        "async_discovered_service_info",
        lambda hass: [info],
    )
    _patch_paths(monkeypatch, [
        {"name": "atom-lite", "rssi": -64, "is_local": False},
    ])

    result = await flow.async_step_user_bleak()

    options = result["data_schema"].schema["address"].config["options"]
    assert "via atom-lite (proxy)" in options[0]["label"]


async def test_picker_labels_strip_local_adapter_mac(monkeypatch) -> None:
    import time as time_mod

    flow = _flow()
    flow._address = None

    info = SimpleNamespace(
        name="Philips Sonicare",
        address=ADDRESS,
        time=time_mod.monotonic() - 3,
        rssi=-76,
    )
    monkeypatch.setattr(
        "custom_components.philips_sonicare_ble.config_flow."
        "async_discovered_service_info",
        lambda hass: [info],
    )
    _patch_paths(monkeypatch, [
        {"name": "hci0 (00:0A:CD:46:B2:2D)", "rssi": -76, "is_local": True},
    ])

    result = await flow.async_step_user_bleak()

    options = result["data_schema"].schema["address"].config["options"]
    assert "via hci0" in options[0]["label"]
    assert "(proxy)" not in options[0]["label"]


def test_capabilities_label_distinguishes_proxy() -> None:
    text = PhilipsSonicareConfigFlow._get_connection_status_text

    assert "Direct Bluetooth" in text("bleak", "hci0 (00:0A:CD:46:B2:2D)")
    assert "Bluetooth proxy" in text("bleak", "atom-lite", via_proxy=True)
    assert "ESP32 Bridge" in text("esp_bridge", "Atom Lite / sonicare_1")
    # ESP labelling is untouched by the proxy flag.
    assert "ESP32 Bridge" in text("esp_bridge", "x", via_proxy=True)


def test_describe_available_paths_sorts_and_classifies(monkeypatch) -> None:
    from custom_components.philips_sonicare_ble import transport

    class _FakeHaScanner(transport.HaScanner):
        # Bypass HaScanner.__init__ — only isinstance matters here.
        def __init__(self, name):
            self.name = name

    local = _FakeHaScanner.__new__(_FakeHaScanner)
    local.name = "hci0 (00:0A:CD:46:B2:2D)"
    remote = SimpleNamespace(name="atom-lite", source="F4:65:0B:01:B9:6E")

    devices = [
        SimpleNamespace(scanner=local, advertisement=SimpleNamespace(rssi=-82)),
        SimpleNamespace(scanner=remote, advertisement=SimpleNamespace(rssi=-64)),
    ]
    monkeypatch.setattr(
        transport, "async_scanner_devices_by_address",
        MagicMock(return_value=devices),
    )

    paths = transport.describe_available_paths(SimpleNamespace(), ADDRESS)

    assert [p["name"] for p in paths] == ["atom-lite", "hci0 (00:0A:CD:46:B2:2D)"]
    assert paths[0]["is_local"] is False
    assert paths[1]["is_local"] is True
