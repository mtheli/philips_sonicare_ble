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
    "bluetooth_confirm_failed",
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
