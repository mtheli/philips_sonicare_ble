"""The pacing steps of a routine, as the handle actually buzzes them.

`current_sector` already walks the mode sequences; this covers the other
half of the same knowledge - how many steps a routine takes and how long
one lasts - which a display needs to draw the routine as segments without
knowing the sequences itself.
"""

import pytest

from custom_components.philips_sonicare_ble.condor_adapter import (
    CONDOR_BRUSHING_MODES,
)
from custom_components.philips_sonicare_ble.const import (
    BRUSHING_MODES,
    MODE_SECTOR_SEQUENCES,
    current_sector,
    sector_sequence,
    sector_step_seconds,
)


@pytest.mark.parametrize(
    "mode,routine,expected",
    [
        # Six zones, six steps: the plain modes, where a segment per zone
        # was right all along.
        ("clean", 120, [20.0] * 6),
        ("sensitive", 120, [20.0] * 6),
        ("deep_clean", 180, [30.0] * 6),
        # More steps than zones, because these revisit: this is where a
        # segment per zone lands the boundaries in the wrong place.
        ("white", 160, [20.0] * 8),
        ("gum_care", 200, [20.0] * 10),
        # Classic labels for the same two routines.
        ("white_plus", 160, [20.0] * 8),
        ("gum_health", 200, [20.0] * 10),
    ],
)
def test_step_seconds_premium(mode, routine, expected):
    assert sector_step_seconds("HX742X", mode, routine) == expected


def test_steps_line_up_with_the_reported_sector():
    """Each step boundary is where `current_sector` changes its answer.

    Gum Care buzzes ten times over 200 s; the card's old six-segment split
    put a boundary at 33 s, where the handle is a third into its second
    step.
    """
    steps = sector_step_seconds("HX742X", "gum_care", 200)
    assert steps is not None
    elapsed = 0.0
    seen = []
    for step in steps:
        seen.append(current_sector("HX742X", "gum_care", elapsed + 1, 200))
        elapsed += step
    assert seen == MODE_SECTOR_SEQUENCES["gum_care"]


def test_tongue_care_is_one_step():
    # It has no sectors, and it never moves the user on - so it is one
    # undivided stretch, not six. Saying nothing here would send a consumer
    # back to the zone count and draw five boundaries the handle does not
    # have.
    assert sector_step_seconds("HX742X", "tongue_care", 60) == [60.0]


def test_kids_ignores_the_sequences():
    assert sector_step_seconds("HX6340", "gum_care", 120) == [30.0] * 4


def test_unknown_mode_falls_back_to_the_zone_count():
    assert sector_step_seconds("HX742X", "cocoa", 120) == [20.0] * 6
    assert sector_step_seconds("HX742X", None, 120) == [20.0] * 6


@pytest.mark.parametrize("routine", [None, 0, -1])
def test_no_routine_length_no_steps(routine):
    assert sector_step_seconds("HX742X", "clean", routine) is None


def test_every_known_mode_is_covered():
    """The alias trap from v0.18.1, guarded for this consumer too.

    A mode label that reaches the table but is missing from it silently
    falls back to the uniform split - which is exactly the bug this
    function exists to prevent.
    """
    for mode in set(BRUSHING_MODES.values()) | set(CONDOR_BRUSHING_MODES.values()):
        seq = MODE_SECTOR_SEQUENCES.get(mode)
        assert seq is not None, f"{mode} missing from MODE_SECTOR_SEQUENCES"
        steps = sector_step_seconds("HX742X", mode, 120)
        if not seq:
            assert steps == [120.0], "a sectorless routine is one step"
        else:
            assert steps is not None and len(steps) == len(seq)


@pytest.mark.parametrize(
    "model,mode,expected",
    [
        ("HX742X", "clean", [1, 2, 3, 4, 5, 6]),
        ("HX742X", "white", [1, 2, 3, 4, 5, 6, 2, 5]),
        ("HX742X", "gum_care", [1, 2, 3, 4, 5, 6, 1, 3, 4, 6]),
        ("HX742X", "gum_health", [1, 2, 3, 4, 5, 6, 1, 3, 4, 6]),
        # No zones at all, but still one step - the two lists differ in
        # length here, which is why both are published.
        ("HX742X", "tongue_care", []),
        # Kids sweeps its four zones once, whatever the mode is called.
        ("HX6340", "gum_care", [1, 2, 3, 4]),
    ],
)
def test_sequence(model, mode, expected):
    assert sector_sequence(model, mode) == expected


def test_unknown_mode_has_no_sequence():
    # Not a guessed 1..6: the steps fall back to a uniform split because a
    # bar has to be drawn somehow, but claiming to know the order would be
    # an invention.
    assert sector_sequence("HX742X", "cocoa") is None
    assert sector_sequence("HX742X", None) is None
    assert sector_step_seconds("HX742X", "cocoa", 120) == [20.0] * 6


def test_sequence_and_steps_agree_where_there_are_zones():
    for mode in ("clean", "white", "gum_care", "deep_clean", "sensitive"):
        seq = sector_sequence("HX742X", mode)
        steps = sector_step_seconds("HX742X", mode, 120)
        assert seq and steps and len(seq) == len(steps)


def test_a_record_carries_the_shape_of_the_routine_that_ran():
    """The recap outlives the session, and the handle moves on.

    Without this the pacing would be read from whatever mode is set when
    somebody looks - a Gum Health session divided into Clean's six steps.
    """
    from types import SimpleNamespace

    from custom_components.philips_sonicare_ble.sensor import (
        SonicareLastSessionSensor,
    )

    record = {
        "started_at": "2026-08-22T15:34:16+00:00",
        "duration": 200,
        "routine_length": 200,
        "brushing_mode": "gum_care",
        "source": "observed",
        "time_source": "session_end",
    }
    sensor = SonicareLastSessionSensor.__new__(SonicareLastSessionSensor)
    sensor.coordinator = SimpleNamespace(data={"last_session": record})
    sensor._model = "HX742X"
    attributes = sensor.extra_state_attributes
    assert attributes["sector_sequence"] == [1, 2, 3, 4, 5, 6, 1, 3, 4, 6]
    assert attributes["step_times_seconds"] == [20.0] * 10
    # The fields it already had are untouched.
    assert attributes["duration_seconds"] == 200
    assert attributes["mode"] == "gum_care"


def test_a_record_without_a_known_mode_claims_no_shape():
    from types import SimpleNamespace

    from custom_components.philips_sonicare_ble.sensor import (
        SonicareLastSessionSensor,
    )

    sensor = SonicareLastSessionSensor.__new__(SonicareLastSessionSensor)
    sensor.coordinator = SimpleNamespace(
        data={"last_session": {"duration": 90, "routine_length": 120}}
    )
    attributes = sensor.extra_state_attributes
    # No order to claim - inventing one would put zones in the recap that
    # were never announced.
    assert "sector_sequence" not in attributes
    # The lengths are still there, evenly split: a bar has to be drawn
    # somehow, and six equal steps is what every mode without a sequence
    # does anyway.
    assert attributes["step_times_seconds"] == [20.0] * 6
