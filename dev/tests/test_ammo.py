"""rtse/ammo.py against fake resource pools: exact matching by the game's pool definitions, the word fallback marked
unverified, the maximum read with bWantBaseValue=False, and the write path.

    python -I -m pytest dev/tests -k ammo
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from unrealsdk import unreal

from rtse import ammo


class Obj(unreal.UObject):
    """A fake game object: a UObject stand-in with a path name, a Class with no fields, and whatever attributes given."""

    def __init__(self, path: str, **attrs) -> None:
        self._path = path
        self.Class = attrs.pop("Class", SimpleNamespace(_fields=lambda: [], Name="Object"))
        self.__dict__.update(attrs)

    def _path_name(self) -> str:
        return self._path


def make_pool(definition: str | None, value: float = 100.0, maximum: float = 300.0, calls: list | None = None):
    """A fake AmmoResourcePool. `definition` is the Definition's object path (None = no Definition)."""
    state = {"value": value}
    max_calls = calls if calls is not None else []

    def get_max(*args):
        max_calls.append(args)
        return maximum

    def set_value(new: float) -> None:
        state["value"] = new

    return Obj(
        f"pool:{definition}",
        Class=SimpleNamespace(_fields=lambda: [], Name="AmmoResourcePool"),
        Definition=None if definition is None else Obj(definition),
        GetCurrentValue=lambda: state["value"],
        SetCurrentValue=set_value,
        GetMaxValue=get_max,
    )


def give_pools(game, *pools) -> None:
    game.pc = SimpleNamespace(ResourcePoolManager=SimpleNamespace(ResourcePools=list(pools)))


@pytest.fixture(autouse=True)
def _tmp_debug(monkeypatch, tmp_path):
    monkeypatch.setattr(ammo, "DEBUG_AMMO_FILE", tmp_path / "debug_ammo.json")


def test_the_seven_definitions_are_the_seven_ammo_kinds():
    assert sorted(key for key, _label in ammo.POOL_DEFINITIONS.values()) == sorted(ammo.ORDER)
    assert all(path.startswith("D_Resourcepools.AmmoPools.Ammo_") for path in ammo.POOL_DEFINITIONS)


def test_exact_definition_match_is_settled(game):
    give_pools(game, make_pool("D_Resourcepools.AmmoPools.Ammo_Repeater_Pistol_Pool", value=150, maximum=300))
    state = ammo.read()
    assert [(p["key"], p["label"], p["matched"], p["value"], p["max"]) for p in state["pools"]] == [("pistol", "Pistol", "definition", 150, 300)]
    assert state["unverified"] == {}


def test_word_fallback_is_marked_unverified(game):
    """A pool whose definition isn't in the table is matched by the words in its names: that is a guess."""
    give_pools(game, make_pool("D_Resourcepools.AmmoPools.Ammo_Sniper_Thing_Pool"))
    state = ammo.read()
    assert state["pools"][0]["key"] == "sniper"
    assert state["pools"][0]["matched"] == "word"
    assert set(state["unverified"]) == {"sniper"}
    assert "not by its pool definition" in state["unverified"]["sniper"]


def test_unknown_ammo_pool_gets_a_positional_unverified_key(game):
    give_pools(game, make_pool("D_Resourcepools.AmmoPools.Ammo_Mystery_Pool"))
    state = ammo.read()
    assert state["pools"][0]["key"] == "other0"
    assert state["pools"][0]["matched"] == "none"
    assert "other0" in state["unverified"]


def test_non_ammo_pools_are_left_out(game):
    give_pools(game, make_pool("D_Resourcepools.PlayerPools.HealthPool"), make_pool("D_Resourcepools.AmmoPools.Ammo_Combat_Shotgun_Pool"))
    state = ammo.read()
    assert [p["key"] for p in state["pools"]] == ["shotgun"]


def test_maximum_is_read_with_bWantBaseValue_false(game):
    calls: list = []
    give_pools(game, make_pool("D_Resourcepools.AmmoPools.Ammo_Rocket_Launcher_Pool", calls=calls))
    assert ammo.read()["pools"][0]["max"] == 300
    assert calls and calls[0] == (False,)


def test_set_value_writes_through_set_current_value(game):
    give_pools(game, make_pool("D_Resourcepools.AmmoPools.Ammo_Grenade_Protean_Pool", value=1, maximum=5))
    result = ammo.set_value({"id": 0, "value": 4})
    assert result["applied"]["via"] == "SetCurrentValue"
    assert result["applied"]["after"] == 4
    assert result["pools"][0]["value"] == 4


def test_debug_dump_records_how_each_pool_was_matched(game, tmp_path):
    give_pools(game, make_pool("D_Resourcepools.AmmoPools.Ammo_Patrol_SMG_Pool"), make_pool("D_Resourcepools.AmmoPools.Ammo_Sniper_Thing_Pool"))
    out = ammo.debug_dump()
    assert out["pools"] == 2
    import json

    saved = json.loads((tmp_path / "debug_ammo.json").read_text(encoding="utf-8"))
    assert [(p["matched"], p["definition_path"]) for p in saved["pools"]] == [
        ("definition", "D_Resourcepools.AmmoPools.Ammo_Patrol_SMG_Pool"),
        ("word", "D_Resourcepools.AmmoPools.Ammo_Sniper_Thing_Pool"),
    ]
    assert "pool_definitions" in saved
