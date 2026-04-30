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


async def async_is_device_paired(mac: str) -> bool | None:
    """Quick D-Bus check whether BlueZ considers a device paired.

    Returns True/False, or None if the check fails.
    """
    bus: MessageBus | None = None
    try:
        bus = MessageBus(bus_type=BusType.SYSTEM)
        await bus.connect()

        device_path = await _find_device_path(bus, mac)
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


async def _find_device_path(bus: MessageBus, mac: str) -> str | None:
    """Find the BlueZ D-Bus object path for a device by MAC address."""
    introspection = await bus.introspect("org.bluez", "/")
    proxy = bus.get_proxy_object("org.bluez", "/", introspection)
    obj_mgr = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
    objects = await obj_mgr.call_get_managed_objects()

    mac_upper = mac.upper()
    for path, ifaces in objects.items():
        if "org.bluez.Device1" not in ifaces:
            continue
        props = ifaces["org.bluez.Device1"]
        addr = props.get("Address")
        if addr and addr.value.upper() == mac_upper:
            return path
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def async_remove_device(mac: str) -> bool:
    """Remove a BlueZ-known device — drops bond, trust, and cached GATT.

    Returns True if the device was removed (or wasn't present), False if
    BlueZ couldn't be reached or rejected the call. No agent registration
    is needed for RemoveDevice — just locating the device path via the
    object manager and dispatching to its parent adapter.

    Used by ``async_remove_entry`` to release the host-side bond when a
    Direct-BLE config entry is permanently removed, mirroring how the
    ESP-bridge path drops its NVS-stored identity.
    """
    bus: MessageBus | None = None
    try:
        bus = MessageBus(bus_type=BusType.SYSTEM)
        await bus.connect()

        device_path = await _find_device_path(bus, mac)
        if not device_path:
            _LOGGER.debug(
                "Device %s not in BlueZ — nothing to remove", mac
            )
            return True

        adapter_path = device_path.rsplit("/", 1)[0]
        adapter_intro = await bus.introspect("org.bluez", adapter_path)
        adapter_proxy = bus.get_proxy_object(
            "org.bluez", adapter_path, adapter_intro
        )
        adapter_iface = adapter_proxy.get_interface("org.bluez.Adapter1")
        await adapter_iface.call_remove_device(device_path)
        _LOGGER.info("Removed BlueZ bond for %s", mac)
        return True
    except DBusError as err:
        _LOGGER.warning("BlueZ refused RemoveDevice on %s: %s", mac, err)
        return False
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("D-Bus error removing %s: %s", mac, err)
        return False
    finally:
        if bus and bus.connected:
            bus.disconnect()


async def async_pair_and_trust(mac: str) -> None:
    """Pair and trust a BLE device via BlueZ D-Bus.

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
        device_path = await _find_device_path(bus, mac)
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
                device_path = await _find_device_path(bus, mac)
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
            raise PairingError(
                f"Pairing timed out after {PAIR_TIMEOUT}s — "
                "ensure the toothbrush is powered on and not connected "
                "to another device"
            ) from None
        except DBusError as err:
            msg = str(err)
            if "AuthenticationFailed" in msg:
                raise PairingError(
                    "Authentication failed — the toothbrush may have a stale "
                    "bond from a previous system. Try turning the toothbrush "
                    "off and back on, then retry."
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
