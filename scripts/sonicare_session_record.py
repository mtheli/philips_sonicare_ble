#!/usr/bin/env python3
"""Record a complete brushing session from a Classic Sonicare handle.

``sonicare_scan.py --listen`` only covers the newer (Condor/e50b) protocol —
its listen phase lives on ``NewerProtocolProbe``. Classic handles (477ea600
services) never reach it, so this script is the Classic counterpart: subscribe
to the characteristics the integration itself subscribes to, then write every
notification to a JSONL file with a timestamp.

The result is a replayable session: what the handle reported, in the order and
at the pace it reported it. That is the input the toothbrush-card tests need
and which a one-shot GATT probe (``--json``) cannot provide.

Usage:
  python sonicare_session_record.py                     # scan, record 180 s
  python sonicare_session_record.py AA:BB:.. --seconds 240
  python sonicare_session_record.py --pressure          # + SensorData stream

  python sonicare_session_record.py --watch --pressure  # leave it running
  python sonicare_session_record.py --fetch-session     # + stored-record probe

Requirements:
  pip install bleak

Stored sessions
---------------
A handle keeps its finished sessions itself, in a storage service built around
a session id, a count, a type, a selector and a data characteristic. That is a
better source than a recording of the live values: it is what the handle
concluded rather than what was overheard, and it survives a link that dropped
at the wrong moment.

``--fetch-session`` probes that service. The request goes out the instant a
session ends - not after the settle window - because a handle switches itself
off once it is done and takes the link with it. In one-shot mode the exchange
is also run once at startup, so whether it works at all can be answered in
seconds instead of after a full routine.

It is off by default because it writes to the handle, and because nothing
about the exchange is confirmed yet. Everything it does is recorded, refusals
included: a capture showing that a handle rejected the request answers the
question just as well as one carrying a record.

Watch mode
----------
``--watch`` turns the one-shot capture into something that can be left running:
it scans continuously, connects to any Sonicare that turns up, and writes one
file per session for as long as it is there. Several handles at once are fine
(``--max-connections``), which is the point - collecting captures from more
models is easier when nobody has to remember to start a recording first.

A handle only advertises while it is awake, so a sighting is a good moment to
connect: somebody has just picked the brush up. The connection is dropped
again after ``--idle-timeout`` with nothing happening, which keeps the handle's
battery out of it and gives its single BLE slot back.

That slot is the thing to know about. **A Sonicare accepts one connection at a
time**, so while this holds a handle, the Home Assistant integration cannot
have it - and the other way round. Watch mode is for a machine that is not the
one running Home Assistant, or for a handle Home Assistant does not know.

Recording does not stop when the motor does. The values that describe a
finished session arrive afterwards: a Prestige reports ``session_complete``
and wipes its timer in the same instant, and the display face lands later
still. ``--settle`` keeps the recording open past the end for that reason;
shortening it loses the end of the session, which is the part worth having.

Waiting is not the same as requiring, though. A handle switches itself off
once it is done, so the link usually drops a few seconds into that settle
window - which means the recording is written whenever the handle goes away,
not only when the window runs out. Each file says which of the two happened
in its ``ended`` field, so a short recording can be told apart from a
complete one without guessing.

Linux
-----
Watch mode has to work around two BlueZ rules that the Windows and macOS
backends do not have, and that a recording made there will never run into.
An adapter refuses to connect while it is discovering, so scanning is paused
for the moment a connect takes; and a client built from an address scans for
that address itself before connecting, which costs the seconds a handle is
awake for - so what the scan found is handed on directly instead.

Bonding
-------
Encrypted reads (Battery Level, Device Information) need a bond. On Linux
``sonicare_scan.py`` registers a BlueZ auto-confirm agent; on Windows and macOS
the OS owns pairing, so pair the handle through the system Bluetooth settings
first. Without a bond this script still records everything the proprietary
477ea600 characteristics report — those are what a session is made of — and
only the encrypted extras are missing.

Privacy
-------
The output carries no MAC address, no serial number and no device name, so a
recording can be shared as a fixture as-is. Only the model number is kept,
because a capture is meaningless without knowing which handle produced it.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

from bleak import BleakClient, BleakScanner


def _load_const():
    """Load the integration's const.py as a standalone module.

    Imported by file rather than as ``custom_components.philips_sonicare_ble
    .const``, because the package __init__ pulls in Home Assistant and this
    script has to run against a bare ``pip install bleak``. const.py itself is
    pure constants, so loading it in isolation is safe — and it keeps the UUID
    list single-sourced, so a characteristic added to the integration is
    recorded here without a second edit.
    """
    path = (Path(__file__).resolve().parent.parent
            / "custom_components" / "philips_sonicare_ble" / "const.py")
    spec = importlib.util.spec_from_file_location("_sonicare_const", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


const = _load_const()

# uuid -> short name, derived from the CHAR_* constants (CHAR_BRUSHING_TIME
# becomes "brushing_time"). Keeps the log readable without a second table.
CHAR_NAMES = {
    value.lower(): name[len("CHAR_"):].lower()
    for name, value in vars(const).items()
    if name.startswith("CHAR_") and isinstance(value, str)
}

# Read once at the start so the recording says what produced it. Serial Number
# and System ID are deliberately absent — see "Privacy" above.
IDENTITY_CHARS = [
    ("model", const.CHAR_MODEL_NUMBER),
    ("firmware", const.CHAR_FIRMWARE_REVISION),
]

# Read once after subscribing, and then polled as a safety net.
#
# The baseline read is not optional: a characteristic notifies on *change*, so
# without it the recording starts with no idea what the handle was already
# reporting - a routine length that never changes mid-session would never
# appear at all. Polling on top covers a missed CCCD write; the integration
# itself is push-only (coordinator update_interval=None), so it is the gentler
# default to lower or disable this if the link proves unstable.
# Three characteristics every Classic handle carries whose readings nothing
# here has ever seen. Their names are established - the segment of the
# quadrant pacer, the easy-start run-in stage, and whichever feature the
# handle has active - but what they actually report has never been observed,
# and a session is the only time it could show. So they are watched rather
# than guessed at.
PROBE_CHARS = [
    const.CHAR_QUADPACER_SEGMENT,
    const.CHAR_EASY_START_STAGE,
    const.CHAR_ACTIVE_FEATURE,
]

POLL_CHARS = [
    const.CHAR_HANDLE_STATE,
    const.CHAR_BRUSHING_STATE,
    const.CHAR_BRUSHING_TIME,
    const.CHAR_ROUTINE_LENGTH,
    const.CHAR_BRUSHING_MODE,
    const.CHAR_INTENSITY,
    # Where the *selected* mode lives on HX9996 / HX999X. Those handles report
    # it as a mode-id byte here, while 0x4080 (BRUSHING_MODE) carries something
    # else entirely - see uses_routine_id_mode() and the note above
    # BRUSHING_MODES in const.py. It notifies on neither model, so polling is
    # the only way to see it change, and without it a recording from a Prestige
    # says "clean" no matter which routine was actually running.
    const.CHAR_AVAILABLE_ROUTINE_IDS,
    *PROBE_CHARS,
]

AUTH_HINTS = ("insufficient", "authentication", "encryption", "not paired",
              "not authorized", "access denied")


def _is_auth_error(err: BaseException) -> bool:
    low = str(err).lower()
    return any(hint in low for hint in AUTH_HINTS)


# Connecting can fail for reasons that say nothing about the handle. The
# adapter may already have an operation running against it - a paired handle
# is usually trusted too, and then the system connects to it on its own the
# moment it advertises, which collides with a connect of our own. Aborted
# links are the same kind of noise.
#
# It is worth retrying rather than reporting, because the thing being waited
# for is short-lived: a handle advertises while somebody is holding it and
# stops soon after. Giving up on the first error means giving up on that
# session, and the next advertisement may be a day away.
TRANSIENT_HINTS = ("inprogress", "in progress", "abort", "busy",
                   "not ready", "timeout", "temporarily")


def _is_transient_error(err: BaseException) -> bool:
    low = str(err).lower()
    return any(hint in low for hint in TRANSIENT_HINTS)


def _name(uuid: str) -> str:
    return CHAR_NAMES.get(uuid.lower(), uuid[4:8])


class SessionRecorder:
    """Collects timestamped characteristic values for one recording."""

    def __init__(self, out_path: Path, quiet: bool = False) -> None:
        self.out_path = out_path
        self.quiet = quiet
        self.events: list[dict] = []
        self.started = time.monotonic()
        # Last value per characteristic, so polling only records real changes
        # and a two-minute session stays a few hundred lines rather than a few
        # thousand identical ones.
        self._last: dict[str, str] = {}
        # Set while a stored record is being transferred. It labels the values
        # that arrive with the request they answer, and switches off the
        # unchanged-value filter for the storage characteristics: a record
        # arrives in chunks that may legitimately repeat, and a dropped
        # duplicate would corrupt it.
        self.tag: dict | None = None
        # The newest session id the handle has reported so far, so a fetch
        # that comes back with the previous session can be told apart from
        # one that came back with nothing.
        self.last_session_id: int | None = None

    def record(self, uuid: str, data: bytes, kind: str) -> bool:
        """Append a value if it differs from the last one for that char."""
        hex_str = bytes(data).hex()
        key = uuid.lower()
        tagged = self.tag is not None and key in STORAGE_CHARS
        if not tagged and self._last.get(key) == hex_str:
            return False
        self._last[key] = hex_str
        event = {
            "t": round(time.monotonic() - self.started, 3),
            "kind": kind,
            "char": _name(uuid),
            "uuid": key,
            "hex": hex_str,
        }
        if tagged:
            event.update(self.tag)
        self.events.append(event)
        if not self.quiet:
            print(f"  [{event['t']:8.3f}] {event['char']:<22} {hex_str}")
        return True

    def note(self, label: str, **fields) -> None:
        """Record something that happened rather than a value that arrived.

        A stored-record request is a sequence of writes, and what came back
        only makes sense next to what was asked for - including when the
        answer is a refusal.
        """
        event = {
            "t": round(time.monotonic() - self.started, 3),
            "kind": "note",
            "note": label,
            **fields,
        }
        self.events.append(event)
        if not self.quiet:
            detail = " ".join(f"{k}={v}" for k, v in fields.items())
            print(f"  [{event['t']:8.3f}] {label} {detail}".rstrip())

    def write(self, meta: dict, out_path: Path | None = None) -> Path:
        path = out_path or self.out_path
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "meta", **meta}, ensure_ascii=False) + "\n")
            for event in self.events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path

    def value(self, uuid: str) -> str | None:
        """The last raw value seen for a characteristic, or None."""
        return self._last.get(uuid.lower())

    def since(self, index: int) -> int:
        """How many events have been recorded since a given position."""
        return len(self.events) - index

    def restart(self) -> None:
        """Drop what has been collected so far and start timing again.

        Watch mode calls this after writing a session: the next one gets its
        own file, with timestamps starting at zero rather than counting on
        from whenever the handle happened to connect.
        """
        self.events.clear()
        self._last.clear()
        self.started = time.monotonic()


async def _find_handle(mac: str | None):
    """Return the device to connect to, scanning when none was given.

    The device object rather than its address: handing an address to a client
    makes it scan for the device all over again, and by then a handle that was
    only awake because somebody pressed its button may have gone back to
    sleep. What the scan already found is what should be connected to.
    """
    if mac:
        print(f"Scanning for {mac} (20 s - wake the brush now)...")
        device = await BleakScanner.find_device_by_address(mac, timeout=20)
        if not device:
            sys.exit(f"Device {mac} not found. Wake the handle and try again.")
        return device

    print("Scanning for a Sonicare handle (20 s - press the power button now)...")
    devices = await BleakScanner.discover(timeout=20.0, return_adv=True)
    for device, adv in devices.values():
        name = (device.name or adv.local_name or "").lower()
        if "sonicare" in name or const.SONICARE_MANUFACTURER_ID in adv.manufacturer_data:
            print(f"Found: {device.name or adv.local_name} (RSSI {adv.rssi})")
            return device
    sys.exit("No Sonicare found. A sleeping handle does not advertise - press "
             "the power button, then start again within a few seconds.")


async def _read_identity(client: BleakClient) -> dict:
    meta: dict = {}
    for key, uuid in IDENTITY_CHARS:
        try:
            value = await client.read_gatt_char(uuid)
            meta[key] = value.decode("utf-8", "replace").strip("\x00").strip()
        except Exception as err:  # noqa: BLE001 - any failure is non-fatal here
            if _is_auth_error(err):
                meta[key] = None
                meta.setdefault("_note", "identity reads need a bond - "
                                         "pair the handle in the OS Bluetooth settings")
            else:
                meta[key] = None
    return meta


async def _subscribe_all(client: BleakClient, recorder: SessionRecorder,
                         pressure: bool, storage: bool = False) -> list[str]:
    """Subscribe to every notification characteristic the handle offers."""
    wanted = list(const.NOTIFICATION_CHARS) + PROBE_CHARS
    if pressure:
        wanted.append(const.CHAR_SENSOR_DATA)
    if storage:
        # Subscribed here rather than when a record is requested. The request
        # happens in the seconds between the motor stopping and the handle
        # switching itself off, and a descriptor write per characteristic is
        # exactly the kind of delay that window does not have to spare.
        wanted += [const.CHAR_SESSION_DATA, const.CHAR_ACTIVE_SESSION_ID,
                   const.CHAR_SESSION_ACTION]
        # The one characteristic in the session service that can announce
        # anything. It reports which session is loaded for reading, and a
        # handle appears to keep its newest one loaded - so if it loads a
        # session as it files it, this is where that would show, which is
        # the moment nothing else tells us about.
        wanted.append(CHAR_SESSION_EXTRA)

    available = {c.uuid.lower() for s in client.services for c in s.characteristics}
    subscribed: list[str] = []
    auth_blocked = 0

    for uuid in wanted:
        if uuid.lower() not in available:
            print(f"  - {_name(uuid):<22} not present on this handle")
            continue

        def callback(_char, data: bytearray, _uuid=uuid) -> None:
            recorder.record(_uuid, data, "notify")

        try:
            await client.start_notify(uuid, callback)
            subscribed.append(uuid)
            print(f"  + {_name(uuid)}")
        except Exception as err:  # noqa: BLE001
            if _is_auth_error(err):
                auth_blocked += 1
                print(f"  ! {_name(uuid):<22} needs a bond")
            else:
                print(f"  ! {_name(uuid):<22} {err}")

    if auth_blocked:
        print(f"\n  {auth_blocked} characteristic(s) refused without a bond. Pair the "
              "handle in the OS Bluetooth settings and re-run to capture those too.")
    return subscribed


async def _enable_pressure(client: BleakClient) -> None:
    """Switch on the pressure substream of the SensorData port."""
    try:
        await client.write_gatt_char(const.CHAR_SENSOR_ENABLE, bytes([1]), response=True)
        print("  pressure telemetry enabled")
    except Exception as err:  # noqa: BLE001
        print(f"  could not enable pressure telemetry: {err}")


# Values the handle keeps as descriptors rather than characteristics, which
# is why a scan that lists characteristics never shows them. Two of them
# answer questions this project has had to guess at: how many segments the
# pacer has, and which version of the record format the handle writes.
PROBE_DESCRIPTORS = {
    "477ea600-a260-11e4-ae37-0002a5d5a0a0": "quadpacer_count",
    "477ea600-a260-11e4-ae37-0002a5d5a100": "session_version",
    "477ea600-a260-11e4-ae37-0002a5d5a0d0": "session_count",
    "477ea600-a260-11e4-ae37-0002a5d5a090": "routine_length",
    "477ea600-a260-11e4-ae37-0002a5d5a0b0": "highest_intensity",
    "477ea600-a260-11e4-ae37-0002a5d5a0c0": "easy_start_stage_count",
    "477ea600-a260-11e4-ae37-0002a5d5a030": "available_feature",
    "477ea600-a260-11e4-ae37-0002a5d5a020": "brushing_routine_list",
}


async def _read_descriptors(client: BleakClient, recorder: SessionRecorder) -> None:
    """Read the descriptors the handle carries, once.

    They hold static facts about the handle rather than anything that
    changes during a session, so once at the start is enough. Anything the
    handle does not have is skipped in silence - most models carry only
    some of them.
    """
    found = 0
    for service in client.services:
        for char in service.characteristics:
            for desc in getattr(char, "descriptors", []):
                name = PROBE_DESCRIPTORS.get(desc.uuid.lower())
                if not name:
                    continue
                try:
                    raw = await client.read_gatt_descriptor(desc.handle)
                except Exception as err:  # noqa: BLE001 - a refusal is a result
                    recorder.note("descriptor_failed", name=name, error=str(err))
                    continue
                data = bytes(raw or b"")
                recorder.note("descriptor", name=name, hex=data.hex(),
                              on=_name(char.uuid),
                              value=int.from_bytes(data, "little") if data else None)
                found += 1
    if not found:
        print("  no named descriptors on this handle")


async def _set_mode(client: BleakClient, mode: str) -> None:
    """Ask the handle to switch routine before the recording starts.

    The routine decides how long a session runs, so an experiment that only
    needs a session - not a long one - can say so instead of waiting out
    whatever the handle was left on.
    """
    mode_id = next((k for k, v in const.BRUSHING_MODES.items() if v == mode), None)
    if mode_id is None:
        known = ", ".join(sorted(const.BRUSHING_MODES.values()))
        sys.exit(f"Unknown mode {mode!r}. Known: {known}")
    try:
        await client.write_gatt_char(const.CHAR_AVAILABLE_ROUTINE_IDS,
                                     bytes([mode_id]), response=True)
        print(f"  routine set to {mode} (id {mode_id})")
    except Exception as err:  # noqa: BLE001 - not worth losing the recording
        print(f"  could not set the routine: {err}")


async def _read_baseline(client: BleakClient, recorder: SessionRecorder) -> None:
    """Record the current value of every session characteristic."""
    available = {c.uuid.lower() for s in client.services for c in s.characteristics}
    for uuid in POLL_CHARS:
        if uuid.lower() not in available:
            continue
        try:
            recorder.record(uuid, await client.read_gatt_char(uuid), "baseline")
        except Exception as err:  # noqa: BLE001
            if _is_auth_error(err):
                print(f"  ! {_name(uuid):<22} baseline needs a bond")


# ── Stored sessions ─────────────────────────────────────────────────────────
# A Classic handle keeps finished sessions in its storage service (0x0004),
# which is why that service has an id, a count, a type, a selector, an action
# and a data characteristic rather than one readable value. The layout implies
# a select-then-transfer exchange:
#
#   read  latest_session_id  - the newest session the handle still holds
#   read  session_count      - how many it holds
#   write session_type       - which kind of stored data is wanted
#   write active_session_id  - which session
#   write session_action     - start the transfer
#   -> the data arrives as notifications on session_data
#
# None of this is confirmed. This script is how it gets confirmed: it runs the
# exchange and writes down everything that comes back, refusals included. A
# handle that rejects a write is as useful a capture as one that answers.
#
# The type byte is probed rather than known. 0 is tried first because the
# session's own record is the interesting one; the others are guesses at where
# the pressure statistics and the brush head identity live.
STORAGE_REQUESTS = [
    ("routine", 0),
    ("brush_head", 5),
    ("brush_id", 4),
    # Kinds nobody has asked this handle for yet. The numbering is fixed, but
    # which of them a handle actually keeps is not published anywhere - the
    # only way to find out is to ask and see what comes back. A summary is
    # the one worth hoping for: the pressure recording answers the same
    # question at five hundred times the size.
    ("summary", 8),
    ("diagnostics", 7),
    ("temperature", 2),
    ("acc_gyro", 3),
    ("gyro_compensation", 6),
    # The pressure recording, last because it is by far the largest: 3612
    # bytes in thirty-three chunks against nine for the others. Three bytes
    # per sample, ten samples a second, and the third byte of each says
    # whether the handle objected to how hard it was being pressed - which
    # makes this, and not the brush-head record, where the time spent
    # pressing too hard can be counted. Anything requested before it must
    # not be lost to a window that closed during a transfer this long.
    ("pressure", 1),
]

# Writing 0 is the "send it" case of the control point. Writing 2 takes back
# a transfer that was started and never finished - without it a handle that
# stayed silent is still in that transfer, and answers nothing afterwards.
STORAGE_ACTION_START = 0
STORAGE_ACTION_CANCEL = 2

# Not every handle offers the same storage service. A Sonicare for Kids has
# no service of its own for it at all: the same characteristics sit in the
# brushing service, and the two that drive the exchange - the kind selector
# and the control point - are simply absent. What is there instead is a
# readable data characteristic, which suggests a shorter exchange for a
# handle that only keeps one kind of record: select the session, read it.
#
# Both shapes are probed, whichever the handle presents. One more readable
# characteristic sits beside them there and is read along the way, because
# nothing else says what it holds.
CHAR_SESSION_EXTRA = const.CHAR_LOADED_SESSION

STORAGE_CHARS = {
    const.CHAR_SESSION_DATA.lower(),
    const.CHAR_ACTIVE_SESSION_ID.lower(),
    const.CHAR_SESSION_ACTION.lower(),
    CHAR_SESSION_EXTRA.lower(),
}


async def _probe_next_session(client: BleakClient, recorder: SessionRecorder,
                              announce, known_id: int) -> bool:
    """Ask for the session the handle has not announced yet.

    The exchange takes any id, and the record names the session it describes,
    so asking for one past the newest is safe to interpret: either it comes
    back as that session, or it comes back as something else and is ignored.
    """
    wanted = known_id + 1
    recorder.tag = {"request": "probe_next", "session_id": wanted}
    try:
        await client.write_gatt_char(const.CHAR_ACTIVE_SESSION_ID,
                                     wanted.to_bytes(2, "little"), response=True)
        raw = await client.read_gatt_char(const.CHAR_SESSION_DATA)
    except Exception as err:  # noqa: BLE001 - a refusal is a result
        recorder.note("probe_next_failed", session_id=wanted, error=str(err))
        announce(f"asking for #{wanted} was refused ({err})")
        return False
    finally:
        recorder.tag = None

    data = bytes(raw or b"")
    recorder.record(const.CHAR_SESSION_DATA, data, "storage")
    if not data.strip(b"\x00"):
        announce(f"#{wanted} is not there yet (empty answer)")
        recorder.note("probe_next_empty", session_id=wanted)
        return False
    # The record says which session it is. Anything else is the handle
    # repeating itself, not the session we asked about.
    reported = int.from_bytes(data[4:6], "little") if len(data) >= 6 else None
    recorder.note("probe_next_answer", asked=wanted, reported=reported,
                  hex=data.hex())
    if reported == wanted:
        announce(f"the handle already has #{wanted} — it just had not said so")
        return True
    announce(f"asked for #{wanted}, got #{reported} — not filed yet")
    return False


async def _await_new_session(client: BleakClient, recorder: SessionRecorder,
                             announce, known_id: int | None,
                             timeout: float = 90.0, interval: float = 3.0) -> bool:
    """Wait for a handle that files its sessions late.

    Not every handle has the record ready the moment the motor stops: asked a
    second after a session ended, one answered with the session before it and
    only carried the new one minutes later. Whether that is a short delay or
    something that waits for the next connection is the difference between
    retrying and giving up, so the wait is measured rather than assumed.

    Returns whether a new session turned up inside the window.
    """
    if known_id is None:
        return False
    # Before waiting: ask for the session after the one the handle admits to.
    # It files late, but the record may well exist before it says so - and if
    # it does not, the answer costs nothing, because a record carries the id
    # it belongs to. A handle repeating the old one, or answering with
    # nothing, is recognised rather than mistaken for the new session.
    await _probe_next_session(client, recorder, announce, known_id)

    announce(f"waiting for the handle to file the session (had #{known_id})")
    started = time.monotonic()
    spoken = 0.0
    while time.monotonic() - started < timeout:
        # A silent minute is indistinguishable from a hung script, and this
        # runs while somebody is standing there watching it.
        elapsed = time.monotonic() - started
        if elapsed - spoken >= 15:
            spoken = elapsed
            announce(f"  still #{known_id} after {elapsed:.0f} s")
        await asyncio.sleep(interval)
        if not client.is_connected:
            recorder.note("commit_wait_ended", reason="link dropped",
                          waited=round(time.monotonic() - started, 1))
            announce("link dropped before the handle filed the session")
            return False
        try:
            raw = await client.read_gatt_char(const.CHAR_LATEST_SESSION_ID)
        except Exception as err:  # noqa: BLE001
            recorder.note("commit_wait_failed", error=str(err))
            return False
        value = int.from_bytes(bytes(raw)[:2], "little") if raw else None
        if value is not None and value != known_id:
            waited = round(time.monotonic() - started, 1)
            recorder.note("commit_delay_seconds", waited=waited,
                          was=known_id, now=value)
            announce(f"the handle filed session #{value} after {waited:.0f} s")
            await _fetch_stored_session(client, recorder, announce,
                                        session_id=value)
            return True
    waited = round(time.monotonic() - started, 1)
    recorder.note("commit_wait_timeout", waited=waited, still=known_id)
    announce(f"still session #{known_id} after {waited:.0f} s")
    return False


async def _read_stored_session(client: BleakClient, recorder: SessionRecorder,
                               announce, session_id: int, props: dict) -> bool:
    """The shorter exchange, for a handle with no kind selector.

    Where the full storage service takes three writes and answers by
    notification, this one has a readable data characteristic and nothing to
    choose between: select the session, then read it. Whether a handle shaped
    like that answers at all is exactly what this is here to find out, so a
    silent or refusing one is recorded rather than treated as a failure.
    """
    announce(f"stored session: no kind selector - reading #{session_id} directly")
    recorder.tag = {"request": "direct", "session_id": session_id}
    got = False
    try:
        try:
            await client.write_gatt_char(
                const.CHAR_ACTIVE_SESSION_ID,
                session_id.to_bytes(2, "little"), response=True)
        except Exception as err:  # noqa: BLE001 - a refusal is a result
            recorder.note("storage_select_failed", error=str(err))
            announce(f"stored session: the handle refused the selection ({err})")

        for label, uuid in (("session_data", const.CHAR_SESSION_DATA),
                            ("session_extra", CHAR_SESSION_EXTRA)):
            if uuid.lower() not in props:
                continue
            if "read" not in props[uuid.lower()]:
                recorder.note("storage_not_readable", char=label,
                              props=sorted(props[uuid.lower()]))
                continue
            try:
                raw = await client.read_gatt_char(uuid)
            except Exception as err:  # noqa: BLE001
                recorder.note("storage_read_failed", char=label, error=str(err))
                announce(f"stored session: {label} refused the read ({err})")
                continue
            recorder.record(uuid, raw, "storage")
            announce(f"stored session: {label} answered {len(bytes(raw))} byte(s)")
            # Only the record itself decides whether this worked. The
            # characteristic beside it is read out of curiosity, and a
            # handle answering that one while the record stays empty has
            # not produced a session.
            if uuid == const.CHAR_SESSION_DATA and bytes(raw).strip(b"\x00"):
                got = True
    finally:
        recorder.note("storage_request_done", request="direct", answered=got)
        recorder.tag = None
    if not got:
        announce("stored session: nothing but zeroes came back")
    return got


async def _fetch_stored_session(client: BleakClient, recorder: SessionRecorder,
                                announce, *, session_id: int | None = None,
                                timeout: float = 6.0, gap: float = 1.0) -> bool:
    """Ask the handle for the stored record of its most recent session.

    Called the moment a session ends, because that is the only moment the
    handle is reliably still there: it switches itself off once it is done,
    and the link goes with it. Everything that can be prepared in advance -
    the notification subscriptions - is prepared at connect time, so what
    happens here is three writes and a wait.

    Returns whether any data came back.
    """
    if not client.is_connected:
        return False

    # What the handle has. Read rather than assumed: the newest id is what
    # this request is for, and the count says whether older ones are there
    # too - the thing that would make a full history possible.
    latest: int | None = session_id
    for label, uuid in (("latest_session_id", const.CHAR_LATEST_SESSION_ID),
                        ("session_count", const.CHAR_SESSION_COUNT)):
        try:
            raw = await client.read_gatt_char(uuid)
        except Exception as err:  # noqa: BLE001 - a refusal is a result
            recorder.note("storage_read_failed", char=label, error=str(err))
            continue
        recorder.record(uuid, raw, "storage")
        value = int.from_bytes(bytes(raw)[:2], "little") if raw else None
        recorder.note("storage_state", char=label, value=value)
        if label == "latest_session_id":
            recorder.last_session_id = value
            if latest is None:
                latest = value

    if latest is None:
        announce("stored session: handle did not say which session is newest")
        return False

    # The handle's running clock, read next to the record. A record is
    # stamped with that clock and nothing else, so on its own it says only
    # how long the handle has been counting - a second reading, taken at a
    # moment that is known, is what turns it into a time of day.
    try:
        raw = await client.read_gatt_char(const.CHAR_HANDLE_TIME)
        if raw and len(bytes(raw)) >= 4:
            clock = int.from_bytes(bytes(raw)[:4], "little")
            recorder.record(const.CHAR_HANDLE_TIME, raw, "storage")
            recorder.note("handle_clock", value=clock)
            announce(f"handle clock reads {clock} s")
    except Exception as err:  # noqa: BLE001 - only costs the anchor
        recorder.note("storage_read_failed", char="handle_time", error=str(err))

    props = {c.uuid.lower(): set(c.properties)
             for s in client.services for c in s.characteristics}
    selectable = (const.CHAR_SESSION_TYPE.lower() in props
                  and const.CHAR_SESSION_ACTION.lower() in props)
    if not selectable:
        return await _read_stored_session(client, recorder, announce, latest, props)

    announce(f"stored session: requesting #{latest}")
    got_any = False

    for name, type_byte in STORAGE_REQUESTS:
        if not client.is_connected:
            recorder.note("storage_aborted", request=name, reason="link dropped")
            break

        recorder.tag = {"request": name, "request_type": type_byte,
                        "session_id": latest}
        mark = len(recorder.events)
        try:
            await client.write_gatt_char(const.CHAR_SESSION_TYPE,
                                         bytes([type_byte]), response=True)
            await client.write_gatt_char(const.CHAR_ACTIVE_SESSION_ID,
                                         latest.to_bytes(2, "little"), response=True)
            await client.write_gatt_char(const.CHAR_SESSION_ACTION,
                                         bytes([STORAGE_ACTION_START]), response=True)
        except Exception as err:  # noqa: BLE001
            recorder.note("storage_request_failed", request=name, error=str(err))
            recorder.tag = None
            if _is_auth_error(err):
                announce("stored session: refused without a bond")
                break
            continue

        # Wait for the transfer to go quiet rather than for a fixed time: a
        # record may be one notification or several, and the handle may not
        # answer at all.
        deadline = time.monotonic() + timeout
        last_seen = time.monotonic()
        count = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if not client.is_connected:
                break
            new = recorder.since(mark)
            if new != count:
                count = new
                last_seen = time.monotonic()
            elif count and time.monotonic() - last_seen >= gap:
                break

        # What the handle said about this kind, separately from what it sent:
        # the announcement names the size before the first byte arrives, and
        # the control point says when it considers the transfer over. A kind
        # the handle does not keep is answered by one, the other, or neither
        # - and telling those apart is the point of asking at all.
        announced = status = None
        payload = chunks = 0
        for event in recorder.events[mark:]:
            if event.get("kind") == "note":
                continue
            raw = bytes.fromhex(event["hex"])
            if event["char"] == "active_session_id" and len(raw) >= 6:
                announced = int.from_bytes(raw[2:6], "little")
            elif event["char"] == "session_action" and raw:
                status = raw[0]
            elif event["char"] == "session_data":
                chunks += 1
                payload += max(len(raw) - 1, 0)  # without the chunk index

        recorder.note("storage_request_done", request=name, chunks=count,
                      data_chunks=chunks, payload=payload,
                      announced=announced, status=status)
        recorder.tag = None
        if chunks:
            got_any = True
            announce(f"stored session: {name} -> {payload} B in {chunks} "
                     f"chunk(s), announced {announced}, status {status}")
        else:
            announce(f"stored session: {name} sent nothing "
                     f"(announced {announced}, status {status})")

        # A kind that never got acknowledged left the handle in a transfer.
        # Taking it back is what keeps a probe for one kind from costing
        # every kind after it - the failure mode that hid this whole
        # exchange until now.
        if status is None:
            try:
                await client.write_gatt_char(const.CHAR_SESSION_ACTION,
                                             bytes([STORAGE_ACTION_CANCEL]),
                                             response=True)
                recorder.note("storage_transfer_cancelled", request=name)
            except Exception as err:  # noqa: BLE001 - nothing left to save
                recorder.note("storage_cancel_failed", request=name,
                              error=str(err))
            await asyncio.sleep(gap)

    return got_any


async def _poll_loop(client: BleakClient, recorder: SessionRecorder,
                     interval: float, deadline: float,
                     fetch: bool = False) -> None:
    """Read the session characteristics until the deadline passes.

    Only changed values are recorded, so an idle handle costs nothing in the
    log while a running one is sampled at the interval.
    """
    available = {c.uuid.lower() for s in client.services for c in s.characteristics}
    chars = [u for u in POLL_CHARS if u.lower() in available]
    was_running = False
    while time.monotonic() < deadline:
        if not client.is_connected:
            print("\n  Link dropped (handle switched off?) - ending recording.")
            return
        for uuid in chars:
            try:
                recorder.record(uuid, await client.read_gatt_char(uuid), "read")
            except Exception:  # noqa: BLE001 - a single failed read is not fatal
                pass

        running = _session_is_running(recorder)
        if fetch and was_running and not running:
            # Immediately, before the settle window: the handle switches
            # itself off at the end of a session and takes the link with it.
            print("\n  Session ended - asking for the stored record ...")
            say = lambda m: print(f"  {m}")  # noqa: E731
            before = recorder.last_session_id
            await _fetch_stored_session(client, recorder, say)
            if recorder.last_session_id == before:
                # The record is not there yet. How long it takes is worth
                # knowing: a handle that files late can be waited for, one
                # that files on the next connection cannot.
                await _await_new_session(client, recorder, say, before)
        was_running = running

        await asyncio.sleep(interval)


# ── Watch mode ──────────────────────────────────────────────────────────────
# One long-running scanner, one task per handle it finds. A handle only
# advertises while it is awake, so a sighting is a good moment to connect: the
# brush has just been picked up. The connection is dropped again once nothing
# has happened for a while, which keeps its battery out of it and - more
# importantly - gives the single BLE slot back.

HANDLE_STATE_RUN = 2


def log(message: str) -> None:
    """Timestamped line for the watch log.

    Watch mode is meant to be left running with its output redirected, where
    an untimed line hours after the fact says very little, and where Python's
    block buffering would hold the log back until something else forced a
    flush. Both are dealt with here rather than asking for `python -u`.
    """
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _is_sonicare(device, adv) -> bool:
    name = (device.name or adv.local_name or "").lower()
    return "sonicare" in name or const.SONICARE_MANUFACTURER_ID in adv.manufacturer_data


def _session_is_running(recorder: SessionRecorder) -> bool:
    """Whether the handle currently reports itself as brushing.

    Read from handle_state alone. A Kids handle has no brushing_state sensor
    at all, so anything that relied on it would quietly never record one.
    """
    raw = recorder.value(const.CHAR_HANDLE_STATE)
    return bool(raw) and int(raw[:2], 16) == HANDLE_STATE_RUN


def _session_path(out_dir: Path, model: str | None) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = f"sonicare_session_{(model or 'unknown').lower()}_{stamp}"
    path = out_dir / f"{base}.jsonl"
    # The stamp is only accurate to the second, and the address is deliberately
    # not part of the name (see "Privacy"). Two handles of the same model
    # finishing together would therefore land on one name - and watch mode
    # exists to record several at once, so that is a real collision.
    n = 2
    while path.exists():
        path = out_dir / f"{base}_{n}.jsonl"
        n += 1
    return path


class Discovery:
    """The scanner, and the ability to hold it still while connecting.

    Scanning and connecting cannot overlap: while the adapter is discovering,
    a connect is refused outright with "operation already in progress". A
    one-shot recording never notices, because there the scan is over before
    anything connects - but watch mode scans for as long as it runs, which
    without this would mean it can find handles and never reach one.

    Discovery is paused for the connect alone, not for the recording: once
    the link is up, scanning can carry on and find the next handle. Pauses
    nest, so several handles connecting at once resume it only once.
    """

    def __init__(self, callback) -> None:
        self._callback = callback
        self._scanner: BleakScanner | None = None
        self._lock = asyncio.Lock()
        self._paused_by = 0

    async def start(self) -> None:
        # A fresh scanner each time rather than restarting the old one: this
        # runs for days, and a scanner that has been stopped is not worth
        # assuming anything about.
        self._scanner = BleakScanner(detection_callback=self._callback)
        await self._scanner.start()

    async def stop(self) -> None:
        if self._scanner is not None:
            try:
                await self._scanner.stop()
            except Exception:  # noqa: BLE001 - stopping must not raise
                pass
            self._scanner = None

    @asynccontextmanager
    async def paused(self):
        async with self._lock:
            self._paused_by += 1
            if self._paused_by == 1:
                await self.stop()
        try:
            yield
        finally:
            async with self._lock:
                self._paused_by -= 1
                if self._paused_by == 0:
                    await self.start()


async def _connect_with_retry(device, label: str, discovery: Discovery | None = None,
                              attempts: int = 4, delay: float = 2.0) -> BleakClient:
    """Connect, giving a busy adapter a moment to finish what it was doing.

    Takes the device object a scan produced, not an address: a client built
    from an address scans for the device itself before it can connect, which
    costs the seconds a handle is awake for and fails outright once it has
    gone back to sleep.

    A link the adapter kept from an earlier run is not in the way: connecting
    to a device BlueZ already holds is a no-op it handles itself.

    Returns a client that is connected. Use ``_connected()`` rather than this
    directly - a connected client must not be entered as a context manager,
    because that connects it a second time.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        client = BleakClient(device)
        try:
            async with discovery.paused() if discovery else nullcontext():
                await client.connect()
            return client
        except Exception as err:  # noqa: BLE001
            last = err
            # A client whose connect failed may still hold a half-open
            # object on the adapter, and that is itself a reason for the
            # next attempt to be told an operation is already in progress.
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - nothing to salvage here
                pass
            if not _is_transient_error(err) or attempt == attempts:
                raise
            log(f"[{label}] {type(err).__name__} - retrying in {delay:.0f} s "
                f"({attempt}/{attempts - 1})")
            await asyncio.sleep(delay)
    raise last  # unreachable, but keeps the contract explicit


@asynccontextmanager
async def _connected(device, label: str, discovery: Discovery | None = None):
    """A connected client that disconnects on the way out.

    ``async with BleakClient(...)`` connects on entry, which is why an already
    connected client cannot be handed to it: entering would call connect a
    second time, and the second call is refused. Connecting and cleaning up
    are therefore separated here.
    """
    client = await _connect_with_retry(device, label, discovery)
    try:
        yield client
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - the handle may already be gone
            pass


async def _watch_handle(device, args, slots: asyncio.Semaphore,
                        active: set[str], discovery: Discovery | None = None) -> None:
    """Hold one handle: record every session it runs, until it goes away."""
    address = device.address
    label = address[-5:]
    try:
        async with slots:
            log(f"[{label}] connecting")
            async with _connected(device, label, discovery) as client:
                meta = await _read_identity(client)
                model = meta.get("model")
                log(f"[{label}] connected - model {model or 'unreadable'}")

                recorder = SessionRecorder(Path("unused"), quiet=True)
                if args.pressure:
                    await _enable_pressure(client)
                await _subscribe_all(client, recorder, args.pressure, args.fetch_session)
                await _read_baseline(client, recorder)
                await _read_descriptors(client, recorder)

                available = {c.uuid.lower() for s in client.services
                             for c in s.characteristics}
                chars = [u for u in POLL_CHARS if u.lower() in available]

                was_running = False
                ended_at: float | None = None
                last_activity = time.monotonic()
                sessions = 0
                saw_session = False
                idle = False

                def write_session(ended: str) -> None:
                    nonlocal sessions
                    sessions += 1
                    path = recorder.write({
                        **meta,
                        "protocol": "classic",
                        "duration_s": round(time.monotonic() - recorder.started, 1),
                        "events": len(recorder.events),
                        "polled": True,
                        # How the recording came to an end. "settled" is the
                        # complete case; the others say what is missing, which
                        # a fixture built from this file needs to know.
                        "ended": ended,
                    }, _session_path(args.out_dir, model))
                    log(f"[{label}] wrote {len(recorder.events)} events to "
                        f"{path.name} ({ended})")

                try:
                    while client.is_connected:
                        for uuid in chars:
                            try:
                                if recorder.record(uuid, await client.read_gatt_char(uuid), "read"):
                                    last_activity = time.monotonic()
                            except Exception:  # noqa: BLE001 - one failed read is not fatal
                                pass

                        running = _session_is_running(recorder)
                        if running and not was_running:
                            log(f"[{label}] session started")
                            saw_session = True
                            ended_at = None
                        elif was_running and not running:
                            # Do not write yet. The values that describe the
                            # finished session arrive after the handle stops - a
                            # Prestige reports session_complete and wipes its timer
                            # in the same instant, and the display face lands later
                            # still. Keep recording through the settle window.
                            log(f"[{label}] session ended, settling")
                            ended_at = time.monotonic()
                            if args.fetch_session:
                                # First thing in the settle window, not last:
                                # the handle switches itself off once it is
                                # done and the link goes with it, so the
                                # stored record has to be asked for while
                                # there is still someone to ask.
                                await _fetch_stored_session(
                                    client, recorder,
                                    lambda m: log(f"[{label}] {m}"))
                        was_running = running

                        if ended_at and time.monotonic() - ended_at >= args.settle:
                            write_session("settled")
                            recorder.restart()
                            await _read_baseline(client, recorder)
                            saw_session = False
                            ended_at = None
                            last_activity = time.monotonic()

                        if not running and time.monotonic() - last_activity >= args.idle_timeout:
                            log(f"[{label}] idle for {args.idle_timeout:.0f} s - disconnecting"
                                f" ({sessions} session(s) recorded)")
                            idle = True
                            break

                        await asyncio.sleep(args.poll_interval)

                finally:
                    # Whatever is still buffered has to be written on the way
                    # out, whichever way that is. A Sonicare switches itself off
                    # at the end of a session, so the link normally drops within
                    # seconds of the motor stopping - long before the settle
                    # window is over. Writing only on a completed settle threw
                    # exactly those recordings away; the handle that stayed
                    # connected long enough was the exception, not the rule.
                    # Ctrl+C and a read error land here for the same reason.
                    how = "stopped" if client.is_connected else "link dropped"
                    if saw_session:
                        write_session(
                            f"{how} {'while settling' if ended_at else 'mid-session'}")
                    if not idle:
                        log(f"[{label}] {how}")
    except Exception as err:  # noqa: BLE001 - a handle failing must not stop the watch
        if _is_auth_error(err):
            log(f"[{label}] refused without a bond - pair it in the OS settings")
        else:
            log(f"[{label}] {type(err).__name__}: {err}")
    finally:
        active.discard(address)


async def _watch(args) -> None:
    """Scan forever, recording every Sonicare that turns up."""
    # Redirected output is block-buffered by default, which for something meant
    # to run for days means the log stays empty until a buffer happens to fill.
    # Line buffering costs nothing at this rate and makes `> watch.log` behave.
    sys.stdout.reconfigure(line_buffering=True)

    active: set[str] = set()
    slots = asyncio.Semaphore(args.max_connections)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Watching for Sonicare handles. Recordings go to {args.out_dir}")
    print(f"Up to {args.max_connections} at a time; Ctrl+C to stop.\n")
    print("While this holds a handle, Home Assistant cannot connect to it -")
    print("a Sonicare accepts one connection at a time.\n")

    wanted = args.mac.upper() if args.mac else None
    if wanted:
        print(f"Restricted to {wanted}.\n")

    def seen(device, adv) -> None:
        if wanted and device.address.upper() != wanted:
            return
        if not _is_sonicare(device, adv) or device.address in active:
            return
        active.add(device.address)
        asyncio.create_task(
            _watch_handle(device, args, slots, active, discovery))

    discovery = Discovery(seen)
    await discovery.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await discovery.stop()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record Classic Sonicare brushing sessions to JSONL")
    parser.add_argument("mac", nargs="?", help="BLE MAC address (optional - scans if omitted)")
    parser.add_argument("--seconds", type=int, default=180,
                        help="Recording length. Default: 180 (a 2-minute routine plus slack)")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="Seconds between polled reads; 0 disables polling "
                             "and records notifications only. Default: 1.0")
    parser.add_argument("--pressure", action="store_true",
                        help="Also enable and record the SensorData pressure stream")
    parser.add_argument("--out", default=None,
                        help="Output path. Default: sonicare_session_<timestamp>.jsonl")
    parser.add_argument("--quiet", action="store_true", help="Do not print every value")
    parser.add_argument("--watch", action="store_true",
                        help="Run indefinitely: record every session of every "
                             "handle that turns up, one file each. Meant to be "
                             "left running.")
    parser.add_argument("--out-dir", type=Path, default=Path("."),
                        help="Where --watch puts its recordings. Default: here")
    parser.add_argument("--settle", type=float, default=45.0,
                        help="Seconds to keep recording after a session ends, so "
                             "the values that describe it are caught. Cut short "
                             "when the handle switches itself off; the recording "
                             "is written either way. Default: 45")
    parser.add_argument("--idle-timeout", type=float, default=300.0,
                        help="Disconnect after this long with nothing happening, "
                             "giving the handle's single BLE slot back. Default: 300")
    parser.add_argument("--max-connections", type=int, default=3,
                        help="How many handles to hold at once. Adapters run out "
                             "of slots well before this matters. Default: 3")
    parser.add_argument("--mode",
                        help="Set the brushing routine before recording (e.g. "
                             "clean). Handy for keeping an experiment short "
                             "and repeatable, rather than reaching for the "
                             "handle's own button between runs.")
    parser.add_argument("--fetch-session", action="store_true",
                        help="When a session ends, ask the handle for its own "
                             "stored record of it. Off by default: it writes to "
                             "the storage service, and whether a handle answers "
                             "at all is what this is meant to find out.")
    args = parser.parse_args()

    if args.watch:
        await _watch(args)
        return

    device = await _find_handle(args.mac)
    out_path = Path(args.out or f"sonicare_session_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")

    print(f"\nConnecting to {device.address} ...")
    async with _connected(device, device.address[-5:]) as client:
        print(f"Connected: {client.is_connected}")

        meta = await _read_identity(client)
        print(f"Model: {meta.get('model') or 'unreadable'}  "
              f"Firmware: {meta.get('firmware') or 'unreadable'}")

        recorder = SessionRecorder(out_path, quiet=args.quiet)

        print("\n--- Subscribing ---")
        if args.pressure:
            await _enable_pressure(client)
        await _subscribe_all(client, recorder, args.pressure, args.fetch_session)

        if args.mode:
            await _set_mode(client, args.mode)

        print("\n--- Baseline ---")
        await _read_baseline(client, recorder)
        await _read_descriptors(client, recorder)

        if args.fetch_session:
            # Once up front, before any brushing. Whether the exchange works
            # at all is answered here in a few seconds, rather than after a
            # two-minute routine that may end with the handle gone.
            print("\n--- Stored session (dry run) ---")
            await _fetch_stored_session(client, recorder, lambda m: print(f"  {m}"))

        print(f"\n--- Recording for {args.seconds} s ---")
        print("Start brushing now. Run the full routine, then let the handle "
              "switch itself off.\nPress Ctrl+C to stop early; the recording is "
              "written either way.\n")

        deadline = time.monotonic() + args.seconds
        try:
            if args.poll_interval > 0:
                await _poll_loop(client, recorder, args.poll_interval, deadline,
                                 args.fetch_session)
            else:
                await asyncio.sleep(max(0.0, deadline - time.monotonic()))
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n  Stopped.")

        meta.update({
            "protocol": "classic",
            "duration_s": round(time.monotonic() - recorder.started, 1),
            "events": len(recorder.events),
            "polled": args.poll_interval > 0,
        })
        recorder.write(meta)
        print(f"\n{len(recorder.events)} event(s) written to {out_path}")
        if not recorder.events:
            print("Nothing was captured. Either the handle stayed idle, or every "
                  "characteristic refused without a bond - see the note above.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
