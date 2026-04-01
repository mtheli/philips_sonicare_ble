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
    """Device requires BLE bonding but is not paired."""
