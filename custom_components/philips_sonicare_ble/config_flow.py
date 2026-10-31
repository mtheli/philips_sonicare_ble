"""Config flow for Philips Sonicare BLE."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth as ha_bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
    async_last_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow

try:  # HA ≥ 2025.8
    from homeassistant.config_entries import OptionsFlowWithReload
except ImportError:  # pragma: no cover — older cores + the pinned CI
    # test stack (pytest-homeassistant-custom-component ships HA 2025.1).
    # Fallback loses only the automatic entry reload on options save.
    from homeassistant.config_entries import OptionsFlow as OptionsFlowWithReload
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import Event, callback
from homeassistant.data_entry_flow import AbortFlow, FlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import http as ha_http
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util
from homeassistant.util import language as language_util
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectOptionDict,
)

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection as bleak_establish

from .const import (
    BRIDGE_FIELD_IDENTITY_ADDRESS,
    BRIDGE_FIELD_PEER_IDENTITY,
    ROTATING_ADDRESS_ISSUE_URL,
    DOMAIN,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    CONF_ESP_DEVICE_NAME,
    CONF_ESP_BRIDGE_ID,
    CONF_BOND_ADDRESS,
    CONF_PENDING_IDENTITY,
    CONF_DEVICE_NAME,
    CONF_AREA,
    CONF_NOTIFY_THROTTLE,
    CONF_PIPELINED_READS,
    CONF_SENSOR_PRESSURE,
    CONF_SENSOR_TEMPERATURE,
    CONF_SENSOR_GYROSCOPE,
    CONF_WARN_COUNTERFEIT,
    CONF_BLUETOOTH_ADAPTER,
    BLUETOOTH_ADAPTER_AUTO,
    TRANSPORT_BLEAK,
    TRANSPORT_ESP_BRIDGE,
    DEFAULT_NOTIFY_THROTTLE,
    DEFAULT_PIPELINED_READS,
    DEFAULT_SENSOR_PRESSURE,
    DEFAULT_SENSOR_TEMPERATURE,
    DEFAULT_SENSOR_GYROSCOPE,
    DEFAULT_WARN_COUNTERFEIT,
    MIN_NOTIFY_THROTTLE,
    MAX_NOTIFY_THROTTLE,
    CHAR_BATTERY_LEVEL,
    CHAR_MODEL_NUMBER,
    CHAR_SERIAL_NUMBER,
    CHAR_FIRMWARE_REVISION,
    SVC_BATTERY,
    SVC_DEVICE_INFO,
    SVC_SONICARE,
    SVC_ROUTINE,
    SVC_STORAGE,
    SVC_SENSOR,
    SVC_BRUSHHEAD,
    SVC_DIAGNOSTIC,
    SVC_EXTENDED,
    SVC_BYTESTREAM,
    SVC_CONDOR,
)
from .helpers import esphome_service_id, is_bond_gated_profile
from .transport import (
    EspBridgeTransport,
    _cleanup_disconnected_client,
    async_unpair_bridge_slot,
    bridge_bond_address,
    bridge_identity,
    describe_available_paths,
    describe_connectable_scanners,
    describe_connection_path,
    fixed_scanner_client_class,
    scanner_rssi,
    watch_advertisements,
    is_local_bluez_connection,
    local_adapter_source,
    slot_changed_at,
    UNPAIR_OK,
    UNPAIR_FAILED,
    UNPAIR_UNAVAILABLE,
)
from .exceptions import (
    AdapterNotReceivingException,
    DeviceAsleepException,
    NotPairedException,
    TransportError,
)

_LOGGER = logging.getLogger(__name__)


def _is_hassio(hass) -> bool:
    """Check if Home Assistant is running on HAOS / Supervised."""
    return "hassio" in hass.config.components


def _bluetooth_adapter_options(
    hass, address: str, current: str
) -> list[SelectOptionDict]:
    """Automatic first, then every connectable scanner.

    A saved scanner that is offline right now stays in the list, so
    opening the options does not silently reset the choice.
    """
    options = [
        SelectOptionDict(
            value=BLUETOOTH_ADAPTER_AUTO, label="Chosen by Home Assistant"
        )
    ]
    for scanner in describe_connectable_scanners(hass, address):
        # Name the kind of path, in the same untranslated product names the
        # dialog text uses — "hci0" alone does not say it is the local one.
        kind = "Direct Bluetooth" if scanner["is_local"] else "Bluetooth proxy"
        label = f"{scanner['name']} — {kind}"
        if isinstance(scanner["rssi"], int):
            label = f"{label}, {scanner['rssi']} dBm"
        options.append(
            SelectOptionDict(value=str(scanner["source"]), label=label)
        )
    if current != BLUETOOTH_ADAPTER_AUTO and current not in {
        o["value"] for o in options
    }:
        options.append(SelectOptionDict(value=current, label=current))
    return options


def _brush_family(info: Any) -> str:
    """Name a discovered brush by what its advertisement gives away.

    Every adult brush advertises as "Philips Sonicare", so two of them are
    told apart only by MAC. Kids models name themselves; the service UUID
    in the scan response separates Condor from Classic. A scanner that
    only listened passively has no scan response — then the name stays.
    """
    name = getattr(info, "name", None) or ""
    if "kids" in name.lower():
        return "Sonicare for Kids"
    uuids = {u.lower() for u in (getattr(info, "service_uuids", None) or ())}
    if SVC_CONDOR.lower() in uuids:
        return "Sonicare (Condor)"
    if SVC_SONICARE.lower() in uuids:
        return "Sonicare (Classic)"
    return name or "Philips Sonicare"


def _previous_device_name(hass, address: str) -> str | None:
    """The user's own name for a brush that was set up here before.

    Removing an entry keeps the device in the registry's deleted list for
    a while, with the name the user gave it — enough to recognise the
    brush when it shows up again. Never breaks discovery.
    """
    try:
        deleted = dr.async_get(hass).deleted_devices
        for device in deleted.values():
            name = getattr(device, "name_by_user", None)
            if name and any(
                kind == dr.CONNECTION_BLUETOOTH
                and value.upper() == address.upper()
                for kind, value in device.connections
            ):
                return name
    except Exception:  # noqa: BLE001 — label only
        return None
    return None


def _via_label(paths: list[dict]) -> str:
    """'via hci0' / 'via atom-lite (proxy)' for the strongest path, or ''."""
    if not paths:
        return ""
    best = paths[0]
    # Local scanner names carry the adapter MAC in parentheses; strip it.
    via = str(best["name"]).split(" (")[0]
    return f"via {via}" + ("" if best["is_local"] else " (proxy)")


def _brush_label(hass, info: Any) -> str:
    """'<own name> · <family> · <MAC>' for discovery title and picker."""
    parts = [_brush_family(info), info.address]
    previous = _previous_device_name(hass, info.address)
    if previous:
        parts.insert(0, previous)
    return " · ".join(parts)


_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def _valid_identity(raw: object) -> str:
    """An upper-case BLE address, or "" for anything else."""
    value = raw.upper() if isinstance(raw, str) else ""
    return value if _MAC_RE.match(value) and value != "00:00:00:00:00:00" else ""


def _identity_row(text: dict[str, str], identity: str, mac: str) -> str:
    """Extra table row with the identity the brush handed over at pairing.

    Only shown when it differs from the address in the MAC row — for a brush
    with a fixed address both are the same. The row continues the table, so
    its line break comes from here: hassfest rejects leading whitespace in
    the translation itself.
    """
    identity = (identity or "").upper()
    if not identity or identity == (mac or "").upper():
        return ""
    row = text.get("error.identity_row", "")
    return f"\n{row.replace('{identity}', identity)}" if row else ""


def _alert(alert_type: str, text: str) -> str:
    """Wrap a notice for injection into a step description.

    Markup only ever travels in placeholder *values* — hassfest rejects
    HTML inside translation strings themselves. An empty notice yields an
    empty string so the step renders without a gap.
    """
    if not text:
        return ""
    # A blank line in the notice would end the markdown paragraph the opening
    # tag sits in, pushing everything after it — including the closing tag —
    # out of the alert box. Translate paragraph breaks into <br><br> here,
    # where markup is allowed, so the text stays inside and still renders its
    # markdown (bold, code) as inline content.
    body = text.replace("\n\n", "<br><br>")
    return f'<ha-alert alert-type="{alert_type}">{body}</ha-alert>\n\n'


# The languages we ship translations for. Kept as a constant so the flow
# needs no file access; tests/test_translation_coverage.py pins it against
# the contents of translations/.
_TRANSLATED_LANGUAGES = ("en", "de")


def _language_from_accept_header(header: str | None) -> str | None:
    """Best match between the browser's languages and the ones we ship.

    Only relevant when the user never picked a language: the frontend
    then renders from ``navigator.language``, which the browser derives
    from the same setting it builds this header from. Matching is left
    to Home Assistant's own helper so that regional tags resolve the way
    they do elsewhere (``de-DE`` → ``de``, but ``pt-BR`` ≠ ``pt``).
    """
    if not header:
        return None

    ranked: list[tuple[float, int, str]] = []
    for index, part in enumerate(header.split(",")):
        tag, _, params = part.strip().partition(";")
        if not tag or tag == "*":
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        # Negated so a plain sort puts the highest quality first; the
        # index keeps equally-weighted tags in the order they were sent.
        ranked.append((-quality, index, tag))

    for _quality, _index, tag in sorted(ranked):
        if matched := language_util.matches(tag, _TRANSLATED_LANGUAGES):
            return matched[0]
    return None


async def _async_user_language(hass) -> str:
    """The language the dialog is most likely being rendered in.

    The frontend localises step text in the *user's* profile language,
    while ``hass.config.language`` is the server's — the two differ
    routinely, and a placeholder built from the server language then
    lands in an otherwise differently-worded dialog.

    Every render a user actually sees runs inside an HTTP request: the
    flow view re-runs the step on each GET rather than serving a stored
    result, so even a discovery flow started in the background has a
    request in context by the time anyone looks at it. That request
    carries the authenticated user, whose profile language the frontend
    persists in its own per-user store — the same store the core reads
    when it initialises ``hass.config.language``.

    Falls back to the server language, which is what the placeholder
    would have used anyway, so this can only ever be an improvement.
    """
    try:
        if (request := ha_http.current_request.get()) is not None:
            if (user := request.get("hass_user")) is not None:
                from homeassistant.components.frontend import (  # noqa: PLC0415
                    storage as frontend_store,
                )

                store = await frontend_store.async_user_store(hass, user.id)
                if language := (store.data.get("language") or {}).get("language"):
                    _LOGGER.debug("Flow text: using profile language %s", language)
                    return language
            # No stored preference — the frontend renders from the browser's
            # own language in that case, and so do we.
            if language := _language_from_accept_header(
                request.headers.get("Accept-Language")
            ):
                _LOGGER.debug("Flow text: using Accept-Language %s", language)
                return language
    except Exception:  # noqa: BLE001 — never break a dialog over wording
        _LOGGER.debug("Could not resolve the user's language", exc_info=True)
    _LOGGER.debug(
        "Flow text: falling back to server language %s", hass.config.language
    )
    return hass.config.language


async def _async_text_blocks(hass, category: str = "config") -> dict[str, str]:
    """Resolve the reusable sentence fragments in the user's language.

    A step description can only reference a *whole* other translation
    value (``[%key:...%]``), never a fragment of one. Text shared by
    several outcomes of the same dialog would therefore have to be
    repeated once per outcome and once per language — and a dialog with
    no input fields cannot fall back on ``errors["base"]``, because the
    frontend only renders that inside ``ha-form``.

    Keeping the parts that actually vary as their own keys and injecting
    them as placeholders sidesteps both limits: one step covers what
    used to need five, and the wording still follows the language the
    dialog is rendered in (see ``_async_user_language``). Home Assistant
    overlays the requested language on top of English, so a fragment
    missing from a translation falls back on its English text rather
    than disappearing.

    The blocks live under ``config.error`` next to the real error
    strings: hassfest validates strings.json against a fixed schema and
    rejects a section of our own. Keys are returned with their path below
    the domain (``error.<name>``), so the same lookup also reaches the
    error strings themselves — which is what makes them usable as notices
    on forms that cannot render ``errors[]`` at all.
    """
    prefix = f"component.{DOMAIN}.{category}."
    try:
        resources = await async_get_translations(
            hass, await _async_user_language(hass), category, [DOMAIN]
        )
    except Exception:  # noqa: BLE001 — wording is cosmetic, never fatal
        _LOGGER.debug("Could not load flow text blocks", exc_info=True)
        return {}
    return {
        key[len(prefix):]: value
        for key, value in resources.items()
        if key.startswith(prefix)
    }


# How long a slot probe may be reused. Long enough to carry the dropdown's
# probe into the picker and the picker's into the health check that follows
# a submit; short enough that a flow rendered again later — a discovery
# banner opened hours after it appeared — re-probes instead of showing the
# bridge state from when the flow was created.
_PROBE_CACHE_MAX_AGE = 30.0

# Length of the active-scan window requested while pair-mode is armed. Matches
# the bridge's own 60 s window; habluetooth clamps a single request to its
# AUTO_WINDOW_MAX_DURATION (35 s), so the caller re-arms until the pair window
# closes rather than relying on one call to cover it.
_ACTIVE_SCAN_WINDOW = 60.0

# Pause before asking again after a request that opened no window. Long enough
# not to spin on a setup that has no Auto-mode scanner at all, short enough to
# catch the next gap between a busy proxy's connection attempts.
_ACTIVE_SCAN_RETRY_DELAY = 2.0


# Sentinel option in the Direct-BLE picker that switches to free-text entry.
# Picked when the user wants to type a MAC manually (e.g. an RPA-rotating
# brush whose current address is not the freshest one in the discovery list).
_MANUAL_ADDRESS = "__manual__"

# Condor brushes advertise a resolvable private address that rotates on every
# wake, so each wake spawns a fresh discovery flow while the previous address
# never returns. We drop a sibling Condor discovery flow once its address has
# not been advertised for this long, keeping the list to the currently-present
# devices (a neighbour's brush advertises every ~1-2 s while awake, so its flow
# survives; it is only pruned once that brush has actually gone quiet).
_CONDOR_FLOW_STALE_SECONDS = 120

# Max age of the last *connectable* advertisement before we treat the brush as
# asleep. habluetooth keeps returning a connectable BLEDevice for up to ~195 s
# after the last advertisement, so a fresh BLEDevice reference alone does not
# prove the device is reachable; the brush only advertises every ~1-2 s while
# awake, so a stricter window cleanly separates "awake now" from "asleep".
_STALE_ADV_MAX_SECONDS = 15.0

# Standard BLE services to hide from display
_STANDARD_BLE_SERVICES = {
    "00001800-0000-1000-8000-00805f9b34fb",  # Generic Access
    "00001801-0000-1000-8000-00805f9b34fb",  # Generic Attribute
}

# Services any supported Sonicare exposes. A device qualifies as a
# Sonicare if *any* of these appear — older models fan out into the
# per-feature Classic services, HX742X / Series 7100 (Condor) collapses
# everything onto a single framed transport service.
_EXPECTED_SERVICES = {
    SVC_BATTERY.lower(),
    SVC_DEVICE_INFO.lower(),
    SVC_SONICARE.lower(),
    SVC_ROUTINE.lower(),
    SVC_STORAGE.lower(),
    SVC_SENSOR.lower(),
    SVC_BRUSHHEAD.lower(),
    SVC_DIAGNOSTIC.lower(),
    SVC_EXTENDED.lower(),
    SVC_CONDOR.lower(),
}

# Human-readable names for services
SERVICE_NAMES: dict[str, str] = {
    SVC_BATTERY.lower(): "Battery",
    SVC_DEVICE_INFO.lower(): "Device Information",
    SVC_SONICARE.lower(): "Sonicare (Main)",
    SVC_ROUTINE.lower(): "Routine / Session",
    SVC_STORAGE.lower(): "Storage / History",
    SVC_SENSOR.lower(): "Sensor (IMU)",
    SVC_BRUSHHEAD.lower(): "Brush Head",
    SVC_DIAGNOSTIC.lower(): "Diagnostic",
    SVC_EXTENDED.lower(): "Extended / Settings",
    SVC_BYTESTREAM.lower(): "ByteStreaming",
    SVC_CONDOR.lower(): "Condor (HX742X / Series 7100)",
}

# What each service enables in HA — shown as the "Provides" column.
SERVICE_FEATURES: dict[str, str] = {
    SVC_BATTERY.lower(): "Battery level",
    SVC_DEVICE_INFO.lower(): "Model, serial, firmware",
    SVC_SONICARE.lower(): "Handle state, brushing mode",
    SVC_ROUTINE.lower(): "Session timing, current mode",
    SVC_STORAGE.lower(): "Session history",
    SVC_SENSOR.lower(): "Pressure & motion sensors",
    SVC_BRUSHHEAD.lower(): "Brush head NFC, wear tracking",
    SVC_DIAGNOSTIC.lower(): "Error log",
    SVC_EXTENDED.lower(): "Adaptive intensity, feedback toggles",
    SVC_BYTESTREAM.lower(): "Streaming data channel",
    SVC_CONDOR.lower(): "Newer transport protocol (HX742X)",
}

# Classic services that the Condor protocol replaces with its single
# framed transport service.
_CLASSIC_SERVICE_UUIDS = {
    SVC_BATTERY.lower(),
    SVC_SONICARE.lower(),
    SVC_ROUTINE.lower(),
    SVC_STORAGE.lower(),
    SVC_SENSOR.lower(),
    SVC_BRUSHHEAD.lower(),
    SVC_DIAGNOSTIC.lower(),
    SVC_EXTENDED.lower(),
}

# Map each service to one representative characteristic for ESP probing.
# The ESP bridge has no "list services" call, so we read one char per
# Classic service and add the service when the read returns data. The
# Condor service has no universally-readable char (e50b0005 is optional
# firmware-side and missing on HX742X FW 1.8.20.0), so Condor is
# inferred by exclusion below — if Device Information answered but no
# Classic service did, the device must be Condor.
SERVICE_PROBE_CHARS: dict[str, str] = {
    SVC_BATTERY: CHAR_BATTERY_LEVEL,
    SVC_DEVICE_INFO: CHAR_MODEL_NUMBER,
    SVC_SONICARE: "477ea600-a260-11e4-ae37-0002a5d54010",  # CHAR_HANDLE_STATE
    SVC_ROUTINE: "477ea600-a260-11e4-ae37-0002a5d54080",   # CHAR_BRUSHING_MODE
    SVC_STORAGE: "477ea600-a260-11e4-ae37-0002a5d540d0",   # CHAR_LATEST_SESSION_ID
    SVC_SENSOR: "477ea600-a260-11e4-ae37-0002a5d54120",    # CHAR_SENSOR_ENABLE
    SVC_BRUSHHEAD: "477ea600-a260-11e4-ae37-0002a5d54210",  # CHAR_BRUSHHEAD_NFC_VERSION
    SVC_DIAGNOSTIC: "477ea600-a260-11e4-ae37-0002a5d54310",  # CHAR_ERROR_PERSISTENT
    SVC_EXTENDED: "477ea600-a260-11e4-ae37-0002a5d54420",   # CHAR_SETTINGS
}


class PhilipsSonicareConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Philips Sonicare BLE."""

    VERSION = 1
    MINOR_VERSION = 2  # 2: drop Classic-only sensors on Condor (see #23)

    # ------------------------------------------------------------------
    # Flow tracing
    #
    # Setup spans many steps and several minutes, and when it goes wrong the
    # only artefact left is the log a user attaches to an issue — the dialog
    # they saw is gone. Every step ends in one of four results, so logging
    # those covers the whole path without scattering a line through each of
    # the ~30 handlers. Steps stay at debug (a trace is only interesting once
    # something went wrong); the two outcomes that end the flow are info and
    # warning, so they show up without anyone enabling debug first.
    # ------------------------------------------------------------------

    @callback
    def async_show_form(self, **kwargs: Any) -> FlowResult:
        """Log which dialog the user is being shown."""
        errors = kwargs.get("errors") or {}
        _LOGGER.debug(
            "Flow step: showing %s%s",
            kwargs.get("step_id") or "?",
            f" with errors {sorted(errors)}" if errors else "",
        )
        return super().async_show_form(**kwargs)

    @callback
    def async_show_progress(self, **kwargs: Any) -> FlowResult:
        """Log a long-running step while it spins."""
        _LOGGER.debug(
            "Flow step: %s in progress (%s)",
            kwargs.get("step_id") or "?",
            kwargs.get("progress_action") or "no action",
        )
        return super().async_show_progress(**kwargs)

    @callback
    def async_show_menu(self, **kwargs: Any) -> FlowResult:
        """Log a step that offers the user a choice."""
        _LOGGER.debug(
            "Flow step: menu %s with options %s",
            kwargs.get("step_id") or "?",
            list(kwargs.get("menu_options") or []),
        )
        return super().async_show_menu(**kwargs)

    @callback
    def async_abort(self, **kwargs: Any) -> FlowResult:
        """Log why the flow ended without creating an entry.

        Debug, not info: zeroconf starts a flow for every ESPHome device on
        the network and most of them abort as not-ours. The aborts that carry
        a real diagnosis are logged with their cause where they are raised.
        """
        _LOGGER.debug("Flow aborted: %s", kwargs.get("reason") or "unknown")
        return super().async_abort(**kwargs)

    @callback
    def async_create_entry(self, **kwargs: Any) -> FlowResult:
        """Log the device that setup just produced."""
        data = kwargs.get("data") or {}
        _LOGGER.info(
            "Flow complete: created '%s' (transport %s, address %s)",
            kwargs.get("title") or "?",
            data.get(CONF_TRANSPORT_TYPE, "unknown"),
            data.get(CONF_ADDRESS, "unknown"),
        )
        return super().async_create_entry(**kwargs)

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._name: str | None = None
        self._fetched_data: dict[str, Any] | None = None
        self._pair_error: str | None = None
        self._transport_type: str = TRANSPORT_BLEAK
        # Direct BLE only: the adapter picked before the first connect, so
        # the probe — and its pairing — already runs through it.
        self._bluetooth_adapter: str = BLUETOOTH_ADAPTER_AUTO
        self._esp_device_name: str | None = None
        self._esp_bridge_id: str = ""
        # Address the bridge files the bond under; differs from _address
        # (the identity) for a brush with a rotating address.
        self._bond_address: str = ""
        self._esp_bridge_ids: list[str] = []
        self._bridge_info: dict[str, str] | None = None
        self._probed_bridges: dict[
            str, list[tuple[str, dict[str, str] | None]]
        ] = {}
        # When each ESP's slots were last probed, so a stale reuse can be
        # told apart from a fresh one (see _PROBE_CACHE_MAX_AGE).
        self._probed_at: dict[str, float] = {}
        # ESP dropdown values listed without a probe because the node is
        # offline — the sole-ESP auto-select must not skip the picker for
        # one of these.
        self._offline_esp_values: set[str] = set()
        self._manual_address_entry: bool = False
        self._configured_bridge_ids: set[str] = set()
        # One-shot: the ESP auto-route in bluetooth_confirm runs multi-second
        # slot probes — check once per flow, not on every re-render.
        self._esp_redirect_checked: bool = False
        # Transport of the last probe that actually connected. None until a
        # probe establishes a connection; deliberately NOT reset on failed
        # connects so a retry that never reaches the device keeps showing
        # the pairing dialog that matches the last known transport.
        self._probe_via_proxy: bool | None = None
        # MAC of the local adapter that carried the last probe — bonds
        # are per adapter, so bond checks and pairing must target it.
        self._probe_adapter: str | None = None
        self._probe_proxy_name: str | None = None
        # Set when a probe read had to wait for SMP (encryption required).
        # Used to label BLE security on the proxy path, where the BlueZ
        # bond state is meaningless (the bond lives in the proxy's NVS).
        self._probe_needed_encryption: bool = False
        # One-shot marker: wait_pair just bonded the bridge, so the next
        # esp_bridge_status render acknowledges the success.
        self._just_paired: bool = False
        # Set with _just_paired when the bridge reported a bond under a
        # rotating address on a chip that cannot follow it (addr_res=ctrl).
        self._paired_rpa_unsupported: bool = False
        # Set once the user picked an action for a bonded-but-unconfigured
        # slot, so re-entering the health check doesn't re-show the menu.
        self._slot_action_chosen: bool = False
        # One-shot marker: reset_bridge just cleared the bond, so the next
        # request_pair render confirms the slot is free.
        self._just_unpaired: bool = False
        # Progress state: unpair (reset_bridge) and ESP capabilities read.
        self._unpair_task: asyncio.Task | None = None
        self._unpair_outcome: str = ""
        self._esp_caps_task: asyncio.Task | None = None
        self._esp_caps_result: dict[str, Any] | None = None
        self._esp_read_error: str = ""
        # Pair-mode progress state (async_show_progress two-phase flow).
        self._pair_arm_task: asyncio.Task | None = None
        self._pair_scan_task: asyncio.Task | None = None
        self._pair_future: asyncio.Future[dict[str, str]] | None = None
        self._pair_unsub: Callable[[], None] | None = None
        self._pair_active_scan_task: asyncio.Task | None = None
        self._pair_svc_name: str = ""
        self._pair_result: dict[str, str] | None = None
        # Direct-BLE probe progress state, shared by bluetooth_confirm and
        # user_bleak; ble_probe_finish routes the outcome back to whichever
        # step started the probe (``_ble_probe_origin``).
        self._ble_probe_task: asyncio.Task | None = None
        self._ble_probe_result: dict[str, Any] | None = None
        self._ble_probe_origin: str = ""
        # One-shot <ha-alert> for the next bluetooth_confirm render
        # (errors[] doesn't render on that schema-less step).
        self._confirm_status: str = ""
        # One-shot errors["base"] for the next user_bleak render.
        self._manual_error: str = ""

    # ------------------------------------------------------------------
    # Duplicate check
    # ------------------------------------------------------------------
    @staticmethod
    def _is_condor_rpa(discovery_info: BluetoothServiceInfoBleak) -> bool:
        """True for a Condor brush seen under a rotating private address.

        The Philips public identity (OUI ``24:E5:AA``) is excluded — that is
        the stable address the config entry is keyed on and is handled by the
        normal already-configured check.
        """
        if discovery_info.address.upper().startswith("24:E5:AA"):
            return False
        uuids = {u.lower() for u in (discovery_info.service_uuids or ())}
        name = discovery_info.name or ""
        return SVC_CONDOR.lower() in uuids or name.startswith("Philips Sonicare")

    def _prune_stale_condor_flows(self) -> None:
        """Abort sibling Condor RPA discovery flows that have gone stale.

        Triggered when a fresh Condor advertisement arrives. A flow is dropped
        once its address has not been advertised for ``_CONDOR_FLOW_STALE_
        SECONDS`` — a rotated-away RPA never returns, so its flow would linger
        forever otherwise. Flows for still-advertising addresses (e.g. a
        neighbour's brush) are kept.
        """
        now = time.monotonic()
        flow_mgr = self.hass.config_entries.flow
        for flow in flow_mgr.async_progress_by_handler(DOMAIN):
            if flow["flow_id"] == self.flow_id:
                continue
            address = flow.get("context", {}).get("condor_rpa_address")
            if not address:
                continue
            info = async_last_service_info(self.hass, address, connectable=False)
            if info is not None and (now - info.time) < _CONDOR_FLOW_STALE_SECONDS:
                continue
            try:
                flow_mgr.async_abort(flow["flow_id"])
                _LOGGER.debug(
                    "Pruned stale Condor discovery flow for %s", address
                )
            except Exception:  # noqa: BLE001 — flow may have just finished
                pass

    async def _adopt_identity(self, identity: str | None) -> None:
        """Key the entry on the identity a direct-BLE pairing revealed.

        Same pattern as the bridge: the address is the identity, the
        address the brush was found under is kept as bond_address. A brush
        that already has an entry under its identity aborts here.
        """
        identity = _valid_identity(identity)
        if not identity or identity == (self._address or "").upper():
            return
        self._bond_address = self._address or ""
        self._address = identity
        await self.async_set_unique_id(identity, raise_on_progress=False)
        self._abort_if_already_configured()

    def _abort_if_already_configured(self) -> None:
        """Abort with detailed message if this unique_id is already configured."""
        for entry in self._async_current_entries():
            if entry.unique_id and entry.unique_id == self.unique_id:
                transport = entry.data.get(CONF_TRANSPORT_TYPE, TRANSPORT_BLEAK)
                transport_label = (
                    "ESP32 Bridge" if transport == TRANSPORT_ESP_BRIDGE
                    else "Direct Bluetooth"
                )
                # Two reasons rather than a translated "{status}" word:
                # placeholder values are built here and would stay English
                # in a non-English frontend.
                reason = (
                    "already_configured_disabled"
                    if entry.disabled_by is not None
                    else "already_configured_detail"
                )
                raise AbortFlow(
                    reason,
                    description_placeholders={"transport": transport_label},
                )

    def _abort_if_esp_entry_configured(self, just_paired: bool = False) -> None:
        """Abort for a brush that already has an entry.

        ESP bridge entries are keyed by the brush's identity, so pairing it
        again finds its entry even under a new bond address — also an entry
        set up before bridge firmware 1.15.0, which only knows the identity
        as pending. On the same slot the entry keeps working as it is. A
        brush is not set up on a second slot: its entry stays where it is,
        and a pairing just made on this slot is undone so the two slots don't
        compete for the brush.
        """
        identity = self.unique_id
        for entry in self._async_current_entries():
            if (
                entry.data.get(CONF_TRANSPORT_TYPE) != TRANSPORT_ESP_BRIDGE
                or entry.disabled_by is not None
                or identity
                not in (entry.unique_id, entry.data.get(CONF_PENDING_IDENTITY))
            ):
                continue
            old_esp = entry.data.get(CONF_ESP_DEVICE_NAME, "")
            old_slot = entry.data.get(CONF_ESP_BRIDGE_ID, "")
            if (old_esp.lower(), old_slot.lower()) == (
                self._esp_device_name.lower(), (self._esp_bridge_id or "").lower()
            ):
                if self._bond_address:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, CONF_BOND_ADDRESS: self._bond_address},
                    )
                if entry.unique_id != identity:
                    # Still keyed by its old bond address, which the bridge no
                    # longer uses: the reload switches it to the identity.
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                raise AbortFlow(
                    "esp_bridge_repaired",
                    description_placeholders={"target": self._target_label()},
                )
            _LOGGER.warning(
                "Toothbrush %s is already set up on %s (slot %s); not setting "
                "it up on %s (slot %s)%s",
                identity, old_esp, old_slot or "default",
                self._esp_device_name, self._esp_bridge_id or "default",
                " — undoing the pairing" if just_paired else "",
            )
            if just_paired:
                self.hass.async_create_task(
                    self._async_undo_pairing(
                        self._esp_device_name, self._esp_bridge_id or ""
                    ),
                    "philips_sonicare_ble undo pairing",
                )
            raise AbortFlow(
                "esp_bridge_elsewhere_undone" if just_paired
                else "esp_bridge_elsewhere",
                description_placeholders={
                    "old_target": f"{old_esp} / {old_slot or 'default'}",
                    "target": self._target_label(),
                },
            )
        self._abort_if_already_configured()

    def _target_label(self) -> str:
        return f"{self._esp_device_name} / {self._esp_bridge_id or 'default'}"

    async def _async_undo_pairing(self, esp: str, slot: str) -> None:
        """Unpair a slot that was just paired with an already set-up brush."""
        result = await async_unpair_bridge_slot(self.hass, esp, slot)
        if result != UNPAIR_OK:
            _LOGGER.warning(
                "Could not undo the pairing on %s (slot %s): %s — reset the "
                "slot from the integration's setup dialog",
                esp, slot or "default", result,
            )

    # ------------------------------------------------------------------
    # Capabilities fetch (direct BLE)
    # ------------------------------------------------------------------
    async def _read_with_auth_retry(
        self,
        client: BleakClient,
        char_uuid: str,
        timeout: float = 5.0,
    ) -> bytes | None:
        """Read a GATT characteristic, retrying once on auth errors.

        ESPHome bluetooth_proxy negotiates SMP in the background on the
        first read of a protected characteristic. That read returns
        status=0x05; auth completes ~500-1500 ms later. A single retry
        after a 2s grace period turns the transient failure into a
        success without false-positive "not paired" errors.
        """
        try:
            return await asyncio.wait_for(
                client.read_gatt_char(char_uuid), timeout=timeout
            )
        except (BleakError, TimeoutError) as err:
            err_msg = str(err).lower()
            auth_error = any(
                hint in err_msg
                for hint in (
                    "0x05", "0x0e", "0x0f",
                    "unlikely error",
                    "insufficient auth", "insufficient enc",
                    "not permitted", "authentication", "security",
                )
            )
            if not auth_error or not client.is_connected:
                raise
            _LOGGER.info(
                "Read on %s returned auth error — waiting for SMP to complete",
                char_uuid,
            )
            # Encryption was required — remember it so the proxy path can
            # label BLE security correctly (BlueZ can't see the ESP bond).
            self._probe_needed_encryption = True
            await asyncio.sleep(2.0)
            return await asyncio.wait_for(
                client.read_gatt_char(char_uuid), timeout=timeout
            )

    def _bump_progress(self, value: float) -> None:
        """Advance the determinate progress bar, if this core supports it.

        ``async_update_progress`` arrived in HA 2025.5 — on older cores the
        progress step simply keeps its indeterminate spinner. Calls made
        while no progress step is showing (e.g. the probe re-run from the
        not_paired retry) fire an update event nothing listens to; harmless.
        """
        update = getattr(self, "async_update_progress", None)
        if update is not None:
            update(min(1.0, max(0.0, value)))

    async def _creep_progress(
        self, start: float, end: float, duration: float
    ) -> None:
        """Creep the bar on wall-clock time while a long single await runs.

        ``establish_connection`` retries internally for up to ~90 s
        against an unreachable device or a stale bond without any
        callback we could hook — without this the bar sits frozen at the
        pre-connect milestone the whole time. The caller cancels the
        task the moment the await returns; real milestones then overwrite
        whatever the creep reached.
        """
        loop = self.hass.loop
        t0 = loop.time()
        while True:
            await asyncio.sleep(2.0)
            frac = min(1.0, (loop.time() - t0) / duration)
            self._bump_progress(start + (end - start) * frac)
            if frac >= 1.0:
                return

    async def _async_fetch_capabilities(self, address: str) -> dict[str, Any]:
        """Connect to the device and read capabilities via direct BLE."""
        self._probe_needed_encryption = False
        # Pre-fill services from advertisement data (available before connect)
        adv_services: list[str] = []
        if self._discovery_info is not None:
            adv_services = [
                u.lower() for u in (self._discovery_info.service_uuids or [])
            ]

        result: dict[str, Any] = {"services": list(adv_services)}

        # Gate on the age of the last *connectable* advertisement. Within the
        # ~195 s habluetooth fallback window async_ble_device_from_address (and
        # the frozen discovery_info.device) still hand back a stale BLEDevice
        # whose connect just drops mid-handshake — surfacing to the user as
        # five "device disconnected" retries and a confusing "Authentication
        # Canceled". The brush advertises every ~1-2 s while awake and stops
        # when it sleeps, so a stale last-ADV means it is asleep: bail out early
        # with an actionable signal instead. The history timestamp is updated on
        # every received advertisement (including deduplicated identical ones —
        # dedup only suppresses callback dispatch, not the history write), so an
        # awake brush is never misread as asleep here.
        last = async_last_service_info(self.hass, address, connectable=True)
        # A BlueZ RSSI-invalidation event (RSSI -127) also bumps the history
        # timestamp without a packet on the air — treat it as "not seen", or
        # the sentinel keeps the entry fresh and a stale BLEDevice slips past
        # the gate (seen after an adapter power-cycle, where the doomed
        # connects were then misread as a stale bond).
        stale_rssi = (
            last is not None and last.rssi is not None and last.rssi <= -127
        )
        age = None if last is None else (time.monotonic() - last.time)
        if last is None or stale_rssi or age > _STALE_ADV_MAX_SECONDS:
            _LOGGER.info(
                "%s: no recent connectable advertisement (%s) — device asleep",
                address,
                "never seen" if last is None
                else "stale RSSI -127" if stale_rssi
                else f"{age:.0f}s ago",
            )
            raise DeviceAsleepException

        device = async_ble_device_from_address(self.hass, address)
        if not device:
            _LOGGER.warning("Device %s not found despite recent ADV", address)
            raise DeviceAsleepException

        fixed = self._bluetooth_adapter
        if fixed != BLUETOOTH_ADAPTER_AUTO and fixed not in {
            p["source"] for p in describe_available_paths(self.hass, address)
        }:
            # Fail fast with a readable reason instead of letting the fixed
            # client run establish_connection into its retries.
            _LOGGER.info(
                "%s: fixed Bluetooth adapter %s does not receive the toothbrush",
                address, fixed,
            )
            raise AdapterNotReceivingException

        client: BleakClient | None = None
        try:
            # Progress milestones: the connect is a single await and by far
            # the longest leg, so the bar sits low until it lands, then
            # advances per characteristic read.
            self._bump_progress(0.05)
            # A stale bond makes establish_connection retry internally for
            # up to ~90 s (4 attempts) — creep the bar toward the
            # post-connect milestone so the dialog visibly keeps working.
            creep = self.hass.async_create_task(
                self._creep_progress(0.05, 0.38, 90.0)
            )
            try:
                client = await bleak_establish(
                    fixed_scanner_client_class(self._bluetooth_adapter),
                    device, "philips_sonicare_ble",
                    use_services_cache=True, timeout=30.0,
                )
            finally:
                creep.cancel()
            if not client or not client.is_connected:
                return result
            self._bump_progress(0.4)

            connection_path = describe_connection_path(self.hass, client, device)
            result["connection_path"] = connection_path
            # Remember which transport carried this probe: a later
            # NotPairedException must route to the matching pairing
            # dialog (host instructions vs. proxy guidance) and decide
            # whether the D-Bus auto-pair machinery applies at all.
            self._probe_via_proxy = not is_local_bluez_connection(client)
            self._probe_adapter = local_adapter_source(client)
            # Scanner names carry the adapter MAC in parentheses — strip it
            # for the dialog (same as _short_scanner in the preview).
            self._probe_proxy_name = (
                connection_path.split(" (")[0]
                if self._probe_via_proxy else None
            )
            _LOGGER.info(
                "%s: capabilities probe connected via %s",
                address,
                connection_path,
            )

            # GATT services are more complete than advertisement — use them
            gatt_services = [str(s.uuid).lower() for s in client.services]
            if gatt_services:
                result["services"] = gatt_services
            self._bump_progress(0.5)

            # Condor brushes (HX742X / Series 7100) require BLE bonding
            # before the e50b… handshake's first CCCD write is accepted.
            # The probe below only touches Device-Info chars which are
            # open-read on these devices, so the bond requirement wouldn't
            # surface as an auth error here. Trigger auto-pair preemptively
            # when the Condor service is discovered and no bond exists yet
            # — mirrors the ESP bridge's esp_ble_set_encryption() trigger
            # on Condor detection.
            just_paired_in_place = False

            if SVC_CONDOR.lower() in gatt_services:
                from .dbus_pairing import (
                    PairingError,
                    async_is_device_paired,
                    async_pair_via_existing_client,
                )
                if not await async_is_device_paired(
                    address, self._probe_adapter
                ):
                    _LOGGER.info(
                        "%s: Condor service present without a bond — "
                        "pairing on the existing probe connection",
                        address,
                    )
                    try:
                        await async_pair_via_existing_client(client, address)
                        # let BlueZ settle SMP/encryption before reads
                        await asyncio.sleep(0.5)
                        just_paired_in_place = True
                    except PairingError as err:
                        _LOGGER.warning(
                            "%s: in-place pairing failed (%s) — "
                            "falling back to disconnect+reconnect pair",
                            address,
                            err,
                        )
                        raise NotPairedException(
                            "Condor brush requires bonding"
                        ) from err

            # Pairing hands BlueZ the brush's identity key; from then on it
            # files the device under the identity, and bleak's client
            # follows (Device1.Address). The rotating address it was found
            # under is unreachable afterwards — the entry must use this.
            identity = _valid_identity(getattr(client, "address", None))
            if identity and identity != address.upper():
                _LOGGER.info(
                    "%s: toothbrush identity is %s", address, identity
                )
                result["identity"] = identity

            # Battery is on the standard 0x180F service. Condor brushes
            # don't expose it (battery comes via Condor port-property at
            # runtime), so probing 0x2A19 there raises CharacteristicNotFound.
            # Skip the probe unless the service is actually present.
            probe_chars: list[tuple[str, str]] = []
            if SVC_BATTERY.lower() in gatt_services:
                probe_chars.append((CHAR_BATTERY_LEVEL, "battery"))
            probe_chars += [
                (CHAR_MODEL_NUMBER, "model"),
                (CHAR_SERIAL_NUMBER, "serial"),
                (CHAR_FIRMWARE_REVISION, "firmware"),
            ]

            progress_step = 0.45 / max(1, len(probe_chars))
            for index, (char_uuid, key) in enumerate(probe_chars, start=1):
                try:
                    raw = await self._read_with_auth_retry(
                        client, char_uuid, timeout=5.0
                    )
                    if raw:
                        if key == "battery":
                            result[key] = raw[0]
                        else:
                            result[key] = raw.decode("utf-8", "ignore").strip("\x00 ")
                except (BleakError, TimeoutError, Exception) as err:
                    err_msg = str(err).lower()
                    auth_error = any(
                        hint in err_msg
                        for hint in (
                            "0x05", "0x0e", "0x0f",
                            "unlikely error",
                            "insufficient auth", "insufficient enc",
                            "not permitted", "authentication", "security",
                        )
                    )
                    # Only an explicit auth hint means "not paired". A read
                    # that fails for any other reason (char absent, timeout,
                    # transient stack issue) must NOT trigger the destructive
                    # legacy auto-pair path, which would RemoveDevice() on
                    # what may be a perfectly good bond.
                    # The auth_error flag (stale-bond evidence, issue #25)
                    # is only trustworthy when the probe rode the local
                    # BlueZ adapter — bond state is per-controller, so an
                    # auth error via a remote (proxy) scanner says nothing
                    # about the BlueZ bond and must not get it wiped.
                    if auth_error and not just_paired_in_place:
                        raise NotPairedException(
                            auth_error=is_local_bluez_connection(client)
                        ) from err
                    _LOGGER.debug("Failed to read %s: %s", key, err)
                self._bump_progress(0.5 + progress_step * index)

            # Bond-gated profile (see helpers.is_bond_gated_profile): none
            # of the probe reads produced data and Device Information is
            # absent — hand this to the pairing path instead of letting it
            # surface as "no characteristics found".
            if not just_paired_in_place and is_bond_gated_profile(
                result, gatt_services, adv_services
            ):
                _LOGGER.info(
                    "%s: connected but the Device Information service is "
                    "missing from the GATT table — bond-gated profile, "
                    "requesting pairing",
                    address,
                )
                raise NotPairedException

        except NotPairedException:
            raise
        except (BleakError, TimeoutError) as err:
            err_msg = str(err).lower()
            if "failed to discover services" in err_msg:
                _LOGGER.warning("Service discovery failed (stale bond?): %s", err)
                raise NotPairedException from err
            _LOGGER.warning("Could not connect during capabilities fetch: %s", err)
        except Exception as err:
            _LOGGER.warning("Could not connect during capabilities fetch: %s", err)
        finally:
            # Not gated on is_connected: a client the brush already
            # disconnected still needs disconnect() called to close
            # bleak's own D-Bus connection — #45. Reuses the shared
            # helper instead of a duplicated inline try/except.
            if client:
                await _cleanup_disconnected_client(client)

        return result

    # ------------------------------------------------------------------
    # BLE pairing helpers
    # ------------------------------------------------------------------
    async def _try_auto_pair(self, address: str) -> bool:
        """Attempt D-Bus auto-pairing. Returns True on success."""
        from .dbus_pairing import PairingError, async_pair_and_trust, is_dbus_available

        if not is_dbus_available():
            _LOGGER.debug("D-Bus not available — cannot auto-pair")
            return False

        try:
            _LOGGER.info("Auto-pairing %s via D-Bus ...", address)
            await async_pair_and_trust(
                address, self._pair_target_adapter(address)
            )
            await asyncio.sleep(2)  # let BlueZ key distribution settle
            return True
        except PairingError as err:
            _LOGGER.warning("Auto-pairing failed for %s: %s", address, err)
            # Remember the reason so the not_paired step can show the user
            # why pairing failed instead of a generic instruction wall.
            self._pair_error = str(err)
            return False

    def _fixed_adapter_is_local(self, address: str) -> bool:
        fixed = self._bluetooth_adapter.upper()
        return any(
            s["is_local"] and str(s["source"]).upper() == fixed
            for s in describe_connectable_scanners(self.hass, address)
        )

    def _pair_target_adapter(self, address: str) -> str | None:
        """Local adapter whose bond a pairing step should check and create.

        A fixed adapter is the only one the entry will ever connect
        through. On Automatic, the adapter that carried the probe; when
        the probe never connected, the strongest local adapter hearing
        the brush. None when no local adapter is known — then BlueZ picks.
        """
        if self._bluetooth_adapter != BLUETOOTH_ADAPTER_AUTO:
            # A fixed proxy is no BlueZ adapter: nothing to pair there.
            return (
                self._bluetooth_adapter
                if self._fixed_adapter_is_local(address) else None
            )
        if self._probe_adapter:
            return self._probe_adapter
        local = next(
            (
                p for p in describe_available_paths(self.hass, address)
                if p.get("is_local") and p.get("source")
            ),
            None,
        )
        return str(local["source"]) if local else None

    async def _fetch_with_pair_retry(self, address: str) -> dict[str, Any]:
        """Fetch capabilities, auto-pairing on auth errors.

        Probe first without pairing; only pair on auth failure. After a
        successful probe, query BlueZ for the bond state to label the
        device — this distinguishes true open GATT from a device that
        is already bonded (e.g. a stale bond that survived a previous
        config entry removal and lets reads succeed without a new
        handshake).

        Raises NotPairedException if pairing fails or is not possible.
        """
        from .dbus_pairing import async_is_device_paired

        auth_error = False
        try:
            result = await self._async_fetch_capabilities(address)
            if self._probe_via_proxy:
                # BlueZ can't see the bond on a proxy connection (it lives
                # in the proxy's NVS), so async_is_device_paired would
                # mislabel an encrypted link as "unpaired". Derive it from
                # whether a read had to wait for SMP instead.
                result["pairing"] = (
                    "bonded" if self._probe_needed_encryption else "open_gatt"
                )
            else:
                paired = await async_is_device_paired(
                    address, self._probe_adapter
                )
                result["pairing"] = "bonded" if paired else "open_gatt"
            return result
        except NotPairedException as err:
            auth_error = err.auth_error

        # A proxy-carried connection bonds on the ESP itself (Bluedroid
        # pairs lazily during the auth read) — the host-side D-Bus
        # machinery below can neither inspect nor repair that bond, and
        # its RemoveDevice would only touch the unrelated BlueZ device
        # entry. Skip it and surface the failure; the pairing step then
        # shows proxy-specific guidance, and its Retry re-triggers the
        # ESP-side SMP via a fresh probe read.
        if self._probe_via_proxy or (
            self._bluetooth_adapter != BLUETOOTH_ADAPTER_AUTO
            and not self._fixed_adapter_is_local(address)
        ):
            raise NotPairedException

        # If a bond already exists, the destructive RemoveDevice in
        # async_pair_and_trust would wipe it and leave the device
        # unreachable until it re-advertises (Condor brushes only
        # re-advertise on rotating RPAs, so the public identity is gone
        # for ~30 s after the wipe). The probe failed for some other
        # reason — surface that to the user instead of nuking the bond.
        # Exception: an explicit auth error *despite* the bond means the
        # device no longer accepts our key — the bond is stale and
        # already worthless, so remove+re-pair is the only recovery
        # (issue #25: otherwise the user is stuck until a manual
        # ``bluetoothctl remove``).
        if await async_is_device_paired(
            address, self._pair_target_adapter(address)
        ):
            if not auth_error:
                _LOGGER.warning(
                    "%s: capability read failed but a bond exists — "
                    "refusing to wipe it via legacy auto-pair",
                    address,
                )
                raise NotPairedException
            _LOGGER.warning(
                "%s: authentication failed although a bond exists — "
                "the bond is stale, removing it and re-pairing",
                address,
            )

        # No bond, or a stale one — auto-pair (RemoveDevice + fresh pair)
        if await self._try_auto_pair(address):
            try:
                result = await self._async_fetch_capabilities(address)
                result["pairing"] = "bonded"
                return result
            except NotPairedException:
                pass
        raise NotPairedException

    # ------------------------------------------------------------------
    # Capabilities fetch (ESP bridge)
    # ------------------------------------------------------------------
    async def _async_fetch_capabilities_esp(
        self,
        address: str,
        esp_device_name: str,
        esp_bridge_id: str = "",
    ) -> dict[str, Any]:
        """Read capabilities and probe services via ESP32 bridge.

        Two paths:

        - **Deterministic** (bridge ≥ v1.3.0): ``ble_list_services``
          returns the GATT service table in one shot. Protocol detection
          (Condor / Classic) drops straight out of that list, and only
          characteristics we actually care about are read (model number;
          battery if the standard service is present). Failed reads of
          characteristics that don't exist on a given model are avoided —
          they only ever surfaced as bridge-side warnings.
        - **Legacy probe** (bridge older than v1.3.0 or transient
          failure): falls back to reading one probe char per known
          Sonicare service, with Condor inferred by exclusion when only
          Device Information answered. Kept for back-compat; the noisy
          warnings come from this path.

        Both paths populate the same ``found_services`` list; downstream
        code is identical.
        """
        transport = EspBridgeTransport(self.hass, address, esp_device_name, esp_bridge_id)
        try:
            # Progress milestones — each read is its own bridge round-trip,
            # so the bar advances per characteristic.
            self._bump_progress(0.05)
            await transport.connect()
            self._bump_progress(0.25)

            found_services: list[str] = []
            model_number: str | None = None
            battery: int | None = None

            services_from_bridge = await transport.list_services()
            if services_from_bridge:
                self._bump_progress(0.45)
                found_services = [s.lower() for s in services_from_bridge]
                services_set = set(found_services)
                # Model number — always emitted on Device Information.
                raw_model = await transport.read_char(CHAR_MODEL_NUMBER)
                if raw_model:
                    model_number = raw_model.decode("utf-8", errors="replace").strip()
                self._bump_progress(0.55)
                # Battery — only on the standard 0x180F service. Condor
                # brushes route battery through their port-property layer
                # instead and would 404 the 0x2A19 read.
                if SVC_BATTERY.lower() in services_set:
                    raw_batt = await transport.read_char(CHAR_BATTERY_LEVEL)
                    if raw_batt:
                        battery = raw_batt[0]
                self._bump_progress(0.65)
            else:
                # Legacy probe — old bridges without ble_list_services.
                probe_count = max(1, len(SERVICE_PROBE_CHARS))
                for index, (svc_uuid, probe_char) in enumerate(
                    SERVICE_PROBE_CHARS.items(), start=1
                ):
                    raw = await transport.read_char(probe_char)
                    if raw is not None:
                        found_services.append(svc_uuid)
                        if probe_char == CHAR_MODEL_NUMBER:
                            model_number = raw.decode("utf-8", errors="replace").strip()
                        elif probe_char == CHAR_BATTERY_LEVEL and raw:
                            battery = raw[0]
                    self._bump_progress(0.25 + 0.4 * index / probe_count)
                # Condor-by-exclusion: the only readable Condor char
                # (e50b0005) is optional — HX742X FW 1.8.20.0 omits it
                # entirely, so a direct probe misses that device. If the
                # device answered Device Information but none of the
                # Classic feature services, the only supported protocol
                # left is Condor.
                classic_seen = any(
                    svc in found_services for svc in (
                        SVC_SONICARE, SVC_ROUTINE, SVC_STORAGE, SVC_SENSOR,
                        SVC_BRUSHHEAD, SVC_DIAGNOSTIC, SVC_EXTENDED, SVC_BATTERY,
                    )
                )
                if (
                    model_number
                    and not classic_seen
                    and SVC_CONDOR not in found_services
                ):
                    found_services.append(SVC_CONDOR)
                    _LOGGER.debug(
                        "ESP bridge: inferred Condor protocol on %s (model=%s, no Classic services)",
                        address, model_number,
                    )

            if not found_services:
                raise TransportError(
                    "Could not read any service via ESP bridge - toothbrush may not be connected"
                )

            # Serial
            serial: str | None = None
            raw_serial = await transport.read_char(CHAR_SERIAL_NUMBER)
            if raw_serial:
                serial = raw_serial.decode("utf-8", errors="replace").strip()
            self._bump_progress(0.8)

            # Firmware
            firmware: str | None = None
            raw_fw = await transport.read_char(CHAR_FIRMWARE_REVISION)
            if raw_fw:
                firmware = raw_fw.decode("utf-8", errors="replace").strip()
            self._bump_progress(0.95)

            connection_path = self._esp_target_label(esp_device_name, esp_bridge_id)
            return {
                "services": found_services,
                "sonicare_mac": transport.detected_mac,
                "model": model_number,
                "serial": serial,
                "firmware": firmware,
                "battery": battery,
                "connection_path": connection_path,
            }

        except TransportError:
            raise
        finally:
            await transport.disconnect()

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_family(fetched_lower: set[str], model: str) -> str:
        """Return device family: 'condor', 'mode_b', or 'classic'."""
        if SVC_CONDOR.lower() in fetched_lower:
            return "condor"
        # HX63xx / HX64xx are Mode-B brushes with a reduced service set.
        m = (model or "").upper()
        if m.startswith(("HX63", "HX64")):
            return "mode_b"
        return "classic"

    @staticmethod
    def _missing_reason(uuid_lower: str, family: str) -> str:
        """Why a service is absent on this device, if we can explain it."""
        if family == "condor" and uuid_lower in _CLASSIC_SERVICE_UUIDS:
            return "via Condor protocol"
        if family == "mode_b":
            return "not on this model"
        return ""

    @classmethod
    def _get_service_status_text(
        cls, fetched_uuids: list[str], model: str = ""
    ) -> tuple[str, bool, bool]:
        """Return ``(table, needs_condor_note, has_missing)`` for the services block.

        The table is a 2-column HTML table — left column: ✅ available
        services, right column: ❌ services not present on this model.
        A Condor handle never has the Classic feature services, so they
        are left out rather than listed as missing.
        Its row count depends on the model, so unlike the device-info
        table it cannot become static translated text; the service names
        it lists are protocol identifiers and stay as they are.

        ``needs_condor_note`` asks for the text block explaining why the
        Classic feature services are absent on a Condor handle;
        ``has_missing`` asks for the ❌ legend.
        """
        fetched_lower = {s.lower() for s in fetched_uuids} - _STANDARD_BLE_SERVICES
        family = cls._detect_family(fetched_lower, model)

        found: list[str] = []
        missing: list[str] = []
        used_reasons: set[str] = set()

        for uuid in sorted(_EXPECTED_SERVICES):
            name = SERVICE_NAMES.get(uuid)
            if not name:
                continue
            if uuid == SVC_CONDOR.lower() and family != "condor":
                continue
            if uuid in fetched_lower:
                found.append(name)
            else:
                reason = cls._missing_reason(uuid, family)
                if reason:
                    used_reasons.add(reason)
                if reason == "via Condor protocol":
                    continue
                missing.append(name)

        known_all = _EXPECTED_SERVICES | {SVC_BYTESTREAM.lower()}
        for uuid in sorted(fetched_lower - _EXPECTED_SERVICES):
            name = SERVICE_NAMES.get(uuid)
            if not name or uuid not in known_all:
                continue
            found.append(name)

        if not found and not missing:
            # A dash, not a sentence — this value is built here and could
            # not follow the user's frontend language.
            return "—", False, False

        # Layout: ✅ left column, ❌ right column when both groups are non-empty.
        # When only one group is present (e.g. premium model with all services
        # supported), split it evenly across both columns instead of leaving a
        # blank column.
        if found and missing:
            left_items = [f"✅ {n}" for n in found]
            right_items = [f"❌ {n}" for n in missing]
        elif found:
            mid = (len(found) + 1) // 2
            left_items = [f"✅ {n}" for n in found[:mid]]
            right_items = [f"✅ {n}" for n in found[mid:]]
        else:
            mid = (len(missing) + 1) // 2
            left_items = [f"❌ {n}" for n in missing[:mid]]
            right_items = [f"❌ {n}" for n in missing[mid:]]

        rows: list[str] = []
        for i in range(max(len(left_items), len(right_items))):
            left = left_items[i] if i < len(left_items) else ""
            right = right_items[i] if i < len(right_items) else ""
            rows.append(f"<tr><td>{left}</td><td>{right}</td></tr>")

        table = f"<table><tbody>{''.join(rows)}</tbody></table>"

        # The "❌ = not on this model" note is a static legend in the
        # step's translated text. The Condor note is model-specific, so
        # it rides in as a translated block — neither may be a sentence
        # built here, which would not follow the frontend language.
        return table, "via Condor protocol" in used_reasons, bool(missing)

    @staticmethod
    def _get_device_info_values(
        data: dict[str, Any], address: str | None = None
    ) -> dict[str, str]:
        """Values for the device-info table in the capabilities dialog.

        The row labels live in the step's translated description; only
        the readings themselves are passed, so the table follows the
        user's frontend language. Missing readings render as a dash
        rather than dropping the row — a fixed row set is what lets the
        table be static, translated text.
        """
        battery = data.get("battery")
        return {
            "model": data.get("model") or "—",
            "serial": data.get("serial") or "—",
            "firmware": data.get("firmware") or "—",
            "battery": f"{battery}%" if battery is not None else "—",
            "mac": address.upper() if address else "—",
            # Same 🔐/🔓 vocabulary as the bridge status table; the
            # legend under the table explains both.
            "ble_security": {
                "bonded": "\U0001f510", "open_gatt": "\U0001f513",
            }.get(data.get("pairing", ""), "—"),
        }

    @staticmethod
    def _has_sonicare_services(data: dict[str, Any]) -> bool:
        """Check if any Sonicare-specific GATT services were discovered."""
        services = data.get("services", [])
        fetched_lower = {s.lower() for s in services} - _STANDARD_BLE_SERVICES
        return bool(fetched_lower & _EXPECTED_SERVICES)

    @staticmethod
    def _connection_status_placeholders(
        transport_type: str, path: str | None, *, via_proxy: bool = False
    ) -> dict[str, str]:
        """Placeholders for the capabilities dialog's connection line.

        Names the transport *class* (``ESP32 Bridge`` / ``Bluetooth
        proxy`` / ``Direct Bluetooth``) — same framing as the other
        ``via`` dialogs — and appends the slot/adapter label (YAML
        ``friendly_name`` for ESP, scanner name otherwise) in
        parentheses as a disambiguator, never in the ``via`` position
        itself. ``via_proxy`` marks a TRANSPORT_BLEAK probe that rode a
        remote scanner — labelling that "Direct Bluetooth" would hide
        the very path distinction the pairing dialogs are keyed on.

        The sentence itself lives in strings.json so it follows the
        user's frontend language; only the transport class (a product
        name), the adapter detail and the <ha-alert> wrapper are passed
        in — hassfest rejects HTML inside translation values, and
        ha-markdown does not parse markdown inside an HTML block, so the
        <b> emphasis rides along in the placeholder. The step is only
        reachable after a successful capability read, so there is no
        disconnected variant.
        """
        if transport_type == TRANSPORT_ESP_BRIDGE:
            transport_label = "ESP32 Bridge"
        elif via_proxy:
            transport_label = "Bluetooth proxy"
        else:
            transport_label = "Direct Bluetooth"
        return {
            "alert_open": '<ha-alert alert-type="success">',
            "alert_close": "</ha-alert>",
            "transport": f"<b>{transport_label}</b>",
            "detail": f" ({path})" if path else "",
        }

    # ------------------------------------------------------------------
    # ESP bridge helpers
    # ------------------------------------------------------------------
    async def _get_esphome_device_options(self) -> list[SelectOptionDict]:
        """Build a list of ESPHome devices that host a Sonicare bridge.

        Service-name detection alone is not enough — philips_shaver
        registers the same service names. We probe ble_get_info on each
        candidate and only accept ESPs where at least one bridge replies
        on the Sonicare event channel.
        """
        esphome_entries = self.hass.config_entries.async_entries("esphome")
        options: list[SelectOptionDict] = []
        self._probed_bridges = {}
        self._probed_at = {}
        self._offline_esp_values = set()

        # An offline ESP cannot be probed, and the probe is what tells our
        # bridge apart from a philips_shaver one — the service names are
        # identical. So an offline node is only worth showing when an
        # existing entry already vouches for it being ours; anything else
        # stays hidden rather than advertising someone else's hardware.
        ours = {
            esphome_service_id(name)
            for entry in self._async_current_entries()
            if (name := entry.data.get(CONF_ESP_DEVICE_NAME))
        }

        for entry in esphome_entries:
            if entry.disabled_by:
                _LOGGER.debug(
                    "esp_select: skipping disabled ESPHome entry '%s'",
                    entry.title,
                )
                continue
            device_name = entry.data.get("device_name")
            if not device_name:
                continue
            device_name = esphome_service_id(device_name)
            bridge_ids = self._detect_esp_bridge_ids(device_name)
            if not bridge_ids:
                continue

            # ESPHome already knows the link is down — don't burn the probe
            # timeout, show it as offline instead of dropping it silently.
            runtime = getattr(entry, "runtime_data", None)
            if runtime is not None and getattr(runtime, "available", True) is False:
                if device_name not in ours:
                    _LOGGER.debug(
                        "esp_select: skipping offline ESPHome entry '%s' "
                        "— never set up as a Sonicare bridge",
                        entry.title,
                    )
                    continue
                _LOGGER.debug(
                    "esp_select: ESPHome entry '%s' is offline — listing it "
                    "without a probe", entry.title,
                )
                options.append(
                    SelectOptionDict(value=device_name, label=f"⚪ {entry.title}")
                )
                self._offline_esp_values.add(device_name)
                continue

            sonicare = await self._probe_sonicare_bridges(device_name, bridge_ids)
            # Nothing answered on our event channel. That means either our
            # bridge is unreachable, or this ESP was never ours —
            # philips_shaver registers the same service names, and a failed
            # probe looks identical either way. An existing entry is the
            # only thing that can tell them apart.
            if not any(info is not None for _, info in sonicare):
                if device_name not in ours:
                    _LOGGER.debug(
                        "esp_select: '%s' answered no probe and was never set "
                        "up as a Sonicare bridge — not listing it", device_name
                    )
                    continue
                # Ours, but unreachable: list it so the user can see why it
                # is there but leads nowhere.
                _LOGGER.debug(
                    "esp_select: '%s' answered no probe — listing as offline",
                    device_name,
                )
                options.append(
                    SelectOptionDict(value=device_name, label=f"⚪ {entry.title}")
                )
                self._offline_esp_values.add(device_name)
                continue
            self._probed_bridges[device_name] = sonicare
            self._probed_at[device_name] = time.monotonic()

            slot_info = ""
            answered = [info for _, info in sonicare if info is not None]
            if len(answered) > 1:
                paired_count = sum(
                    1 for info in answered
                    if info.get("pair_capable") != "true"
                    and info.get("mac", "") not in ("", "00:00:00:00:00:00")
                )
                free_count = len(answered) - paired_count
                parts = []
                if paired_count:
                    parts.append(f"{paired_count} paired")
                if free_count:
                    parts.append(f"{free_count} free")
                if parts:
                    slot_info = f"{' / '.join(parts)} slots"

            show_slug = entry.title.lower() != device_name.lower()
            if show_slug and slot_info:
                label = f"{entry.title} ({device_name}, {slot_info})"
            elif show_slug:
                label = f"{entry.title} ({device_name})"
            elif slot_info:
                label = f"{entry.title} ({slot_info})"
            else:
                label = entry.title

            options.append(SelectOptionDict(value=device_name, label=label))
        return options

    def _detect_esp_bridge_ids(self, esp_device_name: str) -> list[str]:
        """Detect available device_id suffixes on an ESP bridge."""
        # Single device (no suffix)
        if self.hass.services.has_service("esphome", f"{esp_device_name}_ble_read_char"):
            return [""]

        # Multi-device: find suffixed services
        esphome_services = self.hass.services.async_services().get("esphome", {})
        prefix = f"{esp_device_name}_ble_read_char_"
        return [
            svc_name[len(prefix):]
            for svc_name in esphome_services
            if svc_name.startswith(prefix)
        ]

    async def _probe_bridge_info(
        self, esp_device_name: str, bridge_id: str, timeout: float = 3.0,
    ) -> dict[str, str] | None:
        """Probe a single bridge via ble_get_info.

        Returns the info-event payload, or ``None`` if the call timed out
        or no Sonicare-bridge response was received. Listening on
        ``philips_sonicare_ble_status`` is the disambiguator versus a
        philips_shaver bridge that happens to share service names.
        """
        svc_name = f"{esp_device_name}_ble_get_info"
        if bridge_id:
            svc_name += f"_{bridge_id}"
        if not self.hass.services.has_service("esphome", svc_name):
            return None

        info_future: asyncio.Future[dict[str, str]] = self.hass.loop.create_future()

        @callback
        def _on_status(event: Event) -> None:
            if (event.data.get("status") == "info"
                    # HA's ServiceRegistry lowercases service names, so a
                    # bridge_id with uppercase (e.g. an HX model number) yields
                    # a lowercase service suffix while the event echoes the
                    # original case — compare case-insensitively.
                    and event.data.get("bridge_id", "").lower() == bridge_id.lower()
                    and not info_future.done()):
                info_future.set_result(dict(event.data))

        unsub = self.hass.bus.async_listen(
            "esphome.philips_sonicare_ble_status", _on_status
        )
        try:
            await self.hass.services.async_call(
                "esphome", svc_name, {}, blocking=True
            )
            return await asyncio.wait_for(info_future, timeout=timeout)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — broad catch is intentional
            return None
        finally:
            unsub()

    @staticmethod
    def _esp_entry_unreachable(entry: ConfigEntry, context: str) -> bool:
        """True when an ESPHome entry cannot serve a bridge probe right now.

        Disabled bridges cannot hold a connection, and bridges whose
        ESPHome API link is down cannot answer — probing either only burns
        the probe timeout (their stale services may still be registered,
        so the service-based detection alone would wrongly pick them up).
        runtime_data is ESPHome's RuntimeEntryData; fall back to probing
        if the attribute layout ever changes.
        """
        if entry.disabled_by:
            _LOGGER.debug(
                "%s: bridge check — skipping disabled ESPHome entry '%s'",
                context, entry.title,
            )
            return True
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None and getattr(runtime, "available", True) is False:
            _LOGGER.debug(
                "%s: bridge check — skipping offline ESPHome entry '%s'",
                context, entry.title,
            )
            return True
        return False

    async def _probe_sonicare_bridges(
        self, esp_device_name: str, bridge_ids: list[str],
    ) -> list[tuple[str, dict[str, str] | None]]:
        """Probe all bridge_ids on an ESP in parallel.

        Returns ``(bridge_id, info | None)`` for **every** slot — ``None``
        marks one that did not answer on our event channel: offline, busy,
        or a different component (philips_shaver registers the same service
        names but fires its own event). Callers that only care about
        responders filter the Nones; the picker keeps them so a slot that
        needs attention stays visible instead of silently disappearing.
        """
        results = await asyncio.gather(
            *(self._probe_bridge_info(esp_device_name, did) for did in bridge_ids)
        )
        return list(zip(bridge_ids, results))

    # ------------------------------------------------------------------
    # Discovery flow
    # ------------------------------------------------------------------
    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> FlowResult:
        """Handle Zeroconf discovery of ESPHome devices.

        Checks if the discovered ESPHome device has our Sonicare bridge
        services registered. If not, aborts silently.
        """
        # Extract device name from zeroconf hostname (e.g. "atom-lite" from "atom-lite.local.")
        host = discovery_info.hostname or ""
        _LOGGER.debug("Flow started: zeroconf discovery of %s", host or "?")
        device_name = esphome_service_id(host.rstrip(".").removesuffix(".local"))
        if not device_name:
            return self.async_abort(reason="not_supported")

        # Wait for ESPHome to register services (may not be ready yet)
        for _ in range(10):
            bridge_ids = self._detect_esp_bridge_ids(device_name)
            if bridge_ids:
                break
            await asyncio.sleep(3)
        else:
            return self.async_abort(reason="not_supported")

        # Found bridges — check if ALL are already configured
        self._esp_device_name = device_name
        self._esp_bridge_ids = bridge_ids

        configured_macs = {
            entry.unique_id.upper()
            for entry in self._async_current_entries()
            if entry.unique_id
        }
        # (esp_device_name, esp_bridge_id) tuples that already have a
        # ConfigEntry — used as a probe-independent fallback when the
        # bridge can't supply mac/identity right after boot (NVS-restore
        # race) or while the bridge is actively connecting.
        configured_bridges = {
            (
                entry.data.get(CONF_ESP_DEVICE_NAME, ""),
                entry.data.get(CONF_ESP_BRIDGE_ID, ""),
            )
            for entry in self._async_current_entries()
            if entry.data.get(CONF_TRANSPORT_TYPE) == TRANSPORT_ESP_BRIDGE
        }

        # Probe bridges to check which are ours and which are already configured
        unconfigured = False
        for did in bridge_ids:
            # Direct ConfigEntry match — skip probe.
            if (device_name, did) in configured_bridges:
                continue
            svc_name = f"{device_name}_ble_get_info"
            if did:
                svc_name += f"_{did}"
            info_future: asyncio.Future[dict[str, str]] = self.hass.loop.create_future()

            @callback
            def _on_status(event: Event, _did=did) -> None:
                if (event.data.get("status") == "info"
                        # bridge_id compared case-insensitively (see above)
                        and event.data.get("bridge_id", "").lower() == _did.lower()
                        and not info_future.done()):
                    info_future.set_result(dict(event.data))

            unsub = self.hass.bus.async_listen(
                "esphome.philips_sonicare_ble_status", _on_status
            )
            try:
                await self.hass.services.async_call(
                    "esphome", svc_name, {}, blocking=True
                )
                info = await asyncio.wait_for(info_future, timeout=3.0)
                # Entries are keyed by the brush's identity or, set up
                # before bridge firmware 1.15.0, by its bond address.
                known = {
                    m for m in (bridge_identity(info), bridge_bond_address(info))
                    if m
                }
                if not known or not known.intersection(configured_macs):
                    unconfigured = True
                    break
            except (asyncio.TimeoutError, Exception):
                pass  # Not our bridge type — skip
            finally:
                unsub()

        if not unconfigured:
            return self.async_abort(reason="already_configured")

        _LOGGER.info("Zeroconf: found Sonicare bridge on ESP device '%s'", device_name)
        self._name = device_name.replace("_", "-")
        self.context["title_placeholders"] = {"name": f"ESP32 Bridge ({self._name})"}

        if len(bridge_ids) > 1:
            return await self.async_step_esp_select_device()
        self._esp_bridge_id = bridge_ids[0]
        return await self._esp_bridge_health_check()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle Bluetooth discovery."""
        _LOGGER.debug(
            "Flow started: Bluetooth discovery of %s (%s, %s dBm)",
            discovery_info.address, discovery_info.name or "unnamed",
            discovery_info.rssi,
        )
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_already_configured()

        self._discovery_info = discovery_info
        self._address = discovery_info.address
        self._name = discovery_info.name or "Philips Sonicare"

        if self._is_condor_rpa(discovery_info):
            # Tag the flow so siblings can find it, drop any that have gone
            # stale, and stamp the discovery time into the title so the user
            # can tell which entry is the freshest (RPAs rotate per wake).
            self.context["condor_rpa_address"] = discovery_info.address
            self._prune_stale_condor_flows()
            seen = dt_util.now().strftime("%H:%M:%S")
            self.context["title_placeholders"] = {
                "name": f"Philips Sonicare (Condor, seen {seen})"
            }
        else:
            # Snapshot at discovery time — the title is not refreshed.
            via = _via_label(
                describe_available_paths(self.hass, discovery_info.address)
            )
            label = _brush_label(self.hass, discovery_info)
            self.context["title_placeholders"] = {
                "name": f"{label} · {via}" if via else label
            }
        return await self.async_step_bluetooth_confirm()

    async def _find_esp_bridge_for_mac(
        self, target_mac: str
    ) -> tuple[str, str] | None:
        """Locate an ESP bridge slot that already has this MAC bonded.

        Returns (esp_device_name, bridge_id) when an ESP slot reports
        `mac` equal to ``target_mac``; otherwise None. We deliberately
        only match bonded slots — an empty pair-capable slot doesn't
        justify diverting a discovered brush away from Direct BLE.
        """
        target = target_mac.upper()
        esphome_entries = self.hass.config_entries.async_entries("esphome")
        for entry in esphome_entries:
            if self._esp_entry_unreachable(entry, target):
                continue
            device_name = entry.data.get("device_name")
            if not device_name:
                continue
            device_name = esphome_service_id(device_name)
            bridge_ids = self._detect_esp_bridge_ids(device_name)
            if not bridge_ids:
                continue
            sonicare = await self._probe_sonicare_bridges(device_name, bridge_ids)
            for bridge_id, info in sonicare:
                if info is None:
                    continue
                mac = info.get("mac", "").upper()
                if mac and mac == target:
                    return (device_name, bridge_id)
        return None

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm Bluetooth discovery."""
        # Progress re-invocations of a running probe land here first —
        # before the ESP auto-route, whose slot probes would add seconds
        # of latency to every re-entry.
        if (progress := self._ble_probe_progress("bluetooth_confirm")) is not None:
            return progress

        # Auto-route to ESP only when an ESP slot already has this MAC
        # bonded — otherwise fall through to Direct BLE confirm so the
        # user can pick the manual ESP path themselves if needed. Checked
        # once per flow: submits and re-renders after a failed probe must
        # not repeat the multi-second slot probes.
        if user_input is None and not self._esp_redirect_checked:
            self._esp_redirect_checked = True
            match = await self._find_esp_bridge_for_mac(self._address or "")
            if match:
                self._esp_device_name, self._esp_bridge_id = match
                self._esp_bridge_ids = self._detect_esp_bridge_ids(
                    self._esp_device_name
                )
                return await self._esp_bridge_health_check()

        if user_input is not None:
            self._bluetooth_adapter = user_input.get(
                CONF_BLUETOOTH_ADAPTER, BLUETOOTH_ADAPTER_AUTO
            )
            return self._start_ble_probe("bluetooth_confirm", self._address)

        # One-shot outcome from ble_probe_finish. errors["base"] does not
        # render on this schema-less confirmation step, so the outcome
        # picks a translated text block; only the <ha-alert> wrapper is
        # injected, because hassfest rejects HTML in translation values.
        #
        # A failure over a proxy keeps BOTH notices: a probe that fails on
        # a proxy-carried connection is the very symptom the proxy caveat
        # warns about, and every retry re-enters through this failure
        # branch — dropping the caveat here would hide it for the rest of
        # the flow, exactly when it matters most.
        outcome = self._confirm_status
        self._confirm_status = ""

        via, warning_variant, warning_values = self._transport_lines()
        text = await _async_text_blocks(self.hass)

        # Two independent slots: the probe failure and the proxy caveat
        # can appear together.
        alert = _alert("error", text.get(f"error.confirm_alert_{outcome}", ""))
        warn = ""
        if warning_variant:
            # After a failure the caveat is phrased for a retry, and the
            # "a local adapter also sees it" detail is dropped — that
            # advice belongs to the first attempt, not to a retry.
            caveat = text.get(
                "error.confirm_warn_proxy_retry" if outcome
                else f"error.confirm_warn_{warning_variant}",
                "",
            )
            # The caveat names the scanners, so it carries placeholders of
            # its own that no longer reach the frontend once it is a value.
            warn = _alert("warning", caveat.format(**warning_values))

        adapter_field = self._bluetooth_adapter_field(self._address or "")
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(adapter_field) if adapter_field else None,
            description_placeholders={
                "name": self._name,
                "address": self._address,
                "via": via,
                "alert": alert,
                "warn": warn,
            },
        )

    @staticmethod
    def _short_scanner(p: dict) -> str:
        # Scanner names carry the adapter MAC in parentheses; strip it —
        # the dialog cares about *which* device, not its MAC.
        return str(p["name"]).split(" (")[0]

    def _transport_lines(self) -> tuple[str, str, dict[str, str]]:
        """Return ``(via_suffix, warning_variant, warning_values)``.

        ``via_suffix`` names the likely carrier inline after "discovered
        at <mac>", mirroring the capabilities dialog's
        ``via <class> (<detail>)`` framing: "Direct Bluetooth" for a
        local adapter, "Bluetooth proxy" for a remote scanner. The
        transport classes stay untranslated on purpose — they are the
        product names used throughout the docs.

        ``warning_variant`` names the proxy-only caveat block ("" for a
        local carrier). Pairing over a standard proxy is model-dependent
        — some models never trigger the proxy-side bonding and fail every
        read; see not_paired_proxy. The wording lives in the translated
        blocks; only names, signal strengths and the markup that
        ha-markdown needs inside an HTML block are passed as values.

        habluetooth routes by signal strength, so the strongest scanner
        is only the *likely* carrier; recomputed each render so the
        ranking stays current.
        """
        paths = describe_available_paths(self.hass, self._address or "")
        empty = {
            "proxy_name": "", "proxy_rssi": "",
            "local_name": "", "local_rssi": "", "nl": "<br><br>",
        }
        if not paths:
            return "", "", empty

        def _rssi(p: dict) -> str:
            return f" ({p['rssi']} dBm)" if p["rssi"] is not None else ""

        best = paths[0]
        best_name = self._short_scanner(best)
        best_rssi = f", {best['rssi']} dBm" if best["rssi"] is not None else ""

        if best["is_local"]:
            return f" via **Direct Bluetooth** ({best_name}{best_rssi})", "", empty

        via = f" via **Bluetooth proxy** ({best_name}{best_rssi})"
        local = next((p for p in paths if p["is_local"]), None)
        values = {
            **empty,
            # Markdown is not parsed inside an HTML block, so emphasis
            # travels as markup in the value.
            "proxy_name": f"<b>{best_name}</b>",
            "proxy_rssi": _rssi(best),
        }
        if local is None:
            # The local adapter does not hear the brush right now, but it
            # may still be in the adapter field below — then point there
            # instead of the generic "use a local adapter" advice.
            listed = next(
                (
                    s for s in describe_connectable_scanners(
                        self.hass, self._address or ""
                    )
                    if s["is_local"]
                ),
                None,
            )
            if listed is None:
                return via, "proxy", values
            values["local_name"] = f"<b>{self._short_scanner(listed)}</b>"
            return via, "proxy_choose", values
        values["local_name"] = f"<b>{self._short_scanner(local)}</b>"
        values["local_rssi"] = _rssi(local)
        return via, "proxy_local", values

    # ------------------------------------------------------------------
    # Direct BLE probe as a progress task (shared by discovery + manual)
    # ------------------------------------------------------------------
    def _ble_probe_placeholders(self) -> dict[str, str]:
        return {"name": self._name or self._address or ""}

    def _ble_probe_progress(self, step_id: str) -> FlowResult | None:
        """Progress bookkeeping for a running direct-BLE probe.

        Returns None when no probe is in flight (the caller renders its
        form as usual), the progress view while the task runs, and the
        transition to ``ble_probe_finish`` once it is done.
        """
        task = self._ble_probe_task
        if task is None:
            return None
        if not task.done():
            return self.async_show_progress(
                step_id=step_id,
                progress_action="ble_probing",
                progress_task=task,
                description_placeholders=self._ble_probe_placeholders(),
            )
        self._ble_probe_result = task.result()
        self._ble_probe_task = None
        return self.async_show_progress_done(next_step_id="ble_probe_finish")

    def _start_ble_probe(self, step_id: str, address: str | None) -> FlowResult:
        """Kick off the capabilities probe as a background progress task."""
        self._ble_probe_origin = step_id
        self._ble_probe_task = self.hass.async_create_task(
            self._async_ble_probe(address or "")
        )
        return self.async_show_progress(
            step_id=step_id,
            progress_action="ble_probing",
            progress_task=self._ble_probe_task,
            description_placeholders=self._ble_probe_placeholders(),
        )

    async def _async_ble_probe(self, address: str) -> dict[str, Any]:
        """Run the capabilities probe (progress task) and box the outcome."""
        try:
            return {
                "ok": True,
                "data": await self._fetch_with_pair_retry(address),
            }
        except DeviceAsleepException:
            return {"ok": False, "error": "asleep"}
        except AdapterNotReceivingException:
            return {"ok": False, "error": "adapter"}
        except NotPairedException:
            return {"ok": False, "error": "not_paired"}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during capabilities fetch")
            return {"ok": False, "error": "unknown"}

    async def async_step_ble_probe_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Route the probe outcome captured by the progress step.

        Success continues to show_capabilities; failures go back to the
        origin step — rendered as errors[] on the manual form (it has a
        schema) and as an <ha-alert> on the schema-less discovery confirm.
        """
        result = self._ble_probe_result or {}
        self._ble_probe_result = None
        manual = self._ble_probe_origin == "user_bleak"

        error = result.get("error", "unknown")
        if result.get("ok"):
            data = result["data"]
            has_device_info = any(
                data.get(k) for k in ("model", "serial", "firmware", "battery")
            )
            if manual and "connection_path" not in data:
                # Refuse to create an entry if the GATT probe never
                # established a connection. ``connection_path`` is set by
                # ``_async_fetch_capabilities`` only after a live client
                # is in hand — its absence means we have no evidence the
                # device is reachable at this address (out of range,
                # rotated RPA, no slot). Creating an entry anyway would
                # leave the user with a permanently "Initializing"
                # device and no actionable feedback in the UI.
                error = "cannot_connect"
            elif has_device_info and self._has_sonicare_services(data):
                self._fetched_data = data
                self._transport_type = TRANSPORT_BLEAK
                await self._adopt_identity(data.get("identity"))
                _LOGGER.info(
                    "Read capabilities over direct BLE from %s: model %s, "
                    "firmware %s, %s Sonicare service(s)",
                    self._address, data.get("model") or "unknown",
                    data.get("firmware") or "unknown",
                    len(data.get("services", [])),
                )
                return await self.async_step_show_capabilities()
            elif manual:
                # Connect succeeded but the device didn't expose any
                # Sonicare service / DeviceInfo we could read. Don't
                # create an empty entry that would just sit in
                # "Initializing" forever — surface as an error so
                # the user can re-try with a different address.
                error = "not_a_sonicare"
            else:
                error = "cannot_connect"

        if error == "not_paired":
            return await self.async_step_not_paired()

        if manual:
            if error == "asleep":
                return self.async_abort(reason="device_asleep")
            if error == "adapter":
                error = "adapter_not_receiving"
            if error == "unknown":
                error = "cannot_connect"
            self._manual_error = error
            return await self.async_step_user_bleak()

        # Keep the discovery flow alive — an abort would dismiss the
        # discovery card, and ADV deduplication stops HA from re-creating
        # it when the brush wakes.
        # A step-variant key, not a sentence: bluetooth_confirm turns it
        # into the matching step so the wording is translatable.
        self._confirm_status = (
            error if error in ("asleep", "adapter") else "failed"
        )
        return await self.async_step_bluetooth_confirm()

    # ------------------------------------------------------------------
    # Manual flow — menu: choose connection type
    # ------------------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user — choose connection type."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["user_bleak", "esp_bridge"],
        )

    # ------------------------------------------------------------------
    # Direct BLE manual setup
    # ------------------------------------------------------------------
    async def async_step_user_bleak(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual MAC address entry for direct BLE."""
        # Progress re-invocations of a running probe land here.
        if (progress := self._ble_probe_progress("user_bleak")) is not None:
            return progress

        errors: dict[str, str] = {}
        # One-shot outcome from ble_probe_finish (this form has a schema,
        # so errors[] renders normally here).
        if self._manual_error:
            errors["base"] = self._manual_error
            self._manual_error = ""

        if user_input is not None:
            raw = user_input[CONF_ADDRESS]
            if raw == _MANUAL_ADDRESS:
                # User picked the "enter manually" sentinel — re-render the
                # step as a free-text field, keep prior errors empty.
                self._manual_address_entry = True
            else:
                address = raw.upper()
                await self.async_set_unique_id(address)
                self._abort_if_already_configured()

                self._address = address
                self._name = address
                self._bluetooth_adapter = user_input.get(
                    CONF_BLUETOOTH_ADAPTER, BLUETOOTH_ADAPTER_AUTO
                )
                return self._start_ble_probe("user_bleak", address)

        # Free-text entry path: nothing to discover, or user asked for it.
        if self._manual_address_entry:
            return self.async_show_form(
                step_id="user_bleak",
                data_schema=vol.Schema({
                    vol.Required(
                        CONF_ADDRESS,
                        default=self._address,
                    ) if self._address else vol.Required(CONF_ADDRESS): str,
                    **self._bluetooth_adapter_field(),
                }),
                errors=errors,
            )

        # Build the discovered-device picker. Each option label carries the
        # advertisement age and RSSI so RPA-rotating brushes (e.g. HX742X)
        # are picked from the freshest entry instead of a stale RPA whose
        # connect attempt would just time out.
        now_mono = time.monotonic()
        scored: list[tuple[int, SelectOptionDict]] = []
        for info in async_discovered_service_info(self.hass):
            name = info.name or ""
            if "sonicare" not in name.lower() and "philips ohc" not in name.lower():
                continue
            # -127 is BlueZ's "no longer received" marker, not a reading —
            # a brush that is out of range or asleep cannot be set up.
            if info.rssi is not None and info.rssi <= -127:
                continue
            age_s = max(0, int(now_mono - info.time)) if info.time else None
            rssi = info.rssi
            label_parts = [_brush_label(self.hass, info)]
            if age_s is not None:
                label_parts.append(f"{age_s}s ago")
            if rssi is not None:
                label_parts.append(f"{rssi} dBm")
            # Name the scanner that will likely carry the connect — the
            # step is titled "Direct Bluetooth", but habluetooth routes
            # by signal strength and may pick a bluetooth_proxy.
            via = _via_label(describe_available_paths(self.hass, info.address))
            if via:
                label_parts.append(via)
            label = label_parts[0] + (" — " + ", ".join(label_parts[1:]) if len(label_parts) > 1 else "")
            scored.append((
                age_s if age_s is not None else 9999,
                SelectOptionDict(value=info.address, label=label),
            ))

        if scored:
            scored.sort(key=lambda t: t[0])  # freshest first
            options: list[SelectOptionDict] = [opt for _, opt in scored]
            options.append(SelectOptionDict(
                value=_MANUAL_ADDRESS,
                label="Other — enter address manually",
            ))
            return self.async_show_form(
                step_id="user_bleak",
                data_schema=vol.Schema({
                    vol.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(options=options)
                    ),
                    **self._bluetooth_adapter_field(),
                }),
                errors=errors,
            )

        # No discoveries — fall back to free text.
        return self.async_show_form(
            step_id="user_bleak",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_ADDRESS,
                    default=self._address,
                ) if self._address else vol.Required(CONF_ADDRESS): str,
                **self._bluetooth_adapter_field(),
            }),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # ESP32 Bridge setup
    # ------------------------------------------------------------------
    async def async_step_esp_bridge(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle ESP32 bridge configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            esp_device_name = esphome_service_id(user_input["esp_device_name"].strip())

            bridge_ids = self._detect_esp_bridge_ids(esp_device_name)
            if not bridge_ids:
                _LOGGER.error("No philips_sonicare services found on %s", esp_device_name)
                errors["base"] = "cannot_connect"
            else:
                self._esp_device_name = esp_device_name
                self._esp_bridge_ids = bridge_ids

                if len(bridge_ids) > 1:
                    return await self.async_step_esp_select_device()

                self._esp_bridge_id = bridge_ids[0]
                return await self._esp_bridge_health_check()

        esp_options = await self._get_esphome_device_options()

        if not esp_options:
            return self.async_abort(reason="no_esphome_devices")

        if (
            len(esp_options) == 1
            and not user_input
            and esp_options[0]["value"] not in self._offline_esp_values
        ):
            sole = esp_options[0]["value"]
            self._esp_device_name = sole
            bridge_ids = self._detect_esp_bridge_ids(sole)
            self._esp_bridge_ids = bridge_ids
            if len(bridge_ids) > 1:
                return await self.async_step_esp_select_device()
            self._esp_bridge_id = bridge_ids[0] if bridge_ids else ""
            return await self._esp_bridge_health_check()

        data_schema = vol.Schema(
            {
                vol.Required("esp_device_name"): SelectSelector(
                    SelectSelectorConfig(options=esp_options)
                ),
            }
        )

        return self.async_show_form(
            step_id="esp_bridge",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_esp_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let user pick which device on a multi-device ESP bridge."""
        if user_input is not None:
            selected = user_input["esp_bridge_id"]
            if selected in self._configured_bridge_ids:
                return self.async_abort(reason="already_configured")
            self._esp_bridge_id = selected
            # Reuse the probe this picker was rendered from for the slot the
            # user just chose — scopes the cache to this one hop.
            self._seed_bridge_info_from_probe()
            return await self._esp_bridge_health_check()

        # Collect MACs already configured for this integration
        configured_macs = {
            entry.unique_id.upper()
            for entry in self._async_current_entries()
            if entry.unique_id
        }

        # Reuse probes collected during the device-list step if present;
        # otherwise probe now (e.g. discovery / bluetooth_confirm path).
        #
        # Only a *recent* probe may be reused. A flow can render this picker
        # long after it was created — a zeroconf discovery builds the whole
        # step in the background, and its banner may sit unopened for hours.
        # Reusing that probe would show the bridge state from whenever the
        # flow started: a slot unbonded since then still reads as bonded.
        # The reuse this cache exists for (dropdown -> picker, and picker ->
        # health check on submit) happens within seconds and stays intact.
        cached = self._probed_bridges.get(self._esp_device_name)
        probed_at = self._probed_at.get(self._esp_device_name, 0.0)
        age = time.monotonic() - probed_at
        # A probe that does not cover exactly this bridge's slots is not
        # usable either — the slot list changed under us.
        covers_slots = cached is not None and (
            {did for did, _ in cached} == set(self._esp_bridge_ids)
        )
        if cached is None or not covers_slots or age > _PROBE_CACHE_MAX_AGE:
            cached = await self._probe_sonicare_bridges(
                self._esp_device_name, self._esp_bridge_ids
            )
            self._probed_bridges[self._esp_device_name] = cached
            self._probed_at[self._esp_device_name] = time.monotonic()
        else:
            # Within the window, but a slot we un-bonded or paired since
            # the probe is known-stale — refresh just those, not the whole
            # bridge. Covers the case the age check cannot: the user acts
            # on one slot and re-opens the picker seconds later.
            stale = [
                did for did, _ in cached
                if slot_changed_at(self.hass, self._esp_device_name, did)
                > probed_at
            ]
            if stale:
                _LOGGER.debug(
                    "esp_select: re-probing %s — bond state changed since "
                    "the cached probe", ", ".join(stale)
                )
                fresh = dict(
                    await self._probe_sonicare_bridges(
                        self._esp_device_name, stale
                    )
                )
                cached = [
                    (did, fresh.get(did, info)) for did, info in cached
                ]
                self._probed_bridges[self._esp_device_name] = cached

        self._configured_bridge_ids = set()
        options: list[SelectOptionDict] = []
        has_available = False

        for did, info in cached:
            if info is None:
                # Did not answer on our event channel — offline, busy, or a
                # different component. Show it so a slot that needs
                # attention stays visible rather than vanishing from the
                # list; selecting it just probes again.
                options.append(
                    SelectOptionDict(value=did, label=f"⚪ {did or 'default'}")
                )
                has_available = True
                continue

            # Keyed by identity, or by bond address for entries from
            # before bridge firmware 1.15.0.
            is_configured = bool(
                {bridge_identity(info), bridge_bond_address(info)} - {""}
                & configured_macs
            )
            label = self._format_bridge_label(did, info)

            if is_configured:
                self._configured_bridge_ids.add(did)
                options.append(SelectOptionDict(value=did, label=f"✅ {label}"))
            else:
                has_available = True
                options.append(SelectOptionDict(value=did, label=label))

        if not any(info is not None for _, info in cached):
            # Nobody answered on our event channel — an offline bridge, or
            # an ESP that only looked like ours by service name. A list of
            # nothing but ⚪ entries would lead nowhere.
            return self.async_abort(reason="no_devices_found")
        if not has_available:
            return self.async_abort(reason="already_configured")

        # Auto-select if only one unconfigured device and no configured ones shown
        unconfigured = [
            o for o in options if o["value"] not in self._configured_bridge_ids
        ]
        if len(unconfigured) == 1 and len(options) == 1:
            self._esp_bridge_id = unconfigured[0]["value"]
            # Sole slot auto-selected — same picker probe, same one-hop reuse.
            self._seed_bridge_info_from_probe()
            return await self._esp_bridge_health_check()

        # Default to first unconfigured option so users don't have to deselect
        # the already-configured ✅ entry every time.
        default_value = unconfigured[0]["value"] if unconfigured else options[0]["value"]

        # The legend + pair hint live as static, translated text in this
        # step's description / data_description (see translations). They must
        # NOT be built here as dynamic placeholders: config-flow descriptions
        # render in the user's FRONTEND language, which a flow handler cannot
        # read (hass.config.language is the *server* language and can differ),
        # producing a mixed-language dialog. Static json keeps them in sync.
        return self.async_show_form(
            step_id="esp_select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("esp_bridge_id", default=default_value): SelectSelector(
                        SelectSelectorConfig(options=options)
                    ),
                }
            ),
        )

    @staticmethod
    def _format_bridge_label(bridge_id: str, info: dict[str, str]) -> str:
        """Human-readable label for a bridge entry in the picker."""
        friendly = (info.get("friendly_name") or "").strip()
        name = friendly or bridge_id or "default"
        if info.get("pair_capable") == "true":
            return name

        mac = info.get("mac", "")
        model = info.get("model", "")
        ble_name = info.get("ble_name", "")
        connected = info.get("ble_connected") == "true"
        paired = info.get("paired", "")

        icons: list[str] = []
        if paired == "true":
            icons.append("🔐")
        elif paired == "false":
            icons.append("🔓")
        icons.append("🟢" if connected else "⚪")

        descriptor = " / ".join(p for p in (model, ble_name) if p)
        body_parts = [name]
        if descriptor:
            body_parts.append(descriptor)
        if mac and mac != "00:00:00:00:00:00":
            body_parts.append(mac.upper())

        return f"{' '.join(icons)} {' — '.join(body_parts)}"

    async def _route_after_health_check(self) -> FlowResult:
        """Decide where a probed bridge slot goes next.

        A slot that is already bonded but has no config entry yet (a
        leftover bond, e.g. after removing an entry while the bridge was
        offline) gets a small menu: set it up as-is, or unpair it. Fresh
        pairings (``_just_paired``) and pair-capable/empty slots skip
        straight to the status step, which handles them.
        """
        info = self._bridge_info or {}
        if (
            info.get("paired") == "true"
            and info.get("pair_capable", "false") != "true"
            and not self._just_paired
            and not self._slot_action_chosen
        ):
            return await self.async_step_esp_slot_action()
        return await self.async_step_esp_bridge_status()

    def _seed_bridge_info_from_probe(self) -> None:
        """Seed ``_bridge_info`` from the picker's slot probe.

        The picker probes every slot via ble_get_info to build its option
        labels; for the slot the user just picked, that payload is exactly
        what the health check would otherwise fetch again. Seeding it here
        lets the immediately-following health check skip a redundant
        roundtrip (seconds on a busy bridge).

        Called ONLY from the picker-submit paths, so the reuse is scoped
        to the one hop picker -> health check. Every other entry into the
        health check (discovery auto-route, post-pair, post-unpair) leaves
        ``_bridge_info`` None and fetches fresh, because the slot's bonded
        state may have changed since the picker rendered.
        """
        cached = self._probed_bridges.get(self._esp_device_name or "")
        if not cached:
            return
        bridge_id = self._esp_bridge_id or ""
        for did, info in cached:
            if info is None:
                continue
            # Cached did is detection-form (lowercase); compare
            # case-insensitively, same as _resolve_friendly_name.
            if did.lower() != bridge_id.lower():
                continue
            self._bridge_info = {
                "version": info.get("version") or "?",
                "ble_connected": info.get("ble_connected", "false"),
                "mac": info.get("mac", ""),
                "paired": info.get("paired", ""),
                "mode": info.get("mode", "external"),
                "pair_capable": info.get("pair_capable", "false"),
                "pair_mode_active": info.get("pair_mode_active", "false"),
                BRIDGE_FIELD_IDENTITY_ADDRESS: info.get(BRIDGE_FIELD_IDENTITY_ADDRESS, ""),
                BRIDGE_FIELD_PEER_IDENTITY: info.get(BRIDGE_FIELD_PEER_IDENTITY, ""),
                "friendly_name": info.get("friendly_name", ""),
                "area": info.get("area", ""),
            }
            return

    async def _esp_bridge_health_check(self) -> FlowResult:
        """Run bridge health check and proceed to status step.

        ``_bridge_info`` is already populated when the picker seeded it
        (``_seed_bridge_info_from_probe``) or a prior step filled it;
        otherwise we fetch it live from the bridge here.
        """
        if self._bridge_info:
            return await self._route_after_health_check()

        transport = EspBridgeTransport(
            self.hass, "", self._esp_device_name, self._esp_bridge_id
        )
        try:
            await transport.connect()
            info = await transport.get_bridge_info()
            raw = info or {}
            self._bridge_info = {
                "version": raw.get("version") or transport.bridge_version or "?",
                "ble_connected": raw.get("ble_connected", str(transport.is_device_connected).lower()),
                "mac": raw.get("mac") or transport.detected_mac or "",
                "paired": transport.ble_paired or "",
                # Mode B pair-flow signals (absent on older bridges → defaults
                # keep the classic flow).
                "mode": raw.get("mode", "external"),
                "pair_capable": raw.get("pair_capable", "false"),
                "pair_mode_active": raw.get("pair_mode_active", "false"),
                BRIDGE_FIELD_IDENTITY_ADDRESS: raw.get(BRIDGE_FIELD_IDENTITY_ADDRESS, ""),
                # Bridge firmware >= 1.15.0; older bridges omit it.
                BRIDGE_FIELD_PEER_IDENTITY: raw.get(BRIDGE_FIELD_PEER_IDENTITY, ""),
                # YAML-supplied per-slot defaults (empty on older bridges →
                # HA falls back to the MAC-suffix default name and no area).
                "friendly_name": raw.get("friendly_name", ""),
                "area": raw.get("area", ""),
            }
        except TransportError:
            _LOGGER.error("ESP bridge not reachable: %s", self._esp_device_name)
            return self.async_show_form(
                step_id="esp_bridge",
                data_schema=vol.Schema({vol.Required("esp_device_name"): str}),
                errors={"base": "cannot_connect"},
            )
        except Exception:
            _LOGGER.exception("Unexpected error checking ESP bridge")
            return self.async_show_form(
                step_id="esp_bridge",
                data_schema=vol.Schema({vol.Required("esp_device_name"): str}),
                errors={"base": "unknown"},
            )
        finally:
            await transport.disconnect()

        return await self._route_after_health_check()

    async def async_step_esp_slot_action(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu for a slot that is bonded but not yet a config entry."""
        return self.async_show_menu(
            step_id="esp_slot_action",
            menu_options=["slot_setup", "slot_unpair"],
            description_placeholders=self._pair_target_placeholders(),
        )

    async def async_step_slot_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu choice: set up the already-bonded brush (read caps)."""
        self._slot_action_chosen = True
        return await self.async_step_esp_bridge_status()

    async def async_step_slot_unpair(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu choice: drop the slot's leftover bond."""
        self._slot_action_chosen = True
        return await self.async_step_reset_bridge()

    async def async_step_esp_bridge_status(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show ESP bridge status before reading toothbrush capabilities."""
        # Mode B with no bound brush → user must arm pair-mode first.
        info = self._bridge_info or {}
        if info.get("pair_capable", "false") == "true":
            return await self.async_step_request_pair()

        # A capabilities read is in flight (progress re-invocations land here).
        if self._esp_caps_task is not None:
            if not self._esp_caps_task.done():
                return self.async_show_progress(
                    step_id="esp_bridge_status",
                    progress_action="esp_reading",
                    progress_task=self._esp_caps_task,
                    description_placeholders=self._pair_target_placeholders(),
                )
            self._esp_caps_result = self._esp_caps_task.result()
            self._esp_caps_task = None
            return self.async_show_progress_done(next_step_id="esp_read_finish")

        # "Read capabilities" clicked → run the read as a background task.
        if user_input is not None:
            self._esp_caps_task = self.hass.async_create_task(self._async_esp_read())
            return self.async_show_progress(
                step_id="esp_bridge_status",
                progress_action="esp_reading",
                progress_task=self._esp_caps_task,
                description_placeholders=self._pair_target_placeholders(),
            )

        # Bridge status table. Only language-neutral values go in here
        # (state symbols, MAC, version) \u2014 the row labels and the symbol
        # legend are static text in strings.json so they follow the
        # user's frontend language, which a value built here cannot.
        info = self._bridge_info or {}
        ble_connected = info.get("ble_connected") == "true" if info else False
        _mac = info.get("mac", "")
        status_values = {
            "ble_state": "\u2705" if ble_connected else "\u274c",
            "security": {"true": "\U0001f510", "false": "\U0001f513"}.get(
                info.get("paired", ""), "\u2014"
            ),
            "mac": (
                _mac if _mac and _mac != "00:00:00:00:00:00" else "\u2014"
            ),
            "version": f"v{info.get('version', '?')}" if info else "\u2014",
        }
        target_placeholders = self._pair_target_placeholders()

        # One-shot outcomes (pairing done, read failed) ride in as text
        # blocks rather than as step variants: errors[] does not render on
        # this schema-less step — same quirk as reset_bridge — and the
        # status table around them is identical either way.
        # Both flags are consumed unconditionally: leaving one set because
        # the other won the race would resurface a stale notice on the
        # next render of this step.
        just_paired, self._just_paired = self._just_paired, False
        rpa_unsupported = self._paired_rpa_unsupported
        self._paired_rpa_unsupported = False
        read_error, self._esp_read_error = self._esp_read_error, ""

        text = await _async_text_blocks(self.hass)
        notice = ""
        alert_type = "success"
        if just_paired and rpa_unsupported:
            alert_type = "warning"
            notice = text.get("error.esp_status_paired_rpa_unsupported", "").replace(
                "{url}", ROTATING_ADDRESS_ISSUE_URL
            )
            action = text.get("error.esp_action_switch_on", "")
        elif just_paired:
            notice = text.get("error.esp_status_paired", "")
            action = text.get("error.esp_action_switch_on", "")
        elif read_error:
            alert_type = "error"
            notice = text.get(
                "error.esp_status_read_failed"
                if read_error == "cannot_connect"
                else "error.esp_status_read_error",
                "",
            )
            action = text.get("error.esp_action_retry", "")
        elif ble_connected:
            action = text.get("error.esp_action_connected", "")
        else:
            action = text.get("error.esp_action_switch_on", "")

        return self.async_show_form(
            step_id="esp_bridge_status",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._esp_device_name or "",
                "target": target_placeholders["target"],
                "alert": _alert(alert_type, notice),
                "action": action,
                **status_values,
                "identity_row": _identity_row(
                    text, info.get(BRIDGE_FIELD_PEER_IDENTITY, ""), _mac
                ),
            },
        )

    async def _async_esp_read(self) -> dict[str, Any]:
        """Read capabilities via the ESP bridge (runs as a progress task)."""
        try:
            caps = await self._async_fetch_capabilities_esp(
                "", self._esp_device_name, self._esp_bridge_id,
            )
            return {"ok": True, "caps": caps}
        except TransportError:
            _LOGGER.error(
                "ESP bridge: unable to read toothbrush capabilities via %s",
                self._esp_device_name,
            )
            return {"ok": False, "error": "cannot_connect"}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error reading toothbrush capabilities")
            return {"ok": False, "error": "unknown"}

    async def async_step_esp_read_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Process the capabilities read captured by esp_bridge_status."""
        result = self._esp_caps_result or {}
        self._esp_caps_result = None

        if not result.get("ok"):
            # A key, not a sentence: esp_bridge_status turns it into the
            # matching step so the wording comes from the translations.
            self._esp_read_error = (
                "cannot_connect"
                if result.get("error") == "cannot_connect"
                else "unknown"
            )
            return await self.async_step_esp_bridge_status()

        capabilities = result["caps"]

        # The entry is keyed by the brush's identity, which stays the same
        # across new pairings (bridge firmware 1.15.0; older bridges report
        # the bond address). The probe's sonicare_mac only backs it up for
        # bridges that report neither.
        info = self._bridge_info or {}
        sonicare_mac = (capabilities.get("sonicare_mac") or "").upper()
        if sonicare_mac == "00:00:00:00:00:00":
            sonicare_mac = ""
        canonical_addr = bridge_identity(info) or sonicare_mac
        self._bond_address = bridge_bond_address(info) or sonicare_mac

        if canonical_addr:
            await self.async_set_unique_id(canonical_addr, raise_on_progress=False)
        else:
            await self.async_set_unique_id(f"esp_{self._esp_device_name}")
        self._abort_if_esp_entry_configured()

        # Add pairing status from bridge info
        paired_str = (self._bridge_info or {}).get("paired", "")
        if paired_str == "true":
            capabilities["pairing"] = "bonded"
        elif paired_str == "false":
            capabilities["pairing"] = "open_gatt"

        # Carry YAML-supplied per-slot defaults through to show_capabilities.
        info = self._bridge_info or {}
        if info.get("friendly_name"):
            capabilities.setdefault("friendly_name", info["friendly_name"])
        if info.get("area"):
            capabilities.setdefault("area", info["area"])

        self._fetched_data = capabilities
        self._address = canonical_addr or None
        model = capabilities.get("model")
        self._name = model if model else self._esp_device_name
        self._transport_type = TRANSPORT_ESP_BRIDGE
        _LOGGER.info(
            "Read capabilities over %s (slot %s) from %s: model %s, "
            "firmware %s, %s Sonicare service(s)",
            self._esp_device_name, self._esp_bridge_id or "default",
            self._address or "unknown address",
            model or "unknown", capabilities.get("firmware") or "unknown",
            len(capabilities.get("services", [])),
        )

        return await self.async_step_show_capabilities()

    # ------------------------------------------------------------------
    # Pair-mode flow (Mode B bridges with no bound identity)
    # ------------------------------------------------------------------
    async def async_step_request_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask the user to confirm before arming pair-mode on the bridge."""
        if user_input is not None:
            return await self.async_step_wait_pair()

        # Acknowledge a bond that reset_bridge just cleared (one-shot), so
        # the jump from "Reset bridge bond" to pair-mode isn't silent.
        notice = ""
        if self._just_unpaired:
            self._just_unpaired = False
            notice = (await _async_text_blocks(self.hass)).get(
                "error.pair_status_unpaired", ""
            )
        return self._show_request_pair(notice)

    def _show_request_pair(
        self, notice: str = "", alert_type: str = "success"
    ) -> FlowResult:
        """Render the pair-mode prompt, optionally headed by a notice.

        Every outcome of the pairing attempt lands back on this one form.
        It carries no input fields, so ``errors[]`` would never reach the
        user — the notice has to travel as description markup instead.
        """
        return self.async_show_form(
            step_id="request_pair",
            data_schema=vol.Schema({}),
            description_placeholders={
                **self._pair_target_placeholders(),
                "alert": _alert(alert_type, notice),
            },
        )

    def _esp_target_label(
        self,
        esp_device_name: str | None = None,
        esp_bridge_id: str | None = None,
    ) -> str:
        """Human label for an ESP bridge slot: ``<node> / <slot>``.

        Leads with the ESP node name so a multi-bridge setup shows which
        bridge carries the connection, then the slot's YAML
        ``friendly_name`` (or the ``bridge_id`` when the slot is unnamed).
        Single-bridge nodes with no slot id collapse to just the node.
        Defaults to current flow state; mid-fetch callers pass explicit
        values.
        """
        device = (
            esp_device_name if esp_device_name is not None
            else self._esp_device_name
        ) or ""
        bridge_id = (
            esp_bridge_id if esp_bridge_id is not None
            else self._esp_bridge_id
        ) or ""
        slot = self._resolve_friendly_name(esp_device_name, esp_bridge_id) or bridge_id
        return f"{device} / {slot}" if slot else device

    def _pair_target_placeholders(self) -> dict[str, str]:
        """Placeholders identifying the bridge being paired/reset."""
        return {
            "device_name": self._esp_device_name or "",
            "bridge_id": self._esp_bridge_id or "",
            "target": self._esp_target_label(),
        }

    def _resolve_friendly_name(
        self,
        esp_device_name: str | None = None,
        esp_bridge_id: str | None = None,
    ) -> str:
        """Look up YAML ``friendly_name`` from probed bridge info for a slot.

        Defaults to current flow state; callers in mid-fetch paths can
        pass explicit values. Returns empty string when no probe data is
        cached or the bridge didn't expose a friendly_name.
        """
        device = (
            esp_device_name if esp_device_name is not None else self._esp_device_name
        )
        bridge_id = (
            esp_bridge_id if esp_bridge_id is not None else self._esp_bridge_id
        )
        if not device:
            return ""
        cached = self._probed_bridges.get(device)
        if not cached:
            return ""
        for did, info in cached:
            # Cached did is detection-form (lowercase); compare case-insensitively
            # so an uppercase bridge_id still resolves its friendly_name.
            if info is not None and did.lower() == bridge_id.lower():
                return (info.get("friendly_name") or "").strip()
        return ""

    async def async_step_wait_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Arm pair-mode and wait for the bond, showing live progress.

        Two ``async_show_progress`` phases so the dialog tells the user
        what is happening instead of freezing on a blank spinner for up
        to a minute: first *arming* pair-mode on the bridge, then
        *scanning/bonding*. Each phase runs as a background task; when it
        finishes HA re-invokes this step. The outcome lands in
        ``_pair_result`` and ``async_step_pair_finish`` renders it.
        """
        # Phase 1 — arm pair-mode on the bridge.
        if (
            self._pair_arm_task is None
            and self._pair_scan_task is None
            and self._pair_result is None
        ):
            self._pair_arm_task = self.hass.async_create_task(
                self._async_arm_pair_mode()
            )

        if self._pair_arm_task is not None:
            if not self._pair_arm_task.done():
                return self.async_show_progress(
                    step_id="wait_pair",
                    progress_action="pair_arming",
                    progress_task=self._pair_arm_task,
                    description_placeholders=self._pair_target_placeholders(),
                )
            armed = self._pair_arm_task.result()
            self._pair_arm_task = None
            if not armed:
                self._pair_result = {"error": "cannot_connect"}
                return self.async_show_progress_done(next_step_id="pair_finish")
            # Arming succeeded — kick off the scan/bond phase.
            self._pair_scan_task = self.hass.async_create_task(
                self._async_scan_and_bond()
            )

        # Phase 2 — wait for the bridge to bond (or time out).
        if self._pair_scan_task is not None:
            if not self._pair_scan_task.done():
                return self.async_show_progress(
                    step_id="wait_pair",
                    progress_action="pair_scanning",
                    progress_task=self._pair_scan_task,
                    description_placeholders=self._pair_target_placeholders(),
                )
            self._pair_result = self._pair_scan_task.result()
            self._pair_scan_task = None

        return self.async_show_progress_done(next_step_id="pair_finish")

    async def _async_arm_pair_mode(self) -> bool:
        """Register the status listener and arm pair-mode on the bridge.

        Returns True when the arm service call succeeded. The listener is
        registered *before* the service call so a fast pair_complete
        can't slip through; ``async_step_pair_finish`` tears it down.
        """
        timeout_s = 60
        bridge_id = self._esp_bridge_id or ""
        self._pair_future = self.hass.loop.create_future()

        @callback
        def _on_status(event: Event) -> None:
            data = event.data
            # bridge_id compared case-insensitively (HA lowercases service names)
            if data.get("bridge_id", "").lower() != bridge_id.lower():
                return
            if data.get("status") not in ("pair_complete", "pair_timeout"):
                return
            if self._pair_future is not None and not self._pair_future.done():
                self._pair_future.set_result(dict(data))

        self._pair_unsub = self.hass.bus.async_listen(
            "esphome.philips_sonicare_ble_status", _on_status
        )

        # Ask Home Assistant for an active scan window before the bridge
        # starts looking. Sonicare handles advertise their service UUID only
        # in the scan response, so a scanner left passive (the "Auto" default
        # since 2026.6) can never match and pair-mode expires unused. Started
        # before the arm call and not awaited — the request sleeps for the
        # whole window, which would delay arming the bridge.
        self._pair_active_scan_task = self.hass.async_create_task(
            self._async_hold_active_scan(self.hass.loop.time() + timeout_s)
        )

        svc_name = f"{self._esp_device_name}_ble_pair_mode"
        if bridge_id:
            svc_name += f"_{bridge_id}"
        self._pair_svc_name = svc_name
        try:
            await self.hass.services.async_call(
                "esphome",
                svc_name,
                {"enabled": True, "timeout_s": str(timeout_s)},
                blocking=True,
            )
        except Exception as err:
            _LOGGER.error("Failed to arm pair-mode on %s: %s",
                          self._esp_device_name, err)
            return False
        # Info, not debug: a pairing attempt is a user-initiated action that
        # can fail minutes later, and the log is what a report comes with.
        _LOGGER.info(
            "Pair-mode armed on %s (slot %s) for %ss — waiting for a toothbrush",
            self._esp_device_name, bridge_id or "default", timeout_s,
        )
        return True

    async def _async_hold_active_scan(self, deadline: float) -> None:
        """Keep AUTO scanners actively scanning until ``deadline``.

        ``async_request_active_scan`` flips every AUTO-mode scanner —
        including ESPHome proxies — to active for one window, then restores
        the previous mode itself; the request sleeps for that window. The
        scheduler clamps a window to 35 s, so one call cannot cover the 60 s
        pair window and we re-arm until the deadline passes.

        Best-effort throughout: HA < 2026.6 has no such API, a proxy that is
        mid-connect is skipped by the scheduler, and a scanner pinned to
        Passive never gets a window at all. None of that should keep
        pair-mode from running, so every failure path just returns.
        """
        request = getattr(ha_bluetooth, "async_request_active_scan", None)
        if request is None:
            _LOGGER.debug(
                "Active-scan windows need HA 2026.6+ — pairing relies on the "
                "scanner already being active"
            )
            return
        loop = self.hass.loop
        misses = 0
        while loop.time() < deadline:
            started = loop.time()
            try:
                await request(self.hass, _ACTIVE_SCAN_WINDOW)
            except Exception as err:  # noqa: BLE001 — never break pairing
                _LOGGER.debug("Active-scan request failed: %s", err)
                return
            if loop.time() - started >= 1.0:
                # The window really opened and we slept through it.
                misses = 0
                continue
            # Returned straight away: either no AUTO scanner exists at all,
            # or every one was mid-connect and got skipped. On a shared ESP
            # the proxy reconnects constantly, so a single miss says nothing
            # — keep trying until the pair window closes, just not in a tight
            # loop. Log once so a genuinely scanner-less setup is visible
            # without flooding the log with retries.
            misses += 1
            if misses == 1:
                _LOGGER.debug(
                    "No scanner opened an active window (every one busy or "
                    "none in Auto mode) — retrying while pair-mode runs"
                )
            await asyncio.sleep(_ACTIVE_SCAN_RETRY_DELAY)
        # Close the trace the first miss opened: "it kept trying and never got
        # one" and "it got one and the brush still didn't show" look identical
        # in the log otherwise.
        if misses:
            _LOGGER.debug(
                "Active-scan: no window opened in %s attempt(s) before "
                "pair-mode ended", misses,
            )

    async def _async_scan_and_bond(self) -> dict[str, str]:
        """Wait for pair_complete / pair_timeout from the bridge.

        Ticks the determinate progress bar along the bridge's 60 s pair
        window while waiting — the only feedback the user gets during a
        wait this long. ``shield`` keeps the per-tick ``wait_for`` from
        cancelling the shared future.
        """
        if self._pair_future is None:  # arming always sets it; defensive
            return {"error": "unknown"}
        timeout_s = 60
        loop = self.hass.loop
        start = loop.time()
        # Wait slightly longer than the bridge's own timeout so its
        # pair_timeout event can arrive before we give up.
        deadline = start + timeout_s + 5
        while True:
            now = loop.time()
            if now >= deadline:
                _LOGGER.warning("Pair-mode wait timed out (no event received)")
                return {"status": "pair_timeout"}
            self._bump_progress((now - start) / timeout_s)
            try:
                return await asyncio.wait_for(
                    asyncio.shield(self._pair_future),
                    timeout=min(2.0, deadline - now),
                )
            except asyncio.TimeoutError:
                continue

    async def async_step_pair_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Render the outcome captured by the wait_pair progress phases."""
        result = self._pair_result or {}
        self._pair_result = None

        # Tear down the status listener and, unless we cleanly bonded,
        # tell the bridge to stand down so a stray Sonicare in range
        # during its leftover window isn't auto-bonded (best-effort — the
        # bridge has its own timer).
        if self._pair_unsub is not None:
            self._pair_unsub()
            self._pair_unsub = None
        # The active-scan window outlives a pair that finished early; cancel
        # it so the scanner returns to its configured mode right away.
        if self._pair_active_scan_task is not None:
            self._pair_active_scan_task.cancel()
            self._pair_active_scan_task = None
        clean_complete = result.get("status") == "pair_complete"
        if not clean_complete and self._pair_svc_name:
            try:
                await self.hass.services.async_call(
                    "esphome", self._pair_svc_name,
                    {"enabled": False, "timeout_s": "0"},
                    blocking=False,
                )
            except Exception:
                _LOGGER.debug("Best-effort pair-mode cancel failed (ignoring)")
        self._pair_future = None
        self._pair_svc_name = ""

        # A failed attempt drops back onto the pair-mode prompt. The reason
        # rides in as an alert, not as errors[]: the prompt has no input
        # fields, so the frontend would drop errors[] silently and the user
        # would just see the same dialog again with nothing to act on.
        error_key = result.get("error") or (
            "pair_timeout" if result.get("status") == "pair_timeout" else ""
        )
        # The bridge reports whether it was scanning passively when the window
        # opened. If it was, waking the brush again cannot help — a passive
        # scan never sees the scan response carrying the Sonicare service UUID.
        # Bridges older than v1.11.0 omit the field, which reads as False and
        # keeps the generic wording.
        if error_key == "pair_timeout" and result.get("scanner_passive") == "true":
            error_key = "pair_timeout_passive_scanner"
        identity = bridge_identity(result)
        if not error_key and not identity:
            _LOGGER.error("pair_complete received without identity_address")
            error_key = "unknown"
        if error_key:
            # Close the trace the arm-time entry opened: without this the log
            # shows an attempt starting and nothing else, and the reason only
            # ever reaches the dialog the user already clicked away.
            _LOGGER.warning(
                "Pairing on %s (slot %s) did not succeed: %s",
                self._esp_device_name, self._esp_bridge_id or "default",
                error_key,
            )
            texts = await _async_text_blocks(self.hass)
            return self._show_request_pair(
                texts.get(f"error.{error_key}", ""), alert_type="error"
            )

        _LOGGER.info(
            "Pairing succeeded on %s (slot %s): toothbrush %s is now bonded",
            self._esp_device_name, self._esp_bridge_id or "default", identity,
        )

        # Pair succeeded — clear bridge_info so the next health check picks up
        # the freshly-bound state, then run capabilities probe via the
        # existing ESP-bridge path.
        self._bridge_info = None
        self._just_paired = True
        # Bridge firmware >= 1.14.2; older bridges omit both fields.
        self._paired_rpa_unsupported = (
            result.get("rpa") == "true" and result.get("addr_res") == "ctrl"
        )
        if self._paired_rpa_unsupported:
            _LOGGER.warning(
                "Toothbrush %s bonded under a rotating address on %s, which "
                "resolves addresses in the controller; it will not reconnect "
                "until the bridge is built with -DCONFIG_BT_BLE_RPA_SUPPORTED=0",
                identity, self._esp_device_name,
            )
        self._address = identity
        self._bond_address = bridge_bond_address(result)
        await self.async_set_unique_id(identity, raise_on_progress=False)
        self._abort_if_esp_entry_configured(just_paired=True)
        return await self._esp_bridge_health_check()

    async def async_step_reset_bridge(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm + execute unpair on a bound Mode B bridge.

        The unpair (service call + waiting for the bridge's ``unpaired``
        confirmation, ~4 s) runs as a background task behind an
        ``async_show_progress`` spinner; ``reset_finish`` renders the
        outcome.
        """
        # An unpair is in flight (progress re-invocations land here).
        if self._unpair_task is not None:
            if not self._unpair_task.done():
                return self.async_show_progress(
                    step_id="reset_bridge",
                    progress_action="unpairing",
                    progress_task=self._unpair_task,
                    description_placeholders=self._reset_bridge_placeholders(),
                )
            self._unpair_outcome = self._unpair_task.result()
            self._unpair_task = None
            return self.async_show_progress_done(next_step_id="reset_finish")

        if user_input is not None:
            self._unpair_task = self.hass.async_create_task(
                async_unpair_bridge_slot(
                    self.hass,
                    self._esp_device_name or "",
                    self._esp_bridge_id or "",
                )
            )
            return self.async_show_progress(
                step_id="reset_bridge",
                progress_action="unpairing",
                progress_task=self._unpair_task,
                description_placeholders=self._reset_bridge_placeholders(),
            )

        return self.async_show_form(
            step_id="reset_bridge",
            data_schema=vol.Schema({}),
            description_placeholders=self._reset_bridge_placeholders(),
        )

    async def async_step_reset_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Render the unpair outcome captured by reset_bridge."""
        outcome = self._unpair_outcome
        self._unpair_outcome = ""

        # Only proceed when the bridge confirmed the bond is gone. A silent
        # failure (call returned but no `unpaired` event) would otherwise
        # drop the user back onto the still-bonded status screen unexplained.
        if outcome == UNPAIR_OK:
            # pair_capable=true again — refetch info, then re-pair.
            # Clearing _bridge_info forces a fresh ble_get_info: the health
            # check only reuses the picker snapshot when a picker-submit
            # path seeded it (_seed_bridge_info_from_probe), and this is not
            # one, so pair_capable/paired reflect the just-cleared slot.
            # _just_unpaired drives the request_pair success notice.
            self._bridge_info = None
            self._just_unpaired = True
            return await self._esp_bridge_health_check()

        _LOGGER.error(
            "Unpair on %s did not succeed (%s)",
            self._esp_device_name, outcome,
        )
        # The wording comes from the translations; errors[] does not render
        # on this schema-less step.
        text = await _async_text_blocks(self.hass)
        notice = text.get(
            "error.reset_alert_offline"
            if outcome in (UNPAIR_FAILED, UNPAIR_UNAVAILABLE)
            else "error.reset_alert_unconfirmed",
            "",
        )
        return self.async_show_form(
            step_id="reset_bridge",
            data_schema=vol.Schema({}),
            description_placeholders=self._reset_bridge_placeholders(notice),
        )

    def _reset_bridge_placeholders(self, notice: str = "") -> dict[str, str]:
        """Placeholders for the reset_bridge step.

        ``errors["base"]`` does not render on this schema-less confirmation
        step (same as bluetooth_confirm), so a failure is surfaced as an
        ``<ha-alert>`` carrying a translated notice.
        """
        placeholders = self._pair_target_placeholders()
        placeholders["identity_address"] = (
            (self._bridge_info or {}).get(BRIDGE_FIELD_IDENTITY_ADDRESS, "")
        )
        placeholders["alert"] = _alert("error", notice)
        return placeholders

    # ------------------------------------------------------------------
    # Capabilities dialog (shared by BLE and ESP)
    # ------------------------------------------------------------------
    def _build_default_name(self) -> str:
        """Generate a unique-by-default device name for the new entry.

        Priority for the disambiguating suffix:
          1. YAML `friendly_name:` — wins outright when set (returns it
             verbatim, no model/suffix wrapping).
          2. ESP `bridge_id` — the human label the user already chose for
             this slot (e.g. "prestige"). Preferred over MAC because it
             carries meaning.
          3. Last-4 of MAC — fallback for Direct BLE, or ESP installs
             with no bridge_id set.
        """
        if self._fetched_data:
            yaml_name = (self._fetched_data.get("friendly_name") or "").strip()
            if yaml_name:
                return yaml_name
        model = self._fetched_data.get("model") if self._fetched_data else None
        if self._esp_bridge_id:
            suffix = self._esp_bridge_id
        elif self._address:
            suffix = self._address.replace(":", "")[-4:].upper()
        else:
            suffix = ""
        base = f"Sonicare {model}" if model else "Sonicare"
        return f"{base} ({suffix})" if suffix else base

    async def async_step_show_capabilities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show detected device info and services, then create entry."""
        default_name = self._build_default_name()

        if user_input is not None:
            services = self._fetched_data.get("services", [])
            device_name = (user_input.get(CONF_DEVICE_NAME) or "").strip() or default_name

            entry_data: dict[str, Any] = {
                CONF_SERVICES: services,
                "model": self._fetched_data.get("model") or "",
                CONF_DEVICE_NAME: device_name,
            }

            area = (self._fetched_data.get("area") or "").strip()
            if area:
                entry_data[CONF_AREA] = area

            if self._transport_type == TRANSPORT_ESP_BRIDGE:
                entry_data[CONF_TRANSPORT_TYPE] = TRANSPORT_ESP_BRIDGE
                entry_data[CONF_ESP_DEVICE_NAME] = self._esp_device_name
                if self._esp_bridge_id:
                    entry_data[CONF_ESP_BRIDGE_ID] = self._esp_bridge_id
                if self._address:
                    entry_data[CONF_ADDRESS] = self._address
                    if self._bond_address and self._bond_address != self._address:
                        entry_data[CONF_BOND_ADDRESS] = self._bond_address
            else:
                entry_data[CONF_ADDRESS] = self._address
                entry_data[CONF_TRANSPORT_TYPE] = TRANSPORT_BLEAK
                if self._bond_address and self._bond_address != self._address:
                    entry_data[CONF_BOND_ADDRESS] = self._bond_address

            entry_options: dict[str, Any] = {}
            if (
                self._transport_type != TRANSPORT_ESP_BRIDGE
                and self._bluetooth_adapter != BLUETOOTH_ADAPTER_AUTO
            ):
                entry_options[CONF_BLUETOOTH_ADAPTER] = self._bluetooth_adapter

            return self.async_create_entry(
                title=f"Philips Sonicare ({device_name})",
                data=entry_data,
                options=entry_options,
            )

        services_text, condor_note, has_missing = self._get_service_status_text(
            self._fetched_data.get("services", []),
            self._fetched_data.get("model") or "",
        )

        path = self._fetched_data.get("connection_path")

        text = await _async_text_blocks(self.hass)

        return self.async_show_form(
            step_id="show_capabilities",
            data_schema=vol.Schema({
                vol.Required(CONF_DEVICE_NAME, default=default_name): str,
            }),
            description_placeholders={
                # Legend and Condor note are translated blocks; the table
                # itself carries no sentence. Without ❌ rows there is
                # nothing for the legend to explain.
                "services_note": " ".join(
                    note
                    for key, wanted in (
                        ("error.caps_missing_legend", has_missing),
                        ("error.caps_condor_note", condor_note),
                    )
                    if wanted and (note := text.get(key, ""))
                ),
                # The title already says "Philips Sonicare"; a brush that
                # advertises just that name is told apart by its model.
                "name": (
                    str(self._fetched_data.get("model") or self._name)
                    if str(self._name).strip().lower() == "philips sonicare"
                    else str(self._name)
                ),
                **self._connection_status_placeholders(
                    self._transport_type, path,
                    via_proxy=bool(self._probe_via_proxy),
                ),
                **self._get_device_info_values(self._fetched_data, self._address),
                "identity_row": _identity_row(
                    text,
                    (self._bridge_info or {}).get(BRIDGE_FIELD_PEER_IDENTITY, "")
                    if self._transport_type == TRANSPORT_ESP_BRIDGE
                    else "",
                    self._address or "",
                ),
                "services": services_text,
            },
        )

    def _bluetooth_adapter_field(self, address: str = "") -> dict[Any, Any]:
        """Adapter field for direct BLE setup, or nothing.

        Asked before the first connect, so the probe and its pairing run
        through the chosen adapter. Without an address (manual entry) the
        list has no signal strengths. With a single connectable scanner
        there is nothing to choose.
        """
        # The current choice stays in the list even when it does not hear
        # the brush right now — that is exactly the "adapter" error case,
        # and a default missing from the options fails schema validation.
        options = _bluetooth_adapter_options(
            self.hass, address, self._bluetooth_adapter
        )
        if len(options) < 3 and self._bluetooth_adapter == BLUETOOTH_ADAPTER_AUTO:
            return {}
        return {
            vol.Required(
                CONF_BLUETOOTH_ADAPTER, default=self._bluetooth_adapter
            ): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    translation_key=CONF_BLUETOOTH_ADAPTER,
                )
            )
        }

    # ------------------------------------------------------------------
    # Pairing fallback (manual instructions)
    # ------------------------------------------------------------------
    def _not_paired_placeholders(self) -> dict[str, str]:
        """Description placeholders for the not_paired step.

        The pairing script ships inside the integration, so its /config
        path is stable; passing the brush address makes it pair that
        exact device without the interactive picker.
        """
        pair_cmd = (
            "bash /config/custom_components/philips_sonicare_ble/"
            f"scripts/pair.sh {self._address or ''}"
        ).strip()
        pair_error = (
            f"**Last attempt:** {self._pair_error}\n\n" if self._pair_error else ""
        )
        return {
            "address": self._address or "",
            "pair_cmd": pair_cmd,
            "pair_error": pair_error,
        }

    async def _show_not_paired_form(self, errors: dict[str, str]) -> FlowResult:
        """Render the pairing dialog matching the probe transport.

        The host variant walks the user through pair.sh/bluetoothctl on
        the HA host; the proxy variant explains that the proxy bonds on
        its own during reads and host tools have no effect. habluetooth
        routes each connect by RSSI, so the transport is re-evaluated on
        every retry and the dialog follows it.

        Where to find a shell differs by install type; that sentence is
        a translated block, so it follows the dialog's language like the
        walkthrough around it.

        Neither variant has input fields, so a retry failure cannot ride
        in ``errors`` — the frontend only renders those inside ``ha-form``
        and skips it on an empty schema. The reason is therefore shown as
        an alert, reusing the very ``config.error`` strings that would
        otherwise never reach anyone.
        """
        notice = ""
        if key := errors.get("base"):
            texts = await _async_text_blocks(self.hass)
            notice = texts.get(f"error.{key}", "")
        alert = _alert("error", notice)

        if self._probe_via_proxy:
            return self.async_show_form(
                step_id="not_paired_proxy",
                description_placeholders={
                    "address": self._address or "",
                    "proxy_name": self._probe_proxy_name or "—",
                    "alert": alert,
                },
            )
        text = await _async_text_blocks(self.hass)
        return self.async_show_form(
            step_id="not_paired",
            description_placeholders={
                **self._not_paired_placeholders(),
                # Where to find a shell differs by install type; the rest
                # of the walkthrough is identical.
                "terminal": text.get(
                    "error.not_paired_terminal_hassio" if _is_hassio(self.hass)
                    else "error.not_paired_terminal_host",
                    "",
                ),
                "alert": alert,
            },
        )

    async def async_step_not_paired(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show pairing instructions when auto-pairing failed.

        Handles both dialog variants: Retry probes again either way (on
        the proxy path the probe read itself re-triggers the ESP-side
        SMP), and ``_show_not_paired_form`` picks the variant for the
        transport that carried the probe.
        """
        if user_input is not None:
            # User clicked retry after manual pairing
            errors: dict[str, str] = {}
            try:
                self._fetched_data = await self._async_fetch_capabilities(
                    self._address
                )
                has_device_info = any(
                    self._fetched_data.get(k)
                    for k in ("model", "serial", "firmware", "battery")
                )
                if has_device_info and self._has_sonicare_services(
                    self._fetched_data
                ):
                    self._transport_type = TRANSPORT_BLEAK
                    return await self.async_step_show_capabilities()
                errors["base"] = "cannot_connect"
            except DeviceAsleepException:
                return self.async_abort(reason="device_asleep")
            except AdapterNotReceivingException:
                errors["base"] = "adapter_not_receiving"
            except NotPairedException:
                errors["base"] = "pairing_failed"
            except Exception:
                _LOGGER.exception("Error after manual pairing retry")
                errors["base"] = "unknown"

            return await self._show_not_paired_form(errors)

        return await self._show_not_paired_form({})

    async def async_step_not_paired_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Proxy variant of not_paired — same handler, different text."""
        return await self.async_step_not_paired(user_input)

    # ------------------------------------------------------------------
    # Options flow
    # ------------------------------------------------------------------
    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PhilipsSonicareOptionsFlow:
        return PhilipsSonicareOptionsFlow()


# Before pairing on a new adapter, wait this long for the brush to show up
# there. After a disconnect the brush often stays silent for minutes until
# its power button is pressed — pairing blindly would only time out.
PAIR_ADVERTISEMENT_WAIT = 45  # seconds
# Below this the target adapter receives the brush too weakly to pair
# reliably (29./30.10.: -96 dBm timed out, -74 dBm paired in 4 s).
PAIR_WEAK_RSSI = -90
# _pair_error value for "no advertisement on the new adapter".
_PAIR_NO_ADVERTISEMENT = "no_advertisement"


class PhilipsSonicareOptionsFlow(OptionsFlowWithReload):
    """Options flow for Philips Sonicare BLE."""

    # Re-pairing on a newly chosen local adapter (bonds are per adapter).
    _pending_data: dict[str, Any] | None = None
    _pair_task: asyncio.Task | None = None
    _pair_error: str = ""
    _pair_adapter_name: str = ""
    _paired_on: str = ""
    # A pairing attempt already ran in this dialog; BlueZ may have
    # finished it after our timeout.
    _pair_attempted: bool = False
    # Set while the entry is unloaded for pairing; whoever ends the flow
    # must bring it back.
    _reload_pending: bool = False

    def _scanner_kinds(self) -> dict[str, tuple[str, bool]]:
        """Connectable scanners by upper-case source: (short name, is_local)."""
        address = self.config_entry.data.get(CONF_ADDRESS) or ""
        return {
            str(sc["source"]).upper(): (self._short_name(sc["name"]), sc["is_local"])
            for sc in describe_connectable_scanners(self.hass, address)
        }

    def _adapter_name(self, adapter: str) -> str:
        if adapter == BLUETOOTH_ADAPTER_AUTO:
            return "Chosen by Home Assistant"
        return self._scanner_kinds().get(adapter.upper(), (adapter, False))[0]

    async def _pairing_state(self, adapter: str) -> str:
        """What switching to ``adapter`` means for pairing.

        ``needed`` only for a local adapter without a bond, and only for a
        brush that is bonded at all: one paired on another local adapter,
        or a Condor, which always needs a bond. Otherwise the reason there
        is nothing to do: ``auto``, ``proxy`` (pairs by itself),
        ``already``, ``not_needed`` (readable without a bond) or
        ``unknown`` (adapter offline, or BlueZ cannot be asked).
        """
        if adapter == BLUETOOTH_ADAPTER_AUTO:
            return "auto"
        kinds = self._scanner_kinds()
        kind = kinds.get(adapter.upper())
        if kind is None:
            return "unknown"
        if not kind[1]:
            return "proxy"

        from .dbus_pairing import async_paired_adapters, is_dbus_available

        if not is_dbus_available():
            return "unknown"
        address = self.config_entry.data.get(CONF_ADDRESS) or ""
        paired = await async_paired_adapters(address)
        if paired is None:
            return "unknown"
        if adapter.upper() in paired:
            return "already"
        services = [
            str(u).lower() for u in self.config_entry.data.get(CONF_SERVICES, [])
        ]
        if not paired and SVC_CONDOR.lower() not in services:
            return "not_needed"
        self._pair_adapter_name = kind[0]
        self._paired_on = (
            ", ".join(kinds.get(a, (a, True))[0] for a in paired) or "—"
        )
        return "needed"

    @staticmethod
    def _short_name(name: object) -> str:
        # "hci0 (00:0A:CD:46:B2:2D)" → "hci0"; the MAC adds nothing here.
        return str(name).split(" (")[0]

    def _pair_placeholders(self) -> dict[str, str]:
        return {
            "adapter": self._pair_adapter_name,
            "paired_on": self._paired_on,
        }

    async def async_step_pair_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pair the brush on the newly chosen adapter before saving."""
        task = self._pair_task
        if task is not None:
            if not task.done():
                return self.async_show_progress(
                    step_id="pair_adapter",
                    progress_action="pair_adapter",
                    progress_task=task,
                    description_placeholders=self._pair_placeholders(),
                )
            self._pair_error = task.result()
            self._pair_task = None
            return self.async_show_progress_done(
                next_step_id="pair_adapter_finish"
            )

        if user_input is not None:
            self._pair_task = self.hass.async_create_task(
                self._async_pair_on_adapter()
            )
            return self.async_show_progress(
                step_id="pair_adapter",
                progress_action="pair_adapter",
                progress_task=self._pair_task,
                description_placeholders=self._pair_placeholders(),
            )

        # A form without fields cannot show errors[] — the failure goes into
        # the description as a notice instead.
        text = await _async_text_blocks(self.hass, "options")
        # Markdown is not parsed inside the alert's HTML block.
        adapter_html = f"<b>{html.escape(self._pair_adapter_name)}</b>"
        alert = ""
        if self._pair_error == _PAIR_NO_ADVERTISEMENT:
            alert = _alert(
                "error",
                text.get("error.pair_adapter_no_advertisement", "")
                .replace("{adapter}", adapter_html)
                .replace("{seconds}", str(PAIR_ADVERTISEMENT_WAIT)),
            )
        elif self._pair_error:
            alert = _alert(
                "error",
                text.get("error.pair_adapter_failed", "")
                .replace("{adapter}", adapter_html)
                .replace("{error}", html.escape(self._pair_error)),
            )
        assert self._pending_data is not None
        rssi = scanner_rssi(
            self.hass,
            self.config_entry.data.get(CONF_ADDRESS) or "",
            self._pending_data[CONF_BLUETOOTH_ADAPTER],
        )
        if rssi is not None and rssi < PAIR_WEAK_RSSI:
            alert += _alert(
                "warning",
                text.get("error.pair_adapter_weak", "")
                .replace("{adapter}", adapter_html)
                .replace("{rssi}", str(rssi)),
            )
        return self.async_show_form(
            step_id="pair_adapter",
            data_schema=vol.Schema({}),
            description_placeholders={**self._pair_placeholders(), "alert": alert},
        )

    async def _async_pair_on_adapter(self) -> str:
        """Pause the entry and pair on the chosen adapter; '' on success.

        The running entry would otherwise keep (re)connecting — over the
        old adapter, or failing over the new one — and race BlueZ's
        Pair(). Unloading reuses the normal teardown.
        """
        from .dbus_pairing import PairingError, async_pair_and_trust

        assert self._pending_data is not None
        entry = self.config_entry
        address = entry.data.get(CONF_ADDRESS) or ""
        adapter = self._pending_data[CONF_BLUETOOTH_ADAPTER]
        retry = self._pair_attempted
        self._pair_attempted = True
        # Watch from before the disconnect, so the first advertisement
        # after it is not missed.
        try:
            heard, stop_watching = watch_advertisements(address, adapter)
        except LookupError as err:
            _LOGGER.debug(
                "%s: cannot watch advertisements per adapter (%s); pairing "
                "without waiting", address, err,
            )
            heard, stop_watching = None, None
        self._reload_pending = True
        try:
            await self.hass.config_entries.async_unload(entry.entry_id)
            if retry and await self._async_bonded_on(address, adapter):
                # The earlier attempt timed out on our side, but BlueZ
                # finished it later. The bond is the one we asked for —
                # removing it as "stale" would throw away a good pairing.
                _LOGGER.info(
                    "%s: already paired on %s by the previous attempt",
                    address, self._pair_adapter_name,
                )
                return ""
            if heard is not None and not await self._async_wait_for_advertisement(
                address, heard
            ):
                _LOGGER.warning(
                    "%s: %s did not receive the brush within %ss",
                    address, self._pair_adapter_name, PAIR_ADVERTISEMENT_WAIT,
                )
                return _PAIR_NO_ADVERTISEMENT
            _LOGGER.info(
                "%s: pairing on Bluetooth adapter %s (%s, %s dBm)",
                address, self._pair_adapter_name, adapter,
                scanner_rssi(self.hass, address, adapter),
            )
            await async_pair_and_trust(address, adapter)
            await asyncio.sleep(2)  # let BlueZ key distribution settle
            return ""
        except PairingError as err:
            _LOGGER.warning(
                "%s: pairing on %s failed: %s",
                address, self._pair_adapter_name, err,
            )
            return str(err)
        except Exception as err:  # noqa: BLE001 — shown in the dialog
            _LOGGER.exception("%s: pairing on %s failed", address, adapter)
            return str(err) or type(err).__name__
        finally:
            if stop_watching is not None:
                stop_watching()

    @staticmethod
    async def _async_bonded_on(address: str, adapter: str) -> bool:
        from .dbus_pairing import async_paired_adapters

        paired = await async_paired_adapters(address)
        return bool(paired) and adapter.upper() in paired

    async def _async_wait_for_advertisement(
        self, address: str, heard: asyncio.Event
    ) -> bool:
        """Wait until the new adapter receives an advertisement of the brush."""
        if heard.is_set():
            return True
        _LOGGER.debug(
            "%s: waiting up to %ss for an advertisement on %s",
            address, PAIR_ADVERTISEMENT_WAIT, self._pair_adapter_name,
        )
        try:
            await asyncio.wait_for(heard.wait(), PAIR_ADVERTISEMENT_WAIT)
        except TimeoutError:
            return False
        _LOGGER.debug(
            "%s: advertisement received on %s", address, self._pair_adapter_name
        )
        return True

    async def async_step_pair_adapter_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if self._pair_error:
            # Back to the form: Submit retries, closing keeps the old options.
            return await self.async_step_pair_adapter()
        return await self._async_finish_adapter_change("paired")

    async def _async_finish_adapter_change(self, pairing: str) -> FlowResult:
        """Drop the other bonds, save, and end with what was done."""
        assert self._pending_data is not None
        adapter = self._pending_data[CONF_BLUETOOTH_ADAPTER]
        dropped, failed, kept = await self._async_drop_other_bonds(adapter)
        name = self._adapter_name(adapter)
        rows: list[tuple[str, dict[str, str]]] = [
            ("adapter_auto" if adapter == BLUETOOTH_ADAPTER_AUTO else "adapter",
             {"adapter": name}),
            (f"pair_{pairing}", {"adapter": name}),
        ]
        # One row per adapter: each line starts with what it is about.
        rows += [("drop_done", {"adapter": other}) for other in dropped]
        rows += [("drop_failed", {"adapter": other}) for other in failed]
        if not dropped and not failed:
            rows.append((f"drop_{kept}", {}))
        rows.append(("reload", {}))

        text = await _async_text_blocks(self.hass, "options")
        lines = []
        for key, values in rows:
            line = text.get(f"error.summary_{key}", "")
            for placeholder, value in values.items():
                line = line.replace("{" + placeholder + "}", value)
            if line:
                lines.append(f"- {line}")

        # The options dialog's own success page cannot carry text of ours,
        # but its abort page can: save here and end with the checklist.
        data = self._pending_data
        self._pending_data = None
        _LOGGER.info(
            "Options saved for %s: %s",
            self.config_entry.title,
            ", ".join(f"{k}={v}" for k, v in sorted(data.items())),
        )
        self.hass.config_entries.async_update_entry(
            self.config_entry, options=data
        )
        # No create_entry, so no automatic reload either.
        self._reload_pending = False
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return self.async_abort(
            reason="adapter_changed",
            description_placeholders={"summary": "\n".join(lines)},
        )

    async def _async_drop_other_bonds(
        self, adapter: str
    ) -> tuple[list[str], list[str], str]:
        """Switching to a fixed local adapter removes the other local bonds.

        A fixed adapter means one bond. A bond left behind on an adapter
        the entry no longer uses can go stale on the brush side while
        BlueZ still reports it — switching back would then skip the
        pairing step and fail. Removing it makes the way back pair again.

        Runs only after the new adapter is paired (or already was). Not
        for Automatic, which may use any adapter, and not for a proxy:
        whether a brush needs a bond at all is only known from a bond
        somewhere, so dropping every local one would hide that.

        Returns (removed, failed, reason) — adapter names, and when nothing
        was removed why: ``auto``, ``proxy``, ``unknown`` or ``none``.
        """
        if adapter == BLUETOOTH_ADAPTER_AUTO:
            return [], [], "auto"
        address = self.config_entry.data.get(CONF_ADDRESS) or ""
        kinds = self._scanner_kinds()
        kind = kinds.get(adapter.upper())
        if kind is None:
            return [], [], "unknown"
        if not kind[1]:
            return [], [], "proxy"

        from .dbus_pairing import (
            async_paired_adapters,
            async_remove_device,
            is_dbus_available,
        )

        if not is_dbus_available():
            return [], [], "unknown"
        paired = await async_paired_adapters(address)
        if paired is None:
            return [], [], "unknown"
        others = [a for a in paired if a != adapter.upper()]
        if not others:
            return [], [], "none"
        if not self._reload_pending:
            # The running entry may be connected through one of them.
            self._reload_pending = True
            await self.hass.config_entries.async_unload(
                self.config_entry.entry_id
            )
        removed: list[str] = []
        failed: list[str] = []
        for other in others:
            other_name = kinds.get(other, (other, True))[0]
            if await async_remove_device(address, other):
                _LOGGER.info(
                    "%s: removed the bond on adapter %s (now fixed to %s)",
                    address, other, adapter,
                )
                removed.append(other_name)
            else:
                _LOGGER.warning(
                    "%s: could not remove the bond on adapter %s", address, other
                )
                failed.append(other_name)
        return removed, failed, "none"

    def _save_options(self, data: dict[str, Any]) -> FlowResult:
        # Info: which options a device runs with explains a lot of later
        # behaviour (disabled sensors, throttling), and nothing here is
        # sensitive enough to keep out of the log.
        _LOGGER.info(
            "Options saved for %s: %s",
            self.config_entry.title,
            ", ".join(f"{k}={v}" for k, v in sorted(data.items())),
        )
        if (
            self._reload_pending
            and getattr(self, "automatic_reload", False)
            and data != dict(self.config_entry.options)
        ):
            # HA reloads by itself once it has stored the changed options.
            self._reload_pending = False
        # Otherwise (options unchanged, or a core without
        # OptionsFlowWithReload) async_remove reloads: HA calls it after
        # storing the options, so the entry comes back with the new ones.
        return self.async_create_entry(title="", data=data)

    @callback
    def async_remove(self) -> None:
        """Dialog closed: never leave the entry unloaded."""
        if self._pair_task is not None and not self._pair_task.done():
            self._pair_task.cancel()
        if self._reload_pending:
            self._reload_pending = False
            self.hass.config_entries.async_schedule_reload(
                self.config_entry.entry_id
            )

    def _is_esp(self) -> bool:
        return (
            self.config_entry.data.get(CONF_TRANSPORT_TYPE) == TRANSPORT_ESP_BRIDGE
        )

    def _current_options(self) -> dict[str, Any]:
        """Stored options with defaults filled in — the base for any save."""
        options = self.config_entry.options
        data: dict[str, Any] = {
            CONF_SENSOR_PRESSURE: options.get(CONF_SENSOR_PRESSURE, DEFAULT_SENSOR_PRESSURE),
            CONF_SENSOR_TEMPERATURE: options.get(CONF_SENSOR_TEMPERATURE, DEFAULT_SENSOR_TEMPERATURE),
            CONF_SENSOR_GYROSCOPE: options.get(CONF_SENSOR_GYROSCOPE, DEFAULT_SENSOR_GYROSCOPE),
            CONF_WARN_COUNTERFEIT: options.get(CONF_WARN_COUNTERFEIT, DEFAULT_WARN_COUNTERFEIT),
        }
        if self._is_esp():
            for key in (CONF_NOTIFY_THROTTLE, CONF_PIPELINED_READS):
                if key in options:
                    data[key] = options[key]
        else:
            data[CONF_BLUETOOTH_ADAPTER] = options.get(
                CONF_BLUETOOTH_ADAPTER, BLUETOOTH_ADAPTER_AUTO
            )
        return data

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Direct BLE: settings or moving the brush to another adapter.

        Switching the adapter is an action with its own steps (pairing,
        removing old bonds), not one more option — so it gets its own
        entry. An ESP bridge entry has no adapter and goes straight to
        the settings, and so does an entry with a single adapter to
        choose from — unless it is already fixed to one, so the way back
        to automatic choice stays open.
        """
        if self._is_esp() or not self._adapter_choice_available():
            return await self.async_step_settings(user_input)
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "adapter"]
        )

    def _adapter_choice_available(self) -> bool:
        """Whether moving the brush to another adapter is possible at all.

        Same rule as the adapter field during setup: "auto" plus a single
        connectable scanner leaves nothing to choose.
        """
        current = self.config_entry.options.get(
            CONF_BLUETOOTH_ADAPTER, BLUETOOTH_ADAPTER_AUTO
        )
        if current != BLUETOOTH_ADAPTER_AUTO:
            return True
        options = _bluetooth_adapter_options(
            self.hass, self.config_entry.data.get(CONF_ADDRESS) or "", current
        )
        return len(options) >= 3

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        is_esp = self._is_esp()

        if user_input is not None:
            data = self._current_options()
            data.update({
                CONF_SENSOR_PRESSURE: user_input.get(CONF_SENSOR_PRESSURE, DEFAULT_SENSOR_PRESSURE),
                CONF_SENSOR_TEMPERATURE: user_input.get(CONF_SENSOR_TEMPERATURE, DEFAULT_SENSOR_TEMPERATURE),
                CONF_SENSOR_GYROSCOPE: user_input.get(CONF_SENSOR_GYROSCOPE, DEFAULT_SENSOR_GYROSCOPE),
                CONF_WARN_COUNTERFEIT: user_input.get(CONF_WARN_COUNTERFEIT, DEFAULT_WARN_COUNTERFEIT),
            })
            if is_esp:
                if CONF_NOTIFY_THROTTLE in user_input:
                    data[CONF_NOTIFY_THROTTLE] = int(user_input[CONF_NOTIFY_THROTTLE])
                if CONF_PIPELINED_READS in user_input:
                    data[CONF_PIPELINED_READS] = bool(user_input[CONF_PIPELINED_READS])
            return self._save_options(data)

        options = self.config_entry.options
        schema_fields: dict = {}
        schema_fields[vol.Required(
                CONF_SENSOR_PRESSURE,
                default=options.get(CONF_SENSOR_PRESSURE, DEFAULT_SENSOR_PRESSURE),
            )] = bool
        schema_fields[vol.Required(
                CONF_SENSOR_TEMPERATURE,
                default=options.get(CONF_SENSOR_TEMPERATURE, DEFAULT_SENSOR_TEMPERATURE),
            )] = bool
        schema_fields[vol.Required(
                CONF_SENSOR_GYROSCOPE,
                default=options.get(CONF_SENSOR_GYROSCOPE, DEFAULT_SENSOR_GYROSCOPE),
            )] = bool
        schema_fields[vol.Required(
                CONF_WARN_COUNTERFEIT,
                default=options.get(CONF_WARN_COUNTERFEIT, DEFAULT_WARN_COUNTERFEIT),
            )] = bool

        if is_esp:
            schema_fields[vol.Required(
                CONF_NOTIFY_THROTTLE,
                default=options.get(CONF_NOTIFY_THROTTLE, DEFAULT_NOTIFY_THROTTLE),
            )] = vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_NOTIFY_THROTTLE, max=MAX_NOTIFY_THROTTLE),
            )
            schema_fields[vol.Required(
                CONF_PIPELINED_READS,
                default=options.get(CONF_PIPELINED_READS, DEFAULT_PIPELINED_READS),
            )] = bool

        _LOGGER.debug(
            "Options flow: showing settings for %s (esp bridge: %s)",
            self.config_entry.title, is_esp,
        )
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(schema_fields),
        )

    async def async_step_adapter(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Move the brush to another Bluetooth adapter (direct BLE only)."""
        current = self.config_entry.options.get(
            CONF_BLUETOOTH_ADAPTER, BLUETOOTH_ADAPTER_AUTO
        )
        if user_input is not None:
            adapter = user_input.get(CONF_BLUETOOTH_ADAPTER, BLUETOOTH_ADAPTER_AUTO)
            data = {**self._current_options(), CONF_BLUETOOTH_ADAPTER: adapter}
            # Same adapter again: nothing to pair or remove.
            if adapter == current:
                return self._save_options(data)
            state = await self._pairing_state(adapter)
            self._pending_data = data
            if state == "needed":
                self._pair_error = ""
                self._pair_attempted = False
                return await self.async_step_pair_adapter()
            return await self._async_finish_adapter_change(state)

        _LOGGER.debug(
            "Options flow: showing adapter for %s", self.config_entry.title
        )
        return self.async_show_form(
            step_id="adapter",
            data_schema=vol.Schema({
                vol.Required(CONF_BLUETOOTH_ADAPTER, default=current): SelectSelector(
                    SelectSelectorConfig(
                        options=_bluetooth_adapter_options(
                            self.hass,
                            self.config_entry.data.get(CONF_ADDRESS) or "",
                            current,
                        ),
                        translation_key=CONF_BLUETOOTH_ADAPTER,
                    )
                ),
            }),
        )
