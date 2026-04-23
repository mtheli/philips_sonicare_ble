"""Condor protocol implementation for newer Sonicare models.

`Condor` is the ASCII string these devices advertise in their
manufacturer-data (Philips Company ID 477). Confirmed on Sonicare
HX742X / Series 7100 running firmware 1.8.20.0.

Wire format (on top of GATT service ``e50ba3c0-…``):

- Version negotiation and channel-config exchange on ``…0006`` / ``…0007``
- Framed messages on ``…0001`` (RX, app→device) and ``…0003`` (TX,
  device→app), each fragment carrying a 1-byte transport header. Frame
  structure: ``FE FF`` + 1-byte type + 2-byte big-endian length + body.
- Properties are addressed as ``product_id/port_name`` (e.g. ``1/Battery``,
  ``1/RoutineStatus``). Bodies are UTF-8 JSON for named ports, raw bytes
  for ``*.b`` streaming ports.

This module is the skeleton: transport handshake, subscribe flow and
JSON-to-data-key adapter are filled in as the implementation phases
progress.
"""

from __future__ import annotations

import logging
from typing import Any

from .protocol import SonicareProtocol, UpdateCallback
from .transport import SonicareTransport

_LOGGER = logging.getLogger(__name__)


# GATT service / characteristics — mirrors scripts/sonicare_scan.py
SVC_CONDOR = "e50ba3c0-af04-4564-92ad-fef019489de6"
CHAR_RX = "e50b0001-af04-4564-92ad-fef019489de6"
CHAR_RX_ACK = "e50b0002-af04-4564-92ad-fef019489de6"
CHAR_TX = "e50b0003-af04-4564-92ad-fef019489de6"
CHAR_TX_ACK = "e50b0004-af04-4564-92ad-fef019489de6"
CHAR_PROTO_CFG = "e50b0005-af04-4564-92ad-fef019489de6"
CHAR_SERVER_CFG = "e50b0006-af04-4564-92ad-fef019489de6"
CHAR_CLIENT_CFG = "e50b0007-af04-4564-92ad-fef019489de6"

# Message types on the framed layer
MSG_INITIALIZE_REQ = 1
MSG_INITIALIZE_RESP = 2
MSG_PUT_PROPS = 3
MSG_GET_PROPS = 4
MSG_SUBSCRIBE = 5
MSG_UNSUBSCRIBE = 6
MSG_GENERIC_RESP = 7
MSG_CHANGE_IND = 8
MSG_CHANGE_IND_RESP = 9
MSG_GET_PRODS = 10
MSG_GET_PORTS = 11

# Subscribe payload value — matches the reference app; ports stay
# subscribed for a year unless explicitly unsubscribed.
SUBSCRIBE_TIMEOUT_SECS = 31_536_000

# Ports we ask the device to push updates for. Empirically verified on
# HX742X: ``Sonicare`` and ``RoutineStatus`` are chatty during sessions;
# ``Battery`` / ``BrushHead`` / ``SessionStorage`` fire only on change.
DEFAULT_SUBSCRIBE_PORTS: tuple[tuple[str, str], ...] = (
    ("1", "Sonicare"),
    ("1", "RoutineStatus"),
    ("1", "Battery"),
    ("1", "BrushHead"),
    ("1", "SessionStorage"),
)


class CondorProtocol(SonicareProtocol):
    """Framed request/response + change-indication protocol."""

    def __init__(self, transport: SonicareTransport) -> None:
        super().__init__(transport)
        # Session state — populated during handshake in connect()
        self._max_packet_size: int = 20
        self._connected: bool = False
        self._live_callback: UpdateCallback | None = None

    # --- Session lifecycle -------------------------------------------------

    async def connect(self) -> None:
        """Run the Condor handshake: version negotiation, channel config,
        data-channel open, then Initialize message."""
        raise NotImplementedError("Condor handshake — implemented in Phase 2")

    async def disconnect(self) -> None:
        raise NotImplementedError("Condor teardown — implemented in Phase 2")

    # --- Reads -------------------------------------------------------------

    async def refresh_all(self) -> dict[str, Any]:
        """Discover products and ports, GetProps each, return flat data dict."""
        raise NotImplementedError("Condor refresh — implemented in Phase 2")

    # --- Live push updates -------------------------------------------------

    async def start_live_updates(self, on_update: UpdateCallback) -> None:
        """Subscribe to DEFAULT_SUBSCRIBE_PORTS and route change-indications
        through ``on_update`` as delta dicts."""
        raise NotImplementedError("Condor subscribe — implemented in Phase 2")

    async def stop_live_updates(self) -> None:
        raise NotImplementedError("Condor unsubscribe — implemented in Phase 2")

    # --- Writes ------------------------------------------------------------

    async def set_brushing_mode(self, mode_key: str) -> None:
        """PutProps on RoutineStatus.Mode (or Sonicare.UserMode[pos])."""
        raise NotImplementedError("Condor PutProps — implemented in Phase 4")

    async def set_intensity(self, intensity_key: str) -> None:
        """PutProps on RoutineStatus.Intensity."""
        raise NotImplementedError("Condor PutProps — implemented in Phase 4")
