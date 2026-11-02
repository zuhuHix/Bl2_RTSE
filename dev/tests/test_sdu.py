"""rtse/sdu.py against fake pawn, inventory and pools: the capacity tables against the game's data, the "unverified"
dict for backpack, bank and word-matched ammo, and the level writes through the real setters.

    python -I -m pytest dev/tests -k sdu
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from unrealsdk import unreal

from rtse import sdu


class Obj(unreal.UObject):
    """A fake game object: a UObject stand-in with a path name, a Class with no fields, and whatever attributes given."""

    def __init__(self, path: str, **attrs) -> None:
        self._path = path
        self.Class = attrs.pop("Class", SimpleNamespace(_fields=lambda: [], Name="Object"))
        self.__dict__.update(attrs)

    def _path_name(self) -> str:
        return self._path


class InvManager(Obj):
    """Backpack and bank as the game's WillowInventoryManager exposes them (names from dev/class_index.py)."""

    def GetUnreadiedInventoryMaxSize(self):
        return self.InventorySlotMax_Misc

    def SetInventoryMaxSize(self, NewSize, bOverrideDefaultMin):
        self.InventorySlotMax_Misc = NewSize

    def ClientSetBankSlots(self, NewSlotCount):
        self.bank_slots = NewSlotCount


class AmmoPool(Obj):
    """An AmmoResourcePool with a stored upgrade level; maximum = base + per-upgrade x level (the rifle's numbers)."""

    def GetUpgradeLevel(self):
        return self.level

    def SetUpgradeLevel(self, NewUpgradeLevel):
        self.level = NewUpgradeLevel
        return True

    def GetMaxValue(self, bWantBaseValue=False):
        return 280 + 140 * self.level

    def SetMaxValueFromProfile(self, Value):
        self.max_override = Value

    def ApplyUpgrades(self):
        return None

    def GetCurrentValue(self):
        return 10.0


def make_world(game, *, level: int = 2, pools: tuple = (), bank: int = 6) -> SimpleNamespace:
    inv = InvManager("invmgr", InventorySlotMax_Misc=12, bank_slots=bank)
    pawn = Obj("pawn", InvManager=inv)
    pc = Obj(
        "/Game/PC.PC", Pawn=pawn, PlayerReplicationInfo=None,
        GetBankUpgradeSlots=lambda: inv.bank_slots,
        ResourcePoolManager=SimpleNamespace(ResourcePools=list(pools)),
    )
    game.pc = pc
    return SimpleNamespace(pc=pc, inv=inv)


def rifle_pool(level: int = 2, definition: str = "D_Resourcepools.AmmoPools.Ammo_Combat_Rifle_Pool") -> AmmoPool:
    return AmmoPool(f"pool:{definition}", Definition=Obj(definition), level=level)


@pytest.fixture(autouse=True)
def _tmp_debug(monkeypatch, tmp_path):
    monkeypatch.setattr(sdu, "DEBUG_SDU_FILE", tmp_path / "debug_sdu.json")


def kind(state: dict, key: str) -> dict:
    return next(k for k in state["kinds"] if k["key"] == key)


def test_tables_match_the_game_data():
    """Base and per-upgrade numbers the game's data gives (see rtse/sdu.py and dev/audit_names.py)."""
    assert sdu.BACKPACK_TABLE[:3] == (12, 15, 18)  # InventorySlotMax_Misc default 12, Att_BackPackSlotsPerUpgrade 3
    assert sdu.BANK_TABLE[:3] == (6, 8, 10)  # Att_BankStartingSlots 6, Att_BankSlotsPerUpgrade 2
    assert len(sdu.BACKPACK_TABLE) == len(sdu.BANK_TABLE) == 10  # 9 upgrades: not in the game data, unverified
    assert sdu.AMMO_TABLES["rifle"][1][:2] == (280, 420)  # BaseMaxValue 280, step 140
    assert sdu.AMMO_TABLES["rifle"][1][-1] == 280 + 140 * 6  # TotalUpgradeCount 6
    assert sdu.AMMO_TABLES["grenade"][1][:2] == (3, 4)  # BaseMaxValue 3, step 1
    assert sdu.AMMO_TABLES["launcher"][1][-1] == 12 + 3 * 6


def test_read_flags_backpack_and_bank_and_nothing_else_when_pools_are_exact(game):
    make_world(game, pools=(rifle_pool(level=2),))
    state = sdu.read()
    assert kind(state, "ammo_rifle")["capacity"] == 280 + 140 * 2
    assert kind(state, "ammo_rifle")["level"] == 2
    assert kind(state, "ammo_rifle")["pool_match"] == "definition"
    assert set(state["unverified"]) == {"backpack", "bank"}
    assert "upgrade count (9)" in state["unverified"]["backpack"]
    assert "getter/setter" in state["unverified"]["bank"]


def test_word_matched_ammo_pool_is_unverified(game):
    make_world(game, pools=(rifle_pool(level=1, definition="D_Resourcepools.AmmoPools.Ammo_Sniper_Thing_Pool"),))
    state = sdu.read()
    assert kind(state, "ammo_sniper")["pool_match"] == "word"
    assert "matched by name words" in state["unverified"]["ammo_sniper"]


def test_bank_and_backpack_capacities_are_read_from_the_game(game):
    make_world(game, bank=8)
    state = sdu.read()
    assert kind(state, "backpack")["capacity"] == 12
    assert kind(state, "bank")["capacity"] == 8
    assert kind(state, "bank")["sources"]["capacity"] == "pc.GetBankUpgradeSlots()"


def test_ammo_level_write_uses_the_real_setter_and_reads_the_maximum_back(game):
    pool = rifle_pool(level=2)
    make_world(game, pools=(pool,))
    result = sdu.set_level({"kind": "ammo_rifle", "level": 3})
    assert pool.level == 3
    assert result["applied"]["via"]["level"] == "pool.SetUpgradeLevel(NewUpgradeLevel)"
    assert result["applied"]["via"]["capacity"] is None  # the maximum followed the level, nothing else was written
    assert result["applied"]["after"]["capacity"] == 280 + 140 * 3


def test_backpack_level_write_sets_the_slot_max_through_set_inventory_max_size(game):
    world = make_world(game)
    result = sdu.set_level({"kind": "backpack", "level": 2})
    assert world.inv.InventorySlotMax_Misc == 18
    assert result["applied"]["via"]["capacity"] == "invmgr.SetInventoryMaxSize(NewSize, bOverrideDefaultMin=True)"
    assert result["applied"]["after"]["capacity"] == 18


def test_bank_level_write_uses_the_client_setter_and_reads_it_back(game):
    world = make_world(game, bank=6)
    result = sdu.set_level({"kind": "bank", "level": 2})
    assert world.inv.bank_slots == 10
    assert result["applied"]["via"]["capacity"] == "invmgr.ClientSetBankSlots(NewSlotCount)"
    assert result["applied"]["after"]["capacity"] == 10


def test_unknown_kind_and_bad_level_are_rejected(game):
    make_world(game)
    with pytest.raises(sdu.SduError) as info:
        sdu.set_level({"kind": "vault", "level": 1})
    assert info.value.status == 400
    with pytest.raises(sdu.SduError):
        sdu.set_level({"kind": "backpack", "level": 99})


def test_debug_dump_writes_the_candidates_and_the_unverified_reasons(game, tmp_path):
    make_world(game, pools=(rifle_pool(level=2),))
    out = sdu.debug_dump()
    assert "backpack" in out["unverified"] and "bank" in out["unverified"]
    saved = json.loads((tmp_path / "debug_sdu.json").read_text(encoding="utf-8"))
    assert saved["ammo"]["rifle"]["matched"] == "definition"
    assert saved["candidates"]["bank"]["capacity_read"][0]["candidate"] == "pc.GetBankUpgradeSlots()"
