"""Labels that tell discovered brushes apart.

Every adult brush advertises as "Philips Sonicare"; with two of them the
discovery card and the picker differed only by MAC. The label now names
the protocol family from the advertisement and, when the brush was set
up here before, the user's own name for it.
"""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.helpers import device_registry as dr

from custom_components.philips_sonicare_ble import config_flow as cf
from custom_components.philips_sonicare_ble.const import SVC_CONDOR, SVC_SONICARE

ADDRESS = "24:E5:AA:BE:9C:1B"


def _info(name="Philips Sonicare", uuids=(), address=ADDRESS):
    return SimpleNamespace(name=name, service_uuids=list(uuids), address=address)


def test_family_from_advertisement() -> None:
    assert cf._brush_family(_info(uuids=[SVC_SONICARE])) == "Sonicare (Classic)"
    assert cf._brush_family(_info(uuids=[SVC_CONDOR.upper()])) == "Sonicare (Condor)"
    # Kids name themselves, the shared Classic UUID does not matter.
    assert (
        cf._brush_family(_info(name="Sonicare4Kids", uuids=[SVC_SONICARE]))
        == "Sonicare for Kids"
    )
    # Passive scan: no scan response, no UUID — the advertised name stays.
    assert cf._brush_family(_info()) == "Philips Sonicare"
    assert cf._brush_family(_info(name=None)) == "Philips Sonicare"


def _patch_deleted(monkeypatch, devices) -> None:
    registry = SimpleNamespace(deleted_devices={str(i): d for i, d in enumerate(devices)})
    monkeypatch.setattr(cf.dr, "async_get", lambda hass: registry)


def test_label_uses_name_of_previously_removed_device(monkeypatch) -> None:
    _patch_deleted(monkeypatch, [
        SimpleNamespace(
            name_by_user="Bad oben",
            connections={(dr.CONNECTION_BLUETOOTH, ADDRESS.lower())},
        ),
        SimpleNamespace(
            name_by_user="Other brush",
            connections={(dr.CONNECTION_BLUETOOTH, "24:E5:AA:00:00:01")},
        ),
    ])
    label = cf._brush_label(SimpleNamespace(), _info(uuids=[SVC_SONICARE]))
    assert label == f"Bad oben · Sonicare (Classic) · {ADDRESS}"


def test_label_without_previous_name(monkeypatch) -> None:
    # Removed without a user-given name: nothing to show.
    _patch_deleted(monkeypatch, [
        SimpleNamespace(
            name_by_user=None,
            connections={(dr.CONNECTION_BLUETOOTH, ADDRESS)},
        ),
    ])
    label = cf._brush_label(SimpleNamespace(), _info(uuids=[SVC_CONDOR]))
    assert label == f"Sonicare (Condor) · {ADDRESS}"


def test_registry_trouble_never_breaks_the_label(monkeypatch) -> None:
    def _boom(hass):
        raise RuntimeError("no registry")

    monkeypatch.setattr(cf.dr, "async_get", _boom)
    assert cf._brush_label(SimpleNamespace(), _info()) == (
        f"Philips Sonicare · {ADDRESS}"
    )


def test_via_label() -> None:
    assert cf._via_label([]) == ""
    assert cf._via_label([
        {"name": "hci1 (58:D8:12:3E:CA:B2)", "is_local": True},
    ]) == "via hci1"
    assert cf._via_label([
        {"name": "atom-s3r (98:88:E0:0E:DA:D2)", "is_local": False},
    ]) == "via atom-s3r (proxy)"


async def test_discovery_title_names_family_and_path(monkeypatch) -> None:
    _patch_deleted(monkeypatch, [])
    monkeypatch.setattr(
        cf, "describe_available_paths",
        lambda hass, address: [
            {"name": "hci0 (00:0A:CD:46:B2:2D)", "rssi": -72, "is_local": True},
        ],
    )
    flow = cf.PhilipsSonicareConfigFlow()
    flow.hass = SimpleNamespace()
    flow.context = {}

    async def _no_op(*args, **kwargs):
        return None

    async def _confirm(*args, **kwargs):
        return {"type": "form"}

    monkeypatch.setattr(flow, "async_set_unique_id", _no_op)
    monkeypatch.setattr(flow, "_abort_if_already_configured", lambda: None)
    monkeypatch.setattr(flow, "async_step_bluetooth_confirm", _confirm)

    info = _info(uuids=[SVC_SONICARE])
    info.rssi = -72
    await flow.async_step_bluetooth(info)

    assert flow.context["title_placeholders"]["name"] == (
        f"Sonicare (Classic) · {ADDRESS} · via hci0"
    )


def test_condor_services_table_lists_only_present_services() -> None:
    # A Condor handle never has the Classic feature services; listing
    # them as ❌ suggested something was missing.
    from custom_components.philips_sonicare_ble.const import (
        SVC_CONDOR, SVC_DEVICE_INFO, SVC_SONICARE,
    )
    flow_cls = cf.PhilipsSonicareConfigFlow

    table, condor_note, has_missing = flow_cls._get_service_status_text(
        [SVC_DEVICE_INFO, SVC_CONDOR], "HX742X"
    )

    assert "❌" not in table
    assert "Device Information" in table and "Condor" in table
    assert condor_note is True
    assert has_missing is False

    table, condor_note, has_missing = flow_cls._get_service_status_text(
        [SVC_DEVICE_INFO, SVC_SONICARE], "HX9999"
    )
    assert condor_note is False
    assert has_missing is ("❌" in table)
