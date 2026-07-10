"""JSON → coordinator-data adapter for the Condor protocol.

Condor ports expose their state as UTF-8 JSON. This module translates
those JSON objects into the flat ``coordinator.data`` keys shared with
Classic — entities stay protocol-agnostic as long as the two protocols
agree on the dict shape.

The same mapper handles both full-state (``GetProps``) and partial
(``ChangeIndication``) responses because Condor uses identical key
names in both directions: a delta omits unchanged properties, never
renames them. Callers merge the returned dict over prior state.

Mapping derived from lonlazer's HX742X probe output (Issue #4
comments 4299950901 + 4307613040, firmware 1.8.20.0).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .const import (
    HANDLE_STATES,
    INTENSITIES,
    PRESSURE_ALARM_STATES,
    SENSOR_FRAME_GYROSCOPE,
    SENSOR_FRAME_PRESSURE,
    SENSOR_FRAME_TEMPERATURE,
)

_LOGGER = logging.getLogger(__name__)

_PortMapper = Callable[[dict[str, Any], dict[str, Any]], None]


def map_sensor_frame(body: bytes) -> dict[str, Any]:
    """Decode one ``SensorData.b`` binary telemetry frame into coordinator keys.

    The frame type is a little-endian u16 at offset 0 and the tail is
    type-specific:

    * **Pressure** (type 1): a 7-byte frame carries a raw value at bytes 4-5
      (little-endian grams) and the state at byte 6; the shorter on-change
      frame most Condor firmwares actually send carries only the state at
      byte 4 and no value. Frames below 5 bytes are counter-only heartbeats
      and map to nothing.
    * **Temperature** (type 2): degrees Celsius as a signed byte at offset 5.
      Byte 4 is a ``/256`` fraction that truncates to zero, so byte 5 is the
      whole value.
    * **IMU** (type 4): not surfaced — the pressure-only Condor models expose
      no gyroscope, and the stream is high-rate telemetry, not a HA sensor.

    ``pressure_state`` reuses ``PRESSURE_ALARM_STATES``: value ``2`` is the
    over-pressure ("too hard") signal, everything else reads as ok.
    """
    if len(body) < 2:
        return {}
    frame_type = int.from_bytes(body[0:2], "little")
    out: dict[str, Any] = {}
    if frame_type == SENSOR_FRAME_PRESSURE:
        if len(body) >= 7:
            out["pressure"] = int.from_bytes(body[4:6], "little", signed=True)
            state = body[6]
        elif len(body) >= 5:
            state = body[4]
        else:
            return {}
        out["pressure_alarm"] = state
        out["pressure_state"] = PRESSURE_ALARM_STATES.get(state)
    elif frame_type == SENSOR_FRAME_TEMPERATURE:
        if len(body) >= 6:
            out["temperature"] = int.from_bytes(body[5:6], "little", signed=True)
    elif frame_type == SENSOR_FRAME_GYROSCOPE:
        # Present in the format for completeness; pressure-only Condor models
        # never send it and we expose no IMU entities for them.
        return {}
    return out


# Universal routine-id → mode-label table used by all Condor devices.
# Devices always report ``RoutineStatus.Mode`` as an ordinal 0..5,
# regardless of which modes a specific model exposes in its UI. Labels
# differ slightly from Classic's ``BRUSHING_MODES``: Condor uses
# ``white`` / ``gum_care`` / ``deep_clean``, Classic (Prestige) uses
# ``white_plus`` / ``gum_health`` / ``deep_clean_plus``.
CONDOR_BRUSHING_MODES: dict[int, str] = {
    0: "clean",
    1: "white",
    2: "gum_care",
    3: "tongue_care",
    4: "deep_clean",
    5: "sensitive",
}


def resolve_brushing_mode(
    routine_ids: list[int] | None, mode_pos: int | None
) -> tuple[int, str | None] | None:
    """Resolve ``RoutineStatus.Mode`` to a (routine_id, label) pair.

    ``Mode`` is a **position** into the device's static ``RoutineIDs`` list
    (from the ``Sonicare`` port), not a routine id — the same numeric axis
    ``CONDOR_BRUSHING_MODES`` labels. Decoding ``Mode`` directly shows the
    wrong mode (e.g. a brush whose slot 1 is Sensitive reports Mode 1, which
    naively reads as "white"). Returns ``None`` when the list isn't known yet
    or the position is out of range, so the caller can leave the label unset.
    """
    if not routine_ids or mode_pos is None or not (0 <= mode_pos < len(routine_ids)):
        return None
    routine_id = routine_ids[mode_pos]
    return routine_id, CONDOR_BRUSHING_MODES.get(routine_id)


def map_port_props(port: str, props: dict[str, Any]) -> dict[str, Any]:
    """Translate one port's JSON properties into coordinator-data keys.

    Unknown ports return an empty dict (logged at debug level) so the
    adapter silently ignores unsupported JSON shapes instead of failing.
    Only keys actually present in ``props`` land in the output — that's
    what makes ChangeIndication deltas merge cleanly over prior state.
    """
    mapper = _PORT_MAPPERS.get(port)
    if mapper is None:
        _LOGGER.debug("Condor: no mapper for port %s (props=%s)", port, props)
        return {}
    out: dict[str, Any] = {}
    mapper(props, out)
    return out


# --- Per-port mappers ------------------------------------------------------


def _map_sonicare(props: dict[str, Any], out: dict[str, Any]) -> None:
    """Port ``Sonicare`` — runtime handle state + slot configuration."""
    if (v := props.get("HandleState")) is not None:
        out["handle_state_value"] = v
        mapped = HANDLE_STATES.get(v)
        if mapped is None:
            _LOGGER.warning("Condor Sonicare.HandleState unknown: %d", v)
        out["handle_state"] = mapped
        # Condor lacks Classic's explicit brushing_state char — derive a
        # coarse on/off from HandleState so entities that key off that
        # label keep working. Pause is not distinguishable here; the
        # RoutineStatus.Duration stream still reflects real activity.
        running = v == 2  # HandleState.Run
        out["brushing_state"] = "on" if running else "off"
        out["brushing_state_value"] = 1 if running else 0
    # Condor HandleTime is a Unix epoch timestamp (wall-clock from the
    # handle) — the same key Classic exposes as a seconds counter. Both
    # go into ``handle_time`` as-is; a future refactor could split them.
    if (v := props.get("HandleTime")) is not None:
        out["handle_time"] = v
    # The device's mode-slot configuration: a static list mapping each UI
    # position to a routine id. Kept so RoutineStatus.Mode (a position) can be
    # resolved to the real mode — see resolve_brushing_mode. Not user-facing.
    if isinstance(v := props.get("RoutineIDs"), list):
        out["routine_ids"] = list(v)


def _map_routine_status(props: dict[str, Any], out: dict[str, Any]) -> None:
    """Port ``RoutineStatus`` — the live brushing session."""
    if (v := props.get("SessionID")) is not None:
        out["session_id"] = v
    # ``Mode`` is a position into the device's ``RoutineIDs`` list, not a
    # routine id. We can't resolve the label here (RoutineIDs lives on the
    # ``Sonicare`` port); expose the raw position and let the coordinator
    # translate it once both are merged — see resolve_brushing_mode.
    if (v := props.get("Mode")) is not None:
        out["brushing_mode_value"] = v
    if (v := props.get("Duration")) is not None:
        out["brushing_time"] = v
    if (v := props.get("Length")) is not None:
        out["routine_length"] = v
    if (v := props.get("Intensity")) is not None:
        out["intensity_value"] = v
        mapped = INTENSITIES.get(v)
        if mapped is None:
            _LOGGER.warning("Condor RoutineStatus.Intensity unknown: %d", v)
        out["intensity"] = mapped


def _map_battery(props: dict[str, Any], out: dict[str, Any]) -> None:
    if (v := props.get("BatteryPercent")) is not None:
        out["battery"] = v


def _map_brush_head(props: dict[str, Any], out: dict[str, Any]) -> None:
    """Port ``BrushHead`` — NFC-sourced head identity + wear.

    Observed payload on HX742X FW 1.8.20.0 (lonlazer's GetProps probe,
    Issue #4 comment 4299950901)::

        {"NfcTagVersion":[2,1], "FactoryMode":2,
         "SerialNumber":[4,43,197,178,75,30,144],
         "LifetimeLimit":21600, "LifetimeUsage":16777, "RingId":4}

    ``Date``, ``Type`` and ``Payload`` aren't present in the Condor port
    (they exist as separate GATT chars on Classic only), so the sensors
    keying off them stay unavailable for Condor devices. ``FactoryMode``
    is a build-tag flag with no user-facing equivalent.
    """
    v = props.get("SerialNumber")
    if isinstance(v, list) and v:
        out["brushhead_serial"] = ":".join(f"{b:02X}" for b in v)
    if (v := props.get("LifetimeLimit")) is not None:
        out["brushhead_lifetime_limit"] = v
    if (v := props.get("LifetimeUsage")) is not None:
        out["brushhead_lifetime_usage"] = v
    if (v := props.get("RingId")) is not None:
        out["brushhead_ring_id"] = v
    v = props.get("NfcTagVersion")
    if isinstance(v, list) and v:
        out["brushhead_nfc_version"] = ".".join(str(b) for b in v)


def _map_session_storage(props: dict[str, Any], out: dict[str, Any]) -> None:
    if (v := props.get("LatestID")) is not None:
        out["latest_session_id"] = v
    if (v := props.get("Count")) is not None:
        out["session_count"] = v


def _map_diagnostics(props: dict[str, Any], out: dict[str, Any]) -> None:
    if (v := props.get("PErrors")) is not None:
        out["error_persistent"] = v
    if (v := props.get("VErrors")) is not None:
        out["error_volatile"] = v


def _map_extended(props: dict[str, Any], out: dict[str, Any]) -> None:
    # Condor's FeatureCtrl is the same 0x4420 settings bitmask Classic
    # exposes as ``CHAR_SETTINGS`` — same bit positions, same semantics.
    if (v := props.get("FeatureCtrl")) is not None:
        out["settings_bitmask"] = v


def _map_firmware(props: dict[str, Any], out: dict[str, Any]) -> None:
    """Product 0 / ``firmware`` — model name and firmware version.

    Duplicates what Device Information Service (0x180A) exposes via
    standard BLE characteristics, but sourcing it through Condor keeps
    the protocol self-contained.
    """
    if v := props.get("name"):
        out["model_number"] = v
    if v := props.get("version"):
        out["firmware"] = v


_PORT_MAPPERS: dict[str, _PortMapper] = {
    "firmware": _map_firmware,
    "Sonicare": _map_sonicare,
    "RoutineStatus": _map_routine_status,
    "Battery": _map_battery,
    "BrushHead": _map_brush_head,
    "SessionStorage": _map_session_storage,
    "Diagnostics": _map_diagnostics,
    "Extended": _map_extended,
    # SensorData JSON props (Sensors/Types/Control bitmasks) are control
    # state, not telemetry — no coordinator keys yet.
    # SensorData.b is the pressure/temperature binary stream; format is
    # Condor-specific and not yet cross-verified, deferred to Phase 3.
}
