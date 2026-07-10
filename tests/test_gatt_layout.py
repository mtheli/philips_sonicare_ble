"""Structural GATT-layout checks against real device captures.

Unlike the golden-decode tests these need no characteristic values — only the
service/characteristic table with properties. That lets community-provided
scans (e.g. a LightBlue log converted via ``scripts/lightblue_to_fixture.py``)
pin the integration's expectations against hardware we never had on a desk:

* every characteristic the coordinator polls is readable where present,
* every characteristic it subscribes to supports notify/indicate,
* ``CHAR_SERVICE_MAP`` (used by the ESP bridge to resolve reads) matches the
  service each characteristic actually lives in.
"""

from __future__ import annotations

import pytest

from custom_components.philips_sonicare_ble.const import (
    CHAR_SESSION_ID,
    CHAR_BATTERY_LEVEL,
    CHAR_BRUSHING_TIME,
    CHAR_HANDLE_STATE,
    CHAR_MODEL_NUMBER,
    CHAR_SERVICE_MAP,
    NOTIFICATION_CHARS,
    POLL_READ_CHARS,
)

from .conftest import load_json_fixture

CLASSIC_FIXTURES = [
    "classic_hx6340_kids.json",
    "classic_hx960x_expertclean.json",
    "classic_hx991x_lightblue.json",
    "classic_hx992x.json",
    "classic_hx993x_lightblue.json",
    "classic_hx999x_prestige.json",
]

# Present on every Classic brush seen so far, from Kids to Prestige. A capture
# missing one of these is either truncated or not a Classic device.
CORE_CHARS = [
    CHAR_BATTERY_LEVEL,
    CHAR_MODEL_NUMBER,
    CHAR_HANDLE_STATE,
    CHAR_BRUSHING_TIME,
]

# Per-model chars that appear in NOTIFICATION_CHARS but are read-only on that
# hardware. Subscribing them fails gracefully (warn-log) at runtime.
KNOWN_READ_ONLY = {
    "HX6340": {CHAR_SESSION_ID},
}


def _char_table(snapshot: dict) -> dict[str, tuple[str, set[str]]]:
    """Flatten a snapshot into ``{char_uuid: (service_uuid, properties)}``."""
    table: dict[str, tuple[str, set[str]]] = {}
    for service in snapshot["gatt_services"]:
        for char in service["characteristics"]:
            table[char["uuid"]] = (service["uuid"], set(char["properties"]))
    return table


@pytest.fixture(params=CLASSIC_FIXTURES)
def classic_snapshot(request) -> dict:
    return load_json_fixture(request.param)


def test_core_chars_present(classic_snapshot):
    table = _char_table(classic_snapshot)
    missing = [uuid for uuid in CORE_CHARS if uuid not in table]
    assert not missing


def test_poll_chars_are_readable(classic_snapshot):
    """Everything in the poll list that the device exposes must be readable."""
    table = _char_table(classic_snapshot)
    unreadable = [
        uuid
        for uuid in POLL_READ_CHARS
        if uuid in table and "read" not in table[uuid][1]
    ]
    assert not unreadable


def test_notification_chars_can_notify(classic_snapshot):
    """Subscription targets must support notify or indicate where present.

    Mirrors the coordinator's setup pruning: a char whose mapped service is
    absent on the device is never subscribed, so it doesn't need notify here.
    Chars in KNOWN_READ_ONLY are tolerated: the coordinator's subscribe batch
    warn-logs individual failures and carries on, so a read-only char costs a
    log line, not functionality.
    """
    table = _char_table(classic_snapshot)
    services = {s["uuid"] for s in classic_snapshot["gatt_services"]}
    model = classic_snapshot["device_info"]["Model Number"].strip()
    quirks = KNOWN_READ_ONLY.get(model, set())
    silent = [
        uuid
        for uuid in NOTIFICATION_CHARS
        if uuid in table
        and uuid not in quirks
        and CHAR_SERVICE_MAP.get(uuid) in services
        and not table[uuid][1] & {"notify", "indicate"}
    ]
    assert not silent


def test_char_service_map_matches_hardware(classic_snapshot):
    """The bridge resolves characteristics via CHAR_SERVICE_MAP; wherever the
    mapped service exists on the device, the characteristic must live there.

    Devices that expose a characteristic under a *different* service (the Kids
    brushes keep session chars in an older service) also lack the mapped
    service, so the coordinator's service filter drops those chars at setup —
    the map is only ever consulted for services the device advertised.
    """
    table = _char_table(classic_snapshot)
    services = {s["uuid"] for s in classic_snapshot["gatt_services"]}
    mismatched = {
        uuid: (found_service, CHAR_SERVICE_MAP[uuid])
        for uuid, (found_service, _) in table.items()
        if uuid in CHAR_SERVICE_MAP
        and CHAR_SERVICE_MAP[uuid] in services
        and found_service != CHAR_SERVICE_MAP[uuid]
    }
    assert not mismatched


def test_hx993x_layout_matches_hx992x():
    """The HX993X capture (community LightBlue log) exposes every
    integration-relevant characteristic the tested HX992X has — the two
    families share the same GATT layout."""
    hx992x = _char_table(load_json_fixture("classic_hx992x.json"))
    hx993x = _char_table(load_json_fixture("classic_hx993x_lightblue.json"))
    relevant = (set(POLL_READ_CHARS) | set(NOTIFICATION_CHARS)) & set(hx992x)
    missing = sorted(uuid for uuid in relevant if uuid not in hx993x)
    assert not missing
