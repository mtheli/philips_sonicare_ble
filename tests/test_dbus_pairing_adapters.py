"""Bond handling with more than one local Bluetooth adapter.

BlueZ keeps one device object — and one bond — per adapter. With the
Bluetooth adapter option, an entry may connect through hci0 while the
brush is bonded on hci1 only (seen live 2026-10-29: every connect over
hci0 ended in "failed to discover services, device disconnected").
Bond checks, pairing and removal must therefore target an adapter; a
first-match lookup by MAC alone hits whichever adapter BlueZ lists first.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.philips_sonicare_ble import dbus_pairing as dp

MAC = "24:E5:AA:BE:9C:1B"
HCI0 = "00:0A:CD:46:B2:2D"
HCI1 = "58:D8:12:3E:CA:B2"
DEV = "dev_24_E5_AA_BE_9C_1B"


def _v(value):
    return SimpleNamespace(value=value)


class FakeBlueZ:
    """Just enough of BlueZ's object tree and calls for dbus_pairing."""

    def __init__(self, paired: dict[str, bool]) -> None:
        self.objects: dict[str, dict] = {
            "/org/bluez/hci0": {"org.bluez.Adapter1": {"Address": _v(HCI0)}},
            "/org/bluez/hci1": {"org.bluez.Adapter1": {"Address": _v(HCI1)}},
        }
        for adapter, is_paired in paired.items():
            self._add_device(adapter, is_paired)
        self.removed: list[str] = []
        self.paired_on: list[str] = []
        # Device objects that reappear after RemoveDevice (brush awake).
        self.reappear = True

    def _add_device(self, adapter: str, is_paired: bool) -> None:
        self.objects[f"/org/bluez/{adapter}/{DEV}"] = {
            "org.bluez.Device1": {
                "Address": _v(MAC),
                "Adapter": _v(f"/org/bluez/{adapter}"),
                "Paired": _v(is_paired),
            }
        }

    def bus(self):
        return _FakeBus(self)


class _FakeBus:
    def __init__(self, bluez: FakeBlueZ) -> None:
        self.bluez = bluez
        self.connected = False

    async def connect(self):
        self.connected = True
        return self

    def disconnect(self) -> None:
        self.connected = False

    def export(self, path, iface) -> None:
        pass

    def unexport(self, path) -> None:
        pass

    async def introspect(self, name, path):
        return None

    def get_proxy_object(self, name, path, intro):
        return _FakeProxy(self.bluez, path)


class _FakeProxy:
    def __init__(self, bluez: FakeBlueZ, path: str) -> None:
        self.bluez = bluez
        self.path = path

    def get_interface(self, iface: str):
        bluez, path = self.bluez, self.path

        async def _noop(*args, **kwargs):
            return None

        if iface == "org.freedesktop.DBus.ObjectManager":
            async def managed():
                return bluez.objects
            return SimpleNamespace(call_get_managed_objects=managed)
        if iface == "org.freedesktop.DBus.Properties":
            async def get(_iface, prop):
                return bluez.objects[path]["org.bluez.Device1"][prop]
            return SimpleNamespace(call_get=get, call_set=_noop)
        if iface == "org.bluez.Adapter1":
            async def remove(device_path):
                bluez.removed.append(device_path)
                bluez.objects.pop(device_path, None)
                if bluez.reappear:
                    bluez._add_device(device_path.split("/")[3], False)
            return SimpleNamespace(call_remove_device=remove)
        if iface == "org.bluez.Device1":
            async def pair():
                bluez.paired_on.append(path)
                bluez.objects[path]["org.bluez.Device1"]["Paired"] = _v(True)
            return SimpleNamespace(call_pair=pair, call_disconnect=_noop)
        if iface == "org.bluez.AgentManager1":
            return SimpleNamespace(
                call_register_agent=_noop,
                call_request_default_agent=_noop,
                call_unregister_agent=_noop,
            )
        raise AssertionError(iface)


@pytest.fixture
def bluez(monkeypatch):
    """hci0 knows the brush unpaired, hci1 holds the bond."""
    fake = FakeBlueZ({"hci0": False, "hci1": True})
    monkeypatch.setattr(dp, "MessageBus", lambda bus_type=None: fake.bus())

    async def _fast_sleep(_delay):
        return None

    monkeypatch.setattr(dp.asyncio, "sleep", _fast_sleep)
    return fake


async def test_find_device_paths_per_adapter(bluez) -> None:
    bus = bluez.bus()
    assert await dp._find_device_paths(bus, MAC) == [
        f"/org/bluez/hci0/{DEV}", f"/org/bluez/hci1/{DEV}",
    ]
    assert await dp._find_device_paths(bus, MAC, HCI1) == [f"/org/bluez/hci1/{DEV}"]
    # Case of the stored option must not matter.
    assert await dp._find_device_path(bus, MAC.lower(), HCI0.lower()) == (
        f"/org/bluez/hci0/{DEV}"
    )
    # A proxy's source is no local adapter: nothing to find.
    assert await dp._find_device_paths(bus, MAC, "98:88:E0:0E:DA:D2") == []


async def test_bond_check_asks_the_target_adapter(bluez) -> None:
    # The bond on hci1 must not count for a connection over hci0.
    assert await dp.async_is_device_paired(MAC, HCI0) is False
    assert await dp.async_is_device_paired(MAC, HCI1) is True


async def test_pairing_on_hci0_leaves_hci1_bond_alone(bluez) -> None:
    await dp.async_pair_and_trust(MAC, HCI0)

    assert bluez.paired_on == [f"/org/bluez/hci0/{DEV}"]
    assert bluez.removed == []
    assert bluez.objects[f"/org/bluez/hci1/{DEV}"]["org.bluez.Device1"]["Paired"].value


async def test_stale_bond_removal_stays_on_target_adapter(bluez) -> None:
    # hci1's bond is stale: removal and re-discovery must stay on hci1 and
    # never pick up hci0's unrelated device object.
    await dp.async_pair_and_trust(MAC, HCI1)

    assert bluez.removed == [f"/org/bluez/hci1/{DEV}"]
    assert bluez.paired_on == [f"/org/bluez/hci1/{DEV}"]


async def test_pairing_waits_for_device_under_target_adapter(bluez) -> None:
    # hci0 has not heard the brush yet: no device object there.
    bluez.objects.pop(f"/org/bluez/hci0/{DEV}")
    with pytest.raises(dp.PairingError, match="not found"):
        await dp.async_pair_and_trust(MAC, HCI0)
    # The bond on hci1 was not touched while looking.
    assert bluez.removed == [] and bluez.paired_on == []


async def test_remove_device_without_adapter_clears_all(bluez) -> None:
    bluez.reappear = False
    assert await dp.async_remove_device(MAC) is True
    assert bluez.removed == [f"/org/bluez/hci0/{DEV}", f"/org/bluez/hci1/{DEV}"]


async def test_remove_device_on_one_adapter(bluez) -> None:
    bluez.reappear = False
    assert await dp.async_remove_device(MAC, HCI0) is True
    assert bluez.removed == [f"/org/bluez/hci0/{DEV}"]


async def test_listener_paths(bluez) -> None:
    bus = bluez.bus()
    # Every adapter, also one that has not seen the brush yet.
    bluez.objects.pop(f"/org/bluez/hci0/{DEV}")
    assert await dp._device_paths_on_adapters(bus, MAC) == [
        f"/org/bluez/hci0/{DEV}", f"/org/bluez/hci1/{DEV}",
    ]
    assert await dp._device_paths_on_adapters(bus, MAC, HCI0) == [
        f"/org/bluez/hci0/{DEV}",
    ]
    # Fixed Bluetooth proxy: no local wake signal at all.
    assert await dp._device_paths_on_adapters(bus, MAC, "98:88:E0:0E:DA:D2") == []


async def test_remove_device_continues_after_one_adapter_refuses(bluez, monkeypatch) -> None:
    # A refusal on hci0 must not leave the bond on hci1 behind.
    bluez.reappear = False
    real = _FakeProxy.get_interface

    def get_interface(self, iface):
        result = real(self, iface)
        if iface == "org.bluez.Adapter1" and self.path == "/org/bluez/hci0":
            async def refuse(device_path):
                raise dp.DBusError("org.bluez.Error.Failed", "busy")
            return SimpleNamespace(call_remove_device=refuse)
        return result

    monkeypatch.setattr(_FakeProxy, "get_interface", get_interface)
    assert await dp.async_remove_device(MAC) is False
    assert bluez.removed == [f"/org/bluez/hci1/{DEV}"]
