"""The handle's own record of a finished session.

A session is only fully described after it has ended, and the live values
say the least at exactly that moment: the handle wipes its timer as it
stops. The record it keeps instead is what these tests are about - decoding
it, asking for it at the right time, and not losing it over a restart.

The decoded values here come from a capture taken on a handle reporting
model HX999X, where the same recording also carried the live characteristics
of that session, so every field has a second source.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from custom_components.philips_sonicare_ble.classic_protocol import (
    decode_session_record,
)

from .conftest import load_json_fixture

CAPTURE = load_json_fixture("classic_hx999x_stored_session.json")
RECORD = bytes.fromhex(CAPTURE["stored_session"]["routine_record"])


def _u16(hex_str: str) -> int:
    return int.from_bytes(bytes.fromhex(hex_str)[:2], "little")


def test_the_record_agrees_with_the_live_session_it_describes():
    """Every decoded field, against the same session as it was watched.

    The capture holds both halves: what the handle reported while brushing,
    and what it later said about that session when asked. Pinning the record
    against the live readings is what makes the layout a measurement rather
    than a reading of the bytes that happens to look sensible - a wrong
    offset produces a plausible number, not an error.
    """
    live = CAPTURE["live"]
    out = decode_session_record(RECORD, CAPTURE["model"], chunked=True)

    assert out["session_id"] == _u16(live["latest_session_id"])
    assert out["duration"] == live["brushing_time_peak_seconds"]
    assert out["routine_length"] == _u16(live["routine_length"])
    # A Prestige reports its selected routine as an id of its own; that is
    # the reading the record has to match, not the other mode characteristic.
    assert out["brushing_mode_value"] == int(live["available_routine_ids"], 16)
    assert out["intensity_value"] == int(live["intensity"], 16)


def test_the_transfer_header_announces_the_record():
    """The reply that precedes a record says how long it will be.

    Length plus the leading byte the decoder skips - which is why a record
    is one byte longer than announced, and why a short answer is refused
    rather than decoded.
    """
    header = bytes.fromhex(CAPTURE["stored_session"]["transfer_header"])
    announced = int.from_bytes(header[2:6], "little")
    assert int.from_bytes(header[0:2], "little") == _u16(
        CAPTURE["live"]["latest_session_id"])
    assert len(RECORD) == announced + 1


def test_decodes_the_captured_record():
    out = decode_session_record(RECORD, "HX999X", chunked=True)
    assert out["session_id"] == 335
    assert out["duration"] == 160
    assert out["routine_length"] == 160
    assert out["intensity"] == "low"


def test_mode_follows_the_model_not_the_offset():
    """Where the mode sits in the record differs per handle.

    The models that report their selected routine as an id read it from one
    byte, the rest read the mode index from another. Both are plausible
    numbers, so picking the wrong one is silently wrong rather than an
    error - which is why it is pinned here.
    """
    routine_id_model = decode_session_record(RECORD, "HX999X", chunked=True)
    index_model = decode_session_record(RECORD, "HX992X", chunked=True)
    assert routine_id_model["brushing_mode_value"] == 1
    assert index_model["brushing_mode_value"] == 0
    assert routine_id_model["brushing_mode"] != index_model["brushing_mode"]


@pytest.mark.parametrize("payload", [b"", RECORD[:13], bytes(13)])
def test_an_answer_too_short_is_not_a_record(payload):
    """Short answers are refused rather than decoded into zeroes.

    The offsets reach into byte 13, so anything shorter would read past the
    end or silently produce a session of zero seconds.
    """
    assert decode_session_record(payload, "HX999X", chunked=True) is None


def test_timestamp_is_the_handles_own_count():
    """The record carries no wall clock, and stamps the start.

    The handle counts from its own start, so the value is only meaningful
    against another reading of the same counter. And it marks when the
    session began: the captured session ran 14:51:03 to 14:53:13, and the
    stamp plus the duration is what lands on the end.
    """
    out = decode_session_record(RECORD, "HX999X", chunked=True)
    assert out["timestamp"] == 453226
    assert out["timestamp"] + out["duration"] == 453386


# ── The exchange ────────────────────────────────────────────────────────────

class FakeTransport:
    """A handle that answers the select-then-transfer exchange.

    It answers the way a handle does: the record on the data characteristic,
    and then, separately, the status on the control point that says the
    transfer is over. A caller that mistakes the first for the second is
    talking over the handle, which is what this stands in for.
    """

    def __init__(self, record: bytes | None = RECORD, latest: int = 335,
                 status: int = 0):
        self.record = record
        self.status = status
        self.kind = 0
        self.latest = latest
        self.writes: list[tuple[str, bytes]] = []
        self.subscribed: set[str] = set()
        self._cbs: dict[str, object] = {}
        self.is_connected = True

    async def read_char(self, uuid):
        return self.latest.to_bytes(2, "little")

    async def subscribe(self, uuid, cb):
        self.subscribed.add(uuid)
        self._cbs[uuid[-4:]] = cb

    async def unsubscribe(self, uuid):
        self.subscribed.discard(uuid)
        self._cbs.pop(uuid[-4:], None)

    def _notify(self, suffix: str, data: bytes) -> None:
        cb = self._cbs.get(suffix)
        if cb:
            cb(f"477ea600-a260-11e4-ae37-0002a5d5{suffix}", data)

    async def write_char(self, uuid, data):
        self.writes.append((uuid[-4:], bytes(data)))
        if uuid.endswith("40d5"):
            self.kind = data[0]
        # The handle answers once the transfer is started, not before.
        if uuid.endswith("4110") and data[0] == 0 and self.record is not None:
            self._notify("4100", self.record)
            self._notify("4110", bytes([self.status]))


def _protocol(transport):
    from custom_components.philips_sonicare_ble.classic_protocol import ClassicProtocol
    proto = ClassicProtocol(transport)
    proto.model = "HX999X"
    return proto


async def test_fetch_selects_then_transfers():
    """Which session, which kind, then start - in that order."""
    transport = FakeTransport()
    out = await _protocol(transport).fetch_stored_session()
    assert out["session_id"] == 335
    assert [u for u, _ in transport.writes] == ["40e0", "40d5", "4110"]
    assert transport.writes[0][1] == (335).to_bytes(2, "little")
    assert transport.writes[1][1] == b"\x00"


async def test_the_data_and_the_control_point_are_both_listened_to():
    """A record is not complete until the handle says so.

    The data characteristic carries the record and the control point carries
    the status that ends the transfer, so a fetch that subscribes to only one
    of them either misses the record or never learns that it is whole.
    """
    class Watching(FakeTransport):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.log: list[str] = []

        async def subscribe(self, uuid, cb):
            self.log.append("subscribe")
            await super().subscribe(uuid, cb)

        async def unsubscribe(self, uuid):
            self.log.append("unsubscribe")
            await super().unsubscribe(uuid)

        async def write_char(self, uuid, data):
            if uuid.endswith("40d5"):
                self.log.append(f"select kind {data[0]}")
            await super().write_char(uuid, data)

    transport = Watching()
    await _protocol(transport).fetch_stored_session()
    assert transport.log == [
        "subscribe", "subscribe", "select kind 0", "unsubscribe", "unsubscribe",
    ]


async def test_a_transfer_that_went_quiet_is_taken_back():
    """A handle left mid-transfer answers nothing afterwards.

    Silence is not the end of a request: as far as the handle is concerned
    the read is still running, and the next one - this connection or the
    next - goes unanswered until somebody ends it.
    """
    transport = FakeTransport(record=None)
    await _protocol(transport).fetch_stored_session(timeout=0.2)
    assert transport.writes[-1] == ("4110", b"\x02")


async def test_a_sequence_that_broke_off_is_taken_back_too():
    """A transfer is left open by more than a silent handle.

    A write refused halfway through the sequence leaves the handle exactly
    as a timeout does, and the cost is the same: nothing it is asked
    afterwards gets an answer.
    """
    class Refusing(FakeTransport):
        async def write_char(self, uuid, data):
            if uuid.endswith("4110") and data[0] == 0:
                self.writes.append((uuid[-4:], bytes(data)))
                raise RuntimeError("write refused")
            await super().write_char(uuid, data)

    transport = Refusing()
    assert await _protocol(transport).fetch_stored_session(timeout=0.2) is None
    assert transport.writes[-1] == ("4110", b"\x02")


async def test_a_short_record_the_handle_admits_to_is_still_an_answer():
    """The handle sends what it has and says the record is incomplete.

    That is a status, not a failure - waiting past it would cost the record
    that did arrive.
    """
    transport = FakeTransport(status=1)
    out = await _protocol(transport).fetch_stored_session(timeout=0.2)
    assert out["session_id"] == 335


async def test_fetch_leaves_no_subscription_behind():
    """The data characteristic stays quiet outside a transfer."""
    transport = FakeTransport()
    await _protocol(transport).fetch_stored_session()
    assert not transport.subscribed


async def test_a_silent_handle_yields_nothing():
    """No answer is not an error, and must not hang the caller."""
    transport = FakeTransport(record=None)
    out = await _protocol(transport).fetch_stored_session(timeout=0.05)
    assert out is None
    assert not transport.subscribed


async def test_a_short_answer_yields_nothing():
    transport = FakeTransport(record=b"\x00\x01")
    assert await _protocol(transport).fetch_stored_session(timeout=0.5) is None


# ── The entity ──────────────────────────────────────────────────────────────

def _last_session_sensor(record):
    from types import SimpleNamespace
    from custom_components.philips_sonicare_ble.sensor import (
        SonicareLastSessionSensor,
    )
    sensor = SonicareLastSessionSensor.__new__(SonicareLastSessionSensor)
    sensor.coordinator = SimpleNamespace(data={"last_session": record})
    return sensor


def test_entity_reports_when_the_session_began():
    """The state is a time, so "when did I last brush" has an answer.

    A duration could not be a timestamp device class, and the handle's own
    counter is not wall time. The start is what the handle stamps on the
    record; the end is that plus ``duration_seconds``, which is beside it.
    """
    sensor = _last_session_sensor(
        {**decode_session_record(RECORD, "HX999X", chunked=True),
         "started_at": "2026-08-16T12:53:14+00:00"}
    )
    value = sensor.native_value
    assert isinstance(value, datetime)
    assert value == datetime(2026, 8, 16, 12, 53, 14, tzinfo=timezone.utc)
    assert sensor.extra_state_attributes["duration_seconds"] == 160
    assert sensor.extra_state_attributes["target_duration_seconds"] == 160


def test_the_record_survives_being_stored_and_read_back():
    """The store keeps JSON, not objects.

    A datetime handed to it comes back as text, and a timestamp entity that
    returns text is broken in a way no mock notices - so the time is kept as
    text throughout and parsed on the way out.
    """
    import json
    record = {**decode_session_record(RECORD, "HX999X", chunked=True),
              "started_at": datetime(2026, 8, 16, 12, 53, 14,
                                   tzinfo=timezone.utc).isoformat()}
    round_tripped = json.loads(json.dumps({"last_session": record}))["last_session"]
    assert round_tripped == record
    assert isinstance(_last_session_sensor(round_tripped).native_value, datetime)


def test_no_record_leaves_the_entity_unknown():
    assert _last_session_sensor(None).native_value is None
    assert _last_session_sensor(None).extra_state_attributes is None


def test_the_handle_clock_places_a_session_found_later():
    """A session nobody watched end still gets a real time.

    Its record is stamped with the handle's own counter, which starts at
    zero and never learns the date. Reading that counter again at a known
    moment turns the stamp into a time: the difference is the age. Without
    it the entity could only report when it looked, which for a session run
    hours ago reads as "just now" - wrong rather than vague.
    """
    from custom_components.philips_sonicare_ble.coordinator import (
        PhilipsSonicareCoordinator,
    )
    coordinator = PhilipsSonicareCoordinator.__new__(PhilipsSonicareCoordinator)
    coordinator.address = "AA:BB:CC:DD:EE:FF"

    # The measured case: a session stamped at 453226 that ran 160 s, with
    # the counter reading 464479 when the record was fetched. Both readings
    # come off the same counter, so the stamp's age is 464479 - 453226 -
    # and that is when the session began.
    record = {**decode_session_record(RECORD, "HX999X", chunked=True), "handle_clock": 464479}
    started_at, source = coordinator._session_started_at(record, witnessed=False)
    assert source == "handle_clock"
    age = datetime.now(timezone.utc) - datetime.fromisoformat(started_at)
    assert 11250 < age.total_seconds() < 11260
    # Adding the duration puts the end where counting the end directly used
    # to: a session that ran 160 s finished 11093 s ago.
    assert 11090 < age.total_seconds() - 160 < 11100

    # Watching it happen beats any calculation - but the state is still the
    # start, so it is one session back from now rather than now.
    started_at, source = coordinator._session_started_at(record, witnessed=True)
    assert source == "session_end"
    since = (datetime.now(timezone.utc)
             - datetime.fromisoformat(started_at)).total_seconds()
    assert 160 <= since < 165


@pytest.mark.parametrize("clock", [None, 453225, 453226 + 500 * 86400])
def test_an_implausible_clock_is_not_trusted(clock):
    """A counter behind the stamp, or absurdly ahead of it, proves nothing.

    Reporting a session in the future or centuries old would be worse than
    admitting the time is only a lower bound.
    """
    from custom_components.philips_sonicare_ble.coordinator import (
        PhilipsSonicareCoordinator,
    )
    coordinator = PhilipsSonicareCoordinator.__new__(PhilipsSonicareCoordinator)
    coordinator.address = "AA:BB:CC:DD:EE:FF"
    record = {**decode_session_record(RECORD, "HX999X", chunked=True)}
    if clock is not None:
        record["handle_clock"] = clock
    _, source = coordinator._session_started_at(record, witnessed=False)
    assert source == "collection"


def test_the_entity_says_where_its_time_came_from():
    base = decode_session_record(RECORD, "HX999X", chunked=True)
    for source in ("session_end", "handle_clock", "collection"):
        sensor = _last_session_sensor(
            {**base, "started_at": "2026-08-16T12:53:14+00:00", "time_source": source}
        )
        assert sensor.extra_state_attributes["time_source"] == source
        assert sensor.extra_state_attributes["duration_seconds"] == 160


# ── When a fetch is triggered ───────────────────────────────────────────────

def _coordinator_with(record):
    from types import SimpleNamespace
    from custom_components.philips_sonicare_ble.coordinator import (
        PhilipsSonicareCoordinator,
    )
    c = PhilipsSonicareCoordinator.__new__(PhilipsSonicareCoordinator)
    c.address = "AA:BB:CC:DD:EE:FF"
    c._use_condor = False
    c.asked = []
    c._start_session_end_task = lambda session_id=None, witnessed=True: (
        c.asked.append((session_id, witnessed))
    )
    data = {"latest_session_id": 335}
    if record is not None:
        data["last_session"] = record
    return c, data


def test_a_session_we_do_not_have_is_fetched():
    c, data = _coordinator_with(None)
    c._update_stored_session({}, data, {"latest_session_id": 335})
    assert c.asked == [(335, False)]


def test_a_session_already_placed_in_time_is_left_alone():
    """Nothing to gain from asking again, on every single connect."""
    for source in ("session_end", "handle_clock"):
        c, data = _coordinator_with({"session_id": 335, "time_source": source})
        c._update_stored_session({}, data, {"latest_session_id": 335})
        assert c.asked == []


@pytest.mark.parametrize("held", [
    {"session_id": 335},                             # written before the clock was read
    {"session_id": 335, "time_source": "collection"},  # the clock did not answer
])
def test_a_session_whose_time_is_unplaced_is_fetched_again(held):
    """A record that only knows when it was collected is worth another try.

    Its state is a time the session demonstrably did not happen at - reading
    the handle's counter once more replaces a wrong answer with a right one.
    """
    c, data = _coordinator_with(held)
    c._update_stored_session({}, data, {"latest_session_id": 335})
    assert c.asked == [(335, False)]


def test_nothing_happens_without_a_fresh_latest_id():
    """Only a reading that just arrived says anything new."""
    c, data = _coordinator_with(None)
    c._update_stored_session({}, data, {})
    assert c.asked == []


# ── A session the handle has not filed yet ──────────────────────────────────

def test_a_record_the_handle_has_outrun_is_marked():
    """Some handles file a session only as they switch off.

    Between the motor stopping and that moment - a minute or more on a Kids
    handle - the record still describes the session *before* the one that
    was just brushed. Anything showing it as "the last session" would be
    showing the wrong one, so it is marked instead.
    """
    sensor = _last_session_sensor(
        {**decode_session_record(RECORD, "HX999X", chunked=True),
         "started_at": "2026-08-16T12:53:14+00:00", "superseded": True}
    )
    assert sensor.extra_state_attributes["superseded"] is True
    # The session it does describe is still worth having.
    assert sensor.extra_state_attributes["duration_seconds"] == 160


def test_a_current_record_is_not_marked():
    sensor = _last_session_sensor(
        {**decode_session_record(RECORD, "HX999X", chunked=True),
         "started_at": "2026-08-16T12:53:14+00:00"}
    )
    assert sensor.extra_state_attributes["superseded"] is False


def test_an_outrun_record_keeps_its_own_time():
    """It must not be dated to the session that has just been brushed.

    Watching a session end normally means "the record is from now". Not
    here: the record is the older one, and the moment being timed belongs
    to the session the handle has yet to write down.
    """
    from custom_components.philips_sonicare_ble.coordinator import (
        PhilipsSonicareCoordinator,
    )
    coordinator = PhilipsSonicareCoordinator.__new__(PhilipsSonicareCoordinator)
    coordinator.address = "AA:BB:CC:DD:EE:FF"
    record = {**decode_session_record(RECORD, "HX999X", chunked=True), "handle_clock": 464479}

    # witnessed, but superseded → dated from the handle's counter, not now.
    _, source = coordinator._session_started_at(record, witnessed=False)
    assert source == "handle_clock"
    _, live_source = coordinator._session_started_at(record, witnessed=True)
    assert live_source == "session_end", "a current record still dates to now"


# ── The Kids handle ─────────────────────────────────────────────────────────
# It keeps the same records without a storage service to put them in: the
# characteristics sit in the brushing service, there is nothing to select a
# kind with, and the data is read rather than notified. A read record has no
# chunk byte in front of it, so its fields sit one byte earlier.

# Captured from a Sonicare for Kids (HX6340, firmware 4.2.2) after a 19 s
# run of a 60 s routine. The brushing timer was observed reaching 19, the
# routine was set to 60, and the handle reported this session as its newest.
KIDS_RECORD = bytes.fromhex("6cda92017a0113003c00020000000000")


def test_a_read_record_is_decoded_without_the_chunk_byte():
    out = decode_session_record(KIDS_RECORD, "HX6340")
    assert out["session_id"] == 378
    assert out["duration"] == 19
    assert out["routine_length"] == 60
    assert out["brushing_mode_value"] == 2
    assert out["intensity_value"] == 0


def test_the_two_shapes_do_not_decode_each_other():
    """The one byte of framing is the whole difference, and it matters.

    Reading a notified record as a read one, or the other way round, shifts
    every field by a byte - which yields numbers rather than an error, so
    nothing would look wrong until somebody compared them to reality.
    """
    # Refused outright rather than decoded into different numbers: a shifted
    # layout produces values no session can have, and that is what gives it
    # away. Believing them would be worse than having nothing.
    assert decode_session_record(KIDS_RECORD, "HX6340", chunked=True) is None
    assert decode_session_record(RECORD, "HX999X") is None


def test_a_kids_handle_is_known_to_keep_sessions():
    """Asking about the storage service would answer no for a Kids handle."""
    from custom_components.philips_sonicare_ble.const import (
        SVC_STORAGE,
        supports_stored_sessions,
        uses_direct_session_read,
    )
    assert supports_stored_sessions("HX6340", set()) is True
    assert uses_direct_session_read("HX6340") is True
    assert supports_stored_sessions("HX999X", {SVC_STORAGE.lower()}) is True
    assert uses_direct_session_read("HX999X") is False
    # A handle with neither is not asked at all.
    assert supports_stored_sessions("HX742X", set()) is False


async def test_the_short_exchange_selects_then_reads():
    """One write, one read - no kind, no control point."""
    class KidsTransport(FakeTransport):
        def __init__(self):
            super().__init__(record=KIDS_RECORD)
            self.reads: list[str] = []

        async def read_char(self, uuid):
            self.reads.append(uuid[-4:])
            if uuid.endswith("40d0"):
                return (378).to_bytes(2, "little")
            if uuid.endswith("4100"):
                return self.record
            return None

    transport = KidsTransport()
    proto = _protocol(transport)
    proto.model = "HX6340"
    proto.direct_session_read = True
    out = await proto.fetch_stored_session()
    assert out["session_id"] == 378
    assert out["duration"] == 19
    assert [u for u, _ in transport.writes] == ["40e0"], transport.writes
    assert "4100" in transport.reads
    assert not transport.subscribed, "nothing to subscribe to on this path"


def test_a_kids_handle_is_addressed_through_its_own_service():
    """The bridge names a service on every operation, so it has to be right.

    A Kids handle keeps the session characteristics in a service of its own
    and has no storage service at all. Addressing the usual one would not
    raise anything - the read would simply come back empty, and the feature
    would look unsupported rather than misconfigured.
    """
    from custom_components.philips_sonicare_ble.const import (
        CHAR_SESSION_DATA,
        KIDS_CHAR_SERVICE_OVERRIDE,
        SVC_KIDS_SESSION,
        SVC_STORAGE,
    )
    from custom_components.philips_sonicare_ble.transport import EspBridgeTransport

    transport = EspBridgeTransport.__new__(EspBridgeTransport)
    # Without the override, every handle is addressed the usual way.
    assert transport._get_service_uuid(CHAR_SESSION_DATA) == SVC_STORAGE
    transport.char_service_overrides = dict(KIDS_CHAR_SERVICE_OVERRIDE)
    assert transport._get_service_uuid(CHAR_SESSION_DATA) == SVC_KIDS_SESSION
    # Characteristics the Kids handle keeps in the usual place are untouched.
    from custom_components.philips_sonicare_ble.const import CHAR_HANDLE_STATE, SVC_SONICARE
    assert transport._get_service_uuid(CHAR_HANDLE_STATE) == SVC_SONICARE


# ── Keeping the sensor stream out of it ─────────────────────────────────────
# Fetching a stored record and tearing down the live sensor stream both
# happen when a session ends, and both need the link - but they are separate
# concerns, and tying them together broke the teardown in three ways at once.

def _coordinator_for_session_end(condor=False, subscribed=True, busy=False):
    import asyncio
    from types import SimpleNamespace
    from custom_components.philips_sonicare_ble.coordinator import (
        PhilipsSonicareCoordinator,
    )
    c = PhilipsSonicareCoordinator.__new__(PhilipsSonicareCoordinator)
    c._use_condor = condor
    c._sensor_subscribed = subscribed
    c.torn_down = 0
    c.fetches = 0
    c._session_task = SimpleNamespace(done=lambda: not busy) if busy else None
    c.hass = SimpleNamespace(async_create_task=lambda coro: (coro.close(),
                                                            c.__dict__.__setitem__(
                                                                "torn_down",
                                                                c.torn_down + 1)))
    c._unsubscribe_sensor_data = lambda: asyncio.sleep(0)
    c._start_session_end_task = lambda **kw: (
        None if condor or busy else c.__dict__.__setitem__("fetches", c.fetches + 1)
    )
    return c


def _end_of_session(c):
    """What _apply_parsed does when brushing_state leaves "on"."""
    if c._sensor_subscribed:
        c.hass.async_create_task(c._unsubscribe_sensor_data())
    c._start_session_end_task()


def test_a_handle_that_keeps_no_records_still_releases_the_stream():
    """Condor keeps no stored sessions, but it does stream sensor data.

    Routing the teardown through the fetch would have left its stream
    subscribed for the rest of the connection - and with the flag still set,
    the next session would not re-subscribe either.
    """
    c = _coordinator_for_session_end(condor=True)
    _end_of_session(c)
    assert c.torn_down == 1
    assert c.fetches == 0, "nothing to fetch on this protocol"


def test_a_fetch_already_in_flight_does_not_swallow_the_teardown():
    """The dedupe guards the fetch, and only the fetch.

    A record request from connect time can still be waiting when somebody
    finishes brushing; that must not cost this session its teardown.
    """
    c = _coordinator_for_session_end(busy=True)
    _end_of_session(c)
    assert c.torn_down == 1


def test_nothing_is_torn_down_when_nothing_is_subscribed():
    c = _coordinator_for_session_end(subscribed=False)
    _end_of_session(c)
    assert c.torn_down == 0


def test_a_session_in_progress_is_not_interrupted_by_a_backfill():
    """Connecting mid-brush must not go looking for older records.

    The initial read reports the running session and the newest stored id in
    one go. Fetching then would take the link mid-session for a record that
    describes the session *before* this one.
    """
    from custom_components.philips_sonicare_ble.coordinator import (
        PhilipsSonicareCoordinator,
    )
    c = PhilipsSonicareCoordinator.__new__(PhilipsSonicareCoordinator)
    c.address = "AA:BB"
    c._use_condor = False
    c.asked = []
    c._start_session_end_task = lambda session_id=None, witnessed=True: (
        c.asked.append(session_id))
    parsed = {"latest_session_id": 335}

    running = {"latest_session_id": 335, "brushing_state": "on"}
    c._update_stored_session({}, running, parsed)
    assert c.asked == [], "left the running session alone"

    idle = {"latest_session_id": 335}
    c._update_stored_session({}, idle, parsed)
    assert c.asked == [335]


def test_a_record_whose_time_cannot_be_placed_is_not_chased_forever():
    """Retrying is fine; retrying on every connect for good is not."""
    from custom_components.philips_sonicare_ble.coordinator import (
        PhilipsSonicareCoordinator,
    )
    c = PhilipsSonicareCoordinator.__new__(PhilipsSonicareCoordinator)
    c.address = "AA:BB"
    c._use_condor = False
    c.asked = []
    c._start_session_end_task = lambda session_id=None, witnessed=True: (
        c.asked.append(session_id))
    parsed = {"latest_session_id": 335}

    for attempts, expected in ((0, 1), (1, 2), (2, 2), (5, 2)):
        data = {"latest_session_id": 335,
                "last_session": {"session_id": 335, "time_source": "collection",
                                 "place_attempts": attempts}}
        c._update_stored_session({}, data, parsed)
        assert len(c.asked) == expected, f"after {attempts} attempts"


async def test_a_selection_that_did_not_take_is_not_mistaken_for_an_answer():
    """Reading straight after selecting confirms nothing by itself.

    A handle asked for a session it does not have answers with whatever is
    still in its buffer, and reports no error - so a failed selection looks
    exactly like a successful one. The record names its own session, and
    that is what settles it.
    """
    class StubbornTransport(FakeTransport):
        """Always answers with session 378, whatever was asked for."""

        async def read_char(self, uuid):
            if uuid.endswith("40d0"):
                return (378).to_bytes(2, "little")
            if uuid.endswith("4100"):
                return KIDS_RECORD
            return None

    proto = _protocol(StubbornTransport())
    proto.model = "HX6340"
    proto.direct_session_read = True

    assert (await proto.fetch_stored_session())["session_id"] == 378
    assert await proto.fetch_stored_session(379) is None, (
        "the previous record must not pass as session 379")


def test_a_record_in_an_unrecognised_format_is_refused():
    """A future format would decode, not fail — so it is bounded instead.

    The handle reports its record version in a descriptor, which not every
    transport here can read. What every transport can do is notice that a
    session of 40000 seconds, or one of a routine that is zero seconds long,
    is not a session at all.
    """
    good = decode_session_record(KIDS_RECORD, "HX6340")
    assert good["duration"] == 19

    def altered(offset, value):
        r = bytearray(KIDS_RECORD)
        r[offset:offset + 2] = value.to_bytes(2, "little")
        return bytes(r)

    assert decode_session_record(altered(8, 0), "HX6340") is None, "no routine"
    assert decode_session_record(altered(8, 40000), "HX6340") is None, "absurd routine"
    assert decode_session_record(altered(6, 9999), "HX6340") is None, "absurd duration"
    # A session that ran a little past its target is still a session.
    assert decode_session_record(altered(6, 70), "HX6340")["duration"] == 70
