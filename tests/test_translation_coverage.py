"""Structural guards for the translation files.

Two failure modes these catch, both of which render as broken UI rather
than as an exception:

* A step or abort reason the flow renders has no entry in strings.json —
  the dialog comes up blank.
* A translation drops or renames a placeholder — Home Assistant leaves
  the raw ``{name}`` in the text, or the step fails to format.

Also enforces the hassfest rule that translation values carry no HTML:
markup may only ride in placeholder *values*, which are built in
config_flow.py and never inspected by hassfest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "philips_sonicare_ble"
FLOW_SRC = (COMPONENT_DIR / "config_flow.py").read_text(encoding="utf-8")
STRINGS = json.loads((COMPONENT_DIR / "strings.json").read_text(encoding="utf-8"))
TRANSLATIONS = sorted((COMPONENT_DIR / "translations").glob("*.json"))

# Built by interpolation at the call site, so the literal scan below
# cannot see them.
DYNAMIC_STEPS = {
    "bluetooth_confirm_asleep",
    "bluetooth_confirm_asleep_proxy",
    "bluetooth_confirm_failed",
    "bluetooth_confirm_failed_proxy",
    "bluetooth_confirm_proxy",
    "bluetooth_confirm_proxy_local",
    "esp_bridge_status",
    "esp_bridge_status_connected",
    "esp_bridge_status_read_failed",
    "esp_bridge_status_read_error",
    "not_paired",
    "not_paired_hassio",
    "reset_bridge",
    "reset_bridge_offline",
    "show_capabilities",
    "show_capabilities_condor",
    "reset_bridge_unconfirmed",
}
DYNAMIC_ABORTS = {
    "already_configured_detail",
    "already_configured_disabled",
}


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


def _leaf_values(obj, prefix: str = ""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _leaf_values(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(obj, str):
        yield prefix, obj


def _form_steps() -> set[str]:
    """Steps rendered as a form — those need a description.

    Excludes ``next_step_id`` routers (no UI of their own) and steps that
    only ever show a progress spinner, whose text comes from
    ``config.progress`` instead.
    """
    rendered = set(re.findall(r'(?<!next_)step_id\s*=\s*"(\w+)"', FLOW_SRC))
    progress_only = set(
        re.findall(r'async_show_progress\(\s*step_id="(\w+)"', FLOW_SRC)
    )
    return (rendered - progress_only) | DYNAMIC_STEPS


def test_rendered_steps_are_defined() -> None:
    defined = set(STRINGS["config"]["step"]) | set(STRINGS["options"]["step"])
    used = _form_steps()
    assert used <= defined, f"steps rendered but not translated: {used - defined}"


def test_every_rendered_step_has_a_handler() -> None:
    """A form step without ``async_step_<id>`` raises on submit."""
    handlers = set(re.findall(r"async def async_step_(\w+)\(", FLOW_SRC))
    routers = set(re.findall(r'next_step_id="(\w+)"', FLOW_SRC))
    missing = (_form_steps() | routers) - handlers - set(STRINGS["options"]["step"])
    assert not missing, f"steps rendered but not handled: {missing}"


def test_every_defined_step_is_reachable() -> None:
    """The reverse check: a translated step with no handler is dead text.

    Catches a typo'd or renamed step id, which would otherwise surface as
    a blank dialog only when that branch is hit at runtime.
    """
    handlers = set(re.findall(r"async def async_step_(\w+)\(", FLOW_SRC))
    orphans = set(STRINGS["config"]["step"]) - handlers
    assert not orphans, f"translated steps with no handler: {orphans}"


def _renders(result) -> str:
    """Format a rendered step's text with the placeholders it supplied.

    This is what Home Assistant does to build the dialog: a placeholder
    the step's text uses but the handler does not supply raises KeyError
    here, and would surface as a broken dialog in the UI.
    """
    step = STRINGS["config"]["step"][result["step_id"]]
    values = result.get("description_placeholders") or {}
    return step["description"].format(**values)


async def test_rendered_dialogs_have_every_placeholder_they_use() -> None:
    """Render each step variant and format its text.

    Covers the variants introduced for translatable one-shot notices —
    their wording lives in the translations while the values stay in
    Python, so the two can drift apart silently.
    """
    from types import SimpleNamespace

    from custom_components.philips_sonicare_ble.config_flow import (
        PhilipsSonicareConfigFlow,
    )
    from custom_components.philips_sonicare_ble.transport import (
        UNPAIR_UNAVAILABLE,
        UNPAIR_UNCONFIRMED,
    )

    def _flow():
        flow = PhilipsSonicareConfigFlow()
        flow.flow_id = "t"
        flow.handler = "philips_sonicare_ble"
        flow.hass = SimpleNamespace(config=SimpleNamespace(components=set()))
        flow._address = "24:E5:AA:BE:9C:1B"
        flow._name = "Philips Sonicare"
        flow._esp_device_name = "atom-s3r"
        flow._esp_bridge_id = "sonicare_1"
        flow._slot_action_chosen = True
        flow._bridge_info = {
            "version": "1.10.0", "mac": "24:E5:AA:BE:9C:1B",
            "paired": "true", "ble_connected": "true", "pair_capable": "false",
            "identity_address": "24:E5:AA:BE:9C:1B",
        }
        return flow

    seen = set()

    # bridge status: plain, freshly paired, and both read failures
    for setup in (
        lambda f: None,
        lambda f: setattr(f, "_just_paired", True),
        lambda f: setattr(f, "_esp_read_error", "cannot_connect"),
        lambda f: setattr(f, "_esp_read_error", "unknown"),
    ):
        flow = _flow()
        setup(flow)
        result = await flow.async_step_esp_bridge_status()
        _renders(result)
        seen.add(result["step_id"])

    # pair-mode request, with and without the "bond removed" notice
    for just_unpaired in (False, True):
        flow = _flow()
        flow._bridge_info["pair_capable"] = "true"
        flow._just_unpaired = just_unpaired
        result = await flow.async_step_request_pair()
        _renders(result)
        seen.add(result["step_id"])

    # discovery confirm: every outcome against every carrier topology
    topologies = {
        "none": [],
        "local": [{"name": "hci0 (00:0A:CD:46:B2:2D)", "rssi": -60, "is_local": True}],
        "proxy": [{"name": "atom-lite", "rssi": -64, "is_local": False}],
        "proxy_local": [
            {"name": "atom-lite", "rssi": -64, "is_local": False},
            {"name": "hci0 (00:0A:CD:46:B2:2D)", "rssi": -82, "is_local": True},
        ],
    }
    import custom_components.philips_sonicare_ble.config_flow as cf

    original = cf.describe_available_paths
    try:
        for paths in topologies.values():
            cf.describe_available_paths = lambda hass, addr, _p=paths: list(_p)
            for outcome in ("", "asleep", "failed"):
                flow = _flow()
                flow._esp_redirect_checked = True
                flow._confirm_status = outcome
                result = await flow.async_step_bluetooth_confirm()
                _renders(result)
                seen.add(result["step_id"])
    finally:
        cf.describe_available_paths = original

    # pairing instructions: host terminal vs. the Supervised addon
    for components in (set(), {"hassio"}):
        flow = _flow()
        flow.hass = SimpleNamespace(config=SimpleNamespace(components=components))
        result = await flow.async_step_not_paired()
        _renders(result)
        seen.add(result["step_id"])

    # bond reset: the confirmation plus both failure outcomes
    flow = _flow()
    result = await flow.async_step_reset_bridge()
    _renders(result)
    seen.add(result["step_id"])
    for outcome in (UNPAIR_UNAVAILABLE, UNPAIR_UNCONFIRMED):
        flow = _flow()
        flow._unpair_outcome = outcome
        result = await flow.async_step_reset_finish()
        _renders(result)
        seen.add(result["step_id"])

    # capabilities summary — the device-info table lives in the text now
    flow = _flow()
    flow._fetched_data = {
        "model": "HX9911/09", "serial": "ABC123", "firmware": "2.0",
        "battery": 80, "pairing": "bonded", "services": [],
        "connection_path": "atom-s3r / sonicare_1",
    }
    flow._build_default_name = lambda: "Master Bath"
    result = await flow.async_step_show_capabilities()
    _renders(result)
    seen.add(result["step_id"])

    # ... and the Condor variant, which explains the absent Classic services
    from custom_components.philips_sonicare_ble.const import SVC_CONDOR

    flow = _flow()
    flow._fetched_data = {
        "model": "HX9911/09", "serial": "ABC123", "firmware": "2.0",
        "battery": 80, "pairing": "bonded", "services": [SVC_CONDOR],
        "connection_path": "atom-s3r / sonicare_1",
    }
    flow._build_default_name = lambda: "Master Bath"
    result = await flow.async_step_show_capabilities()
    _renders(result)
    seen.add(result["step_id"])

    assert {
        "esp_bridge_status_connected", "esp_bridge_status_paired",
        "esp_bridge_status_read_failed", "esp_bridge_status_read_error",
        "request_pair", "request_pair_after_reset",
        "bluetooth_confirm", "bluetooth_confirm_asleep",
        "bluetooth_confirm_failed", "bluetooth_confirm_proxy",
        "bluetooth_confirm_proxy_local", "bluetooth_confirm_asleep_proxy",
        "bluetooth_confirm_failed_proxy", "not_paired", "not_paired_hassio",
        "reset_bridge", "reset_bridge_offline", "reset_bridge_unconfirmed",
        "show_capabilities", "show_capabilities_condor",
    } <= seen, seen


def test_progress_actions_are_defined() -> None:
    used = set(re.findall(r'progress_action="(\w+)"', FLOW_SRC))
    defined = set(STRINGS["config"]["progress"])
    assert used <= defined, f"progress texts missing: {used - defined}"


def test_abort_reasons_are_defined() -> None:
    used = (
        set(re.findall(r'async_abort\(reason="(\w+)"', FLOW_SRC))
        | set(re.findall(r'AbortFlow\(\s*"(\w+)"', FLOW_SRC))
        | DYNAMIC_ABORTS
    )
    defined = set(STRINGS["config"]["abort"])
    assert used <= defined, f"aborts raised but not translated: {used - defined}"


def test_translations_keep_every_placeholder() -> None:
    """A translated string must use exactly the placeholders of its source.

    A dropped placeholder silently hides a MAC or a device name; an added
    one raises KeyError when the step renders.
    """
    source = dict(_leaf_values(STRINGS))
    for path in TRANSLATIONS:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in _leaf_values(data):
            assert _placeholders(value) == _placeholders(source[key]), (
                f"{path.name}: placeholder mismatch at {key}"
            )


def test_no_html_in_translation_values() -> None:
    """hassfest rejects HTML in translation values (release blocker)."""
    for path in [COMPONENT_DIR / "strings.json", *TRANSLATIONS]:
        data = json.loads(path.read_text(encoding="utf-8"))
        offenders = [
            key for key, value in _leaf_values(data)
            if re.search(r"</?[a-zA-Z][^>]*>", value)
        ]
        assert not offenders, f"{path.name}: HTML in {offenders}"
