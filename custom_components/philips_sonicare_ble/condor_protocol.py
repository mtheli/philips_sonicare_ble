"""Condor protocol implementation for newer Sonicare models.

`Condor` is the ASCII string these devices advertise in their
manufacturer-data (Philips Company ID 477). Confirmed on Sonicare
HX742X / Series 7100 running firmware 1.8.20.0.

This module is the message half of the protocol: products, ports,
properties, change indications, and how they map onto the integration's
data keys. Exchanging the messages with the device — over BLE the V4
handshake, framing and acks — is the job of a ``CondorLink``
(``condor_link.py``). By default the link is a ``CondorBleLink`` on the
transport passed in.
"""

from __future__ import annotations

import logging
from typing import Any

from .condor_adapter import map_port_props, map_sensor_frame
from .condor_link import (  # noqa: F401 — re-exported for callers and tests
    MSG_CHANGE_IND,
    MSG_CHANGE_IND_RESP,
    MSG_GENERIC_RESP,
    MSG_GET_PORTS,
    MSG_GET_PRODS,
    MSG_GET_PROPS,
    MSG_INITIALIZE_REQ,
    MSG_INITIALIZE_RESP,
    MSG_PUT_PROPS,
    MSG_SUBSCRIBE,
    MSG_UNSUBSCRIBE,
    RESPONSE_TIMEOUT,
    STATUS_NAMES,
    STATUS_OK,
    SUBSCRIBE_TIMEOUT_SECS,
    CondorBleLink,
    CondorLink,
    status_name,
)
from .exceptions import TransportError
from .protocol import SonicareProtocol, UpdateCallback
from .transport import SonicareTransport

_LOGGER = logging.getLogger(__name__)

# Default port subscription set. Confirmed live on HX742X 2026-04-23:
# Sonicare + RoutineStatus are chatty during sessions; Battery / BrushHead /
# SessionStorage fire only on change.
DEFAULT_SUBSCRIBE_PORTS: tuple[tuple[str, str], ...] = (
    ("1", "Sonicare"),
    ("1", "RoutineStatus"),
    ("1", "Battery"),
    ("1", "BrushHead"),
    ("1", "SessionStorage"),
)


class CondorProtocol(SonicareProtocol):
    """Port reads, writes and change indications over a ``CondorLink``."""

    def __init__(
        self, transport: SonicareTransport, link: CondorLink | None = None,
    ) -> None:
        super().__init__(transport)
        self._link: CondorLink = link if link is not None else CondorBleLink(transport)
        self._live_callback: UpdateCallback | None = None

        # Ports currently under Subscribe. Tracked so stop_live_updates can
        # clean up symmetrically regardless of which set was requested.
        self._subscribed_ports: list[tuple[str, str]] = []

    @property
    def _connected(self) -> bool:
        return self._link.is_open

    # --- Session lifecycle -------------------------------------------------

    async def connect(self) -> None:
        """Open the link's session (over BLE: the Condor V4 handshake)."""
        await self._link.open()

    def invalidate_session(self) -> None:
        """Forget the open session without touching the link.

        When a link drops, the transport drops its notification registrations
        on its own — this drops the session state on both layers,
        synchronously, so ``connect()`` cannot short-circuit on a session that
        ended with the previous link. Safe to call at any time, including when
        no session was ever open.
        """
        self._set_live_callback(None)
        self._subscribed_ports = []
        self._link.invalidate()

    async def disconnect(self) -> None:
        """Tear down the session. The BLE link itself is the transport's
        concern — disconnect is idempotent."""
        self._set_live_callback(None)
        self._subscribed_ports = []
        await self._link.close()

    def _set_live_callback(self, callback: UpdateCallback | None) -> None:
        self._live_callback = callback
        self._link.set_change_handler(
            self._on_change_indication if callback is not None else None
        )

    # --- Property discovery -------------------------------------------------

    async def discover_products(self) -> list[str]:
        """List product IDs the device exposes.

        GetProds returns a JSON object keyed by product id (``"0"`` is
        firmware/OTA, ``"1"`` is the toothbrush functions on HX742X).
        """
        if not self._connected:
            raise TransportError("Condor session not established")
        status, data = await self._link.get_prods()
        if status != STATUS_OK:
            raise TransportError(
                f"GetProds failed: {status_name(status)}"
            )
        return list(data.keys()) if isinstance(data, dict) else []

    async def discover_ports(self, product_id: str) -> list[str]:
        """List port names for a product.

        GetPorts returns a JSON array. Port names are case-sensitive on
        the device (e.g. ``Sonicare``, ``RoutineStatus``, ``SensorData.b``)
        — lower-casing them produces empty GetProps bodies, not errors.
        """
        if not self._connected:
            raise TransportError("Condor session not established")
        status, data = await self._link.get_ports(product_id)
        if status != STATUS_OK:
            raise TransportError(
                f"GetPorts({product_id}) failed: {status_name(status)}"
            )
        if not isinstance(data, list):
            return []
        return [p for p in data if isinstance(p, str)]

    async def get_props(
        self, product_id: str, port: str, timeout: float = RESPONSE_TIMEOUT,
    ) -> dict[str, Any]:
        """Read all properties of a single port.

        Returns the parsed JSON object from the device. NoSuchPort /
        NoSuchProduct status raises ``TransportError``; unknown-port
        names silently return an empty dict, which is how the device
        reports a lower-cased port name.
        """
        if not self._connected:
            raise TransportError("Condor session not established")
        status, data = await self._link.get_props(product_id, port, timeout=timeout)
        if status != STATUS_OK:
            raise TransportError(
                f"GetProps({product_id}/{port}) failed: {status_name(status)}"
            )
        return data if isinstance(data, dict) else {}

    async def put_props(
        self, product_id: str, port: str, props: dict[str, Any],
    ) -> bool:
        """Write properties to a port. Returns True on a NoError response."""
        if not self._connected:
            raise TransportError("Condor session not established")
        status, _ = await self._link.put_props(product_id, port, props)
        if status != STATUS_OK:
            _LOGGER.debug(
                "Condor PutProps(%s/%s) status=%s", product_id, port, status_name(status),
            )
        return status == STATUS_OK

    # --- Change indications -------------------------------------------------

    def _on_change_indication(
        self, product_id: str, port: str, props: dict[str, Any] | bytes,
    ) -> None:
        """Map one change indication and fan the delta out to the callback.

        Binary ``*.b`` streaming ports never go through the JSON adapter:
        ``SensorData.b`` is decoded by ``map_sensor_frame``, other binary
        ports are noted at debug level and dropped.
        """
        if self._live_callback is None:
            return
        if isinstance(props, bytes):
            if port == "SensorData.b":
                delta = map_sensor_frame(props)
                if delta:
                    try:
                        self._live_callback(delta)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.exception("Condor sensor callback failed: %s", err)
                return
            _LOGGER.debug(
                "Condor ChangeInd binary port %s/%s (%dB) — skipped",
                product_id, port, len(props),
            )
            return
        delta = map_port_props(port, props)
        if not delta:
            return
        try:
            self._live_callback(delta)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Condor live callback failed: %s", err)

    # --- Live sensor stream (pressure / temperature) ----------------------
    # The pressure/temperature telemetry rides the ``SensorData.b`` binary
    # port. It only streams once the enable register on the ``SensorData``
    # JSON port is set, so enabling must precede the subscribe and the
    # register is cleared again on stop. Frames are decoded by
    # ``map_sensor_frame`` inside ``_on_change_indication`` and delivered
    # through the change-indication callback already registered by
    # ``start_live_updates`` — the ``callback`` argument (the Classic char
    # signature) is accepted for interface parity but unused here.

    async def start_sensor_stream(
        self, enable_mask: int, callback: object = None,
    ) -> bool:
        """Enable the sensor frontend and subscribe to its binary stream.

        Subscribe to ``SensorData.b`` *before* writing the enable register so
        no frames are emitted before we're listening — the enable write is
        best-effort (some firmwares stream regardless of a stale register),
        but a failed subscribe means no telemetry arrives. Returns True if
        the subscribe succeeded.
        """
        if not self._connected:
            return False
        try:
            ok = await self._subscribe_port("1", "SensorData.b")
        except TransportError as err:
            _LOGGER.warning("Condor SensorData.b subscribe failed: %s", err)
            return False
        try:
            await self.put_props("1", "SensorData", {"Sensors": enable_mask})
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Condor sensor enable write failed: %s", err)
        return ok

    async def stop_sensor_stream(self) -> None:
        """Unsubscribe the binary stream. Best-effort — a transport teardown
        may already have invalidated the subscription. The enable register is
        left as-is; the handle powers its sensor frontend down on its own when
        the session ends."""
        try:
            await self._unsubscribe_port("1", "SensorData.b")
        except Exception:  # noqa: BLE001
            pass

    # --- SonicareProtocol surface ------------------------------------------

    async def refresh_all(self) -> dict[str, Any]:
        """Discover products + ports and read every JSON port's state.

        Iterates both the firmware product (``0``) and the device product
        (``1``), skipping binary streaming ports (``*.b``). Per-port
        failures are logged and skipped so one flaky port never blocks
        the rest of the refresh. The result is a flat dict keyed by
        ``coordinator.data`` names, already merged across ports.
        """
        merged: dict[str, Any] = {}
        products = await self.discover_products()
        for prod in products:
            try:
                ports = await self.discover_ports(prod)
            except TransportError as err:
                _LOGGER.debug("Condor discover_ports(%s) failed: %s", prod, err)
                continue
            for port in ports:
                if port.endswith(".b"):
                    # Binary streams are subscribe-only; refresh_all sticks
                    # to JSON ports that carry named properties.
                    continue
                try:
                    props = await self.get_props(prod, port)
                except TransportError as err:
                    _LOGGER.debug(
                        "Condor get_props(%s/%s) failed: %s", prod, port, err,
                    )
                    continue
                if not props:
                    continue
                merged.update(map_port_props(port, props))
        return merged

    async def start_live_updates(self, on_update: UpdateCallback) -> None:
        """Subscribe to push updates for the default port set.

        Empirically on HX742X only ``Sonicare`` + ``RoutineStatus`` are
        chatty during sessions; ``Battery`` / ``BrushHead`` /
        ``SessionStorage`` fire only on actual change. Per-port subscribe
        failures are logged and skipped — a partially subscribed device
        is still useful.
        """
        if not self._connected:
            raise TransportError("Condor session not established")
        self._set_live_callback(on_update)
        subscribed: list[tuple[str, str]] = []
        for prod, port in DEFAULT_SUBSCRIBE_PORTS:
            try:
                ok = await self._subscribe_port(prod, port)
            except TransportError as err:
                _LOGGER.debug("Subscribe %s/%s raised: %s", prod, port, err)
                continue
            if ok:
                subscribed.append((prod, port))
            else:
                _LOGGER.info("Subscribe %s/%s declined by device", prod, port)
        self._subscribed_ports = subscribed
        _LOGGER.debug("Condor subscribed to %d port(s)", len(subscribed))

    async def stop_live_updates(self) -> None:
        """Unsubscribe every port that successfully subscribed.

        Failures are swallowed — a dropped link already tore the
        subscriptions down device-side, so a best-effort cleanup on our
        end is all that's needed.
        """
        self._set_live_callback(None)
        ports = self._subscribed_ports
        self._subscribed_ports = []
        # Skip the graceful unsubscribe when the link is already gone: the
        # writes would each time out (3 s) against a dead connection — 15 s of
        # stalled teardown that delays the reconnect — and the device already
        # dropped its subscriptions on disconnect. The link state is the
        # authoritative gate here; the session flag is already False on the
        # drop path, because the coordinator invalidates it the moment the
        # link goes away.
        if not self._connected or not self._link.link_up:
            return
        for prod, port in ports:
            try:
                await self._unsubscribe_port(prod, port)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Unsubscribe %s/%s failed: %s", prod, port, err)

    async def _subscribe_port(self, product_id: str, port: str) -> bool:
        """Subscribe one port. Returns True on ``NoError`` status."""
        status, _ = await self._link.subscribe(product_id, port)
        return status == STATUS_OK

    async def _unsubscribe_port(self, product_id: str, port: str) -> None:
        await self._link.unsubscribe(product_id, port)

    async def set_brushing_mode(self, mode_key: str) -> None:
        raise NotImplementedError("Condor PutProps — implemented in Phase 4")

    async def set_intensity(self, intensity_key: str) -> None:
        raise NotImplementedError("Condor PutProps — implemented in Phase 4")
