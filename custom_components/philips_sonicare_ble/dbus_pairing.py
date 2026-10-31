"""BlueZ D-Bus pairing for Philips Sonicare toothbrushes.

Uses dbus_fast (already a bleak dependency) to register a BlueZ Agent1
that auto-confirms pairing.  Some Sonicare models (e.g. HX991M, HX962V)
require BLE bonding before GATT characteristics can be read.  Models
that don't require bonding still benefit from pairing — BlueZ caches
the GATT service table for bonded devices, speeding up reconnects.
"""

import asyncio
import logging
import os

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, method

_LOGGER = logging.getLogger(__name__)

AGENT_PATH = "/org/bluez/agent/philips_sonicare"
AGENT_CAPABILITY = "KeyboardDisplay"
PAIR_TIMEOUT = 30  # seconds


class PairingError(Exception):
    """Raised when D-Bus pairing fails."""


def is_dbus_available() -> bool:
    """Check if the D-Bus system bus is accessible (Linux with BlueZ)."""
    return os.path.exists("/run/dbus/system_bus_socket")


async def async_is_device_paired(
    mac: str, adapter_address: str | None = None
) -> bool | None:
    """Quick D-Bus check whether BlueZ considers a device paired.

    Bonds are per adapter: pass ``adapter_address`` to ask about the
    adapter the connection goes through — a bond on another adapter
    does not help it. Returns True/False, or None if the check fails.
    """
    bus: MessageBus | None = None
    try:
        bus = MessageBus(bus_type=BusType.SYSTEM)
        await bus.connect()

        device_path = await _find_device_path(bus, mac, adapter_address)
        if not device_path:
            return None

        dev_intro = await bus.introspect("org.bluez", device_path)
        dev_proxy = bus.get_proxy_object(
            "org.bluez", device_path, dev_intro
        )
        props = dev_proxy.get_interface(
            "org.freedesktop.DBus.Properties"
        )
        paired = await props.call_get("org.bluez.Device1", "Paired")
        return bool(paired.value)
    except Exception:
        return None
    finally:
        if bus and bus.connected:
            bus.disconnect()


async def async_paired_adapters(mac: str) -> list[str] | None:
    """MACs of the local adapters that hold a bond with the device.

    Upper case, sorted. None if BlueZ cannot be asked.
    """
    bus: MessageBus | None = None
    try:
        bus = MessageBus(bus_type=BusType.SYSTEM)
        await bus.connect()

        introspection = await bus.introspect("org.bluez", "/")
        proxy = bus.get_proxy_object("org.bluez", "/", introspection)
        obj_mgr = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await obj_mgr.call_get_managed_objects()

        adapters = {
            path: str(_value(ifaces["org.bluez.Adapter1"].get("Address", ""))).upper()
            for path, ifaces in objects.items()
            if "org.bluez.Adapter1" in ifaces
        }
        paired: set[str] = set()
        for path in await _find_device_paths(bus, mac):
            props = objects[path]["org.bluez.Device1"]
            if not _value(props.get("Paired", False)):
                continue
            parent = props.get("Adapter")
            parent_path = str(_value(parent)) if parent else path.rsplit("/", 1)[0]
            if adapters.get(parent_path):
                paired.add(adapters[parent_path])
        return sorted(paired)
    except Exception:  # noqa: BLE001
        return None
    finally:
        if bus and bus.connected:
            bus.disconnect()


# ---------------------------------------------------------------------------
# BlueZ Agent1 implementation
# ---------------------------------------------------------------------------


class _AutoConfirmAgent(ServiceInterface):
    """BlueZ Agent1 that auto-confirms pairing requests."""

    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self) -> None:  # noqa: N802
        _LOGGER.debug("Agent released")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # noqa: N802
        _LOGGER.info(
            "Auto-confirming pairing for %s (passkey %06d)", device, passkey
        )

    @method()
    def RequestAuthorization(self, device: "o") -> None:  # noqa: N802
        _LOGGER.debug("Auto-authorizing %s", device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:  # noqa: N802
        _LOGGER.debug("Auto-authorizing service %s on %s", uuid, device)

    @method()
    def Cancel(self) -> None:  # noqa: N802
        _LOGGER.debug("Pairing cancelled by BlueZ")


# ---------------------------------------------------------------------------
# Helper: find device D-Bus path by MAC (supports multiple adapters)
# ---------------------------------------------------------------------------


def _value(prop) -> object:
    return getattr(prop, "value", prop)


async def _find_device_paths(
    bus: MessageBus, mac: str, adapter_address: str | None = None
) -> list[str]:
    """BlueZ object paths of a device, one per adapter that knows it.

    BlueZ keeps a separate device object — and a separate bond — under
    every adapter (``/org/bluez/hci0/dev_…``, ``/org/bluez/hci1/dev_…``).
    ``adapter_address`` (the adapter's MAC, i.e. the scanner ``source``
    the Bluetooth adapter option stores) limits the result to that
    adapter; hci indices are not used because they change on replug.
    A remote scanner's source matches no adapter and yields nothing.
    """
    introspection = await bus.introspect("org.bluez", "/")
    proxy = bus.get_proxy_object("org.bluez", "/", introspection)
    obj_mgr = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
    objects = await obj_mgr.call_get_managed_objects()

    adapters = {
        path: str(_value(ifaces["org.bluez.Adapter1"].get("Address", ""))).upper()
        for path, ifaces in objects.items()
        if "org.bluez.Adapter1" in ifaces
    }
    wanted = adapter_address.upper() if adapter_address else None

    mac_upper = mac.upper()
    paths: list[str] = []
    for path, ifaces in objects.items():
        if "org.bluez.Device1" not in ifaces:
            continue
        props = ifaces["org.bluez.Device1"]
        addr = props.get("Address")
        if not addr or str(_value(addr)).upper() != mac_upper:
            continue
        if wanted is not None:
            parent = props.get("Adapter")
            parent_path = str(_value(parent)) if parent else path.rsplit("/", 1)[0]
            if adapters.get(parent_path) != wanted:
                continue
        paths.append(path)
    return sorted(paths)


async def _device_paths_on_adapters(
    bus: MessageBus, mac: str, adapter_address: str | None = None
) -> list[str]:
    """Where the device object lives or will appear, one path per adapter.

    For signal matching: BlueZ names device objects
    ``<adapter path>/dev_XX_XX_…``, so the path is known before the
    adapter has heard the device. Every adapter without
    ``adapter_address``; only that one when it names a local adapter;
    none when it names something else (a fixed Bluetooth proxy).
    """
    introspection = await bus.introspect("org.bluez", "/")
    proxy = bus.get_proxy_object("org.bluez", "/", introspection)
    obj_mgr = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
    objects = await obj_mgr.call_get_managed_objects()

    adapters = {
        path: str(_value(ifaces["org.bluez.Adapter1"].get("Address", ""))).upper()
        for path, ifaces in objects.items()
        if "org.bluez.Adapter1" in ifaces
    }
    if adapter_address:
        wanted = adapter_address.upper()
        adapters = {p: a for p, a in adapters.items() if a == wanted}
    suffix = "dev_" + mac.upper().replace(":", "_")
    return sorted(f"{path}/{suffix}" for path in adapters)


async def _find_device_path(
    bus: MessageBus, mac: str, adapter_address: str | None = None
) -> str | None:
    """The device's object path under one adapter, or the first known.

    Without ``adapter_address`` any adapter's object is returned — only
    correct with a single adapter; callers that touch a bond pass the
    adapter they connect through.
    """
    paths = await _find_device_paths(bus, mac, adapter_address)
    return paths[0] if paths else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def async_remove_device(
    mac: str, adapter_address: str | None = None
) -> bool:
    """Remove a BlueZ-known device — drops bond, trust, and cached GATT.

    Returns True if the device was removed (or wasn't present), False if
    BlueZ couldn't be reached or rejected the call. No agent registration
    is needed for RemoveDevice — just locating the device path via the
    object manager and dispatching to its parent adapter.

    Used by ``async_remove_entry`` to release the host-side bond when a
    Direct-BLE config entry is permanently removed, mirroring how the
    ESP-bridge path drops its NVS-stored identity. Without
    ``adapter_address`` the device is removed from every adapter — after
    switching the Bluetooth adapter option, bonds can exist on several.
    """
    bus: MessageBus | None = None
    try:
        bus = MessageBus(bus_type=BusType.SYSTEM)
        await bus.connect()

        device_paths = await _find_device_paths(bus, mac, adapter_address)
        if not device_paths:
            _LOGGER.debug(
                "Device %s not in BlueZ — nothing to remove", mac
            )
            return True

        # One adapter refusing must not leave the bonds on the others.
        ok = True
        for device_path in device_paths:
            adapter_path = device_path.rsplit("/", 1)[0]
            try:
                adapter_intro = await bus.introspect("org.bluez", adapter_path)
                adapter_proxy = bus.get_proxy_object(
                    "org.bluez", adapter_path, adapter_intro
                )
                adapter_iface = adapter_proxy.get_interface("org.bluez.Adapter1")
                await adapter_iface.call_remove_device(device_path)
                _LOGGER.info("Removed BlueZ bond for %s (%s)", mac, adapter_path)
            except DBusError as err:
                _LOGGER.warning(
                    "BlueZ refused RemoveDevice on %s (%s): %s",
                    mac, adapter_path, err,
                )
                ok = False
        return ok
    except DBusError as err:
        _LOGGER.warning("BlueZ refused RemoveDevice on %s: %s", mac, err)
        return False
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("D-Bus error removing %s: %s", mac, err)
        return False
    finally:
        if bus and bus.connected:
            bus.disconnect()


async def async_pair_via_existing_client(client, mac: str) -> None:
    """Pair on an already-connected BleakClient — keeps the ACL link.

    Used for devices that rotate their advertisement address (RPA) and
    can't survive a probe-disconnect → BlueZ re-connect cycle: BlueZ
    tries the now-stale address and times out with "Page Timeout".

    Bleak's ``client.pair()`` calls ``Device1.Pair()`` on the existing
    device path (and sets ``Trusted=True`` first), so BlueZ runs SMP
    on the live ACL — no LL re-connect, no race window for the RPA
    to rotate.

    Caller must keep ``client`` connected for the duration. The agent
    must be registered before any encrypted GATT access (e.g. an
    explicit ``client.pair()``); registering it here covers that.

    Raises PairingError on failure. On success the device is bonded
    and the connection remains usable for further reads.
    """
    bus: MessageBus | None = None
    agent_registered = False

    try:
        bus = MessageBus(bus_type=BusType.SYSTEM)
        await bus.connect()

        agent = _AutoConfirmAgent()
        bus.export(AGENT_PATH, agent)

        bluez_intro = await bus.introspect("org.bluez", "/org/bluez")
        bluez_proxy = bus.get_proxy_object(
            "org.bluez", "/org/bluez", bluez_intro
        )
        agent_mgr = bluez_proxy.get_interface("org.bluez.AgentManager1")

        try:
            await agent_mgr.call_register_agent(AGENT_PATH, AGENT_CAPABILITY)
            agent_registered = True
        except DBusError as err:
            # AlreadyExists → another flow already has our agent registered;
            # safe to proceed (default agent will still hit our handler).
            if "AlreadyExists" not in str(err):
                raise PairingError(
                    f"Could not register pairing agent: {err}"
                ) from err
            agent_registered = True

        try:
            await agent_mgr.call_request_default_agent(AGENT_PATH)
        except DBusError as err:
            # The default agent is the only bridge for confirming a
            # just-works pair on a connection bleak opened itself — if
            # another process holds it, our RequestConfirmation never
            # fires and pairing fails later with a cryptic "No agent".
            _LOGGER.warning(
                "RequestDefaultAgent failed: %s — pairing may fail if "
                "another process holds the default agent",
                err,
            )

        _LOGGER.info("Pairing %s on existing connection ...", mac)
        try:
            await asyncio.wait_for(client.pair(), timeout=PAIR_TIMEOUT)
        except asyncio.TimeoutError:
            raise PairingError(
                f"In-place pairing timed out after {PAIR_TIMEOUT}s"
            ) from None
        except Exception as err:  # noqa: BLE001
            raise PairingError(f"In-place pairing failed: {err}") from err

        _LOGGER.info("In-place pairing successful for %s", mac)

    finally:
        if bus and bus.connected:
            if agent_registered:
                try:
                    bluez_intro2 = await bus.introspect(
                        "org.bluez", "/org/bluez"
                    )
                    bluez_proxy2 = bus.get_proxy_object(
                        "org.bluez", "/org/bluez", bluez_intro2
                    )
                    agent_mgr2 = bluez_proxy2.get_interface(
                        "org.bluez.AgentManager1"
                    )
                    await agent_mgr2.call_unregister_agent(AGENT_PATH)
                except Exception:  # noqa: BLE001
                    pass
            try:
                bus.unexport(AGENT_PATH)
            except Exception:  # noqa: BLE001
                pass
            bus.disconnect()


# How long to wait for the device object to appear under the target
# adapter: BlueZ creates it only once that adapter has heard the brush.
# A brush that was just disconnected from another adapter took longer
# than 15 s to show up on the new one (live, 2026-10-29).
DEVICE_APPEAR_TIMEOUT = 30


async def _wait_for_device_path(
    bus: MessageBus, mac: str, adapter_address: str | None
) -> str | None:
    for attempt in range(DEVICE_APPEAR_TIMEOUT):
        device_path = await _find_device_path(bus, mac, adapter_address)
        if device_path or attempt == DEVICE_APPEAR_TIMEOUT - 1:
            return device_path
        await asyncio.sleep(1)
    return None


async def _async_cancel_and_check_paired(device_iface, props_iface, mac: str) -> bool:
    """After a Pair() timeout: cancel it; True if the bond exists anyway."""
    try:
        await device_iface.call_cancel_pairing()
    except Exception as err:  # noqa: BLE001 — nothing left to cancel is fine
        _LOGGER.debug("CancelPairing for %s: %s", mac, err)
    try:
        paired = await props_iface.call_get("org.bluez.Device1", "Paired")
    except Exception:  # noqa: BLE001
        return False
    if paired.value:
        _LOGGER.info("Pairing with %s completed after the timeout", mac)
        return True
    return False


async def async_pair_and_trust(
    mac: str, adapter_address: str | None = None
) -> None:
    """Pair and trust a BLE device via BlueZ D-Bus.

    ``adapter_address`` pairs through that adapter only. The stale-bond
    removal and the re-discovery after it stay on the same adapter, so a
    good bond on another adapter is neither wiped nor mistaken for the
    fresh one.

    Raises PairingError on any failure.
    """
    bus: MessageBus | None = None
    agent_registered = False

    try:
        bus = MessageBus(bus_type=BusType.SYSTEM)
        await bus.connect()

        # --- register auto-confirm agent ---
        agent = _AutoConfirmAgent()
        bus.export(AGENT_PATH, agent)

        bluez_intro = await bus.introspect("org.bluez", "/org/bluez")
        bluez_proxy = bus.get_proxy_object(
            "org.bluez", "/org/bluez", bluez_intro
        )
        agent_mgr = bluez_proxy.get_interface("org.bluez.AgentManager1")

        await agent_mgr.call_register_agent(AGENT_PATH, AGENT_CAPABILITY)
        agent_registered = True
        await agent_mgr.call_request_default_agent(AGENT_PATH)
        _LOGGER.debug("Agent registered at %s", AGENT_PATH)

        # --- find device ---
        device_path = await _wait_for_device_path(bus, mac, adapter_address)
        if not device_path:
            raise PairingError(
                f"Device {mac} not found in BlueZ — "
                "ensure it is powered on and in range"
            )

        dev_intro = await bus.introspect("org.bluez", device_path)
        dev_proxy = bus.get_proxy_object(
            "org.bluez", device_path, dev_intro
        )
        device_iface = dev_proxy.get_interface("org.bluez.Device1")
        props_iface = dev_proxy.get_interface(
            "org.freedesktop.DBus.Properties"
        )

        # --- stale bond? remove and re-discover ---
        paired = await props_iface.call_get("org.bluez.Device1", "Paired")
        if paired.value:
            _LOGGER.info(
                "Device %s has a stale bond — removing before re-pairing", mac
            )
            adapter_path = device_path.rsplit("/", 1)[0]
            adapter_intro = await bus.introspect("org.bluez", adapter_path)
            adapter_proxy = bus.get_proxy_object(
                "org.bluez", adapter_path, adapter_intro
            )
            adapter_iface = adapter_proxy.get_interface("org.bluez.Adapter1")
            await adapter_iface.call_remove_device(device_path)
            _LOGGER.debug("Stale device removed, waiting for re-discovery")

            for _ in range(10):
                await asyncio.sleep(1)
                device_path = await _find_device_path(bus, mac, adapter_address)
                if device_path:
                    break
            if not device_path:
                raise PairingError(
                    f"Device {mac} not found after removing stale bond — "
                    "ensure it is powered on and in range"
                )

            dev_intro = await bus.introspect("org.bluez", device_path)
            dev_proxy = bus.get_proxy_object(
                "org.bluez", device_path, dev_intro
            )
            device_iface = dev_proxy.get_interface("org.bluez.Device1")
            props_iface = dev_proxy.get_interface(
                "org.freedesktop.DBus.Properties"
            )

        # --- pair ---
        _LOGGER.info("Starting BLE pairing with %s ...", mac)
        try:
            await asyncio.wait_for(
                device_iface.call_pair(), timeout=PAIR_TIMEOUT
            )
        except asyncio.TimeoutError:
            # Only our wait ends here — BlueZ keeps pairing and may still
            # finish. Stop it, then take a bond that made it after all.
            if not await _async_cancel_and_check_paired(
                device_iface, props_iface, mac
            ):
                raise PairingError(
                    f"Pairing timed out after {PAIR_TIMEOUT}s — "
                    "ensure the toothbrush is powered on and not connected "
                    "to another device"
                ) from None
        except DBusError as err:
            msg = str(err)
            if "AuthenticationFailed" in msg:
                # A stale bond is already removed before this pair attempt,
                # so an auth failure here is a fresh rejection — usually the
                # brush was not awake/in range, or the host needs the pairing
                # agent the bundled script registers.
                raise PairingError(
                    "Authentication failed while pairing — make sure the "
                    "toothbrush is awake (press the power button and take it "
                    "off the charger) and retry. If it keeps failing, run the "
                    "pairing script shown on the next screen; it registers "
                    "the pairing agent some hosts need."
                ) from err
            if "AlreadyExists" in msg:
                _LOGGER.info("Device %s already paired", mac)
            else:
                raise PairingError(f"BlueZ pairing error: {msg}") from err

        _LOGGER.info("Pairing successful for %s", mac)

        # --- trust ---
        await props_iface.call_set(
            "org.bluez.Device1", "Trusted", Variant("b", True)
        )
        _LOGGER.info("Device %s trusted", mac)

        # --- disconnect (HA/bleak will reconnect) ---
        try:
            await device_iface.call_disconnect()
        except Exception:
            pass

    except PairingError:
        raise
    except Exception as err:
        _LOGGER.error("D-Bus pairing failed for %s: %s", mac, err)
        raise PairingError(str(err)) from err
    finally:
        if bus and bus.connected:
            if agent_registered:
                try:
                    bluez_intro2 = await bus.introspect(
                        "org.bluez", "/org/bluez"
                    )
                    bluez_proxy2 = bus.get_proxy_object(
                        "org.bluez", "/org/bluez", bluez_intro2
                    )
                    agent_mgr2 = bluez_proxy2.get_interface(
                        "org.bluez.AgentManager1"
                    )
                    await agent_mgr2.call_unregister_agent(AGENT_PATH)
                except Exception:
                    pass
            try:
                bus.unexport(AGENT_PATH)
            except Exception:
                pass
            bus.disconnect()
