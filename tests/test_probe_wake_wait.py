"""Setup waits for a sleeping brush instead of failing at once.

Asleep is the brush's normal state. The capabilities probe used to raise
DeviceAsleepException as soon as the last connectable advertisement was
older than _STALE_ADV_MAX_SECONDS; it now waits up to
SETUP_ADVERTISEMENT_WAIT for the next one (the progress text asks to
switch the brush on). Port from philips_shaver (feature/bluetooth-adapter).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.philips_sonicare_ble import config_flow as cf
from custom_components.philips_sonicare_ble.const import BLUETOOTH_ADAPTER_AUTO

ADDRESS = "24:E5:AA:BE:9C:1B"
HCI0 = "00:0A:CD:46:B2:2D"


def _flow(adapter: str = BLUETOOTH_ADAPTER_AUTO) -> cf.PhilipsSonicareConfigFlow:
    flow = cf.PhilipsSonicareConfigFlow()
    flow.hass = SimpleNamespace()
    flow._bluetooth_adapter = adapter
    return flow


@pytest.fixture
def stale(monkeypatch):
    """Last advertisement 60 s old: the brush looks asleep."""
    monkeypatch.setattr(
        cf, "async_last_service_info",
        lambda hass, address, connectable=True: SimpleNamespace(
            rssi=-60, time=cf.time.monotonic() - 60
        ),
    )
    monkeypatch.setattr(cf, "SETUP_ADVERTISEMENT_WAIT", 0.05)


def _watchers(monkeypatch, fire: dict[str | None, bool]):
    """Patch watch_advertisements; ``fire[source]`` sets that event at once."""
    calls: list[str | None] = []
    stopped: list[str | None] = []

    def _watch(address, source):
        calls.append(source)
        event = asyncio.Event()
        if fire.get(source):
            event.set()
        return event, lambda: stopped.append(source)

    monkeypatch.setattr(cf, "watch_advertisements", _watch)
    return calls, stopped


async def test_fresh_advertisement_needs_no_wait(monkeypatch) -> None:
    monkeypatch.setattr(
        cf, "async_last_service_info",
        lambda hass, address, connectable=True: SimpleNamespace(
            rssi=-60, time=cf.time.monotonic()
        ),
    )
    calls, _ = _watchers(monkeypatch, {})
    assert await _flow()._async_ensure_awake(ADDRESS) == "awake"
    assert calls == []


async def test_stale_brush_woken_during_wait(monkeypatch, stale) -> None:
    calls, stopped = _watchers(monkeypatch, {None: True})
    assert await _flow()._async_ensure_awake(ADDRESS) == "awake"
    # Automatic: any scanner counts; the watcher is always released.
    assert calls == [None]
    assert stopped == [None]


async def test_stale_brush_stays_silent(monkeypatch, stale) -> None:
    _, stopped = _watchers(monkeypatch, {})
    assert await _flow()._async_ensure_awake(ADDRESS) == "asleep"
    assert stopped == [None]


async def test_fixed_adapter_silent_while_another_scanner_hears(
    monkeypatch, stale
) -> None:
    calls, stopped = _watchers(monkeypatch, {None: True})
    assert await _flow(HCI0)._async_ensure_awake(ADDRESS) == "adapter"
    assert calls == [HCI0, None]
    assert sorted(stopped, key=str) == sorted([HCI0, None], key=str)


async def test_fixed_adapter_hears_the_brush(monkeypatch, stale) -> None:
    _watchers(monkeypatch, {HCI0: True})
    assert await _flow(HCI0)._async_ensure_awake(ADDRESS) == "awake"


async def test_no_wait_fails_at_once(monkeypatch, stale) -> None:
    # Plain form submits (not_paired retry) must not hang 45 s.
    calls, _ = _watchers(monkeypatch, {None: True})
    assert await _flow()._async_ensure_awake(ADDRESS, wait=False) == "asleep"
    assert calls == []


async def test_just_paired_skips_the_gate_once(monkeypatch, stale) -> None:
    # Pair() just talked to the brush; while connected it sent no
    # advertisements, so the age check would misread it as asleep.
    calls, _ = _watchers(monkeypatch, {})
    flow = _flow()
    flow._ble_just_paired = True
    assert await flow._async_ensure_awake(ADDRESS) == "awake"
    assert calls == []
    assert flow._ble_just_paired is False
    assert await flow._async_ensure_awake(ADDRESS) == "asleep"


async def test_core_without_advertisement_callback(monkeypatch, stale) -> None:
    def _watch(address, source):
        raise LookupError("no per-address callback")

    monkeypatch.setattr(cf, "watch_advertisements", _watch)
    assert await _flow()._async_ensure_awake(ADDRESS) == "asleep"


async def test_never_seen_and_invalidated_rssi_count_as_stale(monkeypatch) -> None:
    flow = _flow()
    monkeypatch.setattr(
        cf, "async_last_service_info", lambda hass, address, connectable=True: None
    )
    assert flow._stale_advertisement(ADDRESS) == "never seen"
    monkeypatch.setattr(
        cf, "async_last_service_info",
        lambda hass, address, connectable=True: SimpleNamespace(
            rssi=-127, time=cf.time.monotonic()
        ),
    )
    assert flow._stale_advertisement(ADDRESS) == "stale RSSI -127"


# --- _at_least ---------------------------------------------------------------

async def test_at_least_holds_a_fast_result(monkeypatch) -> None:
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(cf.asyncio, "sleep", _sleep)

    async def _fast():
        return 42

    assert await cf._at_least(_fast) == 42
    assert slept and 0 < slept[0] <= cf._PROGRESS_MIN_SECONDS


async def test_at_least_holds_a_fast_failure(monkeypatch) -> None:
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(cf.asyncio, "sleep", _sleep)

    async def _boom():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await cf._at_least(_boom)
    assert slept


# --- adapter choice survives the manual-entry sentinel ----------------------

def test_adapter_from_input_falls_back_to_the_earlier_choice() -> None:
    assert cf._adapter_from_input({}, HCI0) == HCI0
    assert cf._adapter_from_input({"adapter": {}}, HCI0) == HCI0
    assert cf._adapter_from_input(
        {"adapter": {"bluetooth_adapter": BLUETOOTH_ADAPTER_AUTO}}, HCI0
    ) == BLUETOOTH_ADAPTER_AUTO
