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


async def test_hassio_help_points_at_ssh_addon() -> None:
    """Supervised installs are pointed at the addon, hosts at a shell.

    Both wordings are translated text blocks injected into one step —
    only where to find a terminal differs, the walkthrough is shared.
    """
    flow = _flow()
    flow.hass = SimpleNamespace(config=SimpleNamespace(components={"hassio"}))
    result = await flow.async_step_not_paired()
    assert result["step_id"] == "not_paired"
    assert "Terminal & SSH" in result["description_placeholders"]["terminal"]


async def test_host_install_gets_plain_terminal_step() -> None:
    result = await _flow().async_step_not_paired()
    assert result["step_id"] == "not_paired"
    assert "terminal on the machine" in (
        result["description_placeholders"]["terminal"]
    )
