"""Gear edits: set_part and set_level are rolled back when the game does not take them, and set_level uses the
gear level range from limits.py.

The weapon is a fake (SimpleNamespace) that stands in for the game object. The game's rebuild is the fake
InitializeInternal: it recomputes stats from the "game" values, rolls a title when there is none, and then calls
the test's on_init hook, which can refuse a change or raise. Overrides live in a tmp_path copy of the file.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from unrealsdk import unreal

from rtse import api, stats
from rtse.api import ApiError, PartRef, Slot


class Part(unreal.UObject):
    """A game object as far as RTSE can see it: a path name, a class and a name."""

    def __init__(self, path: str, cls: str = "WeaponPartDefinition") -> None:
        self.path = path
        self.Name = path.rpartition(".")[2]
        self.Class = SimpleNamespace(Name=cls)

    def _path_name(self) -> str:
        return self.path


def make_gun(
    *,
    uid: int = 7,
    stage: int = 5,
    barrel: str = "Barrel.A",
    clip: int = 10,
    fire: float = 0.5,
    on_init: Callable[[SimpleNamespace], None] | None = None,
) -> SimpleNamespace:
    data = SimpleNamespace(
        UniqueId=uid,
        WeaponTypeDefinition=Part("WeaponType.Pistol", "WeaponTypeDefinition"),
        BalanceDefinition=Part("Balance.Pistol", "WeaponBalanceDefinition"),
        ManufacturerDefinition=Part("Manufacturer.Jakobs", "ManufacturerDefinition"),
        ManufacturerGradeIndex=stage,
        GameStage=stage,
        BodyPartDefinition=Part("Body.A"),
        GripPartDefinition=None,
        BarrelPartDefinition=Part(barrel),
        SightPartDefinition=None,
        StockPartDefinition=None,
        ElementalPartDefinition=None,
        Accessory1PartDefinition=None,
        Accessory2PartDefinition=None,
        MaterialPartDefinition=None,
        PrefixPartDefinition=Part("Prefix.Old", "PrefixPartDefinition"),
        TitlePartDefinition=Part("Title.LogansGun", "TitlePartDefinition"),
    )
    game_values = {"ClipSize": clip, "FireInterval": fire}
    rolls = itertools.count(1)
    new_uids = itertools.count(uid + 1000)

    def initialize(_persist: bool = False) -> None:
        if data.TitlePartDefinition is None:  # The game rolls a name only when there is none
            data.TitlePartDefinition = Part(f"Title.Rolled{next(rolls)}", "TitlePartDefinition")
        if data.PrefixPartDefinition is None:
            data.PrefixPartDefinition = Part(f"Prefix.Rolled{next(rolls)}", "PrefixPartDefinition")
        for stat, value in game_values.items():  # A rebuild recomputes stats from the parts
            setattr(item, stat, value)
            setattr(item, f"{stat}BaseValue", value)
        if on_init is not None:
            on_init(data)

    item = SimpleNamespace(
        Class=SimpleNamespace(Name="WillowWeapon"),
        DefinitionData=data,
        ExpLevel=stage,
        ClipSize=clip,
        ClipSizeBaseValue=clip,
        FireInterval=fire,
        FireIntervalBaseValue=fire,
        InitializeInternal=initialize,
        Unready=lambda _force=False: None,
        GenerateUniqueID=lambda: next(new_uids),
        GetHumanReadableName=lambda: "Logan's Gun",
        _path_name=lambda: "Transient.WillowWeapon_1",
    )
    return item


def use(monkeypatch: pytest.MonkeyPatch, item: Any, slots: list[Slot] | None = None) -> None:
    monkeypatch.setattr(api, "_find_item", lambda _params: (item, api.EQUIPPED))
    monkeypatch.setattr(api, "_slots", lambda _item, _mode: list(slots or []))


def state_of(item: Any) -> dict[str, Any]:
    return {**vars(item.DefinitionData), "ExpLevel": item.ExpLevel}


@pytest.fixture(autouse=True)
def overrides_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    path = tmp_path / "overrides.json"
    monkeypatch.setattr(stats, "OVERRIDES_FILE", path)
    monkeypatch.setattr(api, "_id_aliases", {})
    stats.load()
    yield path
    stats.load()  # Clears the module store before monkeypatch restores the real path


def gun_slot(options: list[PartRef]) -> list[Slot]:
    return [Slot("Barrel", "BarrelPartDefinition", options)]


# --- set_part ------------------------------------------------------------------------------------


def test_set_part_applies_the_part_and_the_gun_gets_a_new_uid(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = make_gun(uid=7)
    use(monkeypatch, item, gun_slot([PartRef("Barrel.New", "WeaponPartDefinition")]))

    result = api.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert item.DefinitionData.BarrelPartDefinition is new_part
    assert item.DefinitionData.UniqueId == 1007
    assert result["applied"]["after"]["after_rebuild"] == "New"


def test_set_part_rebuild_error_restores_every_field_uid_title_and_level(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    calls = []

    def crash_once(_data: SimpleNamespace) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("game crashed while rebuilding")

    item = make_gun(uid=7, on_init=crash_once)
    use(monkeypatch, item, gun_slot([PartRef("Barrel.New", "WeaponPartDefinition")]))
    before = state_of(item)

    with pytest.raises(ApiError) as caught:
        api.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert caught.value.status == 409
    assert "previous parts were restored" in str(caught.value)
    assert state_of(item) == before
    assert item.DefinitionData.UniqueId == 7
    assert item.DefinitionData.TitlePartDefinition.path == "Title.LogansGun"


def test_set_part_readback_mismatch_is_restored_and_reported(game, monkeypatch):
    old_part = Part("Barrel.A")
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part

    def refuse_new_barrel(data: SimpleNamespace) -> None:
        if data.BarrelPartDefinition is new_part:  # The game puts the old barrel back
            data.BarrelPartDefinition = old_part

    item = make_gun(uid=7, on_init=refuse_new_barrel)
    use(monkeypatch, item, gun_slot([PartRef("Barrel.New", "WeaponPartDefinition")]))
    before = state_of(item)

    with pytest.raises(ApiError) as caught:
        api.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert caught.value.status == 409
    assert "the Barrel slot holds 'Barrel.A', not 'Barrel.New'" in str(caught.value)
    assert state_of(item) == before


def test_set_part_restore_failure_is_500_and_logged(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part

    def always_crash(_data: SimpleNamespace) -> None:
        raise RuntimeError("game crashed")

    item = make_gun(uid=7, on_init=always_crash)
    use(monkeypatch, item, gun_slot([PartRef("Barrel.New", "WeaponPartDefinition")]))

    with pytest.raises(ApiError) as caught:
        api.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert caught.value.status == 500
    assert "restoring failed - reload the save before playing" in str(caught.value)
    assert any(level == "error" and "restoring" in message for level, message in game.log)


def test_set_part_moves_the_guns_own_overrides_and_reapplies_them(game, monkeypatch):
    game.objects[("WeaponPartDefinition", "Barrel.New")] = Part("Barrel.New")
    item = make_gun(uid=7)
    use(monkeypatch, item, gun_slot([PartRef("Barrel.New", "WeaponPartDefinition")]))
    stats.set_override(item, "ClipSize", 20)

    api.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert item.DefinitionData.UniqueId == 1007
    assert stats.overrides_for(item) == {"ClipSize": 20}
    assert stats.override_scope(item) == "own"
    assert item.ClipSize == 20  # The rebuild reset it to the game's 10; the moved override is applied again


def test_failed_set_part_does_not_move_overrides(game, monkeypatch, overrides_file):
    game.objects[("WeaponPartDefinition", "Barrel.New")] = Part("Barrel.New")

    def crash_always(_data: SimpleNamespace) -> None:
        raise RuntimeError("game crashed")

    item = make_gun(uid=7, on_init=crash_always)
    use(monkeypatch, item, gun_slot([PartRef("Barrel.New", "WeaponPartDefinition")]))
    stats.set_override(item, "ClipSize", 20)
    saved_before = overrides_file.read_bytes()

    with pytest.raises(ApiError):
        api.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert item.DefinitionData.UniqueId == 7
    assert stats.overrides_for(item) == {"ClipSize": 20}
    assert overrides_file.read_bytes() == saved_before


# --- set_level and the gear range ----------------------------------------------------------------


def gear_pc(character_max: int = 80, overpower_max: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        GetMaximumPossiblePlayerLevelCap=lambda: character_max,
        GetMaxOverpowerLevel=lambda: overpower_max,
    )


def test_set_level_accepts_the_gear_max_from_the_game(game, monkeypatch):
    game.pc = gear_pc(80, 10)
    item = make_gun(stage=5)
    use(monkeypatch, item)

    result = api.set_level({"id": "x", "level": 90})

    assert item.ExpLevel == 90
    assert item.DefinitionData.GameStage == 90
    assert item.DefinitionData.ManufacturerGradeIndex == 90
    assert result["level"] == 90
    assert result["overpower"] == 10
    assert result["limits"]["max"] == 90


def test_set_level_rejects_above_the_gear_max_and_names_the_range(game, monkeypatch):
    game.pc = gear_pc(80, 10)
    item = make_gun(stage=5)
    use(monkeypatch, item)
    before = state_of(item)

    with pytest.raises(ApiError) as caught:
        api.set_level({"id": "x", "level": 91})

    assert caught.value.status == 400
    message = str(caught.value)
    assert "1-90" in message
    assert "90 = level 80 OP10" in message
    assert state_of(item) == before


def test_set_level_uses_the_fallback_range_when_the_game_cannot_be_asked(game, monkeypatch):
    game.pc = None  # Fallback: character 80, overpower 10
    item = make_gun(stage=5)
    use(monkeypatch, item)

    assert api.set_level({"id": "x", "level": 90})["limits"]["max"] == 90
    with pytest.raises(ApiError) as caught:
        api.set_level({"id": "x", "level": 91})
    assert caught.value.status == 400


def test_set_level_readback_mismatch_is_restored(game, monkeypatch):
    game.pc = gear_pc()

    def keep_old_stage(data: SimpleNamespace) -> None:
        data.GameStage = 5  # The game rebuilds the gun at its old stage

    item = make_gun(stage=5, on_init=keep_old_stage)
    use(monkeypatch, item)
    before = state_of(item)

    with pytest.raises(ApiError) as caught:
        api.set_level({"id": "x", "level": 90})

    assert caught.value.status == 409
    assert "GameStage is 5, not 90" in str(caught.value)
    assert state_of(item) == before
    assert item.ExpLevel == 5


def test_set_level_rebuild_error_restores_everything(game, monkeypatch):
    game.pc = gear_pc()
    calls = []

    def crash_once(_data: SimpleNamespace) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("game crashed")

    item = make_gun(stage=5, on_init=crash_once)
    use(monkeypatch, item)
    before = state_of(item)

    with pytest.raises(ApiError) as caught:
        api.set_level({"id": "x", "level": 90})

    assert caught.value.status == 409
    assert state_of(item) == before


def test_set_level_moves_a_legacy_override_onto_the_gun_at_its_new_level(game, monkeypatch, overrides_file):
    game.pc = gear_pc()
    item = make_gun(stage=5)
    legacy_key = stats.fingerprint(item)
    overrides_file.write_text(json.dumps({legacy_key: {"label": "Logan's Gun", "stats": {"ClipSize": 15}}}))
    stats.load()
    use(monkeypatch, item)

    api.set_level({"id": "x", "level": 6})

    assert stats.override_scope(item) == "own"
    assert stats.overrides_for(item) == {"ClipSize": 15}
    assert item.ClipSize == 15
    saved = json.loads(overrides_file.read_text(encoding="utf-8"))
    assert legacy_key in saved["legacy"]  # The other guns at level 5 keep the legacy entry


def test_detail_reports_limits_and_overpower(game, monkeypatch):
    game.pc = gear_pc(80, 10)
    item = make_gun(stage=88)
    use(monkeypatch, item)

    detail = api._detail(item, api.EQUIPPED, "legal")

    assert detail["overpower"] == 8
    assert detail["limits"]["max"] == 90
    assert detail["limits"]["character_max"] == 80
    assert detail["override_scope"] is None


def test_detail_has_no_overpower_for_ordinary_gear(game, monkeypatch):
    game.pc = gear_pc(80, 10)
    item = make_gun(stage=75)
    use(monkeypatch, item)

    assert api._detail(item, api.EQUIPPED, "legal")["overpower"] == 0


def test_snapshot_skips_fields_this_build_does_not_have(game):
    item = make_gun()
    del item.DefinitionData.Accessory2PartDefinition

    snapshot = api._snapshot(item)

    assert "Accessory2PartDefinition" not in snapshot
    assert snapshot["BarrelPartDefinition"].path == "Barrel.A"
    assert snapshot["UniqueId"] == 7
    assert snapshot["ExpLevel"] == 5


# --- Overrides and the UniqueId that a clear rebuild gives the gun --------------------------------


def test_clearing_a_stat_moves_the_guns_own_overrides_to_its_new_uid(game, monkeypatch):
    item = make_gun(uid=7, clip=10, fire=0.5)
    use(monkeypatch, item)
    api.set_stat({"id": "x", "stat": "ClipSize", "value": 20})
    api.set_stat({"id": "x", "stat": "FireInterval", "value": 0.25})

    api.set_stat({"id": "x", "stat": "ClipSize", "value": None})

    assert item.DefinitionData.UniqueId == 1007
    assert stats.overrides_for(item) == {"FireInterval": 0.25}
    assert stats.override_scope(item) == "own"
    assert item.FireInterval == 0.25
    assert item.ClipSize == 10  # The override was removed; the game's value is back
