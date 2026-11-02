"""The level limits in rtse/limits.py: where each number comes from, and the fallbacks when the game cannot be asked."""

from __future__ import annotations

from types import SimpleNamespace

from rtse import limits


def gear_pc(character=80, overpower=10):
    return SimpleNamespace(
        GetMaximumPossiblePlayerLevelCap=lambda: character,
        GetMaxOverpowerLevel=lambda: overpower,
    )


def test_gear_cap_is_character_cap_plus_overpower_levels():
    gear = limits.gear_level_cap(gear_pc(80, 10))

    assert gear == {
        "min": 1,
        "max": 90,
        "character_max": 80,
        "overpower_max": 10,
        "sources": {
            "character_max": "pc.GetMaximumPossiblePlayerLevelCap()",
            "overpower_max": "pc.GetMaxOverpowerLevel()",
        },
    }


def test_no_player_controller_uses_the_marked_fallbacks():
    gear = limits.gear_level_cap(None)

    assert (gear["max"], gear["character_max"], gear["overpower_max"]) == (90, 80, 10)
    assert gear["sources"] == {"character_max": "fallback (unverified)", "overpower_max": "fallback (unverified)"}


def test_a_call_that_raises_falls_back_for_that_number_only():
    pc = SimpleNamespace(
        GetMaximumPossiblePlayerLevelCap=lambda: (_ for _ in ()).throw(RuntimeError("no")),
        GetMaxOverpowerLevel=lambda: 0,
    )

    gear = limits.gear_level_cap(pc)

    assert gear["character_max"] == limits.FALLBACK_CHARACTER_MAX
    assert gear["overpower_max"] == 0
    assert gear["max"] == limits.FALLBACK_CHARACTER_MAX


def test_an_insane_value_is_treated_as_a_failed_read():
    gear = limits.gear_level_cap(gear_pc(character=500, overpower=10))

    assert gear["character_max"] == limits.FALLBACK_CHARACTER_MAX
    assert gear["sources"]["character_max"] == "fallback (unverified)"


def test_out_parameter_tuples_return_their_first_value():
    pc = SimpleNamespace(GetMaximumPossiblePlayerLevelCap=lambda: (50,), GetMaxOverpowerLevel=lambda: (10, "extra"))

    assert limits.gear_level_cap(pc)["max"] == 60


def test_overpower_is_the_gear_level_above_the_character_cap():
    assert limits.overpower_of(88, 80) == 8
    assert limits.overpower_of(90, 80) == 10
    assert limits.overpower_of(75, 80) == 0  # Ordinary gear
    assert limits.overpower_of(80, 80) == 0
