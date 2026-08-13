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

Requirements:
  pip install bleak

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
]

AUTH_HINTS = ("insufficient", "authentication", "encryption", "not paired",
              "not authorized", "access denied")


def _is_auth_error(err: BaseException) -> bool:
    low = str(err).lower()
    return any(hint in low for hint in AUTH_HINTS)


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

    def record(self, uuid: str, data: bytes, kind: str) -> bool:
        """Append a value if it differs from the last one for that char."""
        hex_str = bytes(data).hex()
        key = uuid.lower()
        if self._last.get(key) == hex_str:
            return False
        self._last[key] = hex_str
        event = {
            "t": round(time.monotonic() - self.started, 3),
            "kind": kind,
            "char": _name(uuid),
            "uuid": key,
            "hex": hex_str,
        }
        self.events.append(event)
        if not self.quiet:
            print(f"  [{event['t']:8.3f}] {event['char']:<22} {hex_str}")
        return True

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

    def restart(self) -> None:
        """Drop what has been collected so far and start timing again.

        Watch mode calls this after writing a session: the next one gets its
        own file, with timestamps starting at zero rather than counting on
        from whenever the handle happened to connect.
        """
        self.events.clear()
        self._last.clear()
        self.started = time.monotonic()


async def _find_handle(mac: str | None) -> str:
    """Return the address to connect to, scanning when none was given."""
    if mac:
        print(f"Scanning for {mac} (20 s - wake the brush now)...")
        device = await BleakScanner.find_device_by_address(mac, timeout=20)
        if not device:
            sys.exit(f"Device {mac} not found. Wake the handle and try again.")
        return mac

    print("Scanning for a Sonicare handle (20 s - press the power button now)...")
    devices = await BleakScanner.discover(timeout=20.0, return_adv=True)
    for address, (device, adv) in devices.items():
        name = (device.name or adv.local_name or "").lower()
        if "sonicare" in name or const.SONICARE_MANUFACTURER_ID in adv.manufacturer_data:
            print(f"Found: {device.name or adv.local_name} (RSSI {adv.rssi})")
            return address
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
                         pressure: bool) -> list[str]:
    """Subscribe to every notification characteristic the handle offers."""
    wanted = list(const.NOTIFICATION_CHARS)
    if pressure:
        wanted.append(const.CHAR_SENSOR_DATA)

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


async def _poll_loop(client: BleakClient, recorder: SessionRecorder,
                     interval: float, deadline: float) -> None:
    """Read the session characteristics until the deadline passes.

    Only changed values are recorded, so an idle handle costs nothing in the
    log while a running one is sampled at the interval.
    """
    available = {c.uuid.lower() for s in client.services for c in s.characteristics}
    chars = [u for u in POLL_CHARS if u.lower() in available]
    while time.monotonic() < deadline:
        if not client.is_connected:
            print("\n  Link dropped (handle switched off?) - ending recording.")
            return
        for uuid in chars:
            try:
                recorder.record(uuid, await client.read_gatt_char(uuid), "read")
            except Exception:  # noqa: BLE001 - a single failed read is not fatal
                pass
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
    return out_dir / f"sonicare_session_{(model or 'unknown').lower()}_{stamp}.jsonl"


async def _watch_handle(address: str, args, slots: asyncio.Semaphore,
                        active: set[str]) -> None:
    """Hold one handle: record every session it runs, until it goes away."""
    label = address[-5:]
    try:
        async with slots:
            log(f"[{label}] connecting")
            async with BleakClient(address) as client:
                meta = await _read_identity(client)
                model = meta.get("model")
                log(f"[{label}] connected - model {model or 'unreadable'}")

                recorder = SessionRecorder(Path("unused"), quiet=True)
                if args.pressure:
                    await _enable_pressure(client)
                await _subscribe_all(client, recorder, args.pressure)
                await _read_baseline(client, recorder)

                available = {c.uuid.lower() for s in client.services
                             for c in s.characteristics}
                chars = [u for u in POLL_CHARS if u.lower() in available]

                was_running = False
                ended_at: float | None = None
                last_activity = time.monotonic()
                sessions = 0

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
                        ended_at = None
                    elif was_running and not running:
                        # Do not write yet. The values that describe the
                        # finished session arrive after the handle stops - a
                        # Prestige reports session_complete and wipes its timer
                        # in the same instant, and the display face lands later
                        # still. Keep recording through the settle window.
                        log(f"[{label}] session ended, settling")
                        ended_at = time.monotonic()
                    was_running = running

                    if ended_at and time.monotonic() - ended_at >= args.settle:
                        sessions += 1
                        path = recorder.write({
                            **meta,
                            "protocol": "classic",
                            "duration_s": round(time.monotonic() - recorder.started, 1),
                            "events": len(recorder.events),
                            "polled": True,
                        }, _session_path(args.out_dir, model))
                        log(f"[{label}] wrote {len(recorder.events)} events to {path.name}")
                        recorder.restart()
                        await _read_baseline(client, recorder)
                        ended_at = None
                        last_activity = time.monotonic()

                    if not running and time.monotonic() - last_activity >= args.idle_timeout:
                        log(f"[{label}] idle for {args.idle_timeout:.0f} s - disconnecting"
                            f" ({sessions} session(s) recorded)")
                        return

                    await asyncio.sleep(args.poll_interval)
                log(f"[{label}] link dropped")
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
        asyncio.create_task(_watch_handle(device.address, args, slots, active))

    async with BleakScanner(detection_callback=seen):
        while True:
            await asyncio.sleep(3600)


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
                             "the values that describe it are caught. Default: 45")
    parser.add_argument("--idle-timeout", type=float, default=300.0,
                        help="Disconnect after this long with nothing happening, "
                             "giving the handle's single BLE slot back. Default: 300")
    parser.add_argument("--max-connections", type=int, default=3,
                        help="How many handles to hold at once. Adapters run out "
                             "of slots well before this matters. Default: 3")
    args = parser.parse_args()

    if args.watch:
        await _watch(args)
        return

    address = await _find_handle(args.mac)
    out_path = Path(args.out or f"sonicare_session_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")

    print(f"\nConnecting to {address} ...")
    async with BleakClient(address) as client:
        print(f"Connected: {client.is_connected}")

        meta = await _read_identity(client)
        print(f"Model: {meta.get('model') or 'unreadable'}  "
              f"Firmware: {meta.get('firmware') or 'unreadable'}")

        recorder = SessionRecorder(out_path, quiet=args.quiet)

        print("\n--- Subscribing ---")
        if args.pressure:
            await _enable_pressure(client)
        await _subscribe_all(client, recorder, args.pressure)

        print("\n--- Baseline ---")
        await _read_baseline(client, recorder)

        print(f"\n--- Recording for {args.seconds} s ---")
        print("Start brushing now. Run the full routine, then let the handle "
              "switch itself off.\nPress Ctrl+C to stop early; the recording is "
              "written either way.\n")

        deadline = time.monotonic() + args.seconds
        try:
            if args.poll_interval > 0:
                await _poll_loop(client, recorder, args.poll_interval, deadline)
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
