"""JSON → coordinator-data adapter for the Condor protocol.

Condor ports expose their state as UTF-8 JSON. This module translates
those JSON objects into the flat ``coordinator.data`` keys shared with
Legacy — entities stay protocol-agnostic as long as the two protocols
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

from .const import HANDLE_STATES, INTENSITIES

_LOGGER = logging.getLogger(__name__)

_PortMapper = Callable[[dict[str, Any], dict[str, Any]], None]


# Universal routine-id → mode-label table used by all Condor devices.
# Devices always report ``RoutineStatus.Mode`` as an ordinal 0..5,
# regardless of which modes a specific model exposes in its UI. Labels
# differ slightly from Legacy's ``BRUSHING_MODES``: Condor uses
# ``white`` / ``gum_care`` / ``deep_clean``, Legacy (Prestige) uses
# ``white_plus`` / ``gum_health`` / ``deep_clean_plus``.
CONDOR_BRUSHING_MODES: dict[int, str] = {
    0: "clean",
    1: "white",
    2: "gum_care",
    3: "tongue_care",
    4: "deep_clean",
    5: "sensitive",
}


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
        # Condor lacks Legacy's explicit brushing_state char — derive a
        # coarse on/off from HandleState so entities that key off that
        # label keep working. Pause is not distinguishable here; the
        # RoutineStatus.Duration stream still reflects real activity.
        running = v == 2  # HandleState.Run
        out["brushing_state"] = "on" if running else "off"
        out["brushing_state_value"] = 1 if running else 0
    # Condor HandleTime is a Unix epoch timestamp (wall-clock from the
    # handle) — the same key Legacy exposes as a seconds counter. Both
    # go into ``handle_time`` as-is; a future refactor could split them.
    if (v := props.get("HandleTime")) is not None:
        out["handle_time"] = v


def _map_routine_status(props: dict[str, Any], out: dict[str, Any]) -> None:
    """Port ``RoutineStatus`` — the live brushing session."""
    if (v := props.get("SessionID")) is not None:
        out["session_id"] = v
    # ``Mode`` is the routine id currently running — an ordinal 0..5 on
    # the universal numeric axis shared by every Condor device,
    # regardless of which modes a specific model exposes in its UI.
    # One shared label table (``CONDOR_BRUSHING_MODES``) is enough.
    if (v := props.get("Mode")) is not None:
        out["brushing_mode_value"] = v
        mapped = CONDOR_BRUSHING_MODES.get(v)
        if mapped is None:
            _LOGGER.warning("Condor RoutineStatus.Mode unknown: %d", v)
        out["brushing_mode"] = mapped
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
    """Port ``BrushHead`` — NFC-sourced head identity + wear."""
    v = props.get("SerialNumber")
    if isinstance(v, list) and v:
        out["brushhead_serial"] = ":".join(f"{b:02X}" for b in v)
    if (v := props.get("LifetimeLimit")) is not None:
        out["brushhead_lifetime_limit"] = v
    if (v := props.get("LifetimeUsage")) is not None:
        out["brushhead_lifetime_usage"] = v
    if (v := props.get("RingId")) is not None:
        out["brushhead_ring_id"] = v


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
    # Condor's FeatureCtrl is the same 0x4420 settings bitmask Legacy
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
