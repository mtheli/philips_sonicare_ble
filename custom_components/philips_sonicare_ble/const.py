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

# Minimum ESP bridge component version required for full functionality
MIN_BRIDGE_VERSION = "1.2.3"

# ── Service UUIDs ────────────────────────────────────────────────────────────
SVC_BATTERY = "0000180f-0000-1000-8000-00805f9b34fb"
SVC_DEVICE_INFO = "0000180a-0000-1000-8000-00805f9b34fb"
SVC_GATT = "00001801-0000-1000-8000-00805f9b34fb"
SVC_SONICARE = "477ea600-a260-11e4-ae37-0002a5d50001"
SVC_ROUTINE = "477ea600-a260-11e4-ae37-0002a5d50002"
SVC_STORAGE = "477ea600-a260-11e4-ae37-0002a5d50004"
SVC_SENSOR = "477ea600-a260-11e4-ae37-0002a5d50005"
SVC_BRUSHHEAD = "477ea600-a260-11e4-ae37-0002a5d50006"
SVC_DIAGNOSTIC = "477ea600-a260-11e4-ae37-0002a5d50007"
SVC_EXTENDED = "477ea600-a260-11e4-ae37-0002a5d50008"
SVC_BYTESTREAM = "a651fff1-4074-4131-bce9-56d4261bc7b1"

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
CHAR_UNKNOWN_4030 = "477ea600-a260-11e4-ae37-0002a5d54030"
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
CHAR_UNKNOWN_40A0 = "477ea600-a260-11e4-ae37-0002a5d540a0"
# Intensity: 0=low, 1=medium, 2=high
CHAR_INTENSITY = "477ea600-a260-11e4-ae37-0002a5d540b0"
CHAR_UNKNOWN_40C0 = "477ea600-a260-11e4-ae37-0002a5d540c0"

# ── Storage Service (0x0004) ─────────────────────────────────────────────────
CHAR_LATEST_SESSION_ID = "477ea600-a260-11e4-ae37-0002a5d540d0"
CHAR_SESSION_COUNT = "477ea600-a260-11e4-ae37-0002a5d540d2"
CHAR_SESSION_TYPE = "477ea600-a260-11e4-ae37-0002a5d540d5"
CHAR_ACTIVE_SESSION_ID = "477ea600-a260-11e4-ae37-0002a5d540e0"
CHAR_SESSION_DATA = "477ea600-a260-11e4-ae37-0002a5d54100"
CHAR_SESSION_ACTION = "477ea600-a260-11e4-ae37-0002a5d54110"

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
HANDLE_STATES = {
    0: "off",
    1: "standby",
    2: "run",
    3: "charge",
    4: "shutdown",
    6: "validate",
    7: "background",
}

BRUSHING_MODES = {
    0: "clean",
    1: "white_plus",
    2: "gum_health",
    3: "tongue_care",
    4: "deep_clean_plus",
    5: "sensitive",
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
# Based on decompiled app: Device.java + TuscanyBLEConnector.java
# XIAN = HX999X/HX9996 (Prestige), CAIRO = HX74XX (Kids Plus)
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


# ── Sector / zone count per model family ────────────────────────────────────
# The brush does not report live sector data — sectors are derived from the
# elapsed brushing time and the routine length. The Philips app visualises
# 6 zones for premium Tuscany handles (HX99X, HX96X, HX995X) and 4 zones on
# the Kids line (HX63xx). Default to 4 for unknown handles.
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


# Mode-specific sector visit sequences for Tuscany Premium handles.
# Values are 1-indexed anatomical sector IDs. White+ and Gum Health revisit
# the front-teeth sectors (2, 5) after the initial sweep.
MODE_SECTOR_SEQUENCES: dict[str, list[int]] = {
    "clean":           [1, 2, 3, 4, 5, 6],
    "white_plus":      [1, 2, 3, 4, 5, 6, 2, 5],
    "gum_health":      [1, 2, 3, 4, 5, 6, 1, 3, 4, 6],
    "deep_clean_plus": [1, 2, 3, 4, 5, 6],
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
    CHAR_UNKNOWN_4030: SVC_SONICARE,
    CHAR_MOTOR_RUNTIME: SVC_SONICARE,
    CHAR_HANDLE_TIME: SVC_SONICARE,
    # Routine Service (0x0002)
    CHAR_SESSION_ID: SVC_ROUTINE,
    CHAR_BRUSHING_MODE: SVC_ROUTINE,
    CHAR_BRUSHING_STATE: SVC_ROUTINE,
    CHAR_BRUSHING_TIME: SVC_ROUTINE,
    CHAR_ROUTINE_LENGTH: SVC_ROUTINE,
    CHAR_UNKNOWN_40A0: SVC_ROUTINE,
    CHAR_INTENSITY: SVC_ROUTINE,
    CHAR_UNKNOWN_40C0: SVC_ROUTINE,
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
}

# ── Config ───────────────────────────────────────────────────────────────────
CONF_ADDRESS = "address"
CONF_SERVICES = "services"

CONF_TRANSPORT_TYPE = "transport_type"
TRANSPORT_BLEAK = "bleak"
TRANSPORT_ESP_BRIDGE = "esp_bridge"

CONF_ESP_DEVICE_NAME = "esp_device_name"
CONF_ESP_BRIDGE_ID = "esp_bridge_id"

CONF_NOTIFY_THROTTLE = "notify_throttle_ms"
DEFAULT_NOTIFY_THROTTLE = 500
MIN_NOTIFY_THROTTLE = 100
MAX_NOTIFY_THROTTLE = 5000

CONF_SENSOR_PRESSURE = "sensor_pressure"
CONF_SENSOR_TEMPERATURE = "sensor_temperature"
CONF_SENSOR_GYROSCOPE = "sensor_gyroscope"
DEFAULT_SENSOR_PRESSURE = True
DEFAULT_SENSOR_TEMPERATURE = True
DEFAULT_SENSOR_GYROSCOPE = False
