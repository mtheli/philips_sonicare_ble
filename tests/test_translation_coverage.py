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

import ast
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
    "esp_bridge_status",
    "not_paired",
    "reset_bridge",
    "show_capabilities",
}

# Text blocks the flow selects by interpolating an outcome into the name,
# so the literal scan below cannot see them.
DYNAMIC_BLOCKS = {
    "confirm_alert_asleep",
    "confirm_alert_failed",
    "confirm_warn_proxy",
    "confirm_warn_proxy_local",
}
# Selected at runtime through errors[] / an error_key, never by literal
# name — so the dead-block check below cannot see them.
ERROR_KEYS = {
    "cannot_connect",
    "not_a_sonicare",
    "pairing_failed",
    "pair_timeout",
    "unknown",
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
        "esp_bridge_status", "request_pair", "bluetooth_confirm",
        "not_paired", "reset_bridge", "show_capabilities",
    } <= seen, seen


def test_step_variants_keep_their_shared_block_identical() -> None:
    """Variants of one dialog must repeat their common text verbatim.

    A dialog without input fields cannot show ``errors["base"]``: the
    frontend only renders that inside ``ha-form``, which it skips when
    ``data_schema`` is empty. Notices therefore live in each variant's
    own description, which means the shared part is duplicated — per
    variant and per language. This pins that duplication so an edit to
    one variant cannot silently leave the others behind.
    """
    families = {}
    for path in [COMPONENT_DIR / "strings.json", *TRANSLATIONS]:
        steps = json.loads(path.read_text(encoding="utf-8"))["config"]["step"]
        for family, marker in families.items():
            variants = {
                key: text["description"] for key, text in steps.items()
                if key.startswith(family)
            }
            # The shared block runs from the marker to the end of the text,
            # minus the trailing call to action, which legitimately differs.
            blocks = {
                key: desc[desc.index(marker):]
                for key, desc in variants.items() if marker in desc
            }
            assert len(blocks) > 1, f"{path.name}: {family} lost its variants"
            shared = {b[:b.index("\n\n", 1)] if "\n\n" in b[1:] else b
                      for b in blocks.values()}
            assert len(shared) == 1, (
                f"{path.name}: {family} variants drifted apart: {sorted(blocks)}"
            )


def _requested_text_blocks() -> set[str]:
    """Block names the flow references as string literals."""
    return {
        name for name in STRINGS["config"]["error"]
        if f'"error.{name}"' in FLOW_SRC
    } | DYNAMIC_BLOCKS


def test_requested_text_blocks_are_defined() -> None:
    """Every fragment the flow injects must exist in every language.

    These replace the per-outcome step variants this dialog used to
    carry: a name the flow asks for but no translation defines would
    silently render as an empty sentence rather than raise.
    """
    for path in [COMPONENT_DIR / "strings.json", *TRANSLATIONS]:
        defined = set(
            json.loads(path.read_text(encoding="utf-8"))["config"]["error"]
        )
        missing = _requested_text_blocks() - defined
        assert not missing, f"{path.name}: text blocks missing: {missing}"


def test_no_dead_text_blocks() -> None:
    """The reverse check — a fragment nothing asks for is dead weight."""
    # config.error holds two kinds of string: the classic error keys, which
    # the flow selects by value at runtime, and the blocks it injects by
    # name. Only the latter can be checked for being unused.
    orphans = (
        set(STRINGS["config"]["error"]) - ERROR_KEYS - _requested_text_blocks()
    )
    assert not orphans, f"text blocks defined but never used: {orphans}"


async def test_user_language_prefers_the_profile_over_the_server(
    monkeypatch,
) -> None:
    """The dialog is rendered in the user's language, not the server's.

    Every render a user sees runs inside a request (the flow view re-runs
    the step on each GET), so the authenticated user — and with them the
    profile language the frontend stores — is reachable. Each failure
    along that chain has to fall back on the server language rather than
    raise, since a broken lookup must never break a dialog.
    """
    from types import SimpleNamespace

    import custom_components.philips_sonicare_ble.config_flow as cf

    hass = SimpleNamespace(config=SimpleNamespace(language="de"))

    class _Request(dict):
        """Stand-in for aiohttp's request: mapping plus headers."""

        def __init__(self, user=None, accept=None):
            super().__init__({"hass_user": user} if user is not None else {})
            self.headers = {"Accept-Language": accept} if accept else {}

    def _set_request(user=SimpleNamespace(id="u1"), accept=None):
        """Put a request in context the way the http middleware does."""
        tokens.append(cf.ha_http.current_request.set(_Request(user, accept)))

    def _store(data):
        async def _async_user_store(hass_arg, user_id):
            assert user_id == "u1"
            return SimpleNamespace(data=data)

        monkeypatch.setattr(
            "homeassistant.components.frontend.storage.async_user_store",
            _async_user_store,
        )

    tokens: list = []
    try:
        # No request in context (background init) → server language.
        assert await cf._async_user_language(hass) == "de"

        # Request with a user whose profile language is set → that language.
        _set_request()
        _store({"language": {"language": "en"}})
        assert await cf._async_user_language(hass) == "en"

        # Profile exists but holds no language → server language.
        _store({})
        assert await cf._async_user_language(hass) == "de"
        _store({"language": None})
        assert await cf._async_user_language(hass) == "de"

        # The store blowing up must not escape.
        async def _boom(hass_arg, user_id):
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(
            "homeassistant.components.frontend.storage.async_user_store", _boom
        )
        assert await cf._async_user_language(hass) == "de"

        # Unauthenticated request → server language.
        _set_request(user=None)
        assert await cf._async_user_language(hass) == "de"

        # No stored preference, but the browser tells us what it renders in.
        _store({})
        _set_request(accept="en-GB,en;q=0.9,de;q=0.8")
        assert await cf._async_user_language(hass) == "en"

        # A language we do not ship falls through to the server's.
        _set_request(accept="fr-FR,fr;q=0.9")
        assert await cf._async_user_language(hass) == "de"
    finally:
        for token in reversed(tokens):
            cf.ha_http.current_request.reset(token)


def test_accept_language_ranking() -> None:
    """Quality values decide, and regional tags resolve like HA does."""
    import custom_components.philips_sonicare_ble.config_flow as cf

    parse = cf._language_from_accept_header

    assert parse(None) is None
    assert parse("") is None
    assert parse("*") is None
    # Plain regional tag collapses onto the base language we ship.
    assert parse("de-AT") == "de"
    # Lower-quality entry loses even though it comes first.
    assert parse("de;q=0.5,en;q=0.9") == "en"
    # Equal quality keeps the order the browser sent.
    assert parse("de,en") == "de"
    assert parse("en,de") == "en"
    # Unshipped languages are skipped rather than matched loosely.
    assert parse("fr-FR,fr;q=0.9,de;q=0.1") == "de"
    assert parse("fr,es") is None
    # Malformed quality must not raise.
    assert parse("de;q=notanumber,en") == "en"


def test_shipped_languages_constant_matches_translation_files() -> None:
    """The flow trusts a constant instead of scanning the filesystem."""
    import custom_components.philips_sonicare_ble.config_flow as cf

    on_disk = {path.stem for path in TRANSLATIONS}
    assert set(cf._TRANSLATED_LANGUAGES) == on_disk, (
        "add the new language to _TRANSLATED_LANGUAGES in config_flow.py"
    )


async def test_text_blocks_resolve_through_the_translation_cache(
    monkeypatch, unpatched_text_blocks,
) -> None:
    """The real lookup: strip HA's ``component.<domain>.<category>.`` prefix.

    The shared fixture hands the blocks to the flow directly so the step
    tests stay independent of HA's translation machinery — which leaves
    this one covering the resolver itself, including the fallback when
    translations cannot be loaded at all.
    """
    from types import SimpleNamespace

    import custom_components.philips_sonicare_ble.config_flow as cf

    hass = SimpleNamespace(config=SimpleNamespace(language="de"))
    seen: dict[str, object] = {}

    async def _translations(hass_arg, language, category, integrations=None,
                            config_flow=None):
        seen.update(language=language, category=category, integrations=integrations)
        return {
            "component.philips_sonicare_ble.config.error.esp_status_paired": "Fertig.",
            "component.other_integration.config.error.ignored": "nope",
        }

    monkeypatch.setattr(cf, "async_get_translations", _translations)
    assert await unpatched_text_blocks(hass) == {
        "error.esp_status_paired": "Fertig."
    }
    assert seen == {
        "language": "de", "category": "config",
        "integrations": ["philips_sonicare_ble"],
    }

    async def _boom(*args, **kwargs):
        raise RuntimeError("translations unavailable")

    monkeypatch.setattr(cf, "async_get_translations", _boom)
    assert await unpatched_text_blocks(hass) == {}


def test_no_errors_on_a_form_without_fields() -> None:
    """``errors`` never reaches the user on a form that has no fields.

    The frontend renders backend errors only inside ``ha-form``, and
    skips that element when ``data_schema`` is empty or absent (a change
    in HA 2026.6 — before that the form was always rendered). Setting
    ``errors`` there fails silently: the flow returns the same dialog
    with no hint why, which is exactly what it looks like when nothing
    happened at all.

    Such a step has to carry its reason in the description instead. This
    walks the flow source rather than matching text, so a new occurrence
    cannot slip in unnoticed.
    """
    tree = ast.parse(FLOW_SRC)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "async_show_form":
            continue
        kwargs = {k.arg: k.value for k in node.keywords}
        if "errors" not in kwargs:
            continue
        schema = kwargs.get("data_schema")
        if schema is None:
            offenders.append((node.lineno, "no data_schema"))
        elif _is_empty_schema(schema):
            offenders.append((node.lineno, "empty data_schema"))
    assert not offenders, (
        "errors set on a field-less form (invisible to the user): "
        + ", ".join(f"config_flow.py:{line} ({why})" for line, why in offenders)
    )


def _is_empty_schema(node) -> bool:
    """True for ``vol.Schema({})`` and a bare ``{}``; None-safe."""
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "Schema" and node.args:
            inner = node.args[0]
            return isinstance(inner, ast.Dict) and not inner.keys
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    return isinstance(node, ast.Dict) and not node.keys


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
