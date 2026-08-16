"""Classic protocol implementation — one GATT characteristic per property.

Used by HX992X, HX9992 / Prestige 9900, HX6340 / Kids, HX962V, HX991M,
HX9996 and other Sonicare models that expose Philips' original service
``477ea600-a260-11e4-ae37-0002a5d5…``. Each property (brushing mode,
intensity, session data, brush-head info, …) lives on its own 16-byte
characteristic with simple binary encoding.

This is an intentionally thin wrapper around ``SonicareTransport``.
The coordinator still owns the connection lifecycle and state
bookkeeping (last-seen, sensor-stream gating, device-registry sync);
this class holds the pure transform between raw GATT bytes and
``coordinator.data`` keys, plus the primitive GATT IO operations.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable
from typing import Any

from .const import (
    BRUSHHEAD_TYPES,
    BRUSHING_MODES,
    BRUSHING_STATES,
    CHAR_AVAILABLE_ROUTINE_IDS,
    CHAR_BATTERY_LEVEL,
    CHAR_BRUSHHEAD_DATE,
    CHAR_BRUSHHEAD_LIFETIME_LIMIT,
    CHAR_BRUSHHEAD_LIFETIME_USAGE,
    CHAR_BRUSHHEAD_NFC_VERSION,
    CHAR_BRUSHHEAD_PAYLOAD,
    CHAR_BRUSHHEAD_RING_ID,
    CHAR_BRUSHHEAD_SERIAL,
    CHAR_BRUSHHEAD_TYPE,
    CHAR_BRUSHING_MODE,
    CHAR_BRUSHING_STATE,
    CHAR_BRUSHING_TIME,
    CHAR_ERROR_PERSISTENT,
    CHAR_ERROR_VOLATILE,
    CHAR_FIRMWARE_REVISION,
    CHAR_HANDLE_STATE,
    CHAR_HANDLE_TIME,
    CHAR_HARDWARE_REVISION,
    CHAR_INTENSITY,
    CHAR_LATEST_SESSION_ID,
    CHAR_MANUFACTURER_NAME,
    CHAR_MODEL_NUMBER,
    CHAR_MOTOR_RUNTIME,
    CHAR_ROUTINE_LENGTH,
    CHAR_ACTIVE_SESSION_ID,
    CHAR_SENSOR_DATA,
    CHAR_SENSOR_ENABLE,
    CHAR_SERIAL_NUMBER,
    CHAR_SESSION_ACTION,
    CHAR_SESSION_COUNT,
    CHAR_SESSION_DATA,
    CHAR_SESSION_ID,
    CHAR_SESSION_TYPE,
    CHAR_SETTINGS,
    CHAR_SOFTWARE_REVISION,
    HANDLE_STATES,
    INTENSITIES,
    PRESSURE_ALARM_STATES,
    SENSOR_FRAME_PRESSURE,
    SENSOR_FRAME_TEMPERATURE,
    SESSION_ACTION_START,
    MAX_DURATION_FACTOR,
    MAX_ROUTINE_SECONDS,
    SESSION_RECORD_MIN_LEN,
    SESSION_RECORD_ROUTINE,
    brushing_mode_for_model,
    uses_routine_id_mode,
)
from .protocol import SonicareProtocol, UpdateCallback
from .transport import EspBridgeTransport

_LOGGER = logging.getLogger(__name__)


class ClassicProtocol(SonicareProtocol):
    """Direct-GATT protocol: every property is a distinct characteristic."""

    # --- Session lifecycle -------------------------------------------------
    # Classic has no framed session; the transport-level GATT connection
    # is all that's needed.

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    # --- High-level lifecycle placeholders --------------------------------
    # The coordinator drives reads/subscribes via the primitive GATT
    # methods below because Classic needs per-call connection checks and
    # retry behavior that the state-free protocol interface can't model
    # without duplicating coordinator logic. Condor will implement these
    # directly since its framed transport owns its own session state.

    async def refresh_all(self):
        raise NotImplementedError("Classic refresh still lives in coordinator")

    async def start_live_updates(self, on_update: UpdateCallback) -> None:
        raise NotImplementedError("Classic subscribe still lives in coordinator")

    async def stop_live_updates(self) -> None:
        raise NotImplementedError("Classic unsubscribe still lives in coordinator")

    # --- GATT primitives ---------------------------------------------------

    async def read_chars(
        self, uuids: list[str]
    ) -> dict[str, bytes | None]:
        """Read a batch of characteristics.

        Stops early when the transport drops, so a reconnect can restart
        from a clean state rather than accumulating partial data. Per-char
        failures are logged at debug level and result in a ``None`` entry,
        letting parse_results simply skip them.
        """
        if isinstance(self._transport, EspBridgeTransport):
            # The bridge serialises overlapping GATT ops itself, so its
            # read_chars can fire the whole batch concurrently (gated on
            # bridge version there). Direct BLE must NOT take this path:
            # BleakTransport.read_chars is a connect-read-disconnect poll
            # helper and would tear down the persistent connection.
            return await self._transport.read_chars(uuids)

        results: dict[str, bytes | None] = {}
        for uuid in uuids:
            if not self._transport.is_connected:
                break
            try:
                results[uuid] = await self._transport.read_char(uuid)
            except Exception as e:
                _LOGGER.debug("Read failed for %s: %s", uuid, e)
                results[uuid] = None
        return results

    async def subscribe_notifications(
        self,
        uuids: list[str],
        callback: Callable[[str, bytes], None],
    ) -> int:
        """Subscribe the same callback to each characteristic.

        Returns the number of successful subscriptions. Failures are
        warn-logged but don't abort the batch — a partially subscribed
        device is still useful.
        """
        count = 0
        for uuid in uuids:
            try:
                await self._transport.subscribe(uuid, callback)
                count += 1
                _LOGGER.debug("subscribed to %s", uuid)
            except Exception as e:
                _LOGGER.warning("Failed to subscribe %s: %s", uuid, e)
        return count

    async def unsubscribe_all(self) -> None:
        await self._transport.unsubscribe_all()

    # --- Live sensor stream (pressure / temperature / gyroscope) ----------
    # The sensor-data characteristic (0x4130) only emits while the sensors
    # are explicitly enabled via the enable bitmask on 0x4120. The handle
    # powers down the sensor frontend when the bitmask is cleared, so
    # enabling must happen before subscribe and disabling after unsubscribe.

    async def start_sensor_stream(
        self,
        enable_mask: int,
        callback: Callable[[str, bytes], None],
    ) -> bool:
        """Turn the sensor frontend on and subscribe to its notification
        stream. Returns True if the subscribe succeeded. The enable write
        is attempted even if a previous write failed, matching the OEM
        app's best-effort behavior — some firmwares emit data even with a
        stale enable-bitmask."""
        try:
            await self._transport.write_char(
                CHAR_SENSOR_ENABLE, bytes([enable_mask])
            )
            _LOGGER.debug("Sensor enable written: 0x%02X", enable_mask)
        except Exception as e:
            _LOGGER.warning("Failed to write sensor enable: %s", e)
        try:
            await self._transport.subscribe(CHAR_SENSOR_DATA, callback)
            return True
        except Exception as e:
            _LOGGER.warning("Failed to subscribe sensor data: %s", e)
            return False

    async def stop_sensor_stream(self) -> None:
        """Drop the sensor-data subscription and power the frontend down
        by clearing the enable bitmask. Both calls are best-effort: a
        transport teardown may already have invalidated the subscription."""
        try:
            await self._transport.unsubscribe(CHAR_SENSOR_DATA)
        except Exception:
            pass
        try:
            await self._transport.write_char(CHAR_SENSOR_ENABLE, bytes([0x00]))
        except Exception:
            pass

    # --- Parsing -----------------------------------------------------------

    def parse_results(
        self, results: dict[str, bytes | None]
    ) -> dict[str, Any]:
        """Decode raw GATT payloads into coordinator-data keys.

        Pure transform: given a ``{char_uuid: bytes}`` dict, returns a flat
        ``{data_key: value}`` dict. Mappings (mode IDs, intensity IDs,
        handle states …) are resolved against the tables in ``const.py``.
        Only characteristics present in ``results`` with non-None bytes
        produce entries in the output; derived fields and state-change
        side effects are the coordinator's responsibility.
        """
        out: dict[str, Any] = {}

        # Standard BLE characteristics
        if raw := results.get(CHAR_BATTERY_LEVEL):
            out["battery"] = raw[0]
        if raw := results.get(CHAR_FIRMWARE_REVISION):
            out["firmware"] = raw.decode("utf-8", "ignore").strip()
        if raw := results.get(CHAR_HARDWARE_REVISION):
            out["hardware_revision"] = raw.decode("utf-8", "ignore").strip()
        if raw := results.get(CHAR_SOFTWARE_REVISION):
            out["software_revision"] = raw.decode("utf-8", "ignore").strip()
        if raw := results.get(CHAR_MODEL_NUMBER):
            out["model_number"] = raw.decode("utf-8", "ignore").strip()
        if raw := results.get(CHAR_SERIAL_NUMBER):
            out["serial_number"] = raw.decode("utf-8", "ignore").strip()
        if raw := results.get(CHAR_MANUFACTURER_NAME):
            out["manufacturer_name"] = raw.decode("utf-8", "ignore").strip()

        # Brushing mode is reported by different characteristics with
        # incompatible numbering schemes, gated on the model family:
        #   - HX9996 / HX999X: 0x4022 carries the selected mode-id, decoded
        #     with BRUSHING_MODES.
        #   - every other handle: 0x4080 carries a sequential device-mode
        #     index, decoded per-model via brushing_mode_for_model().
        # On the latter, 0x4022 is instead the (constant) available-id list,
        # NOT the selected mode, so it is ignored here. Decoding 0x4080 with
        # the mode-id table swaps modes 3/4 (tongue_care <-> deep_clean_plus)
        # and mis-shifts models whose list omits an entry (e.g. HX960X has no
        # white mode). Verified on handle captures 2026-06-01
        # (HX999X / HX992X / HX960X).
        routine_id_mode = uses_routine_id_mode(self.model)
        if routine_id_mode and (raw := results.get(CHAR_AVAILABLE_ROUTINE_IDS)):
            mode_value = raw[0]
            mapped = BRUSHING_MODES.get(mode_value)
            if mapped is None:
                _LOGGER.warning(
                    "Unknown routine id on 0x4022: %d (raw: %s)",
                    mode_value, raw.hex(),
                )
            out["brushing_mode_value"] = mode_value
            out["brushing_mode"] = mapped

        if raw := results.get(CHAR_HANDLE_STATE):
            state_byte = raw[0]
            out["handle_state_value"] = state_byte
            mapped = HANDLE_STATES.get(state_byte)
            if mapped is None:
                _LOGGER.warning(
                    "Unknown handle_state value: %d (raw: %s)",
                    state_byte, raw.hex(),
                )
            out["handle_state"] = mapped

        if not routine_id_mode and (raw := results.get(CHAR_BRUSHING_MODE)):
            if len(raw) >= 2:
                mode_value = int.from_bytes(raw[:2], "little")
            else:
                mode_value = raw[0]
            mapped = brushing_mode_for_model(self.model, mode_value)
            if mapped is None:
                _LOGGER.warning(
                    "Unknown brushing_mode index %d for model %s (raw: %s)",
                    mode_value, self.model or "?", raw.hex(),
                )
            out["brushing_mode_value"] = mode_value
            out["brushing_mode"] = mapped

        if raw := results.get(CHAR_BRUSHING_STATE):
            state_value = raw[0]
            out["brushing_state_value"] = state_value
            mapped = BRUSHING_STATES.get(state_value)
            if mapped is None:
                _LOGGER.warning(
                    "Unknown brushing_state value: %d (raw: %s)",
                    state_value, raw.hex(),
                )
            out["brushing_state"] = mapped

        if raw := results.get(CHAR_INTENSITY):
            intensity_value = raw[0]
            out["intensity_value"] = intensity_value
            mapped = INTENSITIES.get(intensity_value)
            if mapped is None:
                _LOGGER.warning(
                    "Unknown intensity value: %d (raw: %s)",
                    intensity_value, raw.hex(),
                )
            out["intensity"] = mapped

        # Uint16 LE counters / timers
        for uuid, key in (
            (CHAR_BRUSHING_TIME, "brushing_time"),
            (CHAR_ROUTINE_LENGTH, "routine_length"),
            (CHAR_SESSION_ID, "session_id"),
            (CHAR_LATEST_SESSION_ID, "latest_session_id"),
            (CHAR_SESSION_COUNT, "session_count"),
            (CHAR_BRUSHHEAD_LIFETIME_LIMIT, "brushhead_lifetime_limit"),
            (CHAR_BRUSHHEAD_LIFETIME_USAGE, "brushhead_lifetime_usage"),
            (CHAR_BRUSHHEAD_NFC_VERSION, "brushhead_nfc_version"),
            (CHAR_BRUSHHEAD_RING_ID, "brushhead_ring_id"),
        ):
            if raw := results.get(uuid):
                out[key] = int.from_bytes(raw[:2], "little")

        # Uint32 LE counters / timers
        for uuid, key in (
            (CHAR_MOTOR_RUNTIME, "motor_runtime"),
            (CHAR_HANDLE_TIME, "handle_time"),
            (CHAR_ERROR_PERSISTENT, "error_persistent"),
            (CHAR_ERROR_VOLATILE, "error_volatile"),
        ):
            if raw := results.get(uuid):
                out[key] = int.from_bytes(raw[:4], "little")

        # Brush head identity and payload. Only publish a real NFC read — an
        # all-zeros serial is the "chip not scanned" placeholder (head removal
        # is handled separately via the serial notification).
        if (raw := results.get(CHAR_BRUSHHEAD_SERIAL)) and any(b != 0 for b in raw):
            out["brushhead_serial"] = ":".join(f"{b:02X}" for b in raw)
        if raw := results.get(CHAR_BRUSHHEAD_DATE):
            out["brushhead_date"] = raw.decode("utf-8", "ignore").strip()
        if raw := results.get(CHAR_BRUSHHEAD_TYPE):
            out["brushhead_type"] = BRUSHHEAD_TYPES.get(
                raw[0], f"unknown_{raw[0]}"
            )
        if raw := results.get(CHAR_BRUSHHEAD_PAYLOAD):
            try:
                text = raw.decode("utf-8")
                out["brushhead_payload"] = text if text.isprintable() else raw.hex()
            except (UnicodeDecodeError, ValueError):
                out["brushhead_payload"] = raw.hex()

        # Settings bitmask is 2 bytes on some firmwares, 4 on others —
        # pad to 4 for a stable uint32.
        if raw := results.get(CHAR_SETTINGS):
            out["settings_bitmask"] = int.from_bytes(
                raw[:4].ljust(4, b"\x00"), "little"
            )

        # Sensor data stream: different frame types share the same
        # characteristic. First 2 bytes identify the frame.
        if raw := results.get(CHAR_SENSOR_DATA):
            if len(raw) >= 4:
                frame_type = struct.unpack("<H", raw[:2])[0]
                if frame_type == SENSOR_FRAME_PRESSURE and len(raw) >= 7:
                    out["pressure"] = struct.unpack("<h", raw[4:6])[0]
                    alarm_value = raw[6]
                    out["pressure_alarm"] = alarm_value
                    out["pressure_state"] = PRESSURE_ALARM_STATES.get(alarm_value)
                elif frame_type == SENSOR_FRAME_TEMPERATURE and len(raw) >= 6:
                    out["temperature"] = round(
                        struct.unpack("<H", raw[4:6])[0] / 256, 1
                    )

        return out

    # --- Writes ------------------------------------------------------------

    async def fetch_stored_session(
        self, session_id: int | None = None, timeout: float = 5.0
    ) -> dict[str, Any] | None:
        """Fetch the handle's own record of a finished session.

        The handle keeps its sessions rather than only reporting them as they
        happen, which makes this the authoritative version of a session: what
        the handle concluded, still available when nobody was listening at
        the moment it ended.

        Selecting a record and receiving it are separate steps - the kind and
        the session are written first, the transfer is then started, and the
        record arrives as a notification. The subscription is set up per
        fetch and taken down again afterwards, so the characteristic stays
        quiet the rest of the time.

        Returns the decoded record, or ``None`` when the handle did not
        answer or answered with something too short to be one.
        """
        if session_id is None:
            raw = await self._transport.read_char(CHAR_LATEST_SESSION_ID)
            if not raw:
                _LOGGER.debug("Stored session: handle did not report a latest id")
                return None
            session_id = int.from_bytes(bytes(raw)[:2], "little")

        if self.direct_session_read:
            record = await self._read_session_record(session_id)
            return await self._decode_fetched(record, session_id, verify=True)

        loop = asyncio.get_running_loop()
        received: asyncio.Future[bytes] = loop.create_future()

        def _on_record(_uuid: str, data: bytes) -> None:
            if not received.done():
                received.set_result(bytes(data))

        await self._transport.subscribe(CHAR_SESSION_DATA, _on_record)
        try:
            await self._transport.write_char(
                CHAR_SESSION_TYPE, bytes([SESSION_RECORD_ROUTINE])
            )
            await self._transport.write_char(
                CHAR_ACTIVE_SESSION_ID, session_id.to_bytes(2, "little")
            )
            await self._transport.write_char(
                CHAR_SESSION_ACTION, bytes([SESSION_ACTION_START])
            )
            record = await asyncio.wait_for(received, timeout)
        except asyncio.TimeoutError:
            _LOGGER.debug("Stored session %d: no answer within %.0fs",
                          session_id, timeout)
            return None
        except Exception as err:  # noqa: BLE001 - a refused write is not fatal
            _LOGGER.debug("Stored session %d could not be requested: %s",
                          session_id, err)
            return None
        finally:
            try:
                await self._transport.unsubscribe(CHAR_SESSION_DATA)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Could not unsubscribe session data: %s", err)

        return await self._decode_fetched(record, session_id, chunked=True)

    async def _read_session_record(self, session_id: int) -> bytes | None:
        """Select a session, then read its record.

        The shorter exchange, for a handle with no kind selector: there is
        only one kind of record to ask for, so choosing the session is the
        whole request.
        """
        try:
            await self._transport.write_char(
                CHAR_ACTIVE_SESSION_ID, session_id.to_bytes(2, "little")
            )
            raw = await self._transport.read_char(CHAR_SESSION_DATA)
        except Exception as err:  # noqa: BLE001 - a refused request is not fatal
            _LOGGER.debug("Stored session %d could not be read: %s", session_id, err)
            return None
        return bytes(raw) if raw else None

    async def _decode_fetched(
        self, record: bytes | None, session_id: int, chunked: bool = False,
        verify: bool = False
    ) -> dict[str, Any] | None:
        """Decode a fetched record and read the clock that dates it."""
        if not record:
            return None
        decoded = decode_session_record(record, self.model, chunked=chunked)
        if decoded is None:
            _LOGGER.debug("Stored session %d: answer too short (%s)",
                          session_id, record.hex())
            return None
        # Reading the record straight after selecting it means nothing
        # confirms the selection took: a handle asked for a session it does
        # not have answers with the one still in its buffer, and says
        # nothing about it. The record names the session it describes, so
        # that is what decides - a mismatch is the previous answer, not this
        # session.
        if verify and decoded["session_id"] != session_id:
            _LOGGER.debug(
                "Asked for stored session %d, got %d — the selection did not "
                "take", session_id, decoded["session_id"],
            )
            return None

        # The handle's running counter, read as close to the record as the
        # link allows. It is the same counter the record is stamped with, so
        # the difference between the two is how long ago the session ended -
        # the only way to place a stored session in real time, since the
        # handle has no notion of the date.
        try:
            raw = await self._transport.read_char(CHAR_HANDLE_TIME)
            if raw and len(raw) >= 4:
                decoded["handle_clock"] = int.from_bytes(bytes(raw)[:4], "little")
        except Exception as err:  # noqa: BLE001 - only costs the anchor
            _LOGGER.debug("Could not read the handle clock: %s", err)
        _LOGGER.info(
            "Stored session %d: %ds of %ds, mode %s",
            decoded["session_id"], decoded["duration"],
            decoded["routine_length"], decoded["brushing_mode"] or "?",
        )
        return decoded

    async def set_brushing_mode(self, mode_key: str) -> None:
        mode_id = _reverse_lookup(BRUSHING_MODES, mode_key)
        if mode_id is None:
            raise ValueError(f"Unknown brushing mode: {mode_key}")
        await self._transport.write_char(
            CHAR_AVAILABLE_ROUTINE_IDS, bytes([mode_id])
        )
        _LOGGER.info("Brushing mode set to %s (id=%d)", mode_key, mode_id)

    async def set_intensity(self, intensity_key: str) -> None:
        intensity_id = _reverse_lookup(INTENSITIES, intensity_key)
        if intensity_id is None:
            raise ValueError(f"Unknown intensity: {intensity_key}")
        await self._transport.write_char(
            CHAR_INTENSITY, bytes([intensity_id])
        )
        _LOGGER.info("Intensity set to %s (id=%d)", intensity_key, intensity_id)

    async def read_settings_bitmask(self) -> int:
        raw = await self._transport.read_char(CHAR_SETTINGS)
        if raw and len(raw) >= 2:
            return int.from_bytes(raw[:4].ljust(4, b"\x00"), "little")
        return 0

    async def write_settings_bit(self, bit_mask: int, enabled: bool) -> None:
        current = await self.read_settings_bitmask()
        new_value = (current | bit_mask) if enabled else (current & ~bit_mask)
        payload = new_value.to_bytes(4, "little")
        await self._transport.write_char(CHAR_SETTINGS, payload)
        _LOGGER.info(
            "Settings updated: bit 0x%04x %s (0x%08x → 0x%08x)",
            bit_mask, "ON" if enabled else "OFF", current, new_value,
        )


def decode_session_record(
    record: bytes, model: str | None, chunked: bool = False
) -> dict[str, Any] | None:
    """Decode the routine record of one stored session.

    Layout, verified against the live characteristics of the same session on
    a handle that reported model HX999X (2026-08-16): the session id matched
    ``latest_session_id``, the duration matched the peak of the live brushing
    timer before it was wiped, and the target matched ``routine_length``.

    A record that arrives as a notification is chunked and leads with a byte
    for that, which the announced transfer length does not count - so its
    fields sit one byte later than in a record that was simply read. Same
    record, same order, one byte of framing.

    Where the mode sits depends on the handle, in the same split the live
    path already makes: the models that report their selected routine as an
    id on 0x4022 carry that id here too, while the others carry the mode
    index that 0x4080 would report. Reading the wrong one yields a plausible
    but wrong mode, so the model decides - as it does for the live value.

    ``timestamp`` is the handle's own clock at the moment the session
    *began*, not when it ended - measured against a session that ran from
    14:51:03 to 14:53:13, counting from the stamp landed on its start. It is
    not wall time either: a handle whose clock was never set counts from
    zero, so the value only means something next to another reading of the
    same clock.
    """
    base = 1 if chunked else 0
    if len(record) < base + SESSION_RECORD_MIN_LEN - 1:
        return None

    routine_id_mode = uses_routine_id_mode(model or "")
    mode_value = record[base + 12] if routine_id_mode else record[base + 10]
    if routine_id_mode:
        mode = BRUSHING_MODES.get(mode_value)
    else:
        mode = brushing_mode_for_model(model or "", mode_value)
    if mode is None:
        _LOGGER.debug(
            "Stored session carries an unmapped mode %d for model %s (raw: %s)",
            mode_value, model or "?", record.hex(),
        )

    routine_length = int.from_bytes(record[base + 8:base + 10], "little")
    duration = int.from_bytes(record[base + 6:base + 8], "little")
    if not 0 < routine_length <= MAX_ROUTINE_SECONDS or (
        duration > routine_length * MAX_DURATION_FACTOR
    ):
        _LOGGER.debug(
            "Stored session does not read as a session (%ds of a %ds routine) "
            "— the handle's record format is not the one this decodes: %s",
            duration, routine_length, record.hex(),
        )
        return None

    return {
        "session_id": int.from_bytes(record[base + 4:base + 6], "little"),
        "duration": duration,
        "routine_length": routine_length,
        "brushing_mode": mode,
        "brushing_mode_value": mode_value,
        "intensity": INTENSITIES.get(record[base + 11]),
        "intensity_value": record[base + 11],
        "timestamp": int.from_bytes(record[base:base + 4], "little"),
    }


def _reverse_lookup(mapping: dict[int, str], value: str) -> int | None:
    for key, name in mapping.items():
        if name == value:
            return key
    return None
