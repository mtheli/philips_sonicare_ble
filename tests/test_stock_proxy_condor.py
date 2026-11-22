"""Condor toothbrushes over a stock ESPHome Bluetooth proxy (#52).

Two failures on HA without D-Bus and a stock ``bluetooth_proxy``:

1. The config flow paired a Condor brush through the host's BlueZ over
   D-Bus even when the probe rode the proxy. Without D-Bus that raised
   before any pairing and surfaced as "not_a_sonicare".
2. The proxy never encrypts on its own, so every reconnect failed the
   Condor handshake's first CCCD write with Insufficient Encryption and
   the coordinator retried every few seconds.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.philips_sonicare_ble import config_flow as cf
from custom_components.philips_sonicare_ble import coordinator as coord_mod
from custom_components.philips_sonicare_ble import dbus_pairing
from custom_components.philips_sonicare_ble.const import (
    CHAR_FIRMWARE_REVISION,
    CHAR_MODEL_NUMBER,
    CHAR_SERIAL_NUMBER,
    CONF_ADDRESS,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    DOMAIN,
    SVC_CONDOR,
    TRANSPORT_BLEAK,
)
from custom_components.philips_sonicare_ble.coordinator import (
    PhilipsSonicareCoordinator,
)
from custom_components.philips_sonicare_ble.exceptions import (
    NotPairedException,
    ProxyEncryptionError,
    TransportError,
)
from custom_components.philips_sonicare_ble.transport import BleakTransport

ADDRESS = "6F:C2:7E:C2:F6:07"

DEVICE_INFO = {
    CHAR_MODEL_NUMBER.lower(): b"HX742X",
    CHAR_SERIAL_NUMBER.lower(): b"01C04D3C69",
    CHAR_FIRMWARE_REVISION.lower(): b"1.8.20.0",
}


# ---------------------------------------------------------------- Fix 1


class ProxyClient:
    """Bleak client stand-in for a connection through a stock proxy."""

    address = ADDRESS

    def __init__(
        self, pair_error: Exception | None = None, pair_result=True
    ) -> None:
        self.is_connected = True
        self.services = [SimpleNamespace(uuid=SVC_CONDOR)]
        self.pair = AsyncMock(side_effect=pair_error, return_value=pair_result)

    async def read_gatt_char(self, char_uuid):
        return DEVICE_INFO[str(char_uuid).lower()]

    async def disconnect(self) -> None:
        self.is_connected = False


def _probe_flow(hass, monkeypatch, client: ProxyClient):
    flow = cf.PhilipsSonicareConfigFlow()
    flow.hass = hass
    flow.flow_id = "probe-flow"
    flow.handler = DOMAIN
    flow._address = ADDRESS
    flow._async_ensure_awake = AsyncMock(return_value="awake")
    monkeypatch.setattr(
        cf, "async_ble_device_from_address", lambda hass, address: object()
    )
    monkeypatch.setattr(cf, "bleak_establish", AsyncMock(return_value=client))
    monkeypatch.setattr(
        cf, "describe_connection_path",
        lambda hass, client, device: "atom-echo-proxy (14:08:08:54:D5:DE)",
    )
    monkeypatch.setattr(cf, "is_local_bluez_connection", lambda client: False)
    monkeypatch.setattr(cf, "local_adapter_source", lambda client: None)
    monkeypatch.setattr(cf, "_cleanup_disconnected_client", AsyncMock())
    flow._creep_progress = AsyncMock()
    return flow


def _forbid_dbus(monkeypatch) -> tuple[AsyncMock, AsyncMock]:
    """HA without D-Bus: both host-side helpers fail like a missing socket."""
    no_bus = FileNotFoundError(2, "No such file or directory")
    paired = AsyncMock(side_effect=no_bus)
    pair = AsyncMock(side_effect=no_bus)
    monkeypatch.setattr(dbus_pairing, "async_is_device_paired", paired)
    monkeypatch.setattr(dbus_pairing, "async_pair_via_existing_client", pair)
    return paired, pair


async def test_proxy_probe_pairs_through_the_proxy(hass, monkeypatch) -> None:
    """The Condor probe over a proxy pairs on the proxy client, no D-Bus."""
    client = ProxyClient()
    flow = _probe_flow(hass, monkeypatch, client)
    paired, dbus_pair = _forbid_dbus(monkeypatch)

    result = await flow._async_fetch_capabilities(ADDRESS)

    client.pair.assert_awaited_once()
    paired.assert_not_awaited()
    dbus_pair.assert_not_awaited()
    assert result["model"] == "HX742X"
    assert result["serial"] == "01C04D3C69"
    # The capabilities page labels the proxy link from this flag.
    assert flow._probe_needed_encryption is True


async def test_proxy_pair_failure_asks_for_pairing(hass, monkeypatch) -> None:
    """A failed pair() routes to the pairing dialog, not "not a Sonicare"."""
    client = ProxyClient(pair_error=RuntimeError("Pairing failed: 0x55"))
    flow = _probe_flow(hass, monkeypatch, client)
    _forbid_dbus(monkeypatch)

    with pytest.raises(NotPairedException):
        await flow._async_fetch_capabilities(ADDRESS)


async def test_proxy_pair_returning_false_asks_for_pairing(
    hass, monkeypatch
) -> None:
    """Older bleak_esphome returned False instead of raising."""
    client = ProxyClient(pair_result=False)
    flow = _probe_flow(hass, monkeypatch, client)
    _forbid_dbus(monkeypatch)

    with pytest.raises(NotPairedException):
        await flow._async_fetch_capabilities(ADDRESS)
    assert flow._probe_needed_encryption is False


async def test_local_probe_keeps_dbus_pairing(hass, monkeypatch) -> None:
    """Local BlueZ connections still pair through D-Bus."""
    client = ProxyClient()
    flow = _probe_flow(hass, monkeypatch, client)
    monkeypatch.setattr(cf, "is_local_bluez_connection", lambda client: True)
    paired = AsyncMock(return_value=False)
    dbus_pair = AsyncMock()
    monkeypatch.setattr(dbus_pairing, "async_is_device_paired", paired)
    monkeypatch.setattr(dbus_pairing, "async_pair_via_existing_client", dbus_pair)

    await flow._async_fetch_capabilities(ADDRESS)

    dbus_pair.assert_awaited_once()
    client.pair.assert_not_awaited()


async def test_missing_dbus_is_a_pairing_error(monkeypatch) -> None:
    """No system D-Bus socket becomes a PairingError, not a raw OSError."""

    class NoBus:
        def __init__(self, *args, **kwargs) -> None:
            self.connected = False

        async def connect(self):
            raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(dbus_pairing, "MessageBus", NoBus)

    with pytest.raises(dbus_pairing.PairingError):
        await dbus_pairing.async_pair_via_existing_client(MagicMock(), ADDRESS)


# ---------------------------------------------------------------- Fix 2


def _condor_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_TRANSPORT_TYPE: TRANSPORT_BLEAK,
            CONF_SERVICES: [SVC_CONDOR],
            "model": "HX742X",
        },
    )
    entry.add_to_hass(hass)
    return entry


class ProxyTransport:
    """BleakTransport stand-in on a stock proxy connection."""

    connection_path = None
    disconnect_count = 0

    def __init__(self, pair_error: Exception | None = None) -> None:
        self.is_connected = True
        self.pair = AsyncMock(side_effect=pair_error)

    def set_disconnect_callback(self, cb) -> None:
        self._cb = cb

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        return None


def _coordinator(hass, transport) -> PhilipsSonicareCoordinator:
    coordinator = PhilipsSonicareCoordinator(hass, _condor_entry(hass), transport)
    assert coordinator._use_condor
    coordinator._last_adapter_type = "stock_proxy"
    return coordinator


@pytest.mark.parametrize(
    ("adapter", "expect_pair"),
    [("stock_proxy", True), ("direct_ble", False)],
)
async def test_condor_session_encrypts_only_on_stock_proxy(
    hass, monkeypatch, adapter, expect_pair
) -> None:
    transport = ProxyTransport()
    coordinator = _coordinator(hass, transport)
    monkeypatch.setattr(
        PhilipsSonicareCoordinator, "adapter_type", property(lambda self: adapter)
    )
    coordinator._protocol = SimpleNamespace(
        connect=AsyncMock(),
        refresh_all=AsyncMock(return_value={}),
        start_live_updates=AsyncMock(),
        _subscribed_ports=[("1", "Battery")],
    )

    await coordinator._setup_condor_session()

    assert transport.pair.await_count == (1 if expect_pair else 0)
    coordinator._protocol.connect.assert_awaited_once()


async def test_pair_runs_before_the_handshake(hass) -> None:
    transport = ProxyTransport()
    coordinator = _coordinator(hass, transport)
    order: list[str] = []
    transport.pair.side_effect = lambda **kw: order.append("pair")

    async def _connect():
        order.append("handshake")

    coordinator._scanner_needs_eager_smp = lambda: True
    coordinator._protocol = SimpleNamespace(
        connect=_connect,
        refresh_all=AsyncMock(return_value={}),
        start_live_updates=AsyncMock(),
        _subscribed_ports=[],
    )

    await coordinator._setup_condor_session()

    assert order == ["pair", "handshake"]


@pytest.mark.parametrize(
    "err",
    [
        TransportError(
            "Bluetooth GATT Error address=6F:C2:7E:C2:F6:07 handle=19 "
            "error=15 description=Insufficient encryption"
        ),
        TransportError(
            "Bluetooth GATT Error address=6F:C2:7E:C2:F6:07 handle=19 "
            "error=5 description=Insufficient authentication"
        ),
        ProxyEncryptionError("encryption through the Bluetooth proxy failed"),
    ],
)
async def test_encryption_errors_are_recognised(hass, err) -> None:
    coordinator = _coordinator(hass, ProxyTransport())
    coordinator.transport.is_connected = False

    assert coordinator._is_proxy_encryption_error(err)


@pytest.mark.parametrize(
    "err",
    [
        TimeoutError("Condor response timeout"),
        # Says nothing about the bond — must not raise the repair issue.
        TransportError("pairing through the Bluetooth proxy timed out after 15s"),
        TransportError("the Bluetooth proxy firmware does not support pairing"),
        TransportError("pairing through the Bluetooth proxy aborted: disconnected"),
        TransportError("Bluetooth GATT Error handle=19 error=150 description=x"),
    ],
)
async def test_errors_unrelated_to_the_bond_do_not_count(hass, err) -> None:
    coordinator = _coordinator(hass, ProxyTransport())
    coordinator.transport.is_connected = False

    assert not coordinator._is_proxy_encryption_error(err)


async def test_encryption_error_on_other_paths_does_not_count(hass) -> None:
    coordinator = _coordinator(hass, ProxyTransport())
    coordinator.transport.is_connected = False
    coordinator._last_adapter_type = "direct_ble"
    assert not coordinator._is_proxy_encryption_error(
        TransportError("error=15 description=Insufficient encryption")
    )


async def test_repair_issue_after_repeated_failures(hass) -> None:
    coordinator = _coordinator(hass, ProxyTransport())
    registry = ir.async_get(hass)
    issue_id = f"stock_proxy_encryption_{ADDRESS.lower()}"

    for _ in range(coord_mod.PROXY_ENCRYPT_ISSUE_AFTER - 1):
        coordinator._count_proxy_encryption_failure()
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    coordinator._count_proxy_encryption_failure()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    coordinator._clear_proxy_encryption_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


def _run_loop_until(hass, coordinator, monkeypatch, done: asyncio.Event):
    """Run the live loop with sleeps collapsed; return (task, sleeps)."""
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(coord_mod.asyncio, "sleep", _sleep)
    return hass.async_create_task(coordinator._start_live_monitoring()), sleeps


async def _stop(task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_encryption_failures_retry_without_pause(hass, monkeypatch) -> None:
    """A brush that will not encrypt is retried at once; the issue follows.

    The brush is awake for two or three minutes at most, so a pause would
    only cost the attempts that could still succeed in that window.
    """
    transport = ProxyTransport()
    coordinator = _coordinator(hass, transport)
    coordinator._protocol = SimpleNamespace(invalidate_session=lambda: None)
    attempts = 0
    done = asyncio.Event()

    async def _fail() -> int:
        nonlocal attempts
        attempts += 1
        if attempts >= coord_mod.PROXY_ENCRYPT_ISSUE_AFTER:
            done.set()
        raise ProxyEncryptionError(
            "encryption through the Bluetooth proxy failed: Pairing failed"
        )

    coordinator._setup_condor_session = _fail
    task, sleeps = _run_loop_until(hass, coordinator, monkeypatch, done)
    try:
        await asyncio.wait_for(done.wait(), 5)
        await asyncio.sleep(0)
    finally:
        await _stop(task)

    assert max(sleeps) <= 5  # only the usual quick-retry spacing
    assert coordinator._proxy_encrypt_failures >= coord_mod.PROXY_ENCRYPT_ISSUE_AFTER
    issue_id = f"stock_proxy_encryption_{ADDRESS.lower()}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_lost_bond_heals_on_the_next_attempt(hass, monkeypatch) -> None:
    """After error 97 the proxy drops its key; the next pair() bonds afresh."""
    transport = ProxyTransport()
    coordinator = _coordinator(hass, transport)
    coordinator._protocol = SimpleNamespace(invalidate_session=lambda: None)
    done = asyncio.Event()
    results = [ProxyEncryptionError("Pairing failed due to error: 97"), 5]

    async def _setup() -> int:
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        done.set()
        return result

    coordinator._setup_condor_session = _setup
    task, sleeps = _run_loop_until(hass, coordinator, monkeypatch, done)
    try:
        await asyncio.wait_for(done.wait(), 5)
        await asyncio.sleep(0)
        assert coordinator._proxy_encrypt_failures == 0
    finally:
        await _stop(task)

    assert max(sleeps) <= 5
    issue_id = f"stock_proxy_encryption_{ADDRESS.lower()}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    ("adapter", "warned"), [("stock_proxy", True), ("direct_ble", False)]
)
async def test_setup_timeout_after_connect_is_a_warning_on_the_proxy(
    hass, monkeypatch, caplog, adapter, warned
) -> None:
    """A GATT timeout on an established proxy link is no longer debug-only.

    The bench case: encryption works, then the CCCD write times out — that
    retried unseen. Direct BLE keeps logging it at debug level.
    """
    transport = ProxyTransport()
    coordinator = _coordinator(hass, transport)
    coordinator._last_adapter_type = adapter
    coordinator._protocol = SimpleNamespace(invalidate_session=lambda: None)
    done = asyncio.Event()

    async def _fail() -> int:
        done.set()
        raise TransportError("Timeout waiting for BluetoothGATTWriteResponse")

    coordinator._setup_condor_session = _fail
    task, _ = _run_loop_until(hass, coordinator, monkeypatch, done)
    try:
        await asyncio.wait_for(done.wait(), 5)
        await asyncio.sleep(0)
    finally:
        await _stop(task)

    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "live monitoring error" in r.message
    ]
    assert bool(warnings) is warned
    assert coordinator._proxy_encrypt_failures == 0


async def test_successful_setup_clears_a_left_over_issue(hass) -> None:
    """A fresh coordinator (counter 0) still clears the repair issue.

    The issue tells the user to remove and re-add the brush — the new
    entry starts with no failure count, so clearing must not depend on it.
    """
    transport = ProxyTransport()
    coordinator = _coordinator(hass, transport)
    coordinator._protocol = SimpleNamespace(invalidate_session=lambda: None)
    coordinator._setup_condor_session = AsyncMock(return_value=1)
    coordinator._create_proxy_encryption_issue()
    registry = ir.async_get(hass)
    issue_id = f"stock_proxy_encryption_{ADDRESS.lower()}"
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    assert coordinator._proxy_encrypt_failures == 0

    task = hass.async_create_task(coordinator._start_live_monitoring())
    try:
        for _ in range(100):
            if coordinator._setup_condor_session.await_count:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)
        assert coordinator._setup_condor_session.await_count == 1
        assert registry.async_get_issue(DOMAIN, issue_id) is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _bleak_transport(client) -> BleakTransport:
    transport = BleakTransport.__new__(BleakTransport)
    transport._address = ADDRESS
    transport._client = client
    return transport


@pytest.mark.parametrize(
    ("pair", "expected", "text"),
    [
        (AsyncMock(side_effect=Exception("Pairing failed due to error: 97")),
         ProxyEncryptionError, "error: 97"),
        (AsyncMock(return_value=False), ProxyEncryptionError, "failed"),
        (AsyncMock(side_effect=NotImplementedError()), TransportError,
         "does not support pairing"),
        (AsyncMock(side_effect=Exception("Disconnected")), TransportError,
         "aborted: Disconnected"),
    ],
)
async def test_transport_pair_maps_errors(pair, expected, text) -> None:
    client = SimpleNamespace(is_connected=True, pair=pair)

    with pytest.raises(expected, match=text) as info:
        await _bleak_transport(client).pair(timeout=1)

    if expected is TransportError:
        assert not isinstance(info.value, ProxyEncryptionError)


async def test_transport_pair_timeout_is_not_an_encryption_error() -> None:
    async def _hang():
        await asyncio.sleep(10)

    client = SimpleNamespace(is_connected=True, pair=_hang)

    with pytest.raises(TransportError, match="timed out") as info:
        await _bleak_transport(client).pair(timeout=0.01)
    assert not isinstance(info.value, ProxyEncryptionError)


async def test_transport_pair_success() -> None:
    client = SimpleNamespace(is_connected=True, pair=AsyncMock(return_value=True))

    await _bleak_transport(client).pair(timeout=1)

    client.pair.assert_awaited_once()

