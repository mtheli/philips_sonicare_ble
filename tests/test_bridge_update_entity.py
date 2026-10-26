"""Smoke tests for the Bridge Firmware update entity.

An unreachable bridge must not show "update available" from a stale
version. Only a version the bridge reported in this session counts as
installed; until then the entity stays unknown.
"""

from types import SimpleNamespace

from custom_components.philips_sonicare_ble.update import SonicareBridgeUpdate


def _entity(*, alive: bool, live: str | None, latest: str | None) -> SonicareBridgeUpdate:
    entity = SonicareBridgeUpdate.__new__(SonicareBridgeUpdate)
    entity.coordinator = SimpleNamespace(
        transport=SimpleNamespace(is_bridge_alive=alive, bridge_version=live)
    )
    entity._latest_version = latest
    return entity


def test_offline_bridge_is_unavailable() -> None:
    assert _entity(alive=False, live=None, latest="1.14.2").available is False


def test_unknown_until_the_bridge_reports_its_version() -> None:
    entity = _entity(alive=True, live=None, latest="1.14.2")
    assert entity.installed_version is None
    assert entity.latest_version == "1.14.2"
    # HA reports no update while installed_version is None.


def test_update_for_older_version() -> None:
    entity = _entity(alive=True, live="1.12.0", latest="1.14.2")
    assert entity.available is True
    assert entity.latest_version == "1.14.2"


def test_no_update_for_same_or_newer_version() -> None:
    assert _entity(alive=True, live="1.14.2", latest="1.14.2").latest_version == "1.14.2"
    assert _entity(alive=True, live="1.15.0", latest="1.14.2").latest_version == "1.15.0"
