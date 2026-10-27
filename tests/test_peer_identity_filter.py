"""Bridge events reach the entry by mac or by the brush's identity.

A brush with a rotating address is bonded under one of its private
addresses, new with every pairing. Bridge firmware 1.15.0 adds the identity
the brush keeps as peer_identity whenever it differs from mac, so an entry
holding that identity keeps receiving the brush's events.
"""

from __future__ import annotations

import pytest

from custom_components.philips_sonicare_ble.transport import EspBridgeTransport

BOND = "77:F0:B2:D9:FD:17"
NEW_BOND = "50:DC:06:58:51:F8"
IDENTITY = "24:E5:AA:92:32:F9"


def _transport(address: str | None) -> EspBridgeTransport:
    transport = EspBridgeTransport.__new__(EspBridgeTransport)
    transport._detected_mac = address
    return transport


@pytest.mark.parametrize(
    ("ours", "event", "other"),
    [
        # Entry on the bond address (all entries so far).
        (BOND, {"mac": BOND}, False),
        (BOND, {"mac": BOND, "peer_identity": IDENTITY}, False),
        (BOND, {"mac": NEW_BOND, "peer_identity": IDENTITY}, True),
        # Entry on the identity: follows the brush across a new pairing.
        (IDENTITY, {"mac": BOND, "peer_identity": IDENTITY}, False),
        (IDENTITY, {"mac": NEW_BOND, "peer_identity": IDENTITY.lower()}, False),
        (IDENTITY, {"mac": BOND}, True),  # bridge before 1.15.0
        # Fixed address: the bridge leaves peer_identity out.
        (IDENTITY, {"mac": IDENTITY}, False),
        # Unpaired slot and foreign brushes never match.
        (IDENTITY, {"mac": "00:00:00:00:00:00"}, True),
        (IDENTITY, {"mac": BOND, "peer_identity": ""}, True),
        (IDENTITY, {"mac": BOND, "peer_identity": "11:22:33:44:55:66"}, True),
        # Events without a mac, or before we know ours, pass as before.
        (IDENTITY, {}, False),
        (None, {"mac": BOND}, False),
    ],
)
def test_is_other_brush(ours, event, other) -> None:
    assert _transport(ours)._is_other_brush(event) is other
