"""Exceptions for the Philips Sonicare integration."""

from homeassistant.exceptions import HomeAssistantError


class PhilipsSonicareException(HomeAssistantError):
    """Base exception for Philips Sonicare."""


class DeviceNotFoundException(PhilipsSonicareException):
    """Device not found."""


class CannotConnectException(PhilipsSonicareException):
    """Cannot connect to device."""


class TransportError(PhilipsSonicareException):
    """Transport-level error."""


class NotPairedException(PhilipsSonicareException):
    """Device requires BLE bonding but is not paired.

    ``auth_error`` is True when the probe failed with an explicit GATT
    authentication error (0x05/0x0e/0x0f/...) on a connection carried by
    the local BlueZ adapter. If a host-side bond exists at that point,
    the device no longer accepts its key — the bond is stale, and
    removing it to re-pair is the only way forward. All other failure
    modes leave ``auth_error`` False so an existing bond is never wiped
    on ambiguous evidence (in particular an auth error on a proxy-routed
    connection, which says nothing about the BlueZ bond).
    """

    def __init__(self, *args: object, auth_error: bool = False) -> None:
        super().__init__(*args)
        self.auth_error = auth_error


class DeviceAsleepException(PhilipsSonicareException):
    """No recent connectable advertisement — the brush is asleep/unreachable."""
