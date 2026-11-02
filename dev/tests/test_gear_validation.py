"""Part status (natural / DLC / custom / unsupported), save checks and part checks.

Every game call here is a fake (see unreal_stub.py and test_gear_edit.py), so these tests pin RTSE's own logic only:
they say nothing about what the real game does. The DLC and serial calls are UNVERIFIED in game.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from rtse import api, gear_edit, gear_inventory, gear_parts, stats
from rtse.gear_parts import ITEM_SLOTS, WEAPON_SLOTS, PartRef, Slot
from rtse.routing import ApiError
from test_gear_edit import Part, make_gun, state_of, use


@pytest.fixture(autouse=True)
def _fresh_caches_and_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    """Empty part and DLC caches, and the overrides file in tmp_path (so nothing real is written)."""
    monkeypatch.setattr(stats, "OVERRIDES_FILE", tmp_path / "overrides.json")
    monkeypatch.setattr(gear_inventory, "_id_aliases", {})
    stats.load()
    gear_parts.rescan_parts({})
    gear_edit._probe_log.clear()  # noqa: SLF001
    yield
    gear_parts.rescan_parts({})
    gear_edit._probe_log.clear()  # noqa: SLF001
    stats.load()


# --- fakes for balances, DLC sets and items -----------------------------------------------------


class FakeBalance:
    """A weapon or item balance definition as far as RTSE reads it: a path, its part lists and DLC links.

    `dlc_reads` counts how often the DlcItemSet field is read, so the cache tests can see it.
    """

    def __init__(
        self,
        path: str,
        *,
        parts: dict[str, list[Any]] | None = None,
        dlc: Any = None,
        base: Any = None,
        item: bool = False,
    ) -> None:
        self.path = path
        self.Name = path.rpartition(".")[2]
        self.BaseDefinition = base
        self.Class = SimpleNamespace(Name="InventoryBalanceDefinition" if item else "WeaponBalanceDefinition")
        self._dlc = dlc
        self.dlc_reads = 0
        parts = parts or {}

        def weighted(slot: str) -> SimpleNamespace | None:
            found = parts.get(slot)
            return SimpleNamespace(WeightedParts=[SimpleNamespace(Part=p) for p in found]) if found else None

        if item:
            self.InventoryDefinition = None
            self.RuntimePartListCollection = None
            self.PartListCollection = SimpleNamespace(**{f"{s}PartData": weighted(s) for s in ITEM_SLOTS})
        else:
            self.RuntimePartListCollection = SimpleNamespace(**{f"{s}PartData": weighted(s) for s in WEAPON_SLOTS})
            self.PartListCollection = None

    @property
    def DlcItemSet(self) -> Any:  # noqa: N802 - the game's field name
        self.dlc_reads += 1
        return self._dlc

    def _path_name(self) -> str:
        return self.path


class FakeDlc:
    """A DownloadableItemSet as far as RTSE reads it: a display name and IsLicensed(pc)."""

    def __init__(self, name: str, *, path: str = "DownloadableItemSet.Orchid", licensed: bool = True) -> None:
        self.ContentDisplayName = name
        self.PackageDef = None
        self.Name = path.rpartition(".")[2]
        self.path = path
        self.licensed = licensed
        self.calls = 0

    def IsLicensed(self, _pc: Any) -> bool:  # noqa: N802 - the game's function name
        self.calls += 1
        return self.licensed

    def _path_name(self) -> str:
        return self.path


ORCHID_NAME = "Captain Scarlett and Her Pirate's Booty"


def make_item(balance: FakeBalance) -> SimpleNamespace:
    """A shield-like item (not a weapon): its own balance is `balance`."""
    data = SimpleNamespace(
        BalanceDefinition=balance,
        ItemDefinition=Part("Item.Shield", "ShieldDefinition"),
        ManufacturerGradeIndex=5,
        GameStage=5,
        UniqueId=3,
        **{f"{s}ItemPartDefinition": None for s in ITEM_SLOTS},
    )
    return SimpleNamespace(
        Class=SimpleNamespace(Name="WillowShield"),
        DefinitionData=data,
        ExpLevel=5,
        GetHumanReadableName=lambda: "Shield",
        _path_name=lambda: "Transient.Shield_1",
    )


@pytest.fixture
def weapon_world(game):
    """Weapon balances covering each status. Returns (the gun, its parts by short name)."""
    game.pc = SimpleNamespace()  # a player, so IsLicensed() runs
    dlc = FakeDlc(ORCHID_NAME)
    parts = {
        "own": Part("Barrel.Own"),  # in the gun's own list: natural
        "orch": Part("Barrel.Orch"),  # only in a DLC balance: dlc
        "child": Part("Barrel.Child"),  # DLC via BaseDefinition: dlc
        "mixed": Part("Barrel.Mixed"),  # in a DLC balance and a base balance: custom
        "other": Part("Barrel.Other"),  # only in a base balance: custom
        "item": Part("Barrel.Item", "ItemPartDefinition"),  # an item part on a weapon: unsupported
    }
    own = FakeBalance("Balance.Pistol", parts={"Barrel": [parts["own"]]})
    orchid = FakeBalance("Balance.Orchid", parts={"Barrel": [parts["orch"], parts["mixed"]]}, dlc=dlc)
    orchid_parent = FakeBalance("Balance.OrchidBase", dlc=dlc)
    child = FakeBalance("Balance.OrchidChild", parts={"Barrel": [parts["child"]]}, base=orchid_parent)
    plain = FakeBalance("Balance.Plain", parts={"Barrel": [parts["mixed"], parts["other"], parts["item"]]})
    game.all["WeaponBalanceDefinition"] = [own, orchid, orchid_parent, child, plain]
    gun = make_gun()
    gun.DefinitionData.BalanceDefinition = own
    return gun, parts, {"own": own, "orchid": orchid, "child": child, "dlc": dlc}


def barrel_statuses(gun: Any) -> dict[str, dict[str, Any]]:
    slot = next(s for s in gear_parts._slots(gun, "everything") if s.name == "Barrel")  # noqa: SLF001
    return slot.statuses or {}


# --- part status ----------------------------------------------------------------------------------


def test_own_list_part_is_natural(weapon_world):
    gun, _parts, _balances = weapon_world
    status = barrel_statuses(gun)["Barrel.Own"]
    assert status["status"] == "natural"
    assert status["status_reason"] == "In this item's own balance list for the Barrel slot"
    assert status["dlc"] is None


def test_part_only_in_a_dlc_balance_is_dlc_with_its_licence(weapon_world):
    gun, _parts, balances = weapon_world
    status = barrel_statuses(gun)["Barrel.Orch"]
    assert status["status"] == "dlc"
    assert status["status_reason"] == f"Only used by DLC gear: {ORCHID_NAME}"
    assert status["dlc"] == {"name": ORCHID_NAME, "licensed": True, "source": "DlcItemSet"}
    assert balances["dlc"].calls == 1


def test_dlc_set_found_through_base_definition_counts_as_dlc(weapon_world):
    gun, _parts, _balances = weapon_world
    status = barrel_statuses(gun)["Barrel.Child"]
    assert status["status"] == "dlc"
    assert status["dlc"]["name"] == ORCHID_NAME


def test_part_in_a_base_balance_as_well_as_a_dlc_one_is_custom(weapon_world):
    gun, _parts, _balances = weapon_world
    status = barrel_statuses(gun)["Barrel.Mixed"]
    assert status["status"] == "custom"
    assert status["status_reason"] == "From another balance, not this item's own Barrel slot"
    assert status["dlc"] is None


def test_part_from_another_balance_without_dlc_is_custom(weapon_world):
    gun, _parts, _balances = weapon_world
    assert barrel_statuses(gun)["Barrel.Other"]["status"] == "custom"


def test_item_part_on_a_weapon_is_unsupported(weapon_world):
    gun, _parts, _balances = weapon_world
    status = barrel_statuses(gun)["Barrel.Item"]
    assert status["status"] == "unsupported"
    assert status["status_reason"].endswith("(not checked in game)")
    assert "ItemPartDefinition" in status["status_reason"]


def test_package_name_fallback_when_no_balance_is_loaded(game):
    game.pc = SimpleNamespace()
    ref = PartRef("GD_Orchid_Weapons.Barrel.X", "WeaponPartDefinition")
    status = gear_parts.part_status(ref, slot="Barrel", weapon=True, legal_ids=set(), own_classes=set())
    assert status["status"] == "dlc"
    assert status["status_reason"] == f"Looks like DLC content from its package name: {ORCHID_NAME} (guess)"
    assert status["dlc"] == {"name": ORCHID_NAME, "licensed": None, "source": "package name (guess)"}


def test_package_hint_matches_whole_tokens_only():
    ref = PartRef("GD_Orchidish_Weapons.Barrel.X", "WeaponPartDefinition")
    status = gear_parts.part_status(ref, slot="Barrel", weapon=True, legal_ids=set(), own_classes=set())
    assert status["status"] == "custom"


def test_natural_beats_dlc_and_unsupported():
    ref = PartRef("GD_Orchid_Weapons.Barrel.X", "WeaponPartDefinition")
    status = gear_parts.part_status(ref, slot="Barrel", weapon=True, legal_ids={ref.id}, own_classes=set())
    assert status["status"] == "natural"


def test_grenade_mod_is_unsupported_on_a_shield_whose_own_parts_are_shields(game):
    shield = Part("Alpha.Shield", "ShieldPartDefinition")
    grenade = Part("Alpha.Grenade", "GrenadeModPartDefinition")
    own = FakeBalance("Balance.ShieldOwn", item=True, parts={"Alpha": [shield]})
    other = FakeBalance("Balance.GrenadeOther", item=True, parts={"Alpha": [grenade]})
    game.all["InventoryBalanceDefinition"] = [own, other]
    item = make_item(own)

    alpha = next(s for s in gear_parts._slots(item, "everything") if s.name == "Alpha")  # noqa: SLF001
    statuses = alpha.statuses or {}

    assert statuses["Alpha.Shield"]["status"] == "natural"
    grenade_status = statuses["Alpha.Grenade"]
    assert grenade_status["status"] == "unsupported"
    assert "GrenadeModPartDefinition" in grenade_status["status_reason"]
    assert "ShieldPartDefinition" in grenade_status["status_reason"]


def test_one_odd_part_does_not_break_the_slot(game, monkeypatch):
    good, odd = Part("Barrel.Good"), Part("Barrel.Odd")
    balance = FakeBalance("Balance.Pistol", parts={"Barrel": [good, odd]})
    game.all["WeaponBalanceDefinition"] = [balance]
    gun = make_gun()
    gun.DefinitionData.BalanceDefinition = balance
    real_status = gear_parts.part_status

    def picky(ref: PartRef, **kwargs: Any) -> dict[str, Any]:
        if ref.id == "Barrel.Odd":
            raise ZeroDivisionError("odd part")
        return real_status(ref, **kwargs)

    monkeypatch.setattr(gear_parts, "part_status", picky)
    barrel = next(s for s in gear_parts._slots(gun, "legal") if s.name == "Barrel")  # noqa: SLF001
    assert "Barrel.Good" in (barrel.statuses or {})
    assert "Barrel.Odd" not in (barrel.statuses or {})
    assert "Barrel.Odd" in [o.id for o in barrel.options]  # still offered, just without a status


# --- DLC caching ----------------------------------------------------------------------------------


def test_balance_dlc_is_read_once_until_a_rescan(game):
    game.pc = SimpleNamespace()
    dlc = FakeDlc(ORCHID_NAME)
    balance = FakeBalance("Balance.Orchid", parts={"Barrel": [Part("Barrel.Orch")]}, dlc=dlc)
    game.all["WeaponBalanceDefinition"] = [balance]

    gear_parts._part_universe(weapon=True)  # noqa: SLF001
    gear_parts._universe.clear()  # noqa: SLF001 - rebuild the universe without a rescan
    gear_parts._owners.clear()  # noqa: SLF001
    gear_parts._part_universe(weapon=True)  # noqa: SLF001
    assert balance.dlc_reads == 1
    assert dlc.calls == 1

    gear_parts.rescan_parts({})
    gear_parts._part_universe(weapon=True)  # noqa: SLF001
    assert balance.dlc_reads == 2
    assert dlc.calls == 2


# --- _detail carries the statuses -----------------------------------------------------------------


def test_detail_options_carry_the_status_keys(weapon_world):
    gun, _parts, _balances = weapon_world
    detail = gear_edit._detail(gun, gear_inventory.EQUIPPED, "everything")  # noqa: SLF001

    barrel = next(s for s in detail["slots"] if s["slot"] == "Barrel")
    orch = next(o for o in barrel["options"] if o["id"] == "Barrel.Orch")
    assert orch["status"] == "dlc"
    assert orch["dlc"]["name"] == ORCHID_NAME
    assert barrel["current"]["id"] == "Barrel.A"
    assert barrel["current"]["status"] == "custom"  # the gun's own barrel is not in its balance list here
    assert detail["dlc"] is None  # the gun's own balance is base game


def test_detail_reports_the_items_own_dlc(weapon_world):
    gun, _parts, balances = weapon_world
    gun.DefinitionData.BalanceDefinition = balances["child"]
    detail = gear_edit._detail(gun, gear_inventory.EQUIPPED, "legal")  # noqa: SLF001
    assert detail["dlc"] == {"name": ORCHID_NAME, "licensed": True, "source": "DlcItemSet"}


def test_options_without_statuses_still_work(game, monkeypatch):
    gun = make_gun()
    use(monkeypatch, gun, [Slot("Barrel", "BarrelPartDefinition", [PartRef("Barrel.New", "WeaponPartDefinition")])])
    detail = gear_edit._detail(gun, gear_inventory.EQUIPPED, "legal")  # noqa: SLF001
    option = detail["slots"][0]["options"][0]
    assert option["id"] == "Barrel.New"
    assert "status" not in option
    assert detail["slots"][0]["current"]["id"] == "Barrel.A"


# --- save checks ----------------------------------------------------------------------------------


def serial_item(
    *,
    drop: tuple[str, ...] = (),
    can_save: bool | None = True,
    match: bool = True,
    unpack_mode: str = "tuple",
    seeds: list[Any] | None = None,
) -> SimpleNamespace:
    """A gun with fake serial functions. Unpacking gives back the definition the serial came from, minus `drop`.

    unpack_mode: "tuple" returns (True, definition); "seed" needs an out parameter (a TypeError without one);
    "bare" returns True with no definition. The seeds list records the out parameter a "seed" unpack was given.
    """
    item = make_gun(uid=7)
    fields = gear_edit._def_fields(item)  # noqa: SLF001

    def pack(definition: Any) -> dict[str, Any]:
        return {f: getattr(definition, f) for f in fields}

    def values_of(serial: dict[str, Any]) -> SimpleNamespace:
        values = dict(serial)
        for field in drop:
            values[field] = None
        return SimpleNamespace(**values)

    if unpack_mode == "seed":
        def unpack(serial: dict[str, Any], seed: Any) -> tuple[bool, SimpleNamespace]:
            if seeds is not None:
                seeds.append(seed)
            return True, values_of(serial)
    elif unpack_mode == "bare":
        def unpack(serial: dict[str, Any]) -> bool:
            return True
    else:
        def unpack(serial: dict[str, Any]) -> tuple[bool, SimpleNamespace]:
            return True, values_of(serial)

    item.CreateSerialNumber = lambda: pack(item.DefinitionData)
    item.PackSerialNumber = pack
    item.UnpackSerialNumber = unpack
    item.SerialNumberMatchesDef = lambda _serial, _definition, _short, _unique: match
    item.CheckDefMatch = lambda _a, _b, _short, _unique: True
    if can_save is not None:
        item.CanBeSaved = lambda: can_save
    return item


def test_save_check_survives_a_round_trip_that_keeps_everything():
    item = serial_item()
    check = gear_edit.save_check(item)
    assert check["survives"] is True
    assert check["verified"] is False
    assert check["lost"] == []
    assert check["summary"] == "Survived a serial round-trip (unverified)"
    assert [c["name"] for c in check["checks"]] == [
        "serial round-trip",
        "SerialNumberMatchesDef",
        "CheckDefMatch",
        "CanBeSaved",
    ]


def test_can_be_saved_false_means_does_not_survive():
    check = gear_edit.save_check(serial_item(can_save=False))
    assert check["survives"] is False
    assert check["summary"] == "CanBeSaved() returned False (unverified meaning)"


def test_a_tuple_unpack_result_is_read_as_ok_and_definition():
    check = gear_edit.save_check(serial_item(unpack_mode="tuple"))
    round_trip = check["checks"][0]
    assert round_trip == {"name": "serial round-trip", "ok": True, "detail": "nothing lost"}


def test_unpack_that_needs_an_out_parameter_gets_a_copy_never_the_live_struct():
    seeds: list[Any] = []
    item = serial_item(unpack_mode="seed", seeds=seeds)
    live = item.DefinitionData
    check = gear_edit.save_check(item)
    assert len(seeds) == 1
    assert seeds[0] is not live
    assert check["survives"] is True


def test_bare_unpack_result_has_no_definition_to_compare():
    check = gear_edit.save_check(serial_item(unpack_mode="bare"))
    assert check["checks"][0]["ok"] is None
    assert check["survives"] is None


def test_an_exception_in_serial_number_matches_def_is_one_unknown_check(game):
    item = serial_item()

    def boom(*_args: Any) -> bool:
        raise RuntimeError("boom")

    item.SerialNumberMatchesDef = boom
    check = gear_edit.save_check(item)  # must not raise
    matches = next(c for c in check["checks"] if c["name"] == "SerialNumberMatchesDef")
    assert matches["ok"] is None
    assert "the game raised RuntimeError('boom')" in matches["detail"]
    assert check["survives"] is True  # the round-trip itself still kept everything


def test_a_serial_that_does_not_match_does_not_survive():
    check = gear_edit.save_check(serial_item(match=False))
    assert check["survives"] is False
    assert check["summary"] == "SerialNumberMatchesDef() returned False (unverified)"


def test_save_check_on_nothing_is_unchecked_and_does_not_raise():
    check = gear_edit.save_check(None)
    assert check["survives"] is None
    assert check["verified"] is False


def test_item_without_serial_functions_is_unchecked():
    check = gear_edit.save_check(make_gun())
    assert check["survives"] is None
    assert all(c["ok"] is None for c in check["checks"])
    assert check["checks"][0]["detail"] == "not available on this object"


# --- check_part -----------------------------------------------------------------------------------


def barrel_option(part: Any) -> list[Slot]:
    return [Slot("Barrel", "BarrelPartDefinition", [PartRef(part.path, "WeaponPartDefinition")])]


def test_check_part_survives_when_the_round_trip_keeps_the_part(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = serial_item()
    use(monkeypatch, item, barrel_option(new_part))

    result = gear_edit.check_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert result["slot"] == "Barrel"
    assert result["part"] == "Barrel.New"
    assert result["save_check"]["survives"] is True
    assert result["save_check"]["verified"] is False


def test_check_part_reports_a_part_the_round_trip_drops(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = serial_item(drop=("BarrelPartDefinition",))
    use(monkeypatch, item, barrel_option(new_part))

    check = gear_edit.check_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})["save_check"]

    assert check["survives"] is False
    assert [d["field"] for d in check["lost"]] == ["BarrelPartDefinition"]
    assert check["lost"][0] == {"field": "BarrelPartDefinition", "expected": "Barrel.New", "got": None}
    assert check["summary"] == "Lost in a serial round-trip: BarrelPartDefinition (unverified)"


def test_check_part_without_serial_functions_is_unchecked(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    use(monkeypatch, make_gun(), barrel_option(new_part))

    check = gear_edit.check_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})["save_check"]

    assert check["survives"] is None
    assert check["checks"][0]["detail"] == "not available on this object"


def test_check_part_never_changes_the_live_item(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = serial_item(drop=("BarrelPartDefinition",))
    data_before = item.DefinitionData
    before = state_of(item)
    use(monkeypatch, item, barrel_option(new_part))

    gear_edit.check_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})
    gear_edit.check_part({"id": "x", "slot": "Barrel", "part": ""})  # removal

    assert item.DefinitionData is data_before
    assert state_of(item) == before


def test_check_part_removal_checks_the_slot_without_its_part(monkeypatch):
    item = serial_item()
    use(monkeypatch, item, [Slot("Barrel", "BarrelPartDefinition", [])])

    result = gear_edit.check_part({"id": "x", "slot": "Barrel"})

    assert result["part"] is None
    assert result["status"] is None
    assert result["save_check"]["survives"] is True  # no barrel went in, and no barrel came back out
    assert item.DefinitionData.BarrelPartDefinition is not None  # the live gun keeps its barrel


def test_copy_that_hands_back_the_live_struct_is_put_back_and_then_unchecked(game, monkeypatch):
    class AliasingData(SimpleNamespace):
        def __copy__(self) -> Any:
            return self  # copy.copy gives back the live struct itself

    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = serial_item()
    item.DefinitionData = AliasingData(**vars(item.DefinitionData))
    data = item.DefinitionData
    before = dict(vars(data))
    use(monkeypatch, item, barrel_option(new_part))

    result = gear_edit.check_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert result["save_check"]["survives"] is None  # no make_struct in the stub, so nothing could be checked
    assert "make_struct" in str(gear_edit._probe_log["struct copy"]["last_error"])  # noqa: SLF001
    assert item.DefinitionData is data
    assert vars(data) == before


class SharedCopy:
    """A "copy" whose writes go straight through to the live struct (storage shared)."""

    def __init__(self, target: Any) -> None:
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._target, name, value)


class SharingData(SimpleNamespace):
    copies = 0

    def __copy__(self) -> Any:
        type(self).copies += 1
        return SharedCopy(self)


def test_make_struct_is_preferred_so_the_live_struct_is_never_written(game, monkeypatch):
    def fake_make_struct(_name: str, **values: Any) -> SimpleNamespace:
        return SimpleNamespace(**values)

    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    monkeypatch.setattr(sys.modules["unrealsdk"], "make_struct", fake_make_struct, raising=False)
    monkeypatch.setattr(SharingData, "copies", 0)
    item = serial_item()
    item.DefinitionData = SharingData(**vars(item.DefinitionData))
    data = item.DefinitionData
    before = dict(vars(data))
    use(monkeypatch, item, barrel_option(new_part))

    result = gear_edit.check_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert SharingData.copies == 0  # copy.copy was never tried
    assert vars(data) == before
    assert gear_edit._probe_log["struct copy"]["last_result"] == "make_struct"  # noqa: SLF001
    assert result["save_check"]["survives"] is True


def test_copy_that_shares_storage_is_put_back_and_unchecked_without_make_struct(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    monkeypatch.delattr(sys.modules["unrealsdk"], "make_struct", raising=False)
    item = serial_item()
    item.DefinitionData = SharingData(**vars(item.DefinitionData))
    data = item.DefinitionData
    before = dict(vars(data))
    use(monkeypatch, item, barrel_option(new_part))

    result = gear_edit.check_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert vars(data) == before  # the live struct was put back
    assert result["save_check"]["survives"] is None
    assert "shared the live struct" in result["save_check"]["summary"]


def test_check_part_rejects_an_unknown_slot(monkeypatch):
    use(monkeypatch, make_gun(), [Slot("Barrel", "BarrelPartDefinition", [])])
    with pytest.raises(ApiError) as caught:
        gear_edit.check_part({"id": "x", "slot": "Scope", "part": ""})
    assert caught.value.status == 400


def test_check_part_rejects_a_part_that_is_not_an_option(game, monkeypatch):
    use(monkeypatch, make_gun(), [Slot("Barrel", "BarrelPartDefinition", [])])
    with pytest.raises(ApiError) as caught:
        gear_edit.check_part({"id": "x", "slot": "Barrel", "part": "Barrel.Nope"})
    assert caught.value.status == 400
    assert "not available in this mode" in str(caught.value)


def test_check_part_for_a_part_that_is_not_loaded_is_unsupported_and_unchecked(game, monkeypatch):
    ghost = PartRef("Barrel.Ghost", "WeaponPartDefinition")
    use(monkeypatch, make_gun(), [Slot("Barrel", "BarrelPartDefinition", [ghost], None)])

    result = gear_edit.check_part({"id": "x", "slot": "Barrel", "part": "Barrel.Ghost"})

    assert result["status"] == "unsupported"
    assert result["status_reason"] == "Not loaded right now - try rescanning parts"
    assert result["save_check"]["survives"] is None


# --- set_part and set_level report what the game kept ---------------------------------------------


def test_set_part_reports_what_the_game_kept(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = make_gun(
        uid=7,
        on_init=lambda data: setattr(item, "ClipSize", 12) if data.BarrelPartDefinition.path == "Barrel.New" else None,
    )
    use(monkeypatch, item, barrel_option(new_part))

    kept = gear_edit.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})["applied"]["kept"]

    assert kept["name"] == {"before": "Logan's Gun", "after": "Logan's Gun"}
    assert len(kept["parts"]) == len(WEAPON_SLOTS)
    barrel = next(p for p in kept["parts"] if p["slot"] == "Barrel")
    assert barrel == {"slot": "Barrel", "before": "A", "after": "New", "changed": True, "edited": True}
    assert not any(p["changed"] for p in kept["parts"] if p["slot"] != "Barrel")
    assert not any(p["edited"] for p in kept["parts"] if p["slot"] != "Barrel")
    label = next(s.label for s in stats.WEAPON_STATS if s.name == "ClipSize")
    assert kept["stats"] == [{"name": "ClipSize", "label": label, "before": 10, "after": 12}]
    assert kept["save_check"]["survives"] is None  # this fake gun has no serial functions


def test_set_level_reports_what_the_game_kept(game, monkeypatch):
    game.pc = SimpleNamespace(GetMaximumPossiblePlayerLevelCap=lambda: 80, GetMaxOverpowerLevel=lambda: 10)
    item = make_gun(stage=5)
    use(monkeypatch, item)

    kept = gear_edit.set_level({"id": "x", "level": 6})["applied"]["kept"]

    assert kept["name"]["after"] == "Logan's Gun"
    assert len(kept["parts"]) == len(WEAPON_SLOTS)
    assert not any(p["changed"] or p["edited"] for p in kept["parts"])
    assert kept["stats"] == []
    assert "save_check" in kept


def test_a_failed_before_capture_gives_an_error_not_a_broken_edit(game, monkeypatch):
    game.pc = SimpleNamespace(GetMaximumPossiblePlayerLevelCap=lambda: 80, GetMaxOverpowerLevel=lambda: 10)
    item = make_gun(stage=5)
    use(monkeypatch, item)

    def broken_names(_item: Any) -> dict[str, Any]:
        raise RuntimeError("names unreadable")

    monkeypatch.setattr(gear_edit, "_part_names", broken_names)
    result = gear_edit.set_level({"id": "x", "level": 6})

    kept = result["applied"]["kept"]
    assert kept == {"error": "could not read the item before the edit (RuntimeError('names unreadable'))"}
    assert item.ExpLevel == 6


# --- debug dump and routes ------------------------------------------------------------------------


def test_debug_validation_writes_the_dump_and_says_where(game, monkeypatch, tmp_path):
    target = tmp_path / "debug_validation.json"
    monkeypatch.setattr(gear_edit, "DEBUG_VALIDATION_FILE", target)
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = serial_item()
    use(monkeypatch, item, barrel_option(new_part))

    result = gear_edit.debug_validation({"id": "x"})

    assert result["saved_to"] == str(target)
    assert result["functions"]["CheckDefMatch"] is True
    assert result["functions"]["IsDLCRequirementMet"] is False
    dump = json.loads(target.read_text(encoding="utf-8"))
    assert "unverified" in dump["note"]
    assert dump["save_check"]["survives"] is True
    assert "CreateSerialNumber" in dump["probe_log"]
    assert "status_counts" in dump


def test_the_new_paths_are_declared_once_and_by_gear_edit():
    for path in ("/api/item/check_part", "/api/debug/validation"):
        owners = [m.__name__ for m in api.ROUTE_MODULES if path in m.GET_ROUTES]
        assert owners == ["rtse.gear_edit"], path
