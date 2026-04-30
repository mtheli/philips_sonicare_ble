#!/usr/bin/env python3
"""Sync custom_components/<domain>/translations/en.json from strings.json.

Home Assistant loads `translations/en.json` for the English UI; `strings.json`
is the source-of-truth that hassfest validates. Keep them identical so that
config-flow text changes don't get half-applied.

Usage:
    python3 scripts/sync_translations.py            # write en.json
    python3 scripts/sync_translations.py --check    # fail if out of sync
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO / "custom_components" / "philips_sonicare_ble"
STRINGS = COMPONENT_DIR / "strings.json"
EN_JSON = COMPONENT_DIR / "translations" / "en.json"


def canonical(path: Path) -> str:
    return json.dumps(json.loads(path.read_text()), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if en.json is out of sync (no write)",
    )
    args = parser.parse_args()

    source = canonical(STRINGS)

    if args.check:
        if not EN_JSON.exists():
            print(f"::error::{EN_JSON} missing — run scripts/sync_translations.py")
            return 1
        if canonical(EN_JSON) != source:
            print(
                f"::error::{EN_JSON} out of sync with {STRINGS} — "
                "run scripts/sync_translations.py",
                file=sys.stderr,
            )
            return 1
        print(f"OK {EN_JSON.relative_to(REPO)} matches {STRINGS.relative_to(REPO)}")
        return 0

    EN_JSON.parent.mkdir(parents=True, exist_ok=True)
    EN_JSON.write_text(source)
    print(f"wrote {EN_JSON.relative_to(REPO)} ({len(source)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
