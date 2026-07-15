"""Tests for the not_paired step description placeholders.

Issue #25 follow-up: surface the actual pairing-failure reason in the
not_paired step instead of a generic instruction wall. ``pair_error`` is
empty when no auto-pair was attempted (e.g. a bond-gated profile) and a
formatted note when ``_try_auto_pair`` recorded a reason.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.philips_sonicare_ble.config_flow import (
    PhilipsSonicareConfigFlow,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _flow() -> PhilipsSonicareConfigFlow:
    flow = PhilipsSonicareConfigFlow()
    flow._address = ADDRESS
    # _is_hassio only reads hass.config.components
    flow.hass = SimpleNamespace(config=SimpleNamespace(components=set()))
    return flow


def test_pair_error_empty_when_no_reason() -> None:
    placeholders = _flow()._not_paired_placeholders()
    assert placeholders["pair_error"] == ""
    # The static template always needs every key present.
    assert placeholders["address"] == ADDRESS
    assert ADDRESS in placeholders["pair_cmd"]


def test_pair_error_rendered_when_reason_recorded() -> None:
    flow = _flow()
    flow._pair_error = "Pairing timed out after 30s"
    placeholders = flow._not_paired_placeholders()
    assert "Pairing timed out after 30s" in placeholders["pair_error"]
    assert placeholders["pair_error"].endswith("\n\n")


def test_hassio_help_points_at_ssh_addon() -> None:
    flow = _flow()
    flow.hass = SimpleNamespace(config=SimpleNamespace(components={"hassio"}))
    assert "Terminal & SSH" in flow._not_paired_placeholders()["pairing_help"]
