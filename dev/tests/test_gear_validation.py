"""Part status (natural / DLC / custom / unsupported), the automatic quick check, the explicit save check, the inherited
and weighted part lists, and the part caches.

Every game call here is a fake (see unreal_stub.py and test_gear_edit.py), so these tests pin RTSE's own logic only:
they say nothing about what the real game does. Every serial, weight and DLC call is UNVERIFIED in game.
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


def part_list(*entries: Any, enabled: bool = True) -> SimpleNamespace:
    """A <Slot>PartData struct: its WeightedParts entries and bEnabled."""
    return SimpleNamespace(WeightedParts=list(entries), bEnabled=enabled)


def entry(part: Any, *, weight: int | None = None, manufacturers: list[tuple[Any, int]] | None = None) -> Any:
    """One WeightedParts entry. Fields that are not given are left off the struct, as an old build would."""
    fields: dict[str, Any] = {"Part": part}
    if weight is not None:
        fields["DefaultWeightIndex"] = weight
    if manufacturers is not None:
        fields["Manufacturers"] = [SimpleNamespace(Manufacturer=m, DefaultWeightIndex=w) for m, w in manufacturers]
    return SimpleNamespace(**fields)


class FakeBalance:
    """A weapon or item balance definition as far as RTSE reads it: a path, its part lists and DLC links.

    `data` replaces whole <Slot>PartData structs (for bEnabled, weights and manufacturers). `dlc_reads` counts how often
    the DlcItemSet field is read, so the cache tests can see it.
    """

    def __init__(
        self,
        path: str,
        *,
        parts: dict[str, list[Any]] | None = None,
        data: dict[str, SimpleNamespace] | None = None,
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

        collection = SimpleNamespace(**{f"{s}PartData": weighted(s) for s in (ITEM_SLOTS if item else WEAPON_SLOTS)})
        for slot, struct in (data or {}).items():
            setattr(collection, f"{slot}PartData", struct)
        if item:
            self.InventoryDefinition = None
            self.RuntimePartListCollection = None
            self.PartListCollection = collection
        else:
            self.RuntimePartListCollection = collection
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
WEIGHT_ZERO = "In this item's own Barrel list, but with weight index 0, so it probably never drops (unverified)"


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
    """Weapon balances covering each status. Returns (the gun, its parts by short name, the balances)."""
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


def slot_named(gun: Any, mode: str, name: str) -> Slot:
    return next(s for s in gear_parts._slots(gun, mode) if s.name == name)  # noqa: SLF001


def barrel_statuses(gun: Any) -> dict[str, dict[str, Any]]:
    return slot_named(gun, "everything", "Barrel").statuses or {}


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
    barrel = slot_named(gun, "legal", "Barrel")
    assert "Barrel.Good" in (barrel.statuses or {})
    assert "Barrel.Odd" not in (barrel.statuses or {})
    assert "Barrel.Odd" in [o.id for o in barrel.options]  # still offered, just without a status


# --- inherited and weighted part lists (Fix 3; class data, UNVERIFIED in game) --------------------


def test_a_disabled_child_slot_inherits_the_parents_barrel(game):
    parent = FakeBalance("Balance.ParentGun", parts={"Barrel": [Part("Barrel.Parent")]})
    child = FakeBalance("Balance.ChildGun", data={"Barrel": part_list(enabled=False)}, base=parent)
    game.all["WeaponBalanceDefinition"] = [parent, child]
    gun = make_gun()
    gun.DefinitionData.BalanceDefinition = child

    barrel = slot_named(gun, "legal", "Barrel")

    assert "Barrel.Parent" in [o.id for o in barrel.options]
    assert barrel.statuses["Barrel.Parent"]["status"] == "natural"


def test_an_enabled_child_slot_does_not_inherit_the_parents_barrel(game):
    parent = FakeBalance("Balance.ParentGun", parts={"Barrel": [Part("Barrel.Parent")]})
    child = FakeBalance("Balance.ChildGun", data={"Barrel": part_list(entry(Part("Barrel.Child")))}, base=parent)
    game.all["WeaponBalanceDefinition"] = [parent, child]
    gun = make_gun()
    gun.DefinitionData.BalanceDefinition = child

    ids = [o.id for o in slot_named(gun, "legal", "Barrel").options]

    assert "Barrel.Child" in ids
    assert "Barrel.Parent" not in ids


def test_an_own_entry_with_weight_index_zero_is_custom_with_the_weight_reason(game):
    zero, ok = Part("Barrel.Zero"), Part("Barrel.Ok")
    balance = FakeBalance("Balance.Pistol", data={"Barrel": part_list(entry(zero, weight=0), entry(ok, weight=3))})
    game.all["WeaponBalanceDefinition"] = [balance]
    gun = make_gun()
    gun.DefinitionData.BalanceDefinition = balance

    statuses = slot_named(gun, "legal", "Barrel").statuses or {}

    assert statuses["Barrel.Ok"]["status"] == "natural"
    assert statuses["Barrel.Zero"] == {"status": "custom", "status_reason": WEIGHT_ZERO, "dlc": None}


def test_an_own_entry_for_another_manufacturer_is_custom_and_one_for_this_manufacturer_is_natural(game):
    jakobs = Part("Manufacturer.Jakobs", "ManufacturerDefinition")  # the gun's manufacturer (make_gun)
    bandit = Part("Manufacturer.Bandit", "ManufacturerDefinition")
    balance = FakeBalance(
        "Balance.Pistol",
        data={
            "Barrel": part_list(
                entry(Part("Barrel.Bandit"), manufacturers=[(bandit, 1)]),
                entry(Part("Barrel.Both"), manufacturers=[(jakobs, 2), (bandit, 0)]),
                entry(Part("Barrel.JakobsOff"), manufacturers=[(jakobs, 0)]),
            ),
        },
    )
    game.all["WeaponBalanceDefinition"] = [balance]
    gun = make_gun()
    gun.DefinitionData.BalanceDefinition = balance

    statuses = slot_named(gun, "legal", "Barrel").statuses or {}

    assert statuses["Barrel.Bandit"] == {
        "status": "custom",
        "status_reason": "In this item's own Barrel list, but only for other manufacturers (Bandit)",
        "dlc": None,
    }
    assert statuses["Barrel.Both"]["status"] == "natural"
    assert statuses["Barrel.JakobsOff"]["status"] == "custom"
    assert statuses["Barrel.JakobsOff"]["status_reason"] == (
        "In this item's own Barrel list, but with weight index 0 for this manufacturer, "
        "so it probably never drops (unverified)"
    )


# --- DLC for parts in balances that have no DlcItemSet (Fix 4) ------------------------------------


def test_a_balance_without_a_dlc_set_in_a_dlc_package_counts_as_dlc_by_name(game):
    balance = FakeBalance("GD_Orchid_Weapons.Balance.X", parts={"Barrel": [Part("GD_Orchid_Weapons.Barrel.Y")]})
    game.all["WeaponBalanceDefinition"] = [balance]

    status = barrel_statuses(make_gun())["GD_Orchid_Weapons.Barrel.Y"]

    assert status["status"] == "dlc"
    assert status["dlc"] == {"name": ORCHID_NAME, "licensed": None, "source": "package name (guess)"}


def test_a_base_package_balance_without_a_dlc_set_is_custom(game):
    balance = FakeBalance("GD_Weap_Pistol.Balance.X", parts={"Barrel": [Part("GD_Weap_Pistol.Barrel.Z")]})
    game.all["WeaponBalanceDefinition"] = [balance]

    assert barrel_statuses(make_gun())["GD_Weap_Pistol.Barrel.Z"]["status"] == "custom"


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


# --- part caches: map changes and the age shown to the player (Fix 5) -----------------------------


def set_map(game: Any, name: Any) -> None:
    """A player whose world reports `name` as its map."""
    game.pc = SimpleNamespace(WorldInfo=SimpleNamespace(GetMapName=lambda _flag=False: name))


def test_the_map_name_is_read_as_text_and_is_none_without_a_player(game):
    set_map(game, ("Map_C", 0))  # a tuple comes back from the game call
    assert gear_parts._map_name() == "Map_C"  # noqa: SLF001
    game.pc = None
    assert gear_parts._map_name() is None  # noqa: SLF001


def test_a_map_change_rebuilds_the_part_caches(game):
    dlc = FakeDlc(ORCHID_NAME)
    balance = FakeBalance("Balance.Orchid", parts={"Barrel": [Part("Barrel.Orch")]}, dlc=dlc)
    game.all["WeaponBalanceDefinition"] = [balance]
    set_map(game, "Map_A")

    gear_parts._part_universe(weapon=True)  # noqa: SLF001
    assert balance.dlc_reads == 1
    gear_parts._part_universe(weapon=True)  # noqa: SLF001 - same map: still cached
    assert balance.dlc_reads == 1

    set_map(game, "Map_B")
    gear_parts._part_universe(weapon=True)  # noqa: SLF001 - new map: every cache is rebuilt
    assert balance.dlc_reads == 2
    assert gear_parts.parts_cache_info(weapon=True)["map"] == "Map_B"


def test_an_unreadable_map_keeps_the_part_caches(game):
    balance = FakeBalance("Balance.Orchid", parts={"Barrel": [Part("Barrel.Orch")]}, dlc=FakeDlc(ORCHID_NAME))
    game.all["WeaponBalanceDefinition"] = [balance]
    set_map(game, "Map_A")
    gear_parts._part_universe(weapon=True)  # noqa: SLF001

    game.pc = None  # no player: the map cannot be read
    gear_parts._part_universe(weapon=True)  # noqa: SLF001
    game.pc = SimpleNamespace()  # a player with no world info: still unreadable
    gear_parts._part_universe(weapon=True)  # noqa: SLF001

    assert balance.dlc_reads == 1
    assert gear_parts.parts_cache_info(weapon=True)["map"] == "Map_A"


def test_the_item_detail_reports_its_part_cache(game, monkeypatch):
    set_map(game, "Map_A")
    gun = make_gun()
    use(monkeypatch, gun)

    assert gear_edit._detail(gun, gear_inventory.EQUIPPED, "legal")["parts_cache"] == {  # noqa: SLF001
        "scanned": False,
        "age_s": None,
        "map": None,
    }

    gear_parts._part_universe(weapon=True)  # noqa: SLF001
    cache = gear_edit._detail(gun, gear_inventory.EQUIPPED, "everything")["parts_cache"]  # noqa: SLF001
    assert cache["scanned"] is True
    assert cache["map"] == "Map_A"
    assert isinstance(cache["age_s"], float)


def test_a_rescan_forgets_the_map_and_the_age(game):
    set_map(game, "Map_A")
    gear_parts._part_universe(weapon=True)  # noqa: SLF001

    gear_parts.rescan_parts({})

    assert gear_parts.parts_cache_info(weapon=True) == {"scanned": False, "age_s": None, "map": None}


# --- the automatic quick check and the explicit save check ----------------------------------------

NATIVE_NAMES = (
    "CheckDefMatch", "SerialNumberMatchesDef", "CanBeSaved", "CreateSerialNumber", "PackSerialNumber",
    "UnpackSerialNumber", "IsDLCRequirementMet", "GetDLCRestrictedMessage",
)  # fmt: skip


def serial_item(
    *,
    drop: tuple[str, ...] = (),
    can_save: bool | None = True,
    match: bool = True,
    unpack_mode: str = "tuple",
) -> SimpleNamespace:
    """A gun with fake serial functions. Unpacking gives back the definition the serial came from, minus `drop`.

    unpack_mode: "tuple" returns (True, definition); "bare" returns True and fills the seed in (an out parameter).
    CreateSerialNumber is present but must never be called: the spy tests record every call.
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

    def unpack_tuple(serial: dict[str, Any], _seed: Any) -> tuple[bool, SimpleNamespace]:
        return True, values_of(serial)

    def unpack_bare(serial: dict[str, Any], seed: Any) -> bool:
        filled = values_of(serial)
        for field, value in vars(filled).items():
            setattr(seed, field, value)
        return True

    item.CreateSerialNumber = lambda: pack(item.DefinitionData)
    item.PackSerialNumber = pack
    item.UnpackSerialNumber = unpack_bare if unpack_mode == "bare" else unpack_tuple
    item.SerialNumberMatchesDef = lambda _serial, _definition, _short, _unique: match
    item.CheckDefMatch = lambda _a, _b, _short, _unique: True
    if can_save is not None:
        item.CanBeSaved = lambda: can_save
    return item


def spy(item: Any, calls: list[tuple[str, tuple[Any, ...]]]) -> list[tuple[str, tuple[Any, ...]]]:
    """Wraps every game function the item has so each call (name, arguments) is recorded. The functions still run."""

    def recording(name: str, function: Any) -> Any:
        def recorded(*args: Any) -> Any:
            calls.append((name, args))
            return function(*args)

        return recorded

    for name in NATIVE_NAMES:
        function = getattr(item, name, None)
        if callable(function):
            setattr(item, name, recording(name, function))
    return calls


def calls_named(calls: list[tuple[str, tuple[Any, ...]]], name: str) -> list[tuple[Any, ...]]:
    return [args for called, args in calls if called == name]


def use_backpack(monkeypatch: pytest.MonkeyPatch, item: Any, slots: list[Slot] | None = None) -> None:
    """Like test_gear_edit.use, but the item is in the backpack (not equipped), so the save check may run."""
    monkeypatch.setattr(gear_edit, "_find_item", lambda _params: (item, gear_inventory.BACKPACK))
    monkeypatch.setattr(gear_edit, "_slots", lambda _item, _mode: list(slots or []))


def run_check(monkeypatch: pytest.MonkeyPatch, item: Any, slots: list[Slot] | None = None, **params: Any) -> dict:
    use_backpack(monkeypatch, item, slots)
    return gear_edit.save_check_route({"id": "x", **params})


def barrel_option(part: Any) -> list[Slot]:
    return [Slot("Barrel", "BarrelPartDefinition", [PartRef(part.path, "WeaponPartDefinition")])]


def test_the_explicit_save_check_never_touches_the_live_struct(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = serial_item()
    calls = spy(item, [])
    live = item.DefinitionData
    before = state_of(item)

    result = run_check(monkeypatch, item, barrel_option(new_part), slot="Barrel", part="Barrel.New")

    assert result["save_check"]["survives"] is True
    assert result["save_check"]["restored"] is False
    names = [name for name, _args in calls]
    assert "PackSerialNumber" in names
    assert "UnpackSerialNumber" in names
    assert "CreateSerialNumber" not in names
    for name, args in calls:
        assert all(arg is not live for arg in args), f"{name} was given the live struct"
    seed = calls_named(calls, "UnpackSerialNumber")[0][1]
    assert seed is not live
    assert item.DefinitionData is live
    assert state_of(item) == before


def test_a_native_that_changes_the_live_item_is_put_back_and_reported(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = serial_item()
    real_pack = item.PackSerialNumber

    def pack_and_mutate(definition: Any) -> dict[str, Any]:
        item.DefinitionData.BarrelPartDefinition = Part("Barrel.Mutated")  # written to the LIVE struct
        return real_pack(definition)

    item.PackSerialNumber = pack_and_mutate
    before = state_of(item)

    check = run_check(monkeypatch, item, barrel_option(new_part), slot="Barrel", part="Barrel.New")["save_check"]

    assert check["restored"] is True
    assert check["survives"] is None
    assert check["summary"] == (
        "The check changed the live item, so RTSE put it back. Please send the validation dump (unverified)"
    )
    assert state_of(item) == before


def test_a_copy_that_shares_the_live_struct_is_caught_by_the_sentinel_even_with_no_changes(game, monkeypatch):
    monkeypatch.delattr(sys.modules["unrealsdk"], "make_struct", raising=False)
    item = serial_item()
    item.DefinitionData = SharingData(**vars(item.DefinitionData))
    data = item.DefinitionData
    before = dict(vars(data))

    check = run_check(monkeypatch, item)["save_check"]

    assert check["survives"] is None
    assert check["restored"] is False  # the sentinel put the live UniqueId back before anything else ran
    assert vars(data) == before
    assert data.UniqueId == 7
    assert "shared the live struct" in str(gear_edit._probe_log["struct copy"]["last_error"])  # noqa: SLF001


def test_a_copy_that_is_the_live_struct_is_refused_and_unchecked(game, monkeypatch):
    monkeypatch.delattr(sys.modules["unrealsdk"], "make_struct", raising=False)
    item = serial_item()
    item.DefinitionData = AliasingData(**vars(item.DefinitionData))
    data = item.DefinitionData
    before = dict(vars(data))

    check = run_check(monkeypatch, item)["save_check"]

    assert check["survives"] is None
    assert vars(data) == before
    assert "returned the live struct" in str(gear_edit._probe_log["struct copy"]["last_error"])  # noqa: SLF001


def test_make_struct_is_preferred_so_copy_copy_is_never_tried(game, monkeypatch):
    def fake_make_struct(_name: str, **values: Any) -> SimpleNamespace:
        return SimpleNamespace(**values)

    monkeypatch.setattr(sys.modules["unrealsdk"], "make_struct", fake_make_struct, raising=False)
    monkeypatch.setattr(SharingData, "copies", 0)
    item = serial_item()
    item.DefinitionData = SharingData(**vars(item.DefinitionData))
    data = item.DefinitionData
    before = dict(vars(data))

    check = run_check(monkeypatch, item)["save_check"]

    assert SharingData.copies == 0
    assert vars(data) == before
    assert gear_edit._probe_log["struct copy"]["last_result"] == "make_struct"  # noqa: SLF001
    assert check["survives"] is True


def test_the_explicit_check_refuses_equipped_gear_and_calls_nothing(game, monkeypatch):
    item = serial_item()
    calls = spy(item, [])
    use(monkeypatch, item)  # equipped

    with pytest.raises(ApiError) as caught:
        gear_edit.save_check_route({"id": "x"})

    assert caught.value.status == 409
    assert "unequip it first" in str(caught.value)
    assert calls == []


def test_the_explicit_check_rejects_an_unknown_slot(monkeypatch):
    use_backpack(monkeypatch, make_gun(), [Slot("Barrel", "BarrelPartDefinition", [])])
    with pytest.raises(ApiError) as caught:
        gear_edit.save_check_route({"id": "x", "slot": "Scope", "part": ""})
    assert caught.value.status == 400


def test_the_explicit_check_rejects_a_part_that_is_not_an_option(monkeypatch):
    use_backpack(monkeypatch, make_gun(), [Slot("Barrel", "BarrelPartDefinition", [])])
    with pytest.raises(ApiError) as caught:
        gear_edit.save_check_route({"id": "x", "slot": "Barrel", "part": "Barrel.Nope"})
    assert caught.value.status == 400
    assert "not available in this mode" in str(caught.value)


def test_a_part_that_is_not_loaded_is_unchecked_with_a_plain_reason(monkeypatch):
    ghost = PartRef("Barrel.Ghost", "WeaponPartDefinition")
    use_backpack(monkeypatch, make_gun(), [Slot("Barrel", "BarrelPartDefinition", [ghost], None)])

    result = gear_edit.save_check_route({"id": "x", "slot": "Barrel", "part": "Barrel.Ghost"})

    assert result["experimental"] is True
    assert result["part"] == "Barrel.Ghost"
    assert result["save_check"]["survives"] is None
    assert result["save_check"]["summary"] == (
        "Could not check: the part is not loaded right now - try rescanning parts"
    )


def test_removing_a_part_checks_the_slot_without_its_part(monkeypatch):
    item = serial_item()
    calls = spy(item, [])

    result = run_check(monkeypatch, item, [Slot("Barrel", "BarrelPartDefinition", [])], slot="Barrel", part="")

    assert result["part"] is None
    assert result["save_check"]["survives"] is True  # no barrel went in, and no barrel came back out
    assert calls_named(calls, "PackSerialNumber")[0][0].BarrelPartDefinition is None
    assert item.DefinitionData.BarrelPartDefinition.path == "Barrel.A"  # the live gun keeps its barrel


def test_a_part_the_round_trip_drops_is_reported_as_lost(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = serial_item(drop=("BarrelPartDefinition",))

    check = run_check(monkeypatch, item, barrel_option(new_part), slot="Barrel", part="Barrel.New")["save_check"]

    assert check["survives"] is False
    assert check["lost"] == [{"field": "BarrelPartDefinition", "expected": "Barrel.New", "got": None}]
    assert check["summary"] == "Lost in a serial round-trip: BarrelPartDefinition (unverified)"


def test_the_whole_item_check_runs_can_be_saved_and_a_slot_check_does_not(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part

    whole = run_check(monkeypatch, serial_item())["save_check"]
    slot = run_check(monkeypatch, serial_item(), barrel_option(new_part), slot="Barrel", part="Barrel.New")

    assert [c["name"] for c in whole["checks"]] == [
        "serial round-trip",
        "SerialNumberMatchesDef",
        "CheckDefMatch",
        "CanBeSaved",
    ]
    assert whole["survives"] is True
    assert whole["summary"] == "Survived a serial round-trip (unverified)"
    assert whole["decoder"] == "UnpackSerialNumber (native)"
    assert whole["verified"] is False
    assert "CanBeSaved" not in [c["name"] for c in slot["save_check"]["checks"]]


def test_can_be_saved_false_on_the_whole_item_means_does_not_survive(monkeypatch):
    check = run_check(monkeypatch, serial_item(can_save=False))["save_check"]
    assert check["survives"] is False
    assert check["summary"] == "CanBeSaved() returned False (unverified meaning)"


def test_a_tuple_unpack_result_is_read_as_ok_and_definition(monkeypatch):
    check = run_check(monkeypatch, serial_item(unpack_mode="tuple"))["save_check"]
    assert check["checks"][0] == {"name": "serial round-trip", "ok": True, "detail": "nothing lost"}


def test_a_bare_unpack_result_is_not_compared_against_the_seed(monkeypatch):
    # The seed is a copy of the item as it is now: comparing it would report a candidate part as lost.
    check = run_check(monkeypatch, serial_item(unpack_mode="bare"))["save_check"]
    assert check["checks"][0] == {
        "name": "serial round-trip", "ok": None, "detail": "the game gave no definition back to compare",
    }
    assert check["survives"] is None


def test_an_unpack_that_takes_no_seed_is_called_once_and_not_retried(game, monkeypatch):
    item = serial_item()
    attempts: list[int] = []

    def one_argument(*args: Any) -> tuple[bool, SimpleNamespace]:
        attempts.append(len(args))
        if len(args) != 1:
            raise TypeError("takes 1 argument")
        return True, SimpleNamespace(**args[0])

    item.UnpackSerialNumber = one_argument

    check = run_check(monkeypatch, item)["save_check"]

    assert attempts == [2]  # the one call, with the seed; no second attempt without it
    assert check["checks"][0]["ok"] is None
    assert "TypeError" in check["checks"][0]["detail"]
    assert check["survives"] is None


def test_an_offline_decoder_is_used_before_the_native_unpack(monkeypatch):
    item = serial_item()
    calls = spy(item, [])
    monkeypatch.setattr(gear_edit, "_decode_offline", lambda serial, _item: SimpleNamespace(**serial))

    check = run_check(monkeypatch, item)["save_check"]

    assert check["decoder"] == "offline (rtse.item_serial)"
    assert calls_named(calls, "UnpackSerialNumber") == []
    assert check["survives"] is True


def test_an_exception_in_serial_number_matches_def_is_one_unknown_check(monkeypatch):
    item = serial_item()

    def boom(*_args: Any) -> bool:
        raise RuntimeError("boom")

    item.SerialNumberMatchesDef = boom
    check = run_check(monkeypatch, item)["save_check"]  # must not raise
    matches = next(c for c in check["checks"] if c["name"] == "SerialNumberMatchesDef")
    assert matches["ok"] is None
    assert "the game raised RuntimeError('boom')" in matches["detail"]
    assert check["survives"] is True  # the round-trip itself still kept everything


def test_a_serial_that_does_not_match_does_not_survive(monkeypatch):
    check = run_check(monkeypatch, serial_item(match=False))["save_check"]
    assert check["survives"] is False
    assert check["summary"] == "SerialNumberMatchesDef() returned False (unverified)"


def test_an_item_without_serial_functions_is_unchecked(monkeypatch):
    check = run_check(monkeypatch, make_gun())["save_check"]
    assert check["survives"] is None
    assert all(c["ok"] is None for c in check["checks"])
    assert check["checks"][0]["detail"] == "not available on this object"


def test_the_run_never_raises_even_with_nothing_to_check(game):
    check = gear_edit._run_experimental(None, {}, whole_item=True)  # noqa: SLF001
    assert check["survives"] is None
    assert check["verified"] is False


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


class AliasingData(SimpleNamespace):
    def __copy__(self) -> Any:
        return self  # copy.copy gives back the live struct itself


# --- automatic paths: quick check only -------------------------------------------------------------


def test_item_detail_and_set_part_call_only_can_be_saved(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = serial_item()
    calls = spy(item, [])
    use_backpack(monkeypatch, item, barrel_option(new_part))

    gear_edit.item_detail({"id": "x"})
    result = gear_edit.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert {name for name, _args in calls} == {"CanBeSaved"}
    kept = result["applied"]["kept"]
    assert kept["quick_checks"] == [{"name": "CanBeSaved", "ok": True, "detail": "returned True"}]
    assert kept["save_check_available"] is True
    assert "save_check" not in kept


def test_set_part_on_equipped_gear_says_the_save_check_is_not_available(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = serial_item()
    calls = spy(item, [])
    use(monkeypatch, item, barrel_option(new_part))

    kept = gear_edit.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})["applied"]["kept"]

    assert kept["save_check_available"] is False
    assert kept["quick_checks"][0]["name"] == "CanBeSaved"
    assert {name for name, _args in calls} == {"CanBeSaved"}


def test_set_level_calls_only_can_be_saved_and_reports_the_quick_check(game, monkeypatch):
    game.pc = SimpleNamespace(GetMaximumPossiblePlayerLevelCap=lambda: 80, GetMaxOverpowerLevel=lambda: 10)
    item = serial_item()
    calls = spy(item, [])
    use_backpack(monkeypatch, item)

    kept = gear_edit.set_level({"id": "x", "level": 6})["applied"]["kept"]

    assert {name for name, _args in calls} == {"CanBeSaved"}
    assert kept["quick_checks"] == [{"name": "CanBeSaved", "ok": True, "detail": "returned True"}]
    assert kept["save_check_available"] is True


def test_the_quick_check_is_empty_for_items_that_are_not_weapons():
    assert gear_edit._quick_checks(make_item(FakeBalance("Balance.Shield", item=True))) == []  # noqa: SLF001


def test_the_debug_dump_runs_no_serial_function(game, monkeypatch, tmp_path):
    target = tmp_path / "debug_validation.json"
    monkeypatch.setattr(gear_edit, "DEBUG_VALIDATION_FILE", target)
    item = serial_item()
    calls = spy(item, [])
    use_backpack(monkeypatch, item)

    result = gear_edit.debug_validation({"id": "x"})

    assert result["saved_to"] == str(target)
    assert result["functions"]["CheckDefMatch"] is True
    assert result["functions"]["IsDLCRequirementMet"] is False
    assert {name for name, _args in calls} <= {"CanBeSaved"}
    dump = json.loads(target.read_text(encoding="utf-8"))
    assert "unverified" in dump["note"]
    assert "save_check" not in dump
    assert dump["quick_checks"] == [{"name": "CanBeSaved", "ok": True, "detail": "returned True"}]
    assert "parts_cache" in dump
    assert "status_counts" in dump
    assert "CreateSerialNumber" not in dump["probe_log"]
    assert "PackSerialNumber" not in dump["probe_log"]


# --- set_part and set_level report what the game kept ---------------------------------------------


def test_set_part_reports_what_the_game_kept(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = make_gun(
        uid=7,
        on_init=lambda data: setattr(item, "ClipSize", 12) if data.BarrelPartDefinition.path == "Barrel.New" else None,
    )
    use_backpack(monkeypatch, item, barrel_option(new_part))

    kept = gear_edit.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})["applied"]["kept"]

    assert kept["name"] == {"before": "Logan's Gun", "after": "Logan's Gun"}
    assert len(kept["parts"]) == len(WEAPON_SLOTS)
    barrel = next(p for p in kept["parts"] if p["slot"] == "Barrel")
    assert barrel == {"slot": "Barrel", "before": "A", "after": "New", "changed": True, "edited": True}
    assert not any(p["changed"] for p in kept["parts"] if p["slot"] != "Barrel")
    assert not any(p["edited"] for p in kept["parts"] if p["slot"] != "Barrel")
    label = next(s.label for s in stats.WEAPON_STATS if s.name == "ClipSize")
    assert kept["stats"] == [{"name": "ClipSize", "label": label, "before": 10, "after": 12}]
    assert kept["quick_checks"] == [{"name": "CanBeSaved", "ok": None, "detail": "not available on this object"}]
    assert kept["save_check_available"] is True
    assert "save_check" not in kept


def test_set_level_reports_what_the_game_kept(game, monkeypatch):
    game.pc = SimpleNamespace(GetMaximumPossiblePlayerLevelCap=lambda: 80, GetMaxOverpowerLevel=lambda: 10)
    item = make_gun(stage=5)
    use(monkeypatch, item)

    kept = gear_edit.set_level({"id": "x", "level": 6})["applied"]["kept"]

    assert kept["name"]["after"] == "Logan's Gun"
    assert len(kept["parts"]) == len(WEAPON_SLOTS)
    assert not any(p["changed"] or p["edited"] for p in kept["parts"])
    assert kept["stats"] == []
    assert kept["save_check_available"] is False  # equipped
    assert "quick_checks" in kept


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


# --- routes ---------------------------------------------------------------------------------------


def test_the_new_paths_are_declared_once_and_by_gear_edit():
    for path in ("/api/item/save_check", "/api/debug/validation"):
        owners = [m.__name__ for m in api.ROUTE_MODULES if path in m.GET_ROUTES]
        assert owners == ["rtse.gear_edit"], path


def test_the_removed_check_part_route_is_gone():
    assert "/api/item/check_part" not in api.GET_ROUTES
    assert "/api/item/check_part" not in api.POST_ROUTES
