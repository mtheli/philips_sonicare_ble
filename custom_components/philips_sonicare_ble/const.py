"""Constants for the Philips Sonicare integration."""

DOMAIN = "philips_sonicare_ble"

# ── Discovery ────────────────────────────────────────────────────────────────
SONICARE_SERVICE_UUIDS = [
    "477ea600-a260-11e4-ae37-0002a5d50001",  # Sonicare Service (primary)
    "477ea600-a260-11e4-ae37-0002a5d50002",  # Routine Service
    "477ea600-a260-11e4-ae37-0002a5d50004",  # Storage Service
    "477ea600-a260-11e4-ae37-0002a5d50005",  # Sensor Service
    "477ea600-a260-11e4-ae37-0002a5d50006",  # Brush Head Service
    "477ea600-a260-11e4-ae37-0002a5d50007",  # Diagnostic Service
    "477ea600-a260-11e4-ae37-0002a5d50008",  # Extended Service
    "0000180f-0000-1000-8000-00805f9b34fb",  # Battery Service (0x180F)
    "0000180a-0000-1000-8000-00805f9b34fb",  # Device Information Service (0x180A)
]
SONICARE_MANUFACTURER_ID = 477

# Minimum ESP bridge component version required for full functionality.
# This is the hard compatibility floor — below it the integration raises a
# Repairs warning. It only bumps on breaking changes.
MIN_BRIDGE_VERSION = "1.4.0"

# Bridges from this version serialise overlapping GATT operations through a
# single-ATT-op scheduler (pending-calls queue, CCCD gate, ATT watchdog), so
# the transport may fire its poll cycle concurrently (asyncio.gather). Older
# bridges queue overlapping reads without those guards — a read racing the
# subscribe burst can lose its GATT event and wedge the queue — so the
# transport reads sequentially against those.
BRIDGE_PIPELINED_READS_VERSION = "1.7.0"

# ── ESP bridge firmware update entity ────────────────────────────────────────
# The latest available bridge firmware version is read straight from the repo
# (same VERSION file the firmware bakes in at build time) so users are notified
# of new bridge firmware without us shipping an integration release. The update
# entity is passive (no install) — flashing happens via ESPHome. Release notes
# are pulled lazily from CHANGELOG.md when the user opens the dialog.
_GH_RAW = "https://raw.githubusercontent.com/mtheli/philips_sonicare_ble/master"
BRIDGE_VERSION_URL = f"{_GH_RAW}/esphome/components/philips_sonicare/VERSION"
BRIDGE_CHANGELOG_URL = f"{_GH_RAW}/esphome/CHANGELOG.md"
BRIDGE_RELEASE_URL = (
    "https://github.com/mtheli/philips_sonicare_ble/blob/master/esphome/CHANGELOG.md"
)

# ── Service UUIDs ────────────────────────────────────────────────────────────
SVC_BATTERY = "0000180f-0000-1000-8000-00805f9b34fb"
SVC_DEVICE_INFO = "0000180a-0000-1000-8000-00805f9b34fb"
SVC_GATT = "00001801-0000-1000-8000-00805f9b34fb"
SVC_SONICARE = "477ea600-a260-11e4-ae37-0002a5d50001"
SVC_ROUTINE = "477ea600-a260-11e4-ae37-0002a5d50002"
# The Kids handle's own home for the session characteristics: it has no
# storage service, and keeps them here instead. No other model has this
# service at all, and on every other model the same characteristics live in
# the storage service - so which one to address is a property of the handle.
SVC_KIDS_SESSION = "477ea600-a260-11e4-ae37-0002a5d50003"
SVC_STORAGE = "477ea600-a260-11e4-ae37-0002a5d50004"
SVC_SENSOR = "477ea600-a260-11e4-ae37-0002a5d50005"
SVC_BRUSHHEAD = "477ea600-a260-11e4-ae37-0002a5d50006"
SVC_DIAGNOSTIC = "477ea600-a260-11e4-ae37-0002a5d50007"
SVC_EXTENDED = "477ea600-a260-11e4-ae37-0002a5d50008"
SVC_BYTESTREAM = "a651fff1-4074-4131-bce9-56d4261bc7b1"
# Condor — newer-protocol transport service (HX742X / Series 7100 family).
# All named properties travel over framed messages on this one service;
# Classic-style per-property chars are absent on these devices.
SVC_CONDOR = "e50ba3c0-af04-4564-92ad-fef019489de6"

# ── Condor Service Characteristics (e50b…) ───────────────────────────────────
# Framed transport: app→device on RX/TX_ACK, device→app on TX/RX_ACK.
# Version + channel negotiation happens on SERVER_CFG / CLIENT_CFG.
# PROTO_CFG (…0005) is absent on V4 firmware (e.g. HX742X 1.8.20.0).
CHAR_RX = "e50b0001-af04-4564-92ad-fef019489de6"
CHAR_RX_ACK = "e50b0002-af04-4564-92ad-fef019489de6"
CHAR_TX = "e50b0003-af04-4564-92ad-fef019489de6"
CHAR_TX_ACK = "e50b0004-af04-4564-92ad-fef019489de6"
CHAR_PROTO_CFG = "e50b0005-af04-4564-92ad-fef019489de6"
CHAR_SERVER_CFG = "e50b0006-af04-4564-92ad-fef019489de6"
CHAR_CLIENT_CFG = "e50b0007-af04-4564-92ad-fef019489de6"

# ── Standard BLE Characteristics ─────────────────────────────────────────────
CHAR_BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"
CHAR_MODEL_NUMBER = "00002a24-0000-1000-8000-00805f9b34fb"
CHAR_SERIAL_NUMBER = "00002a25-0000-1000-8000-00805f9b34fb"
CHAR_FIRMWARE_REVISION = "00002a26-0000-1000-8000-00805f9b34fb"
CHAR_HARDWARE_REVISION = "00002a27-0000-1000-8000-00805f9b34fb"
CHAR_SOFTWARE_REVISION = "00002a28-0000-1000-8000-00805f9b34fb"
CHAR_MANUFACTURER_NAME = "00002a29-0000-1000-8000-00805f9b34fb"

# ── Sonicare Service (0x0001) ────────────────────────────────────────────────
# Handle State: 0=off, 1=standby, 2=run, 3=charge, 4=shutdown, 6=validate, 7=background
CHAR_HANDLE_STATE = "477ea600-a260-11e4-ae37-0002a5d54010"
CHAR_AVAILABLE_ROUTINES = "477ea600-a260-11e4-ae37-0002a5d54020"
CHAR_AVAILABLE_ROUTINE_IDS = "477ea600-a260-11e4-ae37-0002a5d54022"
# Which feature the handle currently has active. The name is established,
# the contents are not. Not read by this integration yet.
CHAR_ACTIVE_FEATURE = "477ea600-a260-11e4-ae37-0002a5d54030"
CHAR_MOTOR_RUNTIME = "477ea600-a260-11e4-ae37-0002a5d54040"
CHAR_HANDLE_TIME = "477ea600-a260-11e4-ae37-0002a5d54050"

# ── Routine Service (0x0002) ─────────────────────────────────────────────────
# Current session ID
CHAR_SESSION_ID = "477ea600-a260-11e4-ae37-0002a5d54070"
# Brushing mode: 0=clean, 1=white+, 2=gum_health, 3=deep_clean+
CHAR_BRUSHING_MODE = "477ea600-a260-11e4-ae37-0002a5d54080"
# Brushing state: 0=off, 1=on, 2=pause
CHAR_BRUSHING_STATE = "477ea600-a260-11e4-ae37-0002a5d54082"
# Brushing time in seconds (uint16 LE)
CHAR_BRUSHING_TIME = "477ea600-a260-11e4-ae37-0002a5d54090"
# Routine length in seconds (uint16 LE)
CHAR_ROUTINE_LENGTH = "477ea600-a260-11e4-ae37-0002a5d54091"
# The segment of the handle's own quadrant pacer - the thing that
# buzzes every 30 seconds to move you along. Present on every Classic
# handle. Not read yet: the card derives sectors from elapsed time,
# and whether this is better has to be measured first.
CHAR_QUADPACER_SEGMENT = "477ea600-a260-11e4-ae37-0002a5d540a0"
# Intensity: 0=low, 1=medium, 2=high
CHAR_INTENSITY = "477ea600-a260-11e4-ae37-0002a5d540b0"
# Easy-start stage: the gentle run-in that raises power over the first
# weeks of use.
CHAR_EASY_START_STAGE = "477ea600-a260-11e4-ae37-0002a5d540c0"

# ── Storage Service (0x0004) ─────────────────────────────────────────────────
CHAR_LATEST_SESSION_ID = "477ea600-a260-11e4-ae37-0002a5d540d0"
CHAR_SESSION_COUNT = "477ea600-a260-11e4-ae37-0002a5d540d2"
CHAR_SESSION_TYPE = "477ea600-a260-11e4-ae37-0002a5d540d5"
CHAR_ACTIVE_SESSION_ID = "477ea600-a260-11e4-ae37-0002a5d540e0"
CHAR_SESSION_DATA = "477ea600-a260-11e4-ae37-0002a5d54100"
CHAR_SESSION_ACTION = "477ea600-a260-11e4-ae37-0002a5d54110"
# Which session the handle has loaded for reading. It answers a selection
# rather than announcing anything: the handle reports the session it just
# loaded, which is how a caller knows the selection took before reading the
# record. Kept for reference - the record names its own session, so this
# integration checks that instead of subscribing here.
CHAR_LOADED_SESSION = "477ea600-a260-11e4-ae37-0002a5d540f0"

# A finished session is kept on the handle and is not read directly: the
# record wanted is selected first (kind, then which session), the transfer is
# then started, and the record arrives as a notification on CHAR_SESSION_DATA.
# The notification on CHAR_ACTIVE_SESSION_ID that precedes it announces the
# transfer: session id, payload length, and two trailing bytes.
#
# Only the routine record is requested. It is the one that describes the
# session itself, and asking for it alone keeps the exchange to a single
# round-trip in the seconds between the motor stopping and the handle
# switching itself off.
SESSION_RECORD_ROUTINE = 0
SESSION_ACTION_START = 0
# The control point answers as well as takes orders: when a transfer is done
# the handle notifies its status there, and that - not the arrival of data -
# is what says the record is complete. A record may be one notification or
# several dozen, so anything that treats the first one as the whole answer
# asks for the next record while the handle is still sending this one, and
# the handle, still in a transfer, does not answer again.
SESSION_ACTION_CANCEL = 2
# Status 0 is a complete record. Status 1 is an answer too: the handle sent
# what it had and says so, which leaves a record that may be short rather
# than no record at all.
SESSION_STATUS_COMPLETE = 0
SESSION_STATUS_PARTIAL = 1

# Not every handle keeps its sessions behind a storage service. A Sonicare
# for Kids has none: the same characteristics sit in the brushing service,
# and the two that drive the selection - the kind and the control point -
# are absent. Its data characteristic is readable instead of notifying, so
# the record is selected and then simply read.
#
# Both shapes answer with the same record. The one difference is that a
# notified record is chunked and carries a leading byte for it, which a read
# one has no need of - so the fields sit one byte earlier.
# Offsets below reach up to 13, and a shorter answer is not a record.
SESSION_RECORD_MIN_LEN = 14
# The record format carries a version, which the handle reports in a
# descriptor - and descriptors are not readable over every transport this
# integration supports. So the guard against a different format is the
# record itself: a layout that shifted by even one byte still decodes, into
# numbers that look like an answer. These bounds are what a session cannot
# plausibly be, so an unrecognised format is refused rather than believed.
#
# Observed on real handles: routines of 60, 120 and 160 seconds, and a timer
# that stops at the routine length rather than running past it. The margin
# is generous on purpose - the point is to catch a shifted layout, not to
# police an unusual routine.
MAX_ROUTINE_SECONDS = 3600
MAX_DURATION_FACTOR = 2

# ── Sensor Service (0x0005) ──────────────────────────────────────────────────
# Sensor enable bitmask: bit0=pressure, bit1=temperature, bit2=gyroscope
CHAR_SENSOR_ENABLE = "477ea600-a260-11e4-ae37-0002a5d54120"
# Sensor data stream (notify only)
CHAR_SENSOR_DATA = "477ea600-a260-11e4-ae37-0002a5d54130"
CHAR_SENSOR_UNKNOWN_4140 = "477ea600-a260-11e4-ae37-0002a5d54140"

# ── Brush Head Service (0x0006) ──────────────────────────────────────────────
CHAR_BRUSHHEAD_NFC_VERSION = "477ea600-a260-11e4-ae37-0002a5d54210"
CHAR_BRUSHHEAD_TYPE = "477ea600-a260-11e4-ae37-0002a5d54220"
CHAR_BRUSHHEAD_SERIAL = "477ea600-a260-11e4-ae37-0002a5d54230"
CHAR_BRUSHHEAD_DATE = "477ea600-a260-11e4-ae37-0002a5d54240"
CHAR_BRUSHHEAD_UNKNOWN_4250 = "477ea600-a260-11e4-ae37-0002a5d54250"
CHAR_BRUSHHEAD_UNKNOWN_4254 = "477ea600-a260-11e4-ae37-0002a5d54254"
CHAR_BRUSHHEAD_UNKNOWN_4260 = "477ea600-a260-11e4-ae37-0002a5d54260"
CHAR_BRUSHHEAD_UNKNOWN_4270 = "477ea600-a260-11e4-ae37-0002a5d54270"
CHAR_BRUSHHEAD_LIFETIME_LIMIT = "477ea600-a260-11e4-ae37-0002a5d54280"
CHAR_BRUSHHEAD_LIFETIME_USAGE = "477ea600-a260-11e4-ae37-0002a5d54290"
CHAR_BRUSHHEAD_UNKNOWN_42A0 = "477ea600-a260-11e4-ae37-0002a5d542a0"
CHAR_BRUSHHEAD_UNKNOWN_42A2 = "477ea600-a260-11e4-ae37-0002a5d542a2"
CHAR_BRUSHHEAD_UNKNOWN_42A4 = "477ea600-a260-11e4-ae37-0002a5d542a4"
CHAR_BRUSHHEAD_UNKNOWN_42A6 = "477ea600-a260-11e4-ae37-0002a5d542a6"
CHAR_BRUSHHEAD_PAYLOAD = "477ea600-a260-11e4-ae37-0002a5d542b0"
CHAR_BRUSHHEAD_RING_ID = "477ea600-a260-11e4-ae37-0002a5d542c0"

# ── Diagnostic Service (0x0007) ──────────────────────────────────────────────
CHAR_ERROR_PERSISTENT = "477ea600-a260-11e4-ae37-0002a5d54310"
CHAR_ERROR_VOLATILE = "477ea600-a260-11e4-ae37-0002a5d54320"
CHAR_DIAG_UNKNOWN_4330 = "477ea600-a260-11e4-ae37-0002a5d54330"
CHAR_DIAG_UNKNOWN_4360 = "477ea600-a260-11e4-ae37-0002a5d54360"

# ── Extended Service (0x0008) ────────────────────────────────────────────────
CHAR_EXTENDED_UNKNOWN_4410 = "477ea600-a260-11e4-ae37-0002a5d54410"
CHAR_SETTINGS = "477ea600-a260-11e4-ae37-0002a5d54420"

# ── Enums ────────────────────────────────────────────────────────────────────
# The state a handle reports while its motor runs. Named because the value
# is the only "is a session happening" signal on handles that report no
# brushing state of their own.
HANDLE_STATE_RUNNING = 2

HANDLE_STATES = {
    0: "off",
    1: "standby",
    2: "run",
    3: "charge",
    4: "shutdown",
    6: "validate",
    7: "background",
}

# Mode-id table for the 0x4022 (AVAILABLE_ROUTINE_IDS) value space. On handles
# that report the *selected* mode there (HX9996 / HX999X), the value is a single
# mode-id byte decoded by this table. NOTE: 0x4080 (BRUSHING_MODE) on every other
# handle uses a DIFFERENT numbering — a sequential device-mode index — and must
# NOT be decoded with this table; see brushing_mode_for_model(). Verified against
# our own handle captures on HX999X (2026-06-01).
BRUSHING_MODES = {
    0: "clean",
    1: "white_plus",
    2: "gum_health",
    3: "tongue_care",
    4: "deep_clean_plus",
    5: "sensitive",
}

# On the remaining handles the selected mode comes from 0x4080 (BRUSHING_MODE)
# as a sequential 0-based index into the device's own ordered mode list — the
# same byte means different modes on different models (e.g. value 1 = white+ on
# HX992X but gum_health on HX960X, which has no white mode). Each model family
# therefore needs its own ordered list, indexed directly by the value; models
# we haven't mapped fall back to the full mode order. Lists are derived from our
# own handle captures (HX992X, HX960X — 2026-06-01).
_SEQUENTIAL_MODE_DEFAULT = (
    "clean",
    "white_plus",
    "gum_health",
    "deep_clean_plus",
    "tongue_care",
    "sensitive",
)
_SEQUENTIAL_MODE_BY_MODEL: dict[str, tuple[str, ...]] = {
    "HX960X": ("clean", "gum_health", "deep_clean_plus"),
    "HX9120": ("clean", "white_plus", "deep_clean_plus"),
    "HX961X": ("clean", "white_plus", "deep_clean_plus"),
    "HX991X": ("clean", "white_plus", "gum_health", "deep_clean_plus"),
}

BRUSHING_STATES = {
    0: "off",
    1: "on",
    2: "pause",
    3: "session_complete",
    4: "session_aborted",
}

INTENSITIES = {
    0: "low",
    1: "medium",
    2: "high",
}

# ── Model-based feature support ─────────────────────────────────────────────
# Prefix-based gating: the Prestige line (HX999X / HX9996) accepts both mode
# and settings writes; the Kids Plus line (HX74xx) accepts mode writes only.
MODE_WRITE_MODELS = ("HX999", "HX9996", "HX74")
SETTINGS_WRITE_MODELS = ("HX999", "HX9996")


def supports_mode_write(model: str) -> bool:
    """Check if the model supports writing brushing mode."""
    upper = (model or "").upper()
    return any(upper.startswith(prefix) for prefix in MODE_WRITE_MODELS)


def supports_settings_write(model: str) -> bool:
    """Check if the model supports settings (0x4420) writes."""
    upper = (model or "").upper()
    return any(upper.startswith(prefix) for prefix in SETTINGS_WRITE_MODELS)


# ── Brushing-mode decode source ─────────────────────────────────────────────
# Two handle families report the selected brushing mode differently: HX9996 /
# HX999X expose it as a mode-id in 0x4022 (AVAILABLE_ROUTINE_IDS), while every
# other handle exposes it as a sequential index in 0x4080 (BRUSHING_MODE). This
# gate decides which characteristic and which table the parser uses.
ROUTINE_ID_MODE_MODELS = ("HX9996", "HX999")


# Which service to address a characteristic through, where a Kids handle
# differs. Reads and writes over the ESP bridge name the service explicitly,
# so addressing the storage service on a handle that has none simply fails.
KIDS_CHAR_SERVICE_OVERRIDE: dict[str, str] = {
    CHAR_LATEST_SESSION_ID: SVC_KIDS_SESSION,
    CHAR_ACTIVE_SESSION_ID: SVC_KIDS_SESSION,
    CHAR_SESSION_DATA: SVC_KIDS_SESSION,
}


def is_kids_model(model: str) -> bool:
    """True for the Sonicare for Kids family (HX63xx)."""
    return (model or "").upper().startswith("HX63")


def uses_direct_session_read(model: str) -> bool:
    """True when a stored record is read back rather than notified."""
    return is_kids_model(model)


def supports_stored_sessions(model: str, services: set[str]) -> bool:
    """True when the handle can be asked for its record of a past session.

    Either it has the storage service, or it is a Kids handle, which keeps
    the same records without one.
    """
    return SVC_STORAGE.lower() in services or is_kids_model(model)


def uses_routine_id_mode(model: str) -> bool:
    """True when the selected mode is read from 0x4022 as a mode-id."""
    upper = (model or "").upper()
    return any(upper.startswith(prefix) for prefix in ROUTINE_ID_MODE_MODELS)


def brushing_mode_for_model(model: str, value: int) -> str | None:
    """Decode a 0x4080 sequential mode index to a label.

    Uses the model family's own ordered mode list, falling back to the full
    mode order for unmapped models. Returns ``None`` for values outside the
    model's mode list (the caller logs the raw value).
    """
    upper = (model or "").upper()
    table = _SEQUENTIAL_MODE_DEFAULT
    for prefix, modes in _SEQUENTIAL_MODE_BY_MODEL.items():
        if upper.startswith(prefix):
            table = modes
            break
    if 0 <= value < len(table):
        return table[value]
    return None


# ── Sector / zone count per model family ────────────────────────────────────
# The brush does not report live sector data — sectors are derived from the
# elapsed brushing time and the routine length. Premium handles (HX99X,
# HX96X, HX995X) are divided into 6 zones; the Kids line (HX63xx) uses 4.
# Unknown handles default to the premium layout.
SECTORS_PREMIUM = 6
SECTORS_KIDS = 4
SECTORS_DEFAULT = SECTORS_PREMIUM


def number_of_sectors_for_model(model: str) -> int:
    """Return the number of brushing sectors (zones) for a model.

    Only the Kids line (HX63xx) uses 4 sectors; every other handle defaults
    to 6.
    """
    upper = (model or "").upper()
    if upper.startswith("HX63"):
        return SECTORS_KIDS
    return SECTORS_PREMIUM


# Mode-specific sector visit sequences for premium handles.
# Values are 1-indexed anatomical sector IDs. White+ and Gum Health revisit
# the front-teeth sectors (2, 5) after the initial sweep.
# Condor devices report the same routines under different labels
# (white / gum_care / deep_clean, see CONDOR_BRUSHING_MODES) — both label
# sets must be present here, otherwise those modes fall back to the uniform
# 6-sector split and the sector changes lag the handle's pacing.
MODE_SECTOR_SEQUENCES: dict[str, list[int]] = {
    "clean":           [1, 2, 3, 4, 5, 6],
    "white_plus":      [1, 2, 3, 4, 5, 6, 2, 5],
    "white":           [1, 2, 3, 4, 5, 6, 2, 5],
    "gum_health":      [1, 2, 3, 4, 5, 6, 1, 3, 4, 6],
    "gum_care":        [1, 2, 3, 4, 5, 6, 1, 3, 4, 6],
    "deep_clean_plus": [1, 2, 3, 4, 5, 6],
    "deep_clean":      [1, 2, 3, 4, 5, 6],
    "sensitive":       [1, 2, 3, 4, 5, 6],
    "tongue_care":     [],
}


def current_sector(
    model: str,
    mode: str | None,
    elapsed: float | None,
    routine_length: float | None,
) -> int | None:
    """Return the 1-indexed anatomical sector at `elapsed` seconds.

    - Tongue Care and unknown-time inputs return None.
    - Kids (HX63xx) always uses a uniform 4-sector distribution.
    - Unknown modes fall back to uniform distribution over the model's
      number of sectors.
    """
    if elapsed is None or routine_length is None or routine_length <= 0:
        return None
    sectors_total = number_of_sectors_for_model(model)
    is_kids = (model or "").upper().startswith("HX63")
    seq = None if is_kids else MODE_SECTOR_SEQUENCES.get(mode or "")
    if seq is not None and not seq:
        return None
    if seq is None:
        per_sector = routine_length / sectors_total
        return min(sectors_total, int(elapsed // per_sector) + 1)
    per_step = routine_length / len(seq)
    step_idx = min(len(seq) - 1, int(elapsed // per_step))
    return seq[step_idx]


PRESSURE_ALARM_STATES = {
    0: "ok",
    1: "optimal",
    2: "too_high",
}

BRUSHHEAD_TYPES = {
    0: "adaptive_clean",
    1: "adaptive_white",
    2: "adaptive_gums",
    3: "tongue_clean",
    4: "premium_all_in_one",
    5: "sensitive",
    6: "non_rfid",
}

# Product-family metadata exposed as attributes on the brush-head type
# sensor: the official family letter (the A in "A3" — the type value only
# identifies the family, not the series) and a short display name.
# TongueCare+ and non-RFID heads have no official letter; T and N are ours.
BRUSHHEAD_TYPE_FAMILY = {
    "adaptive_clean": ("C", "Clean"),
    "adaptive_white": ("W", "White"),
    "adaptive_gums": ("G", "Gums"),
    "tongue_clean": ("T", "Tongue"),
    "premium_all_in_one": ("A", "All-in-One"),
    "sensitive": ("S", "Sensitive"),
    "non_rfid": ("N", "Non-RFID"),
}

# Sensor enable bitmask values (written to CHAR_SENSOR_ENABLE 0x4120)
SENSOR_ENABLE_PRESSURE = 0x01
SENSOR_ENABLE_TEMPERATURE = 0x02
SENSOR_ENABLE_GYROSCOPE = 0x04
SENSOR_ENABLE_DEFAULT = SENSOR_ENABLE_PRESSURE | SENSOR_ENABLE_TEMPERATURE

# ── Brush head chars (re-read after NFC scan completes) ──────────────────────
BRUSHHEAD_CHARS = [
    CHAR_BRUSHHEAD_NFC_VERSION,
    CHAR_BRUSHHEAD_TYPE,
    CHAR_BRUSHHEAD_SERIAL,
    CHAR_BRUSHHEAD_DATE,
    CHAR_BRUSHHEAD_LIFETIME_LIMIT,
    CHAR_BRUSHHEAD_LIFETIME_USAGE,
    CHAR_BRUSHHEAD_RING_ID,
    CHAR_BRUSHHEAD_PAYLOAD,
]

# ── Characteristic lists for polling/live ────────────────────────────────────
NOTIFICATION_CHARS = [
    # Priority 1: Core status (must have for basic functionality)
    CHAR_HANDLE_STATE,        # indicate — off/standby/run/charge
    CHAR_BRUSHING_TIME,       # notify — live brushing timer
    CHAR_BRUSHING_STATE,      # notify — on/off/pause/complete/aborted
    # CHAR_SENSOR_DATA is subscribed dynamically during active sessions only
    # Priority 2: Session details
    CHAR_BRUSHING_MODE,       # indicate — clean/white+/gum/deep
    CHAR_INTENSITY,           # notify — low/medium/high
    CHAR_ROUTINE_LENGTH,      # notify — target duration
    CHAR_SESSION_ID,          # notify — current session
    # Priority 3: Storage & diagnostics (nice to have)
    CHAR_LATEST_SESSION_ID,   # notify
    CHAR_SESSION_COUNT,       # notify
    CHAR_BRUSHHEAD_SERIAL,    # notify
]

# Sensor frame types (from 0x4130 stream)
SENSOR_FRAME_PRESSURE = 1
SENSOR_FRAME_TEMPERATURE = 2
SENSOR_FRAME_GYROSCOPE = 4

POLL_READ_CHARS = [
    CHAR_BATTERY_LEVEL,
    CHAR_MODEL_NUMBER,
    CHAR_SERIAL_NUMBER,
    CHAR_FIRMWARE_REVISION,
    CHAR_HARDWARE_REVISION,
    CHAR_SOFTWARE_REVISION,
    CHAR_MANUFACTURER_NAME,
    CHAR_HANDLE_STATE,
    CHAR_AVAILABLE_ROUTINES,
    CHAR_AVAILABLE_ROUTINE_IDS,
    CHAR_MOTOR_RUNTIME,
    CHAR_HANDLE_TIME,
    CHAR_SESSION_ID,
    CHAR_BRUSHING_MODE,
    CHAR_BRUSHING_STATE,
    CHAR_BRUSHING_TIME,
    CHAR_ROUTINE_LENGTH,
    CHAR_INTENSITY,
    CHAR_LATEST_SESSION_ID,
    CHAR_SESSION_COUNT,
    CHAR_SESSION_TYPE,
    CHAR_BRUSHHEAD_NFC_VERSION,
    CHAR_BRUSHHEAD_TYPE,
    CHAR_BRUSHHEAD_SERIAL,
    CHAR_BRUSHHEAD_DATE,
    CHAR_BRUSHHEAD_LIFETIME_LIMIT,
    CHAR_BRUSHHEAD_LIFETIME_USAGE,
    CHAR_BRUSHHEAD_RING_ID,
    CHAR_BRUSHHEAD_PAYLOAD,
    CHAR_ERROR_PERSISTENT,
    CHAR_ERROR_VOLATILE,
    CHAR_SETTINGS,
    CHAR_SENSOR_ENABLE,
]

# Live monitoring: only dynamic chars on reconnect.
# On first connect, coordinator reads full POLL_READ_CHARS instead.
LIVE_READ_CHARS = [
    CHAR_BATTERY_LEVEL,
    CHAR_HANDLE_STATE,
    CHAR_BRUSHING_MODE,
    CHAR_BRUSHING_STATE,
    CHAR_BRUSHING_TIME,
    CHAR_ROUTINE_LENGTH,
    CHAR_INTENSITY,
    CHAR_SESSION_ID,
    CHAR_LATEST_SESSION_ID,
    CHAR_SESSION_COUNT,
    CHAR_MOTOR_RUNTIME,
    CHAR_BRUSHHEAD_LIFETIME_LIMIT,
    CHAR_BRUSHHEAD_LIFETIME_USAGE,
]

# ── Characteristic → Service map (for ESP bridge) ───────────────────────────
CHAR_SERVICE_MAP: dict[str, str] = {
    # Battery Service
    CHAR_BATTERY_LEVEL: SVC_BATTERY,
    # Device Information Service
    CHAR_MODEL_NUMBER: SVC_DEVICE_INFO,
    CHAR_SERIAL_NUMBER: SVC_DEVICE_INFO,
    CHAR_FIRMWARE_REVISION: SVC_DEVICE_INFO,
    CHAR_HARDWARE_REVISION: SVC_DEVICE_INFO,
    CHAR_SOFTWARE_REVISION: SVC_DEVICE_INFO,
    CHAR_MANUFACTURER_NAME: SVC_DEVICE_INFO,
    # Sonicare Service (0x0001)
    CHAR_HANDLE_STATE: SVC_SONICARE,
    CHAR_AVAILABLE_ROUTINES: SVC_SONICARE,
    CHAR_AVAILABLE_ROUTINE_IDS: SVC_SONICARE,
    CHAR_ACTIVE_FEATURE: SVC_SONICARE,
    CHAR_MOTOR_RUNTIME: SVC_SONICARE,
    CHAR_HANDLE_TIME: SVC_SONICARE,
    # Routine Service (0x0002)
    CHAR_SESSION_ID: SVC_ROUTINE,
    CHAR_BRUSHING_MODE: SVC_ROUTINE,
    CHAR_BRUSHING_STATE: SVC_ROUTINE,
    CHAR_BRUSHING_TIME: SVC_ROUTINE,
    CHAR_ROUTINE_LENGTH: SVC_ROUTINE,
    CHAR_QUADPACER_SEGMENT: SVC_ROUTINE,
    CHAR_INTENSITY: SVC_ROUTINE,
    CHAR_EASY_START_STAGE: SVC_ROUTINE,
    # Storage Service (0x0004)
    CHAR_LATEST_SESSION_ID: SVC_STORAGE,
    CHAR_SESSION_COUNT: SVC_STORAGE,
    CHAR_SESSION_TYPE: SVC_STORAGE,
    CHAR_ACTIVE_SESSION_ID: SVC_STORAGE,
    CHAR_SESSION_DATA: SVC_STORAGE,
    CHAR_SESSION_ACTION: SVC_STORAGE,
    # Sensor Service (0x0005)
    CHAR_SENSOR_ENABLE: SVC_SENSOR,
    CHAR_SENSOR_DATA: SVC_SENSOR,
    CHAR_SENSOR_UNKNOWN_4140: SVC_SENSOR,
    # Brush Head Service (0x0006)
    CHAR_BRUSHHEAD_NFC_VERSION: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_TYPE: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_SERIAL: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_DATE: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_UNKNOWN_4250: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_UNKNOWN_4254: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_UNKNOWN_4260: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_UNKNOWN_4270: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_LIFETIME_LIMIT: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_LIFETIME_USAGE: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_UNKNOWN_42A0: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_UNKNOWN_42A2: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_UNKNOWN_42A4: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_UNKNOWN_42A6: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_PAYLOAD: SVC_BRUSHHEAD,
    CHAR_BRUSHHEAD_RING_ID: SVC_BRUSHHEAD,
    # Diagnostic Service (0x0007)
    CHAR_ERROR_PERSISTENT: SVC_DIAGNOSTIC,
    CHAR_ERROR_VOLATILE: SVC_DIAGNOSTIC,
    CHAR_DIAG_UNKNOWN_4330: SVC_DIAGNOSTIC,
    CHAR_DIAG_UNKNOWN_4360: SVC_DIAGNOSTIC,
    # Extended Service (0x0008)
    CHAR_EXTENDED_UNKNOWN_4410: SVC_EXTENDED,
    CHAR_SETTINGS: SVC_EXTENDED,
    # Condor protocol transport (e50b…) — HX742X / Series 7100 family
    CHAR_RX: SVC_CONDOR,
    CHAR_RX_ACK: SVC_CONDOR,
    CHAR_TX: SVC_CONDOR,
    CHAR_TX_ACK: SVC_CONDOR,
    CHAR_PROTO_CFG: SVC_CONDOR,
    CHAR_SERVER_CFG: SVC_CONDOR,
    CHAR_CLIENT_CFG: SVC_CONDOR,
}

# ── Config ───────────────────────────────────────────────────────────────────
CONF_ADDRESS = "address"
CONF_SERVICES = "services"

CONF_TRANSPORT_TYPE = "transport_type"
TRANSPORT_BLEAK = "bleak"
TRANSPORT_ESP_BRIDGE = "esp_bridge"

CONF_ESP_DEVICE_NAME = "esp_device_name"
CONF_ESP_BRIDGE_ID = "esp_bridge_id"

CONF_DEVICE_NAME = "device_name"
CONF_AREA = "area"

CONF_NOTIFY_THROTTLE = "notify_throttle_ms"
DEFAULT_NOTIFY_THROTTLE = 500

# Opt-out for pipelined poll reads (only effective on bridges >=
# BRIDGE_PIPELINED_READS_VERSION; older bridges are always read serially).
CONF_PIPELINED_READS = "pipelined_reads"
DEFAULT_PIPELINED_READS = True
MIN_NOTIFY_THROTTLE = 100
MAX_NOTIFY_THROTTLE = 5000

CONF_SENSOR_PRESSURE = "sensor_pressure"
CONF_SENSOR_TEMPERATURE = "sensor_temperature"
CONF_SENSOR_GYROSCOPE = "sensor_gyroscope"
DEFAULT_SENSOR_PRESSURE = True
DEFAULT_SENSOR_TEMPERATURE = True
DEFAULT_SENSOR_GYROSCOPE = False

CONF_WARN_COUNTERFEIT = "warn_counterfeit_brushhead"
DEFAULT_WARN_COUNTERFEIT = True
# Seconds of active brushing before a missing/invalid serial raises an alert
COUNTERFEIT_DETECTION_DELAY = 30
