"""Tests for the "Bluetooth adapter" option (direct BLE only)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.philips_sonicare_ble import config_flow as cf
from custom_components.philips_sonicare_ble import transport
from custom_components.philips_sonicare_ble.exceptions import AdapterNotReceivingException
from custom_components.philips_sonicare_ble.const import (
    CONF_NOTIFY_THROTTLE,
    CONF_BLUETOOTH_ADAPTER,
    CONF_TRANSPORT_TYPE,
    BLUETOOTH_ADAPTER_AUTO,
    TRANSPORT_BLEAK,
    TRANSPORT_ESP_BRIDGE,
)

ADDRESS = "24:E5:AA:BE:9C:1B"
HCI0 = "00:0A:CD:46:B2:2D"
BATH = "F4:65:0B:01:B9:6E"
CONF_ADDRESS_KEY = "address"


class _FakeHaScanner(transport.HaScanner):
    # Bypass HaScanner.__init__ — only isinstance matters here.
    def __init__(self):
        pass


def _local(name: str, source: str):
    scanner = _FakeHaScanner.__new__(_FakeHaScanner)
    scanner.name = name
    scanner.source = source
    scanner.connectable = True
    return scanner


def _remote(name: str, source: str, connectable: bool = True):
    return SimpleNamespace(name=name, source=source, connectable=connectable)


@pytest.fixture
def scanners(monkeypatch):
    hci0 = _local("hci0 (00:0A:CD:46:B2:2D)", "00:0A:CD:46:B2:2D")
    bath = _remote("bath-proxy", "F4:65:0B:01:B9:6E")
    kitchen = _remote("Kitchen proxy", "A0:B7:65:11:22:33")
    passive = _remote("shelly-passive", "C8:2B:96:00:00:01", connectable=False)
    monkeypatch.setattr(
        transport, "async_current_scanners",
        MagicMock(return_value=[hci0, kitchen, passive, bath]),
    )
    # The brush is heard by hci0 and the bath proxy only.
    monkeypatch.setattr(
        transport, "async_scanner_devices_by_address",
        MagicMock(return_value=[
            SimpleNamespace(scanner=hci0, advertisement=SimpleNamespace(rssi=-78)),
            SimpleNamespace(scanner=bath, advertisement=SimpleNamespace(rssi=-61)),
        ]),
    )


def test_connectable_scanners_heard_first_then_by_name(scanners) -> None:
    result = transport.describe_connectable_scanners(SimpleNamespace(), ADDRESS)

    assert [s["source"] for s in result] == [
        "F4:65:0B:01:B9:6E",   # bath proxy, -61
        "00:0A:CD:46:B2:2D",   # hci0, -78
        "A0:B7:65:11:22:33",   # kitchen, not hearing the brush
    ]
    assert [s["rssi"] for s in result] == [-61, -78, None]
    assert [s["is_local"] for s in result] == [False, True, False]


async def test_watch_advertisements_only_for_that_adapter(monkeypatch) -> None:
    # Local adapters keep no per-device timestamps (live 2026-10-30:
    # waited 45 s although hci0 heard the brush every 11 s) — the
    # per-address callback fires for every packet of every scanner.
    import habluetooth

    registered = {}
    stop = MagicMock()

    def _register(callback, address):
        registered[address] = callback
        return stop

    monkeypatch.setattr(
        habluetooth, "get_manager",
        lambda: SimpleNamespace(async_register_advertisement_callback=_register),
    )
    event, cancel = transport.watch_advertisements(ADDRESS.lower(), HCI0.lower())

    registered[ADDRESS](SimpleNamespace(source=BATH))
    assert not event.is_set()
    registered[ADDRESS](SimpleNamespace(source=HCI0))
    assert event.is_set()
    assert cancel is stop


def test_watch_advertisements_unsupported(monkeypatch) -> None:
    import habluetooth

    monkeypatch.setattr(habluetooth, "get_manager", lambda: SimpleNamespace())
    with pytest.raises(LookupError):
        transport.watch_advertisements(ADDRESS, HCI0)


def test_scanner_rssi(scanners) -> None:
    assert transport.scanner_rssi(SimpleNamespace(), ADDRESS, HCI0) == -78
    assert transport.scanner_rssi(SimpleNamespace(), ADDRESS, "A0:B7:65:11:22:33") is None


def test_connectable_scanners_skip_passive(scanners) -> None:
    result = transport.describe_connectable_scanners(SimpleNamespace(), ADDRESS)

    assert "C8:2B:96:00:00:01" not in {s["source"] for s in result}


def test_connectable_scanners_survive_lookup_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        transport, "async_current_scanners", MagicMock(side_effect=RuntimeError)
    )
    monkeypatch.setattr(
        transport, "async_scanner_devices_by_address", MagicMock(return_value=[])
    )

    assert transport.describe_connectable_scanners(SimpleNamespace(), ADDRESS) == []


def _options_flow(monkeypatch, transport_type: str, options: dict | None = None):
    entry = SimpleNamespace(
        entry_id="entry-1",
        title="Bathroom",
        data={CONF_ADDRESS_KEY: ADDRESS, CONF_TRANSPORT_TYPE: transport_type},
        options=options or {},
    )
    monkeypatch.setattr(
        cf.PhilipsSonicareOptionsFlow, "config_entry", property(lambda self: entry)
    )
    flow = cf.PhilipsSonicareOptionsFlow()
    flow.hass = SimpleNamespace()
    flow.flow_id = "options-flow"
    flow.handler = "philips_sonicare_ble"
    return flow



def _field(result, key):
    for marker, value in result["data_schema"].schema.items():
        if marker.schema == key:
            return marker, value
    return None, None


async def test_direct_ble_form_offers_auto_and_scanners(monkeypatch, scanners) -> None:
    flow = _options_flow(monkeypatch, TRANSPORT_BLEAK)

    result = await flow.async_step_adapter()

    marker, selector = _field(result, CONF_BLUETOOTH_ADAPTER)
    assert marker is not None
    assert marker.default() == BLUETOOTH_ADAPTER_AUTO
    config = selector.config
    assert config["translation_key"] == CONF_BLUETOOTH_ADAPTER
    assert [o["value"] for o in config["options"]] == [
        BLUETOOTH_ADAPTER_AUTO,
        "F4:65:0B:01:B9:6E",
        "00:0A:CD:46:B2:2D",
        "A0:B7:65:11:22:33",
    ]
    assert config["options"][1]["label"] == "bath-proxy — Bluetooth proxy, -61 dBm"
    assert config["options"][2]["label"] == (
        "hci0 (00:0A:CD:46:B2:2D) — Direct Bluetooth, -78 dBm"
    )
    assert config["options"][3]["label"] == "Kitchen proxy — Bluetooth proxy"


async def test_saved_scanner_offline_stays_selectable(monkeypatch, scanners) -> None:
    flow = _options_flow(
        monkeypatch, TRANSPORT_BLEAK,
        {CONF_BLUETOOTH_ADAPTER: "DE:AD:BE:EF:00:01"},
    )

    result = await flow.async_step_adapter()

    marker, selector = _field(result, CONF_BLUETOOTH_ADAPTER)
    assert marker.default() == "DE:AD:BE:EF:00:01"
    assert selector.config["options"][-1] == {
        "value": "DE:AD:BE:EF:00:01", "label": "DE:AD:BE:EF:00:01",
    }


async def test_esp_bridge_form_has_no_bluetooth_adapter(monkeypatch, scanners) -> None:
    flow = _options_flow(monkeypatch, TRANSPORT_ESP_BRIDGE)

    result = await flow.async_step_init()

    assert _field(result, CONF_BLUETOOTH_ADAPTER) == (None, None)
    assert _field(result, CONF_NOTIFY_THROTTLE)[0] is not None


async def test_direct_ble_saves_choice(monkeypatch, scanners) -> None:
    flow = _options_flow(monkeypatch, TRANSPORT_BLEAK)

    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=MagicMock(), async_schedule_reload=MagicMock(),
        )
    )

    result = await flow.async_step_adapter({CONF_BLUETOOTH_ADAPTER: "F4:65:0B:01:B9:6E"})

    # A changed adapter ends on the checklist page, options already saved.
    assert result["reason"] == "adapter_changed"
    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["options"]
    assert saved[CONF_BLUETOOTH_ADAPTER] == "F4:65:0B:01:B9:6E"


async def test_esp_bridge_saves_no_choice(monkeypatch, scanners) -> None:
    flow = _options_flow(monkeypatch, TRANSPORT_ESP_BRIDGE)

    result = await flow.async_step_init({CONF_BLUETOOTH_ADAPTER: "F4:65:0B:01:B9:6E"})

    assert CONF_BLUETOOTH_ADAPTER not in result["data"]


async def test_direct_ble_starts_with_menu(monkeypatch, scanners) -> None:
    flow = _options_flow(monkeypatch, TRANSPORT_BLEAK)

    result = await flow.async_step_init()

    assert result["type"] == "menu"
    assert result["menu_options"] == ["settings", "adapter"]


async def test_esp_bridge_skips_menu(monkeypatch, scanners) -> None:
    flow = _options_flow(monkeypatch, TRANSPORT_ESP_BRIDGE)

    result = await flow.async_step_init()

    assert result["step_id"] == "settings"


async def test_settings_have_no_adapter_and_keep_it(monkeypatch, scanners) -> None:
    flow = _options_flow(
        monkeypatch, TRANSPORT_BLEAK,
        {CONF_BLUETOOTH_ADAPTER: "00:0A:CD:46:B2:2D"},
    )

    form = await flow.async_step_settings()
    assert _field(form, CONF_BLUETOOTH_ADAPTER) == (None, None)

    result = await flow.async_step_settings({"sensor_gyroscope": True})

    # Saving settings never moves the brush to another adapter.
    assert result["data"][CONF_BLUETOOTH_ADAPTER] == "00:0A:CD:46:B2:2D"
    assert result["data"]["sensor_gyroscope"] is True


def _setup_flow(transport_type: str):
    flow = cf.PhilipsSonicareConfigFlow()
    flow.flow_id = "setup-flow"
    flow.handler = "philips_sonicare_ble"
    flow.hass = SimpleNamespace(config=SimpleNamespace(components=set()))
    flow._address = ADDRESS
    flow._name = "Philips Sonicare"
    flow._transport_type = transport_type
    flow._fetched_data = {
        "model": "HX742X", "services": [],
        "connection_path": "bath-proxy",
    }
    flow._build_default_name = lambda: "Master Bath"
    return flow


def _confirm_flow(monkeypatch):
    flow = _setup_flow(TRANSPORT_BLEAK)
    flow._find_esp_bridge_for_mac = AsyncMock(return_value=None)
    monkeypatch.setattr(cf, "_async_text_blocks", AsyncMock(return_value={}))
    return flow


async def test_confirm_offers_auto_and_scanners(monkeypatch, scanners) -> None:
    # Asked before the first connect, so the probe and its pairing already
    # run through the chosen adapter.
    flow = _confirm_flow(monkeypatch)

    result = await flow.async_step_bluetooth_confirm()

    marker, selector = _field(result, CONF_BLUETOOTH_ADAPTER)
    assert marker.default() == BLUETOOTH_ADAPTER_AUTO
    assert [o["value"] for o in selector.config["options"]] == [
        BLUETOOTH_ADAPTER_AUTO,
        "F4:65:0B:01:B9:6E",
        "00:0A:CD:46:B2:2D",
        "A0:B7:65:11:22:33",
    ]


async def test_confirm_single_scanner_has_no_choice(monkeypatch) -> None:
    monkeypatch.setattr(
        transport, "async_current_scanners",
        MagicMock(return_value=[_local("hci0", "00:0A:CD:46:B2:2D")]),
    )
    monkeypatch.setattr(
        transport, "async_scanner_devices_by_address", MagicMock(return_value=[])
    )
    flow = _confirm_flow(monkeypatch)

    result = await flow.async_step_bluetooth_confirm()

    assert result["data_schema"] is None


async def test_confirm_submit_keeps_choice_for_probe(monkeypatch, scanners) -> None:
    flow = _confirm_flow(monkeypatch)
    flow._start_ble_probe = MagicMock(return_value={"type": "progress"})

    await flow.async_step_bluetooth_confirm({CONF_BLUETOOTH_ADAPTER: BATH})

    assert flow._bluetooth_adapter == BATH
    flow._start_ble_probe.assert_called_once_with("bluetooth_confirm", ADDRESS)


async def test_confirm_rerender_keeps_previous_choice(monkeypatch, scanners) -> None:
    # After a failed probe the form comes back — with the user's pick.
    flow = _confirm_flow(monkeypatch)
    flow._bluetooth_adapter = HCI0

    result = await flow.async_step_bluetooth_confirm()

    marker, _ = _field(result, CONF_BLUETOOTH_ADAPTER)
    assert marker.default() == HCI0


async def test_rerender_keeps_chosen_adapter_that_left(monkeypatch) -> None:
    # The fixed adapter's "does not receive" error: on cores without the
    # scanner list it is missing from the options. The field must stay,
    # with the choice selectable — a default outside the options fails
    # schema validation, a vanished field silently resets to Automatic.
    monkeypatch.setattr(transport, "async_current_scanners", None)
    monkeypatch.setattr(
        transport, "async_scanner_devices_by_address", MagicMock(return_value=[])
    )
    flow = _confirm_flow(monkeypatch)
    flow._bluetooth_adapter = HCI0

    result = await flow.async_step_bluetooth_confirm()

    marker, selector = _field(result, CONF_BLUETOOTH_ADAPTER)
    assert marker.default() == HCI0
    assert HCI0 in [o["value"] for o in selector.config["options"]]


async def test_fixed_proxy_skips_bluez_pairing(monkeypatch, scanners) -> None:
    # A proxy is no BlueZ adapter: an auth error must not start the host
    # pairing (which would wait for a device that never appears there).
    flow = _setup_flow(TRANSPORT_BLEAK)
    flow._bluetooth_adapter = BATH
    flow._probe_via_proxy = None
    monkeypatch.setattr(
        flow, "_async_fetch_capabilities",
        AsyncMock(side_effect=cf.NotPairedException),
    )
    flow._try_auto_pair = AsyncMock(return_value=True)

    with pytest.raises(cf.NotPairedException):
        await flow._fetch_with_pair_retry(ADDRESS)
    flow._try_auto_pair.assert_not_called()
    assert flow._pair_target_adapter(ADDRESS) is None


async def test_not_paired_retry_reports_adapter_error(monkeypatch) -> None:
    flow = _setup_flow(TRANSPORT_BLEAK)
    monkeypatch.setattr(
        flow, "_async_fetch_capabilities",
        AsyncMock(side_effect=AdapterNotReceivingException),
    )
    shown = {}

    async def _form(errors):
        shown.update(errors)
        return {"type": "form"}

    flow._show_not_paired_form = _form
    await flow.async_step_not_paired({})
    assert shown == {"base": "adapter_not_receiving"}


async def test_manual_picker_offers_scanners_without_rssi(monkeypatch, scanners) -> None:
    monkeypatch.setattr(cf, "async_discovered_service_info", lambda hass: [])
    # Without an address no scanner can be said to hear the brush.
    monkeypatch.setattr(
        transport, "async_scanner_devices_by_address", MagicMock(return_value=[])
    )
    flow = _setup_flow(TRANSPORT_BLEAK)
    flow._address = None

    result = await flow.async_step_user_bleak()

    _, selector = _field(result, CONF_BLUETOOTH_ADAPTER)
    labels = [o["label"] for o in selector.config["options"]]
    assert "dBm" not in " ".join(labels)
    assert labels[0] == "Chosen by Home Assistant"


async def test_manual_submit_keeps_choice(monkeypatch, scanners) -> None:
    flow = _setup_flow(TRANSPORT_BLEAK)
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_already_configured = MagicMock()
    flow._start_ble_probe = MagicMock(return_value={"type": "progress"})

    await flow.async_step_user_bleak(
        {CONF_ADDRESS_KEY: ADDRESS, CONF_BLUETOOTH_ADAPTER: HCI0}
    )

    assert flow._bluetooth_adapter == HCI0


async def test_capabilities_has_no_adapter_field(scanners) -> None:
    flow = _setup_flow(TRANSPORT_BLEAK)

    result = await flow.async_step_show_capabilities()

    assert _field(result, CONF_BLUETOOTH_ADAPTER) == (None, None)


async def test_capabilities_title_uses_model_for_generic_name(scanners) -> None:
    flow = _setup_flow(TRANSPORT_BLEAK)

    result = await flow.async_step_show_capabilities()

    assert result["description_placeholders"]["name"] == "HX742X"


async def test_setup_stores_choice_in_options(scanners) -> None:
    flow = _setup_flow(TRANSPORT_BLEAK)
    flow.context = {}
    flow._bluetooth_adapter = BATH

    result = await flow.async_step_show_capabilities({"device_name": "Master Bath"})

    assert result["options"] == {CONF_BLUETOOTH_ADAPTER: BATH}
    assert CONF_BLUETOOTH_ADAPTER not in result["data"]


async def test_setup_auto_stores_no_option(scanners) -> None:
    flow = _setup_flow(TRANSPORT_BLEAK)
    flow.context = {}

    result = await flow.async_step_show_capabilities({"device_name": "Master Bath"})

    assert result["options"] == {}


async def test_setup_esp_bridge_stores_no_option(scanners) -> None:
    flow = _setup_flow(TRANSPORT_ESP_BRIDGE)
    flow.context = {}
    flow._esp_device_name = "atom-s3r"
    flow._esp_bridge_id = "sonicare_1"
    flow._bluetooth_adapter = BATH

    result = await flow.async_step_show_capabilities({"device_name": "Master Bath"})

    assert result["options"] == {}


async def test_probe_fails_fast_when_fixed_adapter_does_not_receive(
    monkeypatch, scanners
) -> None:
    flow = _setup_flow(TRANSPORT_BLEAK)
    flow._discovery_info = None
    flow._bluetooth_adapter = "A0:B7:65:11:22:33"  # kitchen: does not hear it
    monkeypatch.setattr(
        cf, "async_last_service_info",
        lambda hass, address, connectable=True: SimpleNamespace(
            rssi=-60, time=cf.time.monotonic()
        ),
    )
    monkeypatch.setattr(
        cf, "async_ble_device_from_address", lambda hass, address: object()
    )
    establish = AsyncMock()
    monkeypatch.setattr(cf, "bleak_establish", establish)

    with pytest.raises(AdapterNotReceivingException):
        await flow._async_fetch_capabilities(ADDRESS)
    establish.assert_not_awaited()


async def test_probe_wrapper_boxes_adapter_error() -> None:
    flow = _setup_flow(TRANSPORT_BLEAK)
    flow._fetch_with_pair_retry = AsyncMock(side_effect=AdapterNotReceivingException)

    assert await flow._async_ble_probe(ADDRESS) == {"ok": False, "error": "adapter"}


async def test_finish_manual_adapter_error_renders_on_form(monkeypatch) -> None:
    flow = _setup_flow(TRANSPORT_BLEAK)
    flow._ble_probe_origin = "user_bleak"
    flow._ble_probe_result = {"ok": False, "error": "adapter"}
    flow.async_step_user_bleak = AsyncMock(return_value={"type": "form"})

    await flow.async_step_ble_probe_finish()

    assert flow._manual_error == "adapter_not_receiving"


async def test_finish_discovery_adapter_error_picks_its_alert(monkeypatch) -> None:
    flow = _setup_flow(TRANSPORT_BLEAK)
    flow._ble_probe_origin = "bluetooth_confirm"
    flow._ble_probe_result = {"ok": False, "error": "adapter"}
    flow.async_step_bluetooth_confirm = AsyncMock(return_value={"type": "form"})

    await flow.async_step_ble_probe_finish()

    assert flow._confirm_status == "adapter"


# --- Connect: the client class goes through the fixed scanner only -------



def _path(name: str, source: str, rssi: int):
    return SimpleNamespace(
        scanner=SimpleNamespace(name=name, source=source),
        advertisement=SimpleNamespace(rssi=rssi),
        ble_device=SimpleNamespace(address=ADDRESS, name=source),
    )


def _client(source: str):
    cls = transport.fixed_scanner_client_class(source)
    client = object.__new__(cls)
    client._HaBleakClientWrapper__address = ADDRESS  # noqa: SLF001
    return cls, client


def _manager(*paths, known=()):
    by_source = {s.source: s for s in known}
    return SimpleNamespace(
        async_scanner_devices_by_address=MagicMock(return_value=list(paths)),
        async_scanner_by_source=lambda source: by_source.get(source),
    )


@pytest.fixture
def ha_choice(monkeypatch):
    """Stand-in for HA's own selection, recording that it ran."""
    backend = object()
    calls = []

    def fallback(self, manager):
        calls.append(manager)
        return backend

    monkeypatch.setattr(
        transport.HaBleakClientWithServiceCache,
        "_async_get_best_available_backend_and_device",
        fallback,
    )
    return SimpleNamespace(backend=backend, calls=calls)


def test_auto_keeps_plain_client() -> None:
    assert transport.fixed_scanner_client_class(None) is transport.BleakClient
    assert (
        transport.fixed_scanner_client_class(BLUETOOTH_ADAPTER_AUTO)
        is transport.BleakClient
    )


def test_client_class_is_reused_per_source() -> None:
    first = transport.fixed_scanner_client_class(HCI0)
    assert transport.fixed_scanner_client_class(HCI0) is first
    assert transport.fixed_scanner_client_class(BATH) is not first


def test_weaker_fixed_scanner_wins(monkeypatch, ha_choice, caplog) -> None:
    import logging

    hci0 = _path("hci0", HCI0, -82)
    bath = _path("bath-proxy", BATH, -45)
    cls, client = _client(HCI0)
    backend = object()
    calls = []

    def backend_for_device(self, manager, scanner, device):
        calls.append(scanner)
        return backend

    monkeypatch.setattr(cls, "_async_get_backend_for_ble_device", backend_for_device)

    with caplog.at_level(logging.DEBUG, logger=transport.__name__):
        selected = client._async_get_best_available_backend_and_device(
            _manager(bath, hci0)
        )

    assert selected is backend
    assert calls == [hci0.scanner]
    assert ha_choice.calls == []
    assert (
        f"{ADDRESS}: connecting through fixed adapter hci0 (-82 dBm); "
        "other paths: bath-proxy (-45 dBm)"
    ) in caplog.text


def test_fixed_scanner_out_of_range_fails_naming_other_paths(
    monkeypatch, ha_choice
) -> None:
    cls, client = _client(HCI0)
    monkeypatch.setattr(
        cls, "_async_get_backend_for_ble_device",
        MagicMock(side_effect=AssertionError("must not be asked")),
    )
    manager = _manager(
        _path("bath-proxy", BATH, -45),
        known=[SimpleNamespace(source=HCI0, name="hci0 (00:0A:CD:46:B2:2D)")],
    )

    with pytest.raises(transport.BleakError) as err:
        client._async_get_best_available_backend_and_device(manager)

    assert str(err.value) == (
        "Bluetooth adapter hci0 (00:0A:CD:46:B2:2D) does not receive "
        f"{ADDRESS}; received by: bath-proxy (-45 dBm)"
    )
    assert ha_choice.calls == []


def test_fixed_scanner_invalid_rssi_does_not_receive(monkeypatch, ha_choice) -> None:
    # Live 2026-10-30: "connecting through fixed adapter hci1 (-127 dBm);
    # other paths: hci0 (-74 dBm)" — the -127 entry is a BlueZ leftover.
    cls, client = _client(HCI0)
    monkeypatch.setattr(
        cls, "_async_get_backend_for_ble_device",
        MagicMock(side_effect=AssertionError("must not be asked")),
    )
    manager = _manager(_path("hci0", HCI0, -127), _path("bath-proxy", BATH, -74))

    with pytest.raises(transport.BleakError) as err:
        client._async_get_best_available_backend_and_device(manager)

    assert str(err.value) == (
        f"Bluetooth adapter {HCI0} does not receive {ADDRESS}; received by: "
        "bath-proxy (-74 dBm)"
    )
    assert ha_choice.calls == []


async def test_transport_connect_skips_when_connected(monkeypatch) -> None:
    # A second establish would orphan the live client (double
    # "connected via" seen live after a quick retry).
    establish = AsyncMock()
    monkeypatch.setattr(transport, "bleak_establish", establish)
    bt = transport.BleakTransport(SimpleNamespace(), ADDRESS)
    live = SimpleNamespace(is_connected=True)
    bt._client = live

    await bt.connect()

    establish.assert_not_called()
    assert bt._client is live


def test_register_advertisements_only_for_that_adapter(monkeypatch) -> None:
    import habluetooth

    registered = {}
    monkeypatch.setattr(
        habluetooth, "get_manager",
        lambda: SimpleNamespace(
            async_register_advertisement_callback=(
                lambda cb, address: registered.setdefault(address, cb) and None
            )
        ),
    )
    seen = []
    transport.register_advertisements(ADDRESS, HCI0.lower(), seen.append)

    registered[ADDRESS](SimpleNamespace(source=BATH))
    hit = SimpleNamespace(source=HCI0)
    registered[ADDRESS](hit)
    assert seen == [hit]


def test_fixed_scanner_unknown_falls_back_to_source(monkeypatch, ha_choice) -> None:
    _cls, client = _client(HCI0)

    with pytest.raises(transport.BleakError) as err:
        client._async_get_best_available_backend_and_device(_manager())

    assert str(err.value) == (
        f"Bluetooth adapter {HCI0} does not receive {ADDRESS}; received by: none"
    )


def test_fixed_scanner_without_slot_fails(monkeypatch, ha_choice) -> None:
    cls, client = _client(HCI0)
    monkeypatch.setattr(
        cls, "_async_get_backend_for_ble_device",
        lambda self, manager, scanner, device: None,
    )
    manager = _manager(_path("hci0", HCI0, -60), _path("bath-proxy", BATH, -45))

    with pytest.raises(transport.BleakError) as err:
        client._async_get_best_available_backend_and_device(manager)

    assert str(err.value) == (
        f"Bluetooth adapter hci0 (-60 dBm) has no free connection slot for {ADDRESS}"
    )
    assert ha_choice.calls == []


def test_changed_private_hook_lets_ha_pick(monkeypatch, ha_choice) -> None:
    cls, client = _client(HCI0)

    def incompatible(self, manager, scanner, device):
        raise TypeError("changed signature")

    monkeypatch.setattr(cls, "_async_get_backend_for_ble_device", incompatible)
    manager = _manager(_path("hci0", HCI0, -60))

    selected = client._async_get_best_available_backend_and_device(manager)

    assert selected is ha_choice.backend


async def test_transport_connects_with_fixed_class(monkeypatch) -> None:
    options = {CONF_BLUETOOTH_ADAPTER: HCI0}
    used = []

    async def fake_establish(cls, device, name, **kwargs):
        used.append(cls)
        # Not connected, so the second connect() is not skipped.
        return SimpleNamespace(is_connected=False, _connected_scanner=None)

    monkeypatch.setattr(transport, "bleak_establish", fake_establish)
    monkeypatch.setattr(
        transport, "async_last_service_info",
        lambda hass, address: SimpleNamespace(device=SimpleNamespace(address=ADDRESS)),
    )
    monkeypatch.setattr(transport, "describe_connection_path", lambda *a: "hci0")
    bt = transport.BleakTransport(
        SimpleNamespace(), ADDRESS,
        bluetooth_adapter=lambda: options.get(CONF_BLUETOOTH_ADAPTER),
    )

    await bt.connect()
    options[CONF_BLUETOOTH_ADAPTER] = BLUETOOTH_ADAPTER_AUTO
    await bt.connect()

    assert used[0] is transport.fixed_scanner_client_class(HCI0)
    assert used[1] is transport.BleakClient


# --- Diagnostic sensor: automatic vs. fixed ---------------------------------

from custom_components.philips_sonicare_ble import sensor as sensor_mod


def _selection_sensor(options: dict):
    sensor = sensor_mod.SonicareAdapterSelectionSensor.__new__(
        sensor_mod.SonicareAdapterSelectionSensor
    )
    sensor.hass = MagicMock()
    sensor._source = options.get(CONF_BLUETOOTH_ADAPTER, BLUETOOTH_ADAPTER_AUTO)
    return sensor


def test_selection_sensor_automatic_without_option() -> None:
    sensor = _selection_sensor({})
    assert sensor.native_value == "automatic"
    assert sensor.extra_state_attributes is None


def test_selection_sensor_automatic_explicit() -> None:
    sensor = _selection_sensor({CONF_BLUETOOTH_ADAPTER: BLUETOOTH_ADAPTER_AUTO})
    assert sensor.native_value == "automatic"


def test_selection_sensor_fixed_names_the_adapter(monkeypatch) -> None:
    monkeypatch.setattr(
        sensor_mod, "_scanner_name_by_source",
        lambda hass, source: "atom-s3r (98:88:E0:0E:DA:D2)",
    )
    sensor = _selection_sensor({CONF_BLUETOOTH_ADAPTER: "98:88:E0:0E:DA:D2"})
    assert sensor.native_value == "fixed"
    assert sensor.extra_state_attributes == {
        "fixed_adapter": "atom-s3r (98:88:E0:0E:DA:D2)",
        "source": "98:88:E0:0E:DA:D2",
    }


def test_selection_sensor_fixed_offline_adapter_falls_back_to_source(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        sensor_mod, "_scanner_name_by_source", lambda hass, source: None
    )
    sensor = _selection_sensor({CONF_BLUETOOTH_ADAPTER: "98:88:E0:0E:DA:D2"})
    assert sensor.native_value == "fixed"
    assert sensor.extra_state_attributes["fixed_adapter"] == "98:88:E0:0E:DA:D2"


# --- Adapter sensor: a fixed adapter is shown while disconnected ------------


def _adapter_sensor(options: dict, connection_path: str | None):
    sensor = sensor_mod.SonicareAdapterSensor.__new__(sensor_mod.SonicareAdapterSensor)
    sensor.hass = MagicMock()
    sensor.coordinator = SimpleNamespace(
        transport=SimpleNamespace(connection_path=connection_path)
    )
    sensor._fixed_source = options.get(CONF_BLUETOOTH_ADAPTER, BLUETOOTH_ADAPTER_AUTO)
    return sensor


def test_adapter_sensor_automatic_disconnected_is_unknown() -> None:
    assert _adapter_sensor({}, None).native_value is None


def test_adapter_sensor_connected_shows_live_path() -> None:
    sensor = _adapter_sensor(
        {CONF_BLUETOOTH_ADAPTER: "00:0A:CD:46:B2:2D"}, "hci0 (00:0A:CD:46:B2:2D)"
    )
    assert sensor.native_value == "hci0 (00:0A:CD:46:B2:2D)"


def test_adapter_sensor_fixed_disconnected_names_fixed_adapter(monkeypatch) -> None:
    monkeypatch.setattr(
        sensor_mod, "_scanner_name_by_source",
        lambda hass, source: "atom-s3r (98:88:E0:0E:DA:D2)",
    )
    sensor = _adapter_sensor({CONF_BLUETOOTH_ADAPTER: "98:88:E0:0E:DA:D2"}, None)
    assert sensor.native_value == "atom-s3r (98:88:E0:0E:DA:D2)"


def test_adapter_sensor_fixed_offline_adapter_shows_mac(monkeypatch) -> None:
    monkeypatch.setattr(sensor_mod, "_scanner_name_by_source", lambda hass, source: None)
    sensor = _adapter_sensor({CONF_BLUETOOTH_ADAPTER: "98:88:E0:0E:DA:D2"}, None)
    assert sensor.native_value == "98:88:E0:0E:DA:D2"
