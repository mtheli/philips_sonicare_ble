"""Tests for the stale-host-bond recovery in ``_fetch_with_pair_retry``.

Issue #25 (second report): the brush dropped its half of the bond, so
every probe read fails with an auth error (0x05) although BlueZ still
holds a bond. The old guard refused to touch any existing bond, leaving
the user stuck until a manual ``bluetoothctl remove``. An auth error
*despite* a bond proves the bond is stale — that one case may fall
through to auto-pair (which removes the stale bond and pairs fresh).
Every other failure mode must keep protecting the bond.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)
from custom_components.philips_sonicare_ble.exceptions import (
    NotPairedException,
)
from custom_components.philips_sonicare_ble.transport import (
    is_local_bluez_connection,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"

BONDED_RESULT = {"services": [], "model": "HX9911/09", "pairing": "bonded"}


def _flow() -> PhilipsSonicareConfigFlow:
    return PhilipsSonicareConfigFlow()


def _patch_paired(paired: bool | None):
    return patch(
        "custom_components.philips_sonicare_ble.dbus_pairing."
        "async_is_device_paired",
        AsyncMock(return_value=paired),
    )


def test_auth_error_flag_defaults_to_false() -> None:
    """Plumbing: a bare NotPairedException must not look like a stale
    bond — only the probe-read auth path sets the flag explicitly."""
    assert NotPairedException().auth_error is False
    assert NotPairedException("msg").auth_error is False
    assert NotPairedException(auth_error=True).auth_error is True


async def test_stale_bond_falls_through_to_auto_pair() -> None:
    """Auth error + existing bond → bond is stale → auto-pair runs."""
    flow = _flow()
    flow._async_fetch_capabilities = AsyncMock(
        side_effect=[
            NotPairedException(auth_error=True),
            dict(BONDED_RESULT),
        ]
    )
    flow._try_auto_pair = AsyncMock(return_value=True)

    with _patch_paired(True):
        result = await flow._fetch_with_pair_retry(ADDRESS)

    flow._try_auto_pair.assert_awaited_once_with(ADDRESS)
    assert result["pairing"] == "bonded"


async def test_non_auth_failure_still_protects_the_bond() -> None:
    """Any probe failure without an explicit auth error must keep
    refusing to wipe an existing bond (Condor RPA safety)."""
    flow = _flow()
    flow._async_fetch_capabilities = AsyncMock(
        side_effect=NotPairedException()
    )
    flow._try_auto_pair = AsyncMock(return_value=True)

    with _patch_paired(True), pytest.raises(NotPairedException):
        await flow._fetch_with_pair_retry(ADDRESS)

    flow._try_auto_pair.assert_not_awaited()


async def test_no_bond_auto_pairs_as_before() -> None:
    """Without a bond the auto-pair path is unchanged."""
    flow = _flow()
    flow._async_fetch_capabilities = AsyncMock(
        side_effect=[
            NotPairedException(auth_error=True),
            dict(BONDED_RESULT),
        ]
    )
    flow._try_auto_pair = AsyncMock(return_value=True)

    with _patch_paired(False):
        result = await flow._fetch_with_pair_retry(ADDRESS)

    flow._try_auto_pair.assert_awaited_once_with(ADDRESS)
    assert result["pairing"] == "bonded"


async def test_indeterminate_bond_check_never_wipes() -> None:
    """``async_is_device_paired`` answers None when the device is not in
    the BlueZ tree (proxy-only reachability, Condor RPA) or the D-Bus
    check fails. That indeterminate answer must behave like "no bond":
    take the plain auto-pair path, never the stale-bond wipe branch."""
    flow = _flow()
    flow._async_fetch_capabilities = AsyncMock(
        side_effect=[
            NotPairedException(auth_error=True),
            dict(BONDED_RESULT),
        ]
    )
    flow._try_auto_pair = AsyncMock(return_value=True)

    with _patch_paired(None):
        result = await flow._fetch_with_pair_retry(ADDRESS)

    flow._try_auto_pair.assert_awaited_once_with(ADDRESS)
    assert result["pairing"] == "bonded"


class _FakeClient:
    def __init__(self, backend: object | None) -> None:
        self._backend = backend


def _backend_with_module(module: str) -> object:
    backend_cls = type("FakeBackend", (), {})
    backend_cls.__module__ = module
    return backend_cls()


def test_local_bluez_connection_detection() -> None:
    """Bond state is per-controller: only a BlueZ-backed connection may
    mark its auth error as stale-bond evidence. A proxy-routed probe
    (habluetooth picks scanners by RSSI, even for "Direct BLE") must
    never get the BlueZ bond wiped."""
    bluez = _backend_with_module("bleak.backends.bluezdbus.client")
    esphome = _backend_with_module("bleak_esphome.backend.client")

    assert is_local_bluez_connection(_FakeClient(bluez))
    assert not is_local_bluez_connection(_FakeClient(esphome))
    assert not is_local_bluez_connection(_FakeClient(None))
    assert not is_local_bluez_connection(object())


async def test_stale_bond_with_failed_auto_pair_raises() -> None:
    """Stale bond detected but the fresh pair fails → NotPairedException
    surfaces to the flow (not_paired step) instead of a silent success."""
    flow = _flow()
    flow._async_fetch_capabilities = AsyncMock(
        side_effect=NotPairedException(auth_error=True)
    )
    flow._try_auto_pair = AsyncMock(return_value=False)

    with _patch_paired(True), pytest.raises(NotPairedException):
        await flow._fetch_with_pair_retry(ADDRESS)

    flow._try_auto_pair.assert_awaited_once_with(ADDRESS)
