#!/usr/bin/env python3
"""Sync and validate custom_components/philips_sonicare_ble/translations/*.json.

Home Assistant loads `translations/en.json` for the English UI; `strings.json`
is the source-of-truth that hassfest validates. Other languages live next to
en.json (e.g. de.json) and must have the same key structure as strings.json
so that no UI label silently falls back to English.

Usage:
    python3 scripts/sync_translations.py            # write en.json
    python3 scripts/sync_translations.py --check    # CI: fail on any drift

Check semantics:
- en.json must be byte-identical (modulo canonical formatting) to strings.json.
  Auto-fix: re-run without --check.
- Every other translations/*.json must have exactly the same key paths as
  strings.json (values may differ — those are the translations). Missing or
  extra keys fail the check; the maintainer has to add/remove the
  translation manually.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO / "custom_components" / "philips_sonicare_ble"
STRINGS = COMPONENT_DIR / "strings.json"
TRANSLATIONS_DIR = COMPONENT_DIR / "translations"
EN_JSON = TRANSLATIONS_DIR / "en.json"


def canonical(path: Path) -> str:
    return json.dumps(json.loads(path.read_text()), indent=2, ensure_ascii=False) + "\n"


def collect_key_paths(obj, prefix: str = "") -> set[str]:
    """Return the set of leaf paths in a nested JSON structure.

    Lists are indexed; dicts use dotted keys. Used to compare structure
    between strings.json and translation files.
    """
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                out.update(collect_key_paths(v, kp))
            else:
                out.add(kp)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            out.update(collect_key_paths(item, f"{prefix}[{i}]"))
    else:
        out.add(prefix)
    return out


def check_en_json(strings_canonical: str) -> list[str]:
    """Strict canonical match: en.json must equal strings.json."""
    if not EN_JSON.exists():
        return [f"{EN_JSON.relative_to(REPO)} missing — "
                f"run scripts/sync_translations.py"]
    if canonical(EN_JSON) != strings_canonical:
        return [f"{EN_JSON.relative_to(REPO)} out of sync with "
                f"{STRINGS.relative_to(REPO)} — run scripts/sync_translations.py"]
    return []


def check_other_language(lang_path: Path, strings_keys: set[str]) -> list[str]:
    """Structural key-parity check for non-en translation files."""
    try:
        lang_data = json.loads(lang_path.read_text())
    except json.JSONDecodeError as exc:
        return [f"{lang_path.relative_to(REPO)} is not valid JSON: {exc}"]

    lang_keys = collect_key_paths(lang_data)
    missing = strings_keys - lang_keys
    extra = lang_keys - strings_keys
    errors: list[str] = []
    rel = lang_path.relative_to(REPO)
    if missing:
        sample = ", ".join(sorted(missing)[:5])
        more = f" (+{len(missing)-5} more)" if len(missing) > 5 else ""
        errors.append(
            f"{rel} is missing {len(missing)} key(s) from "
            f"{STRINGS.relative_to(REPO)}: {sample}{more}"
        )
    if extra:
        sample = ", ".join(sorted(extra)[:5])
        more = f" (+{len(extra)-5} more)" if len(extra) > 5 else ""
        errors.append(
            f"{rel} has {len(extra)} extra key(s) not in "
            f"{STRINGS.relative_to(REPO)}: {sample}{more}"
        )
    return errors


def run_check() -> int:
    strings_canonical = canonical(STRINGS)
    strings_keys = collect_key_paths(json.loads(STRINGS.read_text()))

    all_errors: list[str] = []
    all_errors.extend(check_en_json(strings_canonical))

    for lang_path in sorted(TRANSLATIONS_DIR.glob("*.json")):
        if lang_path == EN_JSON:
            continue
        all_errors.extend(check_other_language(lang_path, strings_keys))

    if all_errors:
        for err in all_errors:
            print(f"::error::{err}", file=sys.stderr)
        return 1

    files = sorted(TRANSLATIONS_DIR.glob("*.json"))
    print(
        f"OK {len(files)} translation file(s) match "
        f"{STRINGS.relative_to(REPO)}: "
        f"{', '.join(p.name for p in files)}"
    )
    return 0


def run_sync() -> int:
    source = canonical(STRINGS)
    EN_JSON.parent.mkdir(parents=True, exist_ok=True)
    EN_JSON.write_text(source)
    print(f"wrote {EN_JSON.relative_to(REPO)} ({len(source)} bytes)")

    # Other languages: report drift, don't auto-fix (we can't translate).
    strings_keys = collect_key_paths(json.loads(STRINGS.read_text()))
    drifted: list[str] = []
    for lang_path in sorted(TRANSLATIONS_DIR.glob("*.json")):
        if lang_path == EN_JSON:
            continue
        errors = check_other_language(lang_path, strings_keys)
        drifted.extend(errors)
    if drifted:
        print()
        print("Translation files needing manual update:", file=sys.stderr)
        for err in drifted:
            print(f"  - {err}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any translation file is out of sync (no write)",
    )
    args = parser.parse_args()
    return run_check() if args.check else run_sync()


if __name__ == "__main__":
    sys.exit(main())
