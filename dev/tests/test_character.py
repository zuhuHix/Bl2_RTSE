"""rtse/character.py against fake game objects: currency indexes, the level write rules (XP first, refuse without an XP
path, restore and 409 on a desync), the level cap from rtse/limits.py, and the JSON's "unverified" dict.

    python -I -m pytest dev/tests -k character
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest
from unrealsdk import unreal

from rtse import character

XP_TABLE = {level: 0 if level <= 1 else math.ceil(1000 * (level - 1) ** 2) for level in range(1, 120)}  # any monotonic table


class Obj(unreal.UObject):
    """A fake game object: a UObject stand-in with a path name, a Class with no fields, and whatever attributes given."""

    def __init__(self, path: str, **attrs: Any) -> None:
        self._path = path
        self.Class = attrs.pop("Class", SimpleNamespace(_fields=lambda: [], Name="Object"))
        self.__dict__.update(attrs)

    def _path_name(self) -> str:
        return self._path


def make_world(game, *, level: int = 10, xp: int | None = None, cap: int | None = 72, xp_table: bool = True,
               pool: bool = True, desync: bool = False) -> SimpleNamespace:
    """A player with a level, XP and currencies. `cap` None = no level-cap function; `xp_table` False = no
    GetExpPointsRequiredForLevel; `pool` False = no experience pool; `desync` = the XP pool write doesn't reach GetExpPoints."""
    state = SimpleNamespace(level=level, xp=XP_TABLE[level] if xp is None else xp, currency={0: 5000, 1: 40, 2: 7, 3: 2})
    pool_obj = None
    if pool:
        pool_state = {"value": float(state.xp)}

        def set_pool(value: float) -> None:
            pool_state["value"] = float(value)
            if not desync:
                state.xp = round(value)

        pool_obj = Obj(
            "pool", Class=SimpleNamespace(_fields=lambda: [], Name="ExperienceResourcePool"),
            Definition=Obj("D_Resourcepools.PlayerPools.ExperiencePool"),
            GetCurrentValue=lambda: pool_state["value"], SetCurrentValue=set_pool,
        )
    pri = Obj(
        "pri", ExpLevel=state.level, GeneralSkillPoints=3, PlayerName="Zer0",
        GetCurrencyOnHand=lambda FormOfCurrency: state.currency[FormOfCurrency],
        AddCurrencyOnHand=lambda FormOfCurrency, AddValue: state.currency.__setitem__(FormOfCurrency, state.currency[FormOfCurrency] + AddValue),
        SetCurrencyOnHand=lambda FormOfCurrency, NewValue: state.currency.__setitem__(FormOfCurrency, NewValue),
    )
    attrs: dict[str, Any] = {
        "PlayerReplicationInfo": pri, "Pawn": None,
        "GetExpPoints": lambda: state.xp,
        "ResourcePoolManager": SimpleNamespace(ResourcePools=[pool_obj] if pool_obj else []) if pool else None,
    }
    if xp_table:
        attrs["GetExpPointsRequiredForLevel"] = lambda Level: XP_TABLE.get(Level, 0)
    if cap is not None:
        attrs["GetMaximumPossiblePlayerLevelCap"] = lambda: cap
    pc = Obj("/Game/PC.PC", **attrs)
    game.pc = pc
    return SimpleNamespace(pc=pc, pri=pri, pool=pool_obj, state=state)


@pytest.fixture(autouse=True)
def _fresh_class_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(character, "_class_cache", {})
    monkeypatch.setattr(character, "DEBUG_CHARACTER_FILE", tmp_path / "debug_character.json")
    monkeypatch.setattr(character, "_last_write", {})


def test_currency_indexes_follow_the_game_enum():
    """ECurrencyType (Engine.upk): Credits 0, Eridium 1, SeraphCrystals 2, Reserved_A 3. Torgue is the guessed slot 3."""
    assert character.CURRENCIES == (("money", "Cash", 0), ("eridium", "Eridium", 1), ("seraph", "Seraph Crystals", 2), ("torgue", "Torgue Tokens", 3))


def test_read_marks_only_torgue_as_unverified(game):
    make_world(game)
    state = character.read()
    assert set(state["unverified"]) == {"torgue"}
    assert "CURRENCY_Reserved_A" in state["unverified"]["torgue"]
    assert [c["index"] for c in state["currencies"]] == [0, 1, 2, 3]


def test_xp_table_comes_from_the_game_when_available(game):
    make_world(game, level=10)
    state = character.read()
    assert state["xp_this_level"] == XP_TABLE[10]
    assert state["xp_next_level"] == XP_TABLE[11]
    assert state["sources"]["xp_next_level"] == "pc.GetExpPointsRequiredForLevel()"
    assert "xp_next_level" not in state["unverified"]


def test_guessed_xp_table_is_marked_unverified(game):
    make_world(game, level=10, xp_table=False)
    state = character.read()
    assert state["xp_next_level"] == character.xp_for_level(11)
    assert "fallback" in state["sources"]["xp_next_level"]
    assert "xp_next_level" in state["unverified"] and "xp_this_level" in state["unverified"]


def test_level_write_with_no_xp_path_is_refused_and_changes_nothing(game):
    world = make_world(game, level=10, pool=False)
    with pytest.raises(character.CharacterError) as info:
        character.set_field({"field": "level", "value": 20})
    assert info.value.status == 501
    assert world.pri.ExpLevel == 10
    assert world.state.xp == XP_TABLE[10]


def test_level_write_sets_xp_then_level_and_reports_both(game):
    world = make_world(game, level=10)
    result = character.set_field({"field": "level", "value": 20})
    assert world.pri.ExpLevel == 20
    assert world.state.xp == XP_TABLE[20]
    assert result["applied"]["via"]["xp"] == "xp pool.SetCurrentValue"
    assert result["applied"]["via"]["level"] == "pri.ExpLevel"
    assert result["level"] == 20 and result["xp"] == XP_TABLE[20]


def test_desync_after_write_restores_before_values_and_raises_409(game):
    world = make_world(game, level=10, desync=True)  # the XP pool write doesn't reach the controller's XP
    before_xp = world.state.xp
    with pytest.raises(character.CharacterError) as info:
        character.set_field({"field": "level", "value": 20})
    assert info.value.status == 409
    assert world.pri.ExpLevel == 10
    assert world.pool.GetCurrentValue() == before_xp
    assert character._last_write["restored"]["level"] == 10


class RefusingPRI(Obj):
    """A PlayerReplicationInfo whose ExpLevel writes are refused for the next `refuse` tries."""

    refuse = 0

    def __setattr__(self, name, value):
        if name == "ExpLevel" and self.refuse > 0:
            self.refuse -= 1
            raise RuntimeError("the game refused the level write")
        super().__setattr__(name, value)


def test_level_write_failing_after_the_xp_write_restores_both_and_says_so(game):
    world = make_world(game, level=10)
    world.pri.__class__ = RefusingPRI
    world.pri.refuse = 1  # the XP write goes through, the level write is refused once
    with pytest.raises(character.CharacterError) as info:
        character.set_field({"field": "level", "value": 20})
    assert info.value.status == 409
    assert "writing the level/XP failed" in str(info.value)
    assert str(info.value).endswith("restored the previous level and XP")
    assert world.state.xp == XP_TABLE[10] and world.pri.ExpLevel == 10
    assert character._last_write["restored"] == {"level": 10, "xp": XP_TABLE[10]}


def test_unreadable_level_or_xp_is_refused_before_anything_is_written(game):
    world = make_world(game, level=10)
    del world.pri.ExpLevel  # the level can't be read, so the old value can't be put back
    with pytest.raises(character.CharacterError) as info:
        character.set_field({"field": "level", "value": 20})
    assert info.value.status == 409
    assert "nothing was changed" in str(info.value)
    assert world.state.xp == XP_TABLE[10]
    assert world.pool.GetCurrentValue() == XP_TABLE[10]


def test_unreadable_xp_is_refused_before_anything_is_written(game):
    world = make_world(game, level=10)
    def no_xp() -> int:
        raise RuntimeError("no XP")

    world.pc.GetExpPoints = no_xp
    world.pc.ResourcePoolManager = None  # so there is no pool to read XP from either
    with pytest.raises(character.CharacterError) as info:
        character.set_field({"field": "level", "value": 20})
    assert info.value.status == 409
    assert world.pri.ExpLevel == 10 and world.state.xp == XP_TABLE[10]


def test_restore_that_does_not_stick_is_a_500_that_says_so(game):
    world = make_world(game, level=10)
    world.pri.__class__ = RefusingPRI
    world.pri.refuse = 999  # every level write is refused
    real_set = world.pool.SetCurrentValue
    calls = {"n": 0}

    def xp_write_once(value: float) -> None:
        calls["n"] += 1
        if calls["n"] > 1:  # the first write (to the new XP) works, the restore is refused
            raise RuntimeError("the pool is locked")
        real_set(value)

    world.pool.SetCurrentValue = xp_write_once
    with pytest.raises(character.CharacterError) as info:
        character.set_field({"field": "level", "value": 20})
    assert info.value.status == 500
    message = str(info.value)
    assert "restoring ALSO failed" in message and "reload the save before playing" in message
    assert f"XP now {XP_TABLE[20]}" in message
    assert character._last_write["restored"]["xp"] == XP_TABLE[20]


def test_level_cap_comes_from_limits(game):
    make_world(game, cap=72)
    assert character.read()["limits"]["level"] == [1, 72]
    assert character.read()["limits"]["level_source"] == "pc.GetMaximumPossiblePlayerLevelCap()"
    with pytest.raises(character.CharacterError) as info:
        character.set_field({"field": "level", "value": 73})
    assert info.value.status == 400 and "1-72" in str(info.value)
    assert character.set_field({"field": "level", "value": 72})["level"] == 72


def test_level_cap_falls_back_to_80_and_says_so(game):
    make_world(game, cap=None)
    state = character.read()
    assert state["limits"]["level"] == [1, 80]
    assert state["limits"]["level_source"] == "fallback (unverified)"
    assert "limits.level" in state["unverified"]


def test_unknown_field_and_non_number_are_rejected(game):
    make_world(game)
    with pytest.raises(character.CharacterError) as info:
        character.set_field({"field": "nope", "value": 1})
    assert info.value.status == 400
    with pytest.raises(character.CharacterError):
        character.set_field({"field": "level", "value": "ten"})


def test_debug_dump_writes_the_evidence(game, tmp_path):
    make_world(game, level=10)
    character.set_field({"field": "level", "value": 12})
    out = character.debug_dump()
    assert out["xp_pool_definition"] == "D_Resourcepools.PlayerPools.ExperiencePool"
    assert out["level_cap"]["value"] == 72
    assert (tmp_path / "debug_character.json").is_file()


def test_xp_setter_that_changes_xp_then_raises_restores_both(game):
    """The XP write itself is protected: a setter that applies the new XP and then raises still gets undone."""
    world = make_world(game, level=10)
    real_set = world.pool.SetCurrentValue
    calls = {"n": 0}

    def set_then_raise(value: float) -> None:
        calls["n"] += 1
        real_set(value)  # the value really changes...
        if calls["n"] == 1:
            raise RuntimeError("the pool complained after writing")  # ...and then the call fails

    world.pool.SetCurrentValue = set_then_raise
    with pytest.raises(character.CharacterError) as info:
        character.set_field({"field": "level", "value": 20})
    assert "writing the level/XP failed" in str(info.value)
    assert str(info.value).endswith("restored the previous level and XP")
    assert world.state.xp == XP_TABLE[10] and world.pri.ExpLevel == 10
    assert calls["n"] == 2  # the failed write, then the restore
