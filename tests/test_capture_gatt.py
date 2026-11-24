"""GATT capture through the ESP bridge (list_gatt_table + capture_gatt).

The bridge sends one event per service with "uuid/props/handle" entries; the
integration turns them into a fixture-shaped, anonymized snapshot. A capture
rebuilt from an existing fixture must match that fixture's layout.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.philips_sonicare_ble import capture
from custom_components.philips_sonicare_ble.const import CHAR_BRUSHHEAD_SERIAL
from custom_components.philips_sonicare_ble.transport import parse_service_chars

from .conftest import load_json_fixture

_LETTERS = {"read": "R", "write": "W", "write-without-response": "w",
            "notify": "N", "indicate": "I"}
SERIAL = "00002a25-0000-1000-8000-00805f9b34fb"
SYSTEM_ID = "00002a23-0000-1000-8000-00805f9b34fb"
BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
MODEL = "00002a24-0000-1000-8000-00805f9b34fb"

CAPTURED_FIXTURES = [
    "classic_hx6340_kids.json",
    "classic_hx960x_expertclean.json",
    "classic_hx992x.json",
    "classic_hx999x_premium_head.json",
    "classic_hx999x_prestige.json",
    "condor_hx742x.json",
]


def _bridge_table(fixture: dict) -> list[dict]:
    """The services of a fixture as the bridge reports them, parsed."""
    return [
        {
            "uuid": svc["uuid"],
            "characteristics": parse_service_chars(",".join(
                "{}/{}/{:04x}".format(
                    c["uuid"].upper(),
                    "".join(_LETTERS[p] for p in c["properties"] if p in _LETTERS),
                    # fixtures hold the declaration handle, the bridge sends
                    # the value handle
                    c["handle"] + 1,
                )
                for c in svc["characteristics"]
            )),
        }
        for svc in fixture["gatt_services"]
    ]


def test_parse_service_chars() -> None:
    chars = parse_service_chars(
        "477EA600-A260-11E4-AE37-0002A5D54010/RI/0023,0x2A19/RN/0032,abc/,0x2A24/R"
    )
    assert chars[0] == {
        "uuid": "477ea600-a260-11e4-ae37-0002a5d54010",
        "properties": ["read", "indicate"],
        "value_handle": 0x23,
    }
    assert chars[1]["uuid"] == BATTERY
    assert chars[2] == {"uuid": "abc", "properties": [], "value_handle": None}
    # bridges before 1.16.0 send no handle
    assert chars[3] == {"uuid": MODEL, "properties": ["read"], "value_handle": None}


@pytest.mark.parametrize("fixture_name", CAPTURED_FIXTURES)
def test_capture_matches_fixture_layout(fixture_name: str) -> None:
    fixture = load_json_fixture(fixture_name)
    values = {
        c["uuid"]: bytes.fromhex(c["value_hex"])
        for s in fixture["gatt_services"] for c in s["characteristics"]
        if c.get("value_hex") is not None
    }

    snap = capture.build_snapshot(_bridge_table(fixture), values)

    assert snap["address"] == capture.FIXTURE_MAC
    assert snap["protocol"].split()[0] == fixture["protocol"].split()[0]
    assert snap["device_info"]["Model Number"] == fixture["device_info"]["Model Number"]

    def layout(snapshot: dict) -> dict:
        return {
            c["uuid"]: (tuple(c["properties"]), c["handle"], c.get("value_hex"),
                        c.get("value_text"))
            for s in snapshot["gatt_services"] for c in s["characteristics"]
            # the fixtures were anonymized by hand, not all the same way
            if c["uuid"] not in capture.PRIVATE_CHARS
        }

    assert layout(snap) == layout(fixture)


OTHER = "477ea600-a260-11e4-ae37-0002a5d54410"


def _table(*uuids: str) -> list[dict]:
    return [{
        "uuid": "0000180a-0000-1000-8000-00805f9b34fb",
        "characteristics": [
            {"uuid": u, "properties": ["read"], "value_handle": i + 1}
            for i, u in enumerate(uuids)
        ],
    }]


def test_capture_anonymizes() -> None:
    snap = capture.build_snapshot(
        _table(SERIAL, SYSTEM_ID, CHAR_BRUSHHEAD_SERIAL, OTHER),
        {
            SERIAL: b"SN123456",
            SYSTEM_ID: bytes.fromhex("0102030405060708"),
            CHAR_BRUSHHEAD_SERIAL: bytes.fromhex("04506dd2ae2291"),
            # an unknown characteristic mirroring MAC (reversed) and serial
            OTHER: bytes.fromhex("ff665544332211") + b"SN123456" + b"11:22:33:44:55:66",
        },
        addresses=["11:22:33:44:55:66", None],
    )
    chars = {c["uuid"]: c for c in snap["gatt_services"][0]["characteristics"]}
    assert chars[SERIAL]["value_text"] == "00000000"
    assert chars[SERIAL]["value_hex"] == b"00000000".hex()
    assert chars[SYSTEM_ID]["value_hex"] == "00" * 8
    # non-zero placeholder: all zeros would mean "no chip read"
    assert chars[CHAR_BRUSHHEAD_SERIAL]["value_hex"] == "04" + "aa" * 6
    assert chars[OTHER]["value_hex"] == "ff" + "00" * 6 + "00" * 8 + "00" * 17
    assert snap["device_info"]["Serial Number"] == "00000000"
    assert "SN123456" not in str(snap)


MAC = "AA:BB:CC:0D:EE:F1"


@pytest.mark.parametrize(
    "mirrored",
    [
        bytes.fromhex("aabbcc0deef1"),                 # raw
        bytes.fromhex("f1ee0dccbbaa"),                 # raw, reversed
        bytes.fromhex("aabbccfffe0deef1"),             # EUI-64
        b"AA:BB:CC:0D:EE:F1",                          # text
        b"aa:bb:cc:0d:ee:f1",
        b"Aa:bB:cc:0D:ee:F1",                          # mixed case
        b"AA-BB-CC-0D-EE-F1",
        b"aabbcc0deef1",
        b"F1EE0DCCBBAA",                               # text, reversed
    ],
)
def test_mac_is_blanked_in_any_form(mirrored: bytes) -> None:
    values = capture.scrub_values({OTHER: b"<" + mirrored + b">"}, [MAC])
    assert values[OTHER] == b"<" + bytes(len(mirrored)) + b">"


def test_padded_serial_is_blanked_without_padding() -> None:
    values = capture.scrub_values(
        {SERIAL: b"SN123456\x00\x00", OTHER: b"xxSN123456yy"}, []
    )
    assert values[OTHER] == b"xx" + bytes(8) + b"yy"


def test_brushhead_serial_mirrored_as_hex_text() -> None:
    values = capture.scrub_values(
        {CHAR_BRUSHHEAD_SERIAL: bytes.fromhex("04506dd2ae2291"),
         OTHER: b"id=04:50:6D:D2:AE:22:91"},
        [],
    )
    assert values[OTHER] == b"id=" + bytes(20)
    assert values[CHAR_BRUSHHEAD_SERIAL] == bytes.fromhex("04aaaaaaaaaaaa")


@pytest.mark.parametrize(
    "serial",
    [b"0000", b"0000000000", b" " * 8, bytes.fromhex("04000000000000")],
)
def test_placeholder_serials_do_not_blank_other_values(serial: bytes) -> None:
    """A blank or placeholder serial must not eat ordinary data."""
    other = b"FW 1.0000.2 HX6340          " + bytes.fromhex("0400000000000000")
    values = capture.scrub_values({SERIAL: serial, OTHER: other}, [])
    assert values[OTHER] == other


def test_zero_brushhead_serial_stays_zero() -> None:
    snap = capture.build_snapshot(
        _table(CHAR_BRUSHHEAD_SERIAL), {CHAR_BRUSHHEAD_SERIAL: bytes(7)}
    )
    char = snap["gatt_services"][0]["characteristics"][0]
    assert char["value_hex"] == "00" * 7


def test_capture_converts_to_declaration_handle() -> None:
    snap = capture.build_snapshot(_table(MODEL), {MODEL: b"HX992X"})
    assert snap["gatt_services"][0]["characteristics"][0]["handle"] == 0


def test_value_text_only_for_text_characteristics() -> None:
    snap = capture.build_snapshot(
        _table(BATTERY, MODEL), {BATTERY: b"\x5a", MODEL: b"HX992X\x00"}
    )
    text = {c["uuid"]: c["value_text"] for c in snap["gatt_services"][0]["characteristics"]}
    assert text == {BATTERY: None, MODEL: "HX992X"}
    assert snap["device_info"]["Battery Level"] == "90%"


def test_readable_chars() -> None:
    services = _bridge_table(load_json_fixture("classic_hx992x.json"))
    readable = capture.readable_chars(services)
    props = {c["uuid"]: c["properties"] for s in services for c in s["characteristics"]}
    assert readable and all("read" in props[u] for u in readable)
    assert len(readable) == len(set(readable))


class _FakeTransport:
    def __init__(self, values: dict[str, bytes], drop_after: int | None = None) -> None:
        self.values = values
        self.drop_after = drop_after
        self.batches: list[int] = []
        self.single: list[str] = []
        self.reads = 0

    @property
    def is_connected(self) -> bool:
        return self.drop_after is None or self.reads < self.drop_after

    async def read_chars(self, uuids):
        self.batches.append(len(uuids))
        self.reads += len(uuids)
        return {u: self.values.get(u) for u in uuids}

    async def read_char(self, uuid):
        self.single.append(uuid)
        return self.values.get(uuid)


async def test_read_values_in_batches_and_retries_gaps() -> None:
    uuids = [f"u{i}" for i in range(30)]
    transport = _FakeTransport({u: b"\x01" for u in uuids[:-1]})
    values = await capture.async_read_values(transport, uuids)
    assert transport.batches == [12, 12, 6]
    assert transport.single == ["u29"]
    assert values["u0"] == b"\x01" and values["u29"] is None


async def test_read_values_keep_empty_values() -> None:
    """A 0-byte read is a value (b""), not an unreadable characteristic."""
    transport = _FakeTransport({"u0": b"\x01"})
    transport.read_was_empty = lambda uuid: uuid == "u1"
    values = await capture.async_read_values(transport, ["u0", "u1", "u2"])
    assert values == {"u0": b"\x01", "u1": b"", "u2": None}
    assert transport.single == ["u2"]


async def test_read_values_stop_on_disconnect() -> None:
    uuids = [f"u{i}" for i in range(30)]
    transport = _FakeTransport({}, drop_after=12)
    await capture.async_read_values(transport, uuids)
    assert transport.batches == [12]
    assert transport.single == []


def _bridge_transport(events: list[dict], bridge_id: str = ""):
    import asyncio

    from custom_components.philips_sonicare_ble.transport import EspBridgeTransport

    handlers = []

    async def _call(domain, service, data, blocking=False):
        for ev in events:
            for h in list(handlers):
                h(SimpleNamespace(data=ev))

    class _T(EspBridgeTransport):
        is_connected = True

    t = _T.__new__(_T)
    t._hass = SimpleNamespace(
        loop=asyncio.get_running_loop(),
        bus=SimpleNamespace(
            async_listen=lambda et, h: handlers.append(h) or (lambda: handlers.remove(h))
        ),
        services=SimpleNamespace(async_call=_call),
    )
    t._device_name = "atom"
    t._esp_bridge_id = bridge_id
    t._detected_mac = None  # setup flow: mac not known yet
    t._bond_address = None
    t._listed_char_services = {}
    t.prefer_listed_services = False
    t.last_list_error = None
    t.char_service_overrides = {}
    return t


def _svc_event(index, count, uuid, chars, **extra):
    return {"mac": "24:E5:AA:00:00:01", "service_count": str(count),
            "service_index": str(index), "service_uuid": uuid,
            "service_chars": chars, **extra}


async def test_list_gatt_table_maps_unknown_chars() -> None:
    """Characteristics outside CHAR_SERVICE_MAP resolve through the table."""
    t = _bridge_transport([_svc_event(0, 1, "0x1800", "0x2A00/R/0003")])
    table = await t.list_gatt_table(timeout=1)
    assert table[0]["characteristics"][0]["value_handle"] == 3
    assert t._get_service_uuid("00002a00-0000-1000-8000-00805f9b34fb") == (
        "00001800-0000-1000-8000-00805f9b34fb"
    )


async def test_prefer_listed_services_overrides_map() -> None:
    """A capture reads each characteristic where the table lists it."""
    from custom_components.philips_sonicare_ble.const import (
        CHAR_SESSION_DATA,
        SVC_KIDS_SESSION,
        SVC_STORAGE,
    )

    t = _bridge_transport([
        _svc_event(0, 1, SVC_KIDS_SESSION.upper(), f"{CHAR_SESSION_DATA.upper()}/R/0058"),
    ])
    await t.list_gatt_table(timeout=1)
    assert t._get_service_uuid(CHAR_SESSION_DATA) == SVC_STORAGE
    t.prefer_listed_services = True
    assert t._get_service_uuid(CHAR_SESSION_DATA) == SVC_KIDS_SESSION


async def test_events_of_other_slot_are_ignored() -> None:
    """Slot B's answer (error or table) must not end slot A's wait."""
    t = _bridge_transport(
        [
            {"bridge_id": "b", "mac": "11:22:33:44:55:66", "error": "not_connected"},
            {**_svc_event(0, 1, "0x180F", "0x2A19/RN/0032"), "bridge_id": "b"},
            {**_svc_event(0, 1, "0x1800", "0x2A00/R/0003"), "bridge_id": "a"},
        ],
        bridge_id="a",
    )
    table = await t.list_gatt_table(timeout=1)
    assert [s["uuid"] for s in table] == ["00001800-0000-1000-8000-00805f9b34fb"]


async def test_error_reason_and_incomplete_service() -> None:
    t = _bridge_transport([{"mac": "24:E5:AA:00:00:01", "error": "no_memory"}])
    assert await t.list_gatt_table(timeout=1) is None
    assert t.last_list_error == "no_memory"

    t = _bridge_transport([_svc_event(0, 1, "0x180F", "", chars_error="no_memory")])
    table = await t.list_gatt_table(timeout=1)
    assert table[0]["chars_error"] == "no_memory"
    assert table[0]["characteristics"] == []
