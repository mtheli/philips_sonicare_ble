"""Config flow for Philips Sonicare BLE."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
    async_last_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import Event, callback
from homeassistant.data_entry_flow import AbortFlow, FlowResult
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.util import dt as dt_util
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectOptionDict,
)

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection as bleak_establish

from .const import (
    DOMAIN,
    CONF_SERVICES,
    CONF_TRANSPORT_TYPE,
    CONF_ESP_DEVICE_NAME,
    CONF_ESP_BRIDGE_ID,
    CONF_DEVICE_NAME,
    CONF_AREA,
    CONF_NOTIFY_THROTTLE,
    CONF_PIPELINED_READS,
    CONF_SENSOR_PRESSURE,
    CONF_SENSOR_TEMPERATURE,
    CONF_SENSOR_GYROSCOPE,
    CONF_WARN_COUNTERFEIT,
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
    async_unpair_bridge_slot,
    describe_available_paths,
    describe_connection_path,
    is_local_bluez_connection,
    UNPAIR_OK,
    UNPAIR_FAILED,
    UNPAIR_UNAVAILABLE,
)
from .exceptions import DeviceAsleepException, NotPairedException, TransportError

_LOGGER = logging.getLogger(__name__)


def _is_hassio(hass) -> bool:
    """Check if Home Assistant is running on HAOS / Supervised."""
    return "hassio" in hass.config.components


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

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._name: str | None = None
        self._fetched_data: dict[str, Any] | None = None
        self._pair_error: str | None = None
        self._transport_type: str = TRANSPORT_BLEAK
        self._esp_device_name: str | None = None
        self._esp_bridge_id: str = ""
        self._esp_bridge_ids: list[str] = []
        self._bridge_info: dict[str, str] | None = None
        self._probed_bridges: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._manual_address_entry: bool = False
        self._configured_bridge_ids: set[str] = set()
        # Transport of the last probe that actually connected. None until a
        # probe establishes a connection; deliberately NOT reset on failed
        # connects so a retry that never reaches the device keeps showing
        # the pairing dialog that matches the last known transport.
        self._probe_via_proxy: bool | None = None
        self._probe_proxy_name: str | None = None
        # Set when a probe read had to wait for SMP (encryption required).
        # Used to label BLE security on the proxy path, where the BlueZ
        # bond state is meaningless (the bond lives in the proxy's NVS).
        self._probe_needed_encryption: bool = False
        # One-shot marker: wait_pair just bonded the bridge, so the next
        # esp_bridge_status render acknowledges the success.
        self._just_paired: bool = False
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

    def _abort_if_already_configured(self) -> None:
        """Abort with detailed message if this unique_id is already configured."""
        for entry in self._async_current_entries():
            if entry.unique_id and entry.unique_id == self.unique_id:
                transport = entry.data.get(CONF_TRANSPORT_TYPE, TRANSPORT_BLEAK)
                transport_label = (
                    "ESP32 Bridge" if transport == TRANSPORT_ESP_BRIDGE
                    else "Direct Bluetooth"
                )
                disabled = entry.disabled_by is not None
                status = "disabled" if disabled else "active"
                raise AbortFlow(
                    "already_configured_detail",
                    description_placeholders={
                        "transport": transport_label,
                        "status": status,
                    },
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

        client: BleakClient | None = None
        try:
            # Progress milestones: the connect is a single await and by far
            # the longest leg, so the bar sits low until it lands, then
            # advances per characteristic read.
            self._bump_progress(0.05)
            client = await bleak_establish(
                BleakClient, device, "philips_sonicare_ble",
                use_services_cache=True, timeout=30.0,
            )
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
            self._probe_proxy_name = (
                connection_path if self._probe_via_proxy else None
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
                if not await async_is_device_paired(address):
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
            if client and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass

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
            await async_pair_and_trust(address)
            await asyncio.sleep(2)  # let BlueZ key distribution settle
            return True
        except PairingError as err:
            _LOGGER.warning("Auto-pairing failed for %s: %s", address, err)
            # Remember the reason so the not_paired step can show the user
            # why pairing failed instead of a generic instruction wall.
            self._pair_error = str(err)
            return False

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
                paired = await async_is_device_paired(address)
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
        if self._probe_via_proxy:
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
        if await async_is_device_paired(address):
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
    ) -> str:
        """Format found and missing services as a 2-column HTML table.

        Left column: ✅ available services. Right column: ❌ services not
        present on this model. Service-name only — descriptions are
        omitted to keep the dialog compact (Service-Name is sprechend
        genug, and the dialog now fits the viewport without scroll).
        The *why* of missing services collapses into a footer.
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
                missing.append(name)

        known_all = _EXPECTED_SERVICES | {SVC_BYTESTREAM.lower()}
        for uuid in sorted(fetched_lower - _EXPECTED_SERVICES):
            name = SERVICE_NAMES.get(uuid)
            if not name or uuid not in known_all:
                continue
            found.append(name)

        if not found and not missing:
            return "No services detected"

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

        footer_for = {
            "not on this model":
                "❌ entries are not available on this model.",
            "via Condor protocol":
                "Classic feature services are replaced by the Condor "
                "protocol on this model.",
        }
        notes = [footer_for[r] for r in sorted(used_reasons) if r in footer_for]
        if notes:
            table += "\n\n" + "\n\n".join(notes)
        return table

    @staticmethod
    def _get_device_info_text(data: dict[str, Any], address: str | None = None) -> str:
        """Format device info as an HTML table (no header)."""
        rows: list[str] = []
        if model := data.get("model"):
            rows.append(f"<tr><td><b>Model</b></td><td>{model}</td></tr>")
        if serial := data.get("serial"):
            rows.append(f"<tr><td><b>Serial</b></td><td><code>{serial}</code></td></tr>")
        if firmware := data.get("firmware"):
            rows.append(f"<tr><td><b>Firmware</b></td><td>{firmware}</td></tr>")
        if (battery := data.get("battery")) is not None:
            rows.append(f"<tr><td><b>Battery</b></td><td>{battery}%</td></tr>")
        if address:
            rows.append(
                f"<tr><td><b>MAC</b></td><td><code>{address.upper()}</code></td></tr>"
            )
        pairing = data.get("pairing")
        if pairing == "bonded":
            rows.append(
                "<tr><td><b>BLE Security</b></td><td>Bonded (encrypted)</td></tr>"
            )
        elif pairing == "open_gatt":
            rows.append(
                "<tr><td><b>BLE Security</b></td><td>Unpaired (no encryption)</td></tr>"
            )
        if not rows:
            return "Could not read device information"
        return f"<table><tbody>{''.join(rows)}</tbody></table>"

    @staticmethod
    def _has_sonicare_services(data: dict[str, Any]) -> bool:
        """Check if any Sonicare-specific GATT services were discovered."""
        services = data.get("services", [])
        fetched_lower = {s.lower() for s in services} - _STANDARD_BLE_SERVICES
        return bool(fetched_lower & _EXPECTED_SERVICES)

    @staticmethod
    def _get_connection_status_text(
        transport_type: str, path: str | None, *, via_proxy: bool = False
    ) -> str:
        """Return the connection status line for the capabilities dialog.

        Leads with the transport *class* (``ESP32 Bridge`` / ``Bluetooth
        proxy`` / ``Direct Bluetooth``) — same framing as the other
        ``via`` dialogs — and appends the slot/adapter label (YAML
        ``friendly_name`` for ESP, scanner name otherwise) in
        parentheses as a disambiguator, never in the ``via`` position
        itself. ``via_proxy`` marks a TRANSPORT_BLEAK probe that rode a
        remote scanner — labelling that "Direct Bluetooth" would hide
        the very path distinction the pairing dialogs are keyed on.
        """
        if transport_type == TRANSPORT_ESP_BRIDGE:
            transport_label = "ESP32 Bridge"
        elif via_proxy:
            transport_label = "Bluetooth proxy"
        else:
            transport_label = "Direct Bluetooth"
        if path:
            return f"✅ Connected via **{transport_label}** ({path})."
        return f"✅ Connected via **{transport_label}**."

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
        for entry in esphome_entries:
            if self._esp_entry_unreachable(entry, "esp_select"):
                continue
            device_name = entry.data.get("device_name")
            if not device_name:
                continue
            device_name = esphome_service_id(device_name)
            bridge_ids = self._detect_esp_bridge_ids(device_name)
            if not bridge_ids:
                continue
            sonicare = await self._probe_sonicare_bridges(device_name, bridge_ids)
            if not sonicare:
                _LOGGER.debug(
                    "Skipping ESP %s: no philips_sonicare bridge responded", device_name
                )
                continue
            self._probed_bridges[device_name] = sonicare

            slot_info = ""
            if len(sonicare) > 1:
                paired_count = sum(
                    1 for _, info in sonicare
                    if info.get("pair_capable") != "true"
                    and info.get("mac", "") not in ("", "00:00:00:00:00:00")
                )
                free_count = len(sonicare) - paired_count
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
    ) -> list[tuple[str, dict[str, str]]]:
        """Probe all bridge_ids on an ESP in parallel; keep responders only."""
        results = await asyncio.gather(
            *(self._probe_bridge_info(esp_device_name, did) for did in bridge_ids)
        )
        return [(did, info) for did, info in zip(bridge_ids, results) if info is not None]

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
                # Prefer identity_address (persisted in NVS, used as
                # ConfigEntry.unique_id) over mac (= live remote_bda which
                # is 00:00:… while the brush is disconnected).
                identity = info.get("identity_address", "").upper()
                mac = info.get("mac", "").upper()
                known = {m for m in (identity, mac)
                         if m and m != "00:00:00:00:00:00"}
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
            self.context["title_placeholders"] = {
                "name": f"Bluetooth ({discovery_info.address})"
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
        # user can pick the manual ESP path themselves if needed.
        match = await self._find_esp_bridge_for_mac(self._address or "")
        if match:
            self._esp_device_name, self._esp_bridge_id = match
            self._esp_bridge_ids = self._detect_esp_bridge_ids(self._esp_device_name)
            return await self._esp_bridge_health_check()

        if user_input is not None:
            return self._start_ble_probe("bluetooth_confirm", self._address)

        # One-shot outcome from ble_probe_finish. errors["base"] does not
        # render on this schema-less confirmation step, so failures are
        # injected as an <ha-alert> into the description; ha-markdown
        # renders it as a real coloured alert box.
        status = self._confirm_status
        self._confirm_status = ""

        via, warning = self._transport_lines()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._name,
                "address": self._address,
                "status": status,
                "via": via,
                "transport": warning,
            },
        )

    @staticmethod
    def _short_scanner(p: dict) -> str:
        # Scanner names carry the adapter MAC in parentheses; strip it —
        # the dialog cares about *which* device, not its MAC.
        return str(p["name"]).split(" (")[0]

    def _transport_lines(self) -> tuple[str, str]:
        """Return ``(via_suffix, warning)`` for the discovery confirm step.

        ``via_suffix`` names the likely carrier inline after "discovered
        at <mac>", mirroring the capabilities dialog's
        ``via <class> (<detail>)`` framing: "Direct Bluetooth" for a
        local adapter, "Bluetooth proxy" for a remote scanner.

        ``warning`` is a proxy-only <ha-alert> — pairing over a standard
        proxy is model-dependent (some models never trigger the
        proxy-side bonding and fail every read; see not_paired_proxy).

        habluetooth routes by signal strength, so the strongest scanner
        is only the *likely* carrier; recomputed each render so the
        ranking stays current.
        """
        paths = describe_available_paths(self.hass, self._address or "")
        if not paths:
            return "", ""

        def _rssi(p: dict) -> str:
            return f" ({p['rssi']} dBm)" if p["rssi"] is not None else ""

        best = paths[0]
        best_name = self._short_scanner(best)
        best_rssi = f", {best['rssi']} dBm" if best["rssi"] is not None else ""

        if best["is_local"]:
            via = f" via **Direct Bluetooth** ({best_name}{best_rssi})"
            return via, ""

        via = f" via **Bluetooth proxy** ({best_name}{best_rssi})"

        # Markdown is not parsed inside an HTML block, so the warning uses
        # <b>/<br> for emphasis and paragraph breaks.
        local = next((p for p in paths if p["is_local"]), None)
        if local is None:
            tail = (
                "For reliable pairing and live updates use a local "
                "Bluetooth adapter or the ESP32 bridge."
            )
        else:
            tail = (
                f"Your local adapter <b>{self._short_scanner(local)}</b> "
                f"also sees the toothbrush{_rssi(local)} — Home Assistant "
                "connects through the strongest signal, so move the "
                "toothbrush closer to it to prefer that path."
            )
        warning = (
            '<ha-alert alert-type="warning">'
            "This connection will go through the Bluetooth proxy "
            f"<b>{best_name}</b>{_rssi(best)}."
            "<br><br>Depending on the toothbrush model, pairing and "
            "live updates over a standard Bluetooth proxy can be "
            "unreliable."
            f"<br><br>{tail}</ha-alert>\n\n"
        )
        return via, warning

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
            if error == "unknown":
                error = "cannot_connect"
            self._manual_error = error
            return await self.async_step_user_bleak()

        # Keep the discovery flow alive — an abort would dismiss the
        # discovery card, and ADV deduplication stops HA from re-creating
        # it when the brush wakes.
        if error == "asleep":
            self._confirm_status = (
                '<ha-alert alert-type="error">The toothbrush is asleep — '
                "wake it (press the power button or lift it off the "
                "charger), then click Submit again.</ha-alert>\n\n"
            )
        else:
            self._confirm_status = (
                '<ha-alert alert-type="error">Could not read the toothbrush. '
                "Make sure it is awake and in range, then click Submit to "
                "try again.</ha-alert>\n\n"
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
            age_s = max(0, int(now_mono - info.time)) if info.time else None
            rssi = info.rssi
            label_parts = [f"{name} ({info.address})"]
            if age_s is not None:
                label_parts.append(f"{age_s}s ago")
            if rssi is not None:
                label_parts.append(f"{rssi} dBm")
            # Name the scanner that will likely carry the connect — the
            # step is titled "Direct Bluetooth", but habluetooth routes
            # by signal strength and may pick a bluetooth_proxy. Local
            # scanner names carry the adapter MAC in parentheses; strip
            # it to keep the label compact ("hci0" / "atom-lite (proxy)").
            paths = describe_available_paths(self.hass, info.address)
            if paths:
                best = paths[0]
                via = str(best["name"]).split(" (")[0]
                label_parts.append(
                    f"via {via}" + ("" if best["is_local"] else " (proxy)")
                )
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
                    )
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

        if len(esp_options) == 1 and not user_input:
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
        cached = self._probed_bridges.get(self._esp_device_name)
        if cached is None:
            cached = await self._probe_sonicare_bridges(
                self._esp_device_name, self._esp_bridge_ids
            )
            self._probed_bridges[self._esp_device_name] = cached

        self._configured_bridge_ids = set()
        options: list[SelectOptionDict] = []
        has_available = False
        shown_states: set[str] = set()

        for did, info in cached:
            mac = info.get("mac", "")
            has_mac = bool(mac) and mac != "00:00:00:00:00:00"
            is_configured = has_mac and mac.upper() in configured_macs
            label = self._format_bridge_label(did, info)

            if is_configured:
                self._configured_bridge_ids.add(did)
                options.append(SelectOptionDict(value=did, label=f"✅ {label}"))
                shown_states.add("already_configured")
            else:
                has_available = True
                options.append(SelectOptionDict(value=did, label=label))

            if info.get("pair_capable") == "true":
                shown_states.add("pair_required")
                continue
            paired = info.get("paired", "")
            if paired == "true":
                shown_states.add("bonded")
            elif paired == "false":
                shown_states.add("open_gatt")
            if info.get("ble_connected") == "true":
                shown_states.add("online")
            else:
                shown_states.add("offline")

        if not cached:
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

        legend_parts: list[str] = []
        if "already_configured" in shown_states:
            legend_parts.append("✅ already configured")
        if "bonded" in shown_states:
            legend_parts.append("🔒 bonded")
        if "open_gatt" in shown_states:
            legend_parts.append("🔓 unpaired")
        if "online" in shown_states:
            legend_parts.append("🟢 online")
        if "offline" in shown_states:
            legend_parts.append("⚪ offline")

        pair_hint = (
            "Entries without status icons are empty bridges in pair-mode. "
            "Switch on the toothbrush you want to bond before submitting."
            if "pair_required" in shown_states
            else ""
        )

        return self.async_show_form(
            step_id="esp_select_device",
            data_schema=vol.Schema(
                {
                    vol.Required("esp_bridge_id", default=default_value): SelectSelector(
                        SelectSelectorConfig(options=options)
                    ),
                }
            ),
            description_placeholders={
                "legend": " · ".join(legend_parts),
                "pair_hint": pair_hint,
            },
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
            icons.append("🔒")
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
                "identity_address": info.get("identity_address", ""),
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
                "identity_address": raw.get("identity_address", ""),
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

    async def async_step_esp_bridge_status_connected(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Alias step rendered when the bridge is already BLE-connected.

        HA routes step submissions to async_step_<step_id>; we keep the
        translations split between two step IDs (different action hints)
        but share the implementation here.
        """
        return await self.async_step_esp_bridge_status(user_input)

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

        # Format bridge status display
        info = self._bridge_info or {}
        ble_connected = info.get("ble_connected") == "true" if info else False
        if info:
            version = info.get("version", "?")
            mac = info.get("mac", "")

            ble_status = "\u2705 Connected" if ble_connected else "\u274c Disconnected"

            paired_str = info.get("paired", "")
            if paired_str == "true":
                paired_text = "Bonded (encrypted)"
            elif paired_str == "false":
                paired_text = "Unpaired (no encryption)"
            else:
                paired_text = ""

            rows = [f"<tr><td><b>BLE</b></td><td>{ble_status}</td></tr>"]
            if paired_text:
                rows.append(
                    f"<tr><td><b>Security</b></td><td>{paired_text}</td></tr>"
                )
            if mac and mac != "00:00:00:00:00:00":
                rows.append(
                    f"<tr><td><b>Toothbrush MAC</b></td><td><code>{mac}</code></td></tr>"
                )
            rows.append(f"<tr><td><b>Version</b></td><td>v{version}</td></tr>")

            status_text = f"<table><tbody>{''.join(rows)}</tbody></table>"
        else:
            status_text = "Diagnostic details not available."

        # A failed capabilities read surfaces here (errors[] doesn't render
        # on this schema-less step — same quirk as reset_bridge).
        if self._esp_read_error:
            status_text = (
                f'<ha-alert alert-type="error">{self._esp_read_error}</ha-alert>\n\n'
                + status_text
            )
            self._esp_read_error = ""

        # Acknowledge a pairing that wait_pair just completed (one-shot).
        if self._just_paired:
            self._just_paired = False
            status_text = (
                '<ha-alert alert-type="success">Pairing successful — the '
                "bridge is now bonded to your toothbrush.</ha-alert>\n\n"
                + status_text
            )

        target_placeholders = self._pair_target_placeholders()
        target = target_placeholders["target"]

        step_id = (
            "esp_bridge_status_connected" if ble_connected else "esp_bridge_status"
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._esp_device_name or "",
                "target": target,
                "status": status_text,
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
            if result.get("error") == "cannot_connect":
                self._esp_read_error = (
                    "Couldn't read the toothbrush over the bridge. Make sure "
                    "it's switched on and the bridge is online, then try again."
                )
            else:
                self._esp_read_error = (
                    "Something went wrong reading the toothbrush. Check the "
                    "logs (Settings → System → Logs) and try again."
                )
            return await self.async_step_esp_bridge_status()

        capabilities = result["caps"]

        # Prefer the NVS-persisted identity over the live remote_bda. Equal
        # for static-address brushes, but only identity stays valid when the
        # brush is idle and only identity is RPA-stable on Condor.
        def _valid_addr(raw: str) -> str:
            cleaned = (raw or "").upper()
            return cleaned if cleaned and cleaned != "00:00:00:00:00:00" else ""

        identity = _valid_addr(
            (self._bridge_info or {}).get("identity_address", "")
        )
        sonicare_mac = capabilities.get("sonicare_mac", "")
        canonical_addr = identity or _valid_addr(sonicare_mac)

        if canonical_addr:
            await self.async_set_unique_id(canonical_addr, raise_on_progress=False)
        else:
            await self.async_set_unique_id(f"esp_{self._esp_device_name}")
        self._abort_if_already_configured()

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

        placeholders = self._pair_target_placeholders()
        # Acknowledge a bond that reset_bridge just cleared (one-shot), so
        # the jump from "Reset bridge bond" to pair-mode isn't silent.
        if self._just_unpaired:
            self._just_unpaired = False
            placeholders["notice"] = (
                '<ha-alert alert-type="success">Bond removed — the slot is '
                "free. Switch on the Sonicare you want to bond, then start "
                "pairing.</ha-alert>\n\n"
            )
        return self.async_show_form(
            step_id="request_pair",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
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
            # Optional one-shot notice slot (request_pair success alert).
            # Empty by default; every step that renders it must supply it.
            "notice": "",
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
            if did.lower() == bridge_id.lower():
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
        return True

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

        if result.get("error"):
            return self.async_show_form(
                step_id="request_pair",
                data_schema=vol.Schema({}),
                errors={"base": result["error"]},
                description_placeholders=self._pair_target_placeholders(),
            )
        if result.get("status") == "pair_timeout":
            return self.async_show_form(
                step_id="request_pair",
                data_schema=vol.Schema({}),
                errors={"base": "pair_timeout"},
                description_placeholders=self._pair_target_placeholders(),
            )

        identity = result.get("identity_address", "").upper()
        if not identity:
            _LOGGER.error("pair_complete received without identity_address")
            return self.async_show_form(
                step_id="request_pair",
                data_schema=vol.Schema({}),
                errors={"base": "unknown"},
                description_placeholders=self._pair_target_placeholders(),
            )

        # Pair succeeded — clear bridge_info so the next health check picks up
        # the freshly-bound state, then run capabilities probe via the
        # existing ESP-bridge path.
        self._bridge_info = None
        self._just_paired = True
        self._address = identity
        await self.async_set_unique_id(identity, raise_on_progress=False)
        self._abort_if_already_configured()
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
        if outcome in (UNPAIR_FAILED, UNPAIR_UNAVAILABLE):
            msg = (
                "Couldn't reach the ESP bridge to clear the bond. Make "
                "sure it's online and powered, then try again."
            )
        else:  # UNPAIR_UNCONFIRMED
            msg = (
                "Couldn't confirm the bridge cleared the bond — it may "
                "need a reboot. Make sure it's online, then try again."
            )
        return self.async_show_form(
            step_id="reset_bridge",
            data_schema=vol.Schema({}),
            description_placeholders=self._reset_bridge_placeholders(msg),
        )

    def _reset_bridge_placeholders(self, error: str = "") -> dict[str, str]:
        """Placeholders for the reset_bridge step.

        ``errors["base"]`` does not render on this schema-less confirmation
        step (same as bluetooth_confirm), so a failure is surfaced by
        injecting an ``<ha-alert>`` into the ``{error}`` placeholder.
        """
        placeholders = self._pair_target_placeholders()
        placeholders["identity_address"] = (
            (self._bridge_info or {}).get("identity_address", "")
        )
        placeholders["error"] = (
            f'<ha-alert alert-type="error">{error}</ha-alert>\n\n'
            if error else ""
        )
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
                "model": self._fetched_data.get("model", ""),
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
            else:
                entry_data[CONF_ADDRESS] = self._address
                entry_data[CONF_TRANSPORT_TYPE] = TRANSPORT_BLEAK

            return self.async_create_entry(
                title=f"Philips Sonicare ({device_name})",
                data=entry_data,
            )

        device_info_text = self._get_device_info_text(self._fetched_data, self._address)
        services_text = self._get_service_status_text(
            self._fetched_data.get("services", []),
            self._fetched_data.get("model") or "",
        )

        path = self._fetched_data.get("connection_path")
        connection_status = self._get_connection_status_text(
            self._transport_type, path, via_proxy=bool(self._probe_via_proxy)
        )

        return self.async_show_form(
            step_id="show_capabilities",
            data_schema=vol.Schema({
                vol.Required(CONF_DEVICE_NAME, default=default_name): str,
            }),
            description_placeholders={
                "name": str(self._name),
                "connection_status": connection_status,
                "device_info": device_info_text,
                "services": services_text,
            },
        )

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
        if _is_hassio(self.hass):
            pairing_help = (
                "Open the **Terminal & SSH** addon "
                "([install it first](/hassio/addon/core_ssh/info) if needed) "
                "and run the pairing script:"
            )
        else:
            pairing_help = (
                "Open a terminal on the machine running Home Assistant "
                "and run the pairing script:"
            )
        pair_error = (
            f"**Last attempt:** {self._pair_error}\n\n" if self._pair_error else ""
        )
        return {
            "address": self._address or "",
            "pairing_help": pairing_help,
            "pair_cmd": pair_cmd,
            "pair_error": pair_error,
        }

    def _show_not_paired_form(self, errors: dict[str, str]) -> FlowResult:
        """Render the pairing dialog matching the probe transport.

        The host variant walks the user through pair.sh/bluetoothctl on
        the HA host; the proxy variant explains that the proxy bonds on
        its own during reads and host tools have no effect. habluetooth
        routes each connect by RSSI, so the transport is re-evaluated on
        every retry and the dialog follows it.
        """
        if self._probe_via_proxy:
            return self.async_show_form(
                step_id="not_paired_proxy",
                description_placeholders={
                    "address": self._address or "",
                    "proxy_name": self._probe_proxy_name or "unknown",
                },
                errors=errors,
            )
        return self.async_show_form(
            step_id="not_paired",
            description_placeholders=self._not_paired_placeholders(),
            errors=errors,
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
            except NotPairedException:
                errors["base"] = "pairing_failed"
            except Exception:
                _LOGGER.exception("Error after manual pairing retry")
                errors["base"] = "unknown"

            return self._show_not_paired_form(errors)

        return self._show_not_paired_form({})

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
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PhilipsSonicareOptionsFlow(config_entry)


class PhilipsSonicareOptionsFlow(OptionsFlow):
    """Options flow for Philips Sonicare BLE."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        is_esp = (
            self._config_entry.data.get(CONF_TRANSPORT_TYPE) == TRANSPORT_ESP_BRIDGE
        )

        if user_input is not None:
            data = {
                CONF_SENSOR_PRESSURE: user_input.get(CONF_SENSOR_PRESSURE, DEFAULT_SENSOR_PRESSURE),
                CONF_SENSOR_TEMPERATURE: user_input.get(CONF_SENSOR_TEMPERATURE, DEFAULT_SENSOR_TEMPERATURE),
                CONF_SENSOR_GYROSCOPE: user_input.get(CONF_SENSOR_GYROSCOPE, DEFAULT_SENSOR_GYROSCOPE),
                CONF_WARN_COUNTERFEIT: user_input.get(CONF_WARN_COUNTERFEIT, DEFAULT_WARN_COUNTERFEIT),
            }
            if is_esp:
                if CONF_NOTIFY_THROTTLE in user_input:
                    data[CONF_NOTIFY_THROTTLE] = int(user_input[CONF_NOTIFY_THROTTLE])
                if CONF_PIPELINED_READS in user_input:
                    data[CONF_PIPELINED_READS] = bool(user_input[CONF_PIPELINED_READS])
            return self.async_create_entry(title="", data=data)

        options = self._config_entry.options
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

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
        )
