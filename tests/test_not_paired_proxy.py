"""Tests for the transport-aware not_paired step (proxy variant).

Issue #9 (bcutter) rest: when the failing probe rode a Bluetooth proxy,
the host pairing instructions (pair.sh / bluetoothctl) are ineffective —
the proxy bonds on the ESP itself during auth reads. The flow must show
the proxy-specific guidance instead, and ``_fetch_with_pair_retry`` must
not run the host-side D-Bus auto-pair machinery for such a connection.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)
from custom_components.philips_sonicare_ble.exceptions import (
    NotPairedException,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"

COMPONENT_DIR = (
    Path(__file__).parent.parent
    / "custom_components"
    / "philips_sonicare_ble"
)


def _flow() -> PhilipsSonicareConfigFlow:
    flow = PhilipsSonicareConfigFlow()
    flow._address = ADDRESS
    flow.hass = SimpleNamespace(config=SimpleNamespace(components=set()))
    # async_show_form reads these off the instance
    flow.flow_id = "test-flow"
    flow.handler = "philips_sonicare_ble"
    return flow


def _patch_paired(paired: bool | None):
    return patch(
        "custom_components.philips_sonicare_ble.dbus_pairing."
        "async_is_device_paired",
        AsyncMock(return_value=paired),
    )


async def test_proxy_probe_skips_dbus_auto_pair() -> None:
    """A proxy-carried NotPaired failure must never reach the D-Bus
    bond checks or auto-pair — they act on the unrelated BlueZ bond."""
    flow = _flow()
    flow._probe_via_proxy = True
    flow._async_fetch_capabilities = AsyncMock(
        side_effect=NotPairedException()
    )
    flow._try_auto_pair = AsyncMock(return_value=True)

    bond_check = AsyncMock(return_value=True)
    with patch(
        "custom_components.philips_sonicare_ble.dbus_pairing."
        "async_is_device_paired",
        bond_check,
    ), pytest.raises(NotPairedException):
        await flow._fetch_with_pair_retry(ADDRESS)

    flow._try_auto_pair.assert_not_awaited()
    bond_check.assert_not_awaited()


async def test_local_probe_keeps_auto_pair_path() -> None:
    """An explicitly local probe keeps the stale-bond recovery."""
    flow = _flow()
    flow._probe_via_proxy = False
    flow._async_fetch_capabilities = AsyncMock(
        side_effect=[
            NotPairedException(auth_error=True),
            {"services": [], "model": "HX9911/09"},
        ]
    )
    flow._try_auto_pair = AsyncMock(return_value=True)

    with _patch_paired(True):
        result = await flow._fetch_with_pair_retry(ADDRESS)

    flow._try_auto_pair.assert_awaited_once_with(ADDRESS)
    assert result["pairing"] == "bonded"


async def test_not_paired_renders_proxy_variant() -> None:
    """With a proxy-carried probe the step shows the proxy dialog,
    including the proxy's name."""
    flow = _flow()
    flow._probe_via_proxy = True
    flow._probe_proxy_name = "athom-proxy-1"

    result = await flow.async_step_not_paired()

    assert result["step_id"] == "not_paired_proxy"
    placeholders = result["description_placeholders"]
    assert placeholders["proxy_name"] == "athom-proxy-1"
    assert placeholders["address"] == ADDRESS


async def test_not_paired_defaults_to_host_variant() -> None:
    """Without proxy evidence (flag unset) the host instructions with
    pair.sh/bluetoothctl stay in place."""
    flow = _flow()

    result = await flow.async_step_not_paired()

    assert result["step_id"] == "not_paired"
    assert ADDRESS in result["description_placeholders"]["pair_cmd"]


async def test_proxy_retry_failure_stays_in_proxy_dialog() -> None:
    """Retry on the proxy dialog re-probes (which re-triggers the
    ESP-side SMP); a repeated failure surfaces as pairing_failed."""
    flow = _flow()
    flow._probe_via_proxy = True
    flow._probe_proxy_name = "athom-proxy-1"
    flow._async_fetch_capabilities = AsyncMock(
        side_effect=NotPairedException()
    )

    result = await flow.async_step_not_paired_proxy({})

    assert result["step_id"] == "not_paired_proxy"
    assert result["errors"] == {"base": "pairing_failed"}


async def test_retry_follows_transport_flip_to_host() -> None:
    """habluetooth routes connects by RSSI: if the retry probe rode the
    local adapter instead, the dialog must flip to the host variant."""
    flow = _flow()
    flow._probe_via_proxy = True
    flow._probe_proxy_name = "athom-proxy-1"

    async def probe(_address):
        flow._probe_via_proxy = False
        flow._probe_proxy_name = None
        raise NotPairedException()

    flow._async_fetch_capabilities = probe

    result = await flow.async_step_not_paired_proxy({})

    assert result["step_id"] == "not_paired"
    assert result["errors"] == {"base": "pairing_failed"}


async def test_proxy_name_falls_back_to_unknown() -> None:
    """A missing connection path must not break the template."""
    flow = _flow()
    flow._probe_via_proxy = True
    flow._probe_proxy_name = None

    result = await flow.async_step_not_paired()

    assert result["description_placeholders"]["proxy_name"] == "unknown"


def test_strings_define_proxy_step_with_matching_placeholders() -> None:
    """strings.json and en.json carry the step; its template uses
    exactly the placeholders the form provides."""
    for name in ("strings.json", "translations/en.json"):
        data = json.loads((COMPONENT_DIR / name).read_text(encoding="utf-8"))
        step = data["config"]["step"]["not_paired_proxy"]
        used = set(re.findall(r"\{(\w+)\}", step["description"]))
        assert used == {"address", "proxy_name"}, name
        assert "<" not in step["description"], name  # hassfest: no HTML
