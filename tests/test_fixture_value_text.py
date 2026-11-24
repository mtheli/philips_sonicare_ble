"""value_text is set only for characteristics that hold text.

Captures used to fill value_text whenever the bytes happened to be printable,
so a battery level of 0x5a showed up as "Z". sonicare_scan.py and
lightblue_to_fixture.py now set it only for text characteristics, and the
fixtures follow the same rule.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from .conftest import FIXTURES_DIR, load_json_fixture

_SCRIPTS = Path(__file__).parent.parent / "scripts"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lightblue = _load_script("lightblue_to_fixture")

FIXTURES = sorted(p.name for p in FIXTURES_DIR.glob("*.json"))


def test_text_chars_match_scan_script() -> None:
    scan = _load_script("sonicare_scan")
    from_scan = {
        uuid for uuid, info in scan.KNOWN_CHARS.items() if info[2] is scan._dec_str
    }
    assert from_scan == lightblue.TEXT_CHARS
    from custom_components.philips_sonicare_ble import capture

    assert from_scan == capture.TEXT_CHARS


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        (b"HX6340   ", "HX6340   "),
        (b"OLED_1.0\x00\x00", "OLED_1.0"),
        (b"\x00", None),
        (b"", None),
    ],
)
def test_text_value_of(raw: bytes, text: str | None) -> None:
    assert lightblue.text_value_of(raw) == text


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_fixtures_follow_value_text_rule(fixture_name: str) -> None:
    snapshot = load_json_fixture(fixture_name)
    for service in snapshot.get("gatt_services") or []:
        for char in service["characteristics"]:
            if char.get("value_hex") is None or char["uuid"] not in lightblue.TEXT_CHARS:
                assert char.get("value_text") is None, char["uuid"]
            else:
                assert char.get("value_text") == lightblue.text_value_of(
                    bytes.fromhex(char["value_hex"])
                ), char["uuid"]
