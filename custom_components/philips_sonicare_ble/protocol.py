"""Protocol layer for Philips Sonicare devices.

Separates the two on-device protocols so the Coordinator can stay
protocol-agnostic:

- Classic  (`477ea600-…`) — one GATT characteristic per property, used by
  HX992X, HX9992/Prestige 9900, HX6340/Kids, HX962V, HX991M, HX9996.
- Condor  (`e50ba3c0-…`) — framed request/response over a small set of
  GATT characteristics with push-style change indications. Used by
  HX742X / Series 7100 and likely other newer models.

`Condor` is the ASCII marker these newer devices advertise in their
manufacturer-data (Philips Company ID 477, payload `"Condor"`).

Both protocols sit above `SonicareTransport`, which owns the BLE path
(direct BlueZ vs. ESP bridge). A protocol implementation reads and
writes GATT characteristics via the transport without caring which
adapter carries the link.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from typing import Any

from .transport import SonicareTransport


UpdateCallback = Callable[[dict[str, Any]], None]


class SonicareProtocol(abc.ABC):
    """Protocol-level operations on a Philips Sonicare device.

    Implementations translate wire-format values (char UUIDs / JSON ports)
    into the shared ``coordinator.data`` keys used by the integration's
    entities (``brushing_time``, ``battery_level``, ``intensity``, …).
    Numeric IDs and bitmasks are mapped back to the human-readable string
    keys defined in ``const.py``.
    """

    def __init__(self, transport: SonicareTransport) -> None:
        self._transport = transport

    # --- Session lifecycle -------------------------------------------------

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish the protocol session on top of an already-open transport.

        Classic: no-op (direct GATT).
        Condor: runs the version-negotiation / channel-open handshake.
        """

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Tear down the protocol session. Transport stays owned by caller."""

    # --- Reads -------------------------------------------------------------

    @abc.abstractmethod
    async def refresh_all(self) -> dict[str, Any]:
        """Read every supported property once.

        Returns a flat dict mapping coordinator-data keys to current values.
        Used for cold-start and post-reconnect resync.
        """

    # --- Live push updates -------------------------------------------------

    @abc.abstractmethod
    async def start_live_updates(self, on_update: UpdateCallback) -> None:
        """Subscribe to push updates.

        The callback receives partial dicts containing only the changed
        keys (deltas). Implementations handle wire-format acks internally.
        """

    @abc.abstractmethod
    async def stop_live_updates(self) -> None:
        """Unsubscribe from all live updates."""

    # --- Writes ------------------------------------------------------------

    @abc.abstractmethod
    async def set_brushing_mode(self, mode_key: str) -> None:
        """Set the active brushing mode by its string key (BRUSHING_MODES)."""

    @abc.abstractmethod
    async def set_intensity(self, intensity_key: str) -> None:
        """Set the active intensity by its string key (INTENSITIES)."""

    # --- Classic-only extension points --------------------------------------
    # Default implementations treat these as unsupported; the Classic
    # protocol overrides them. Kept on the base class so callers don't
    # need to know which protocol is active.

    async def read_settings_bitmask(self) -> int:
        """Read the settings bitmask. 0 = unsupported by this protocol."""
        return 0

    async def write_settings_bit(self, bit_mask: int, enabled: bool) -> None:
        """Toggle one bit of the settings bitmask. No-op when unsupported."""
        return None
