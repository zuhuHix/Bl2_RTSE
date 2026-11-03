"""Part status (natural / DLC / custom / unsupported / unknown), the automatic quick check, the inherited and weighted
part lists, and the part caches. There is no save check in this module (it comes back later, offline).

Every game call here is a fake (see unreal_stub.py and test_gear_edit.py), so these tests pin RTSE's own logic only:
they say nothing about what the real game does. Every weight, DLC and CanBeSaved result is UNVERIFIED in game.
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


# A balance's ConsolidatedAttributeInitData in these fakes: index 0 and 2 are positive weights, index 1 is zero.
DEFAULT_WEIGHTS = (1.0, 0.0, 2.0)


def caid_entry(weight: float, *, runtime: bool = False) -> SimpleNamespace:
    """One ConsolidatedAttributeInitData entry: a constant weight, or one whose value depends on a runtime object."""
    return SimpleNamespace(
        BaseValueConstant=weight,
        BaseValueScaleConstant=1.0,
        BaseValueAttribute=None,
        InitializationDefinition=Part("Init.Runtime", "AttributeInitializationDefinition") if runtime else None,
    )


def entry(part: Any, *, weight: int | None = 0, manufacturers: list[tuple[Any, int]] | None = None) -> Any:
    """One WeightedParts entry. `weight` is the part-level DefaultWeightIndex; None leaves that field off the struct."""
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
            found = parts.get(slot)  # every part gets part-level DefaultWeightIndex 0: a positive weight
            if not found:
                return None
            return SimpleNamespace(WeightedParts=[SimpleNamespace(Part=p, DefaultWeightIndex=0) for p in found])

        collection = SimpleNamespace(
            ConsolidatedAttributeInitData=[caid_entry(w) for w in DEFAULT_WEIGHTS],
            **{f"{s}PartData": weighted(s) for s in (ITEM_SLOTS if item else WEAPON_SLOTS)},
        )
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
WEIGHT_ZERO = "In this item's own Barrel list, but its drop weight is 0, so it never drops naturally (unverified)"
WEIGHT_UNKNOWN = "In this item's own Barrel list, but its drop weight could not be read (unverified)"
UNKNOWN_DLC = "From another balance; whether it needs a DLC could not be told"
NO_WEIGHT = {"status": "unknown", "status_reason": WEIGHT_UNKNOWN, "dlc": None, "unknown": "weight"}


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
    # A base-game package name: positive evidence that the balance is base game (see _is_base_package)
    plain = FakeBalance(
        "GD_Weap_Pistol.Balance.Plain", parts={"Barrel": [parts["mixed"], parts["other"], parts["item"]]},
    )
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
    # Not a DLC hint, and not a base-game prefix either: no positive evidence, so the DLC status is unknown
    ref = PartRef("GD_Orchidish_Weapons.Barrel.X", "WeaponPartDefinition")
    status = gear_parts.part_status(ref, slot="Barrel", weapon=True, legal_ids=set(), own_classes=set())
    assert status == {"status": "unknown", "status_reason": UNKNOWN_DLC, "dlc": None, "unknown": "dlc"}


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


KEEP_MANUFACTURER = object()  # own_barrel's default: make_gun's Jakobs manufacturer


def own_barrel(game: Any, balance: FakeBalance, manufacturer: Any = KEEP_MANUFACTURER) -> SimpleNamespace:
    """A make_gun() whose own balance is `balance`, with the given manufacturer (None included) when one is passed."""
    game.all["WeaponBalanceDefinition"] = [balance]
    gun = make_gun()
    gun.DefinitionData.BalanceDefinition = balance
    if manufacturer is not KEEP_MANUFACTURER:
        gun.DefinitionData.ManufacturerDefinition = manufacturer
    return gun


def test_an_own_entry_whose_index_points_at_weight_zero_is_custom_with_the_weight_reason(game):
    zero, ok = Part("Barrel.Zero"), Part("Barrel.Ok")
    balance = FakeBalance("Balance.Pistol", data={"Barrel": part_list(entry(zero, weight=1), entry(ok, weight=2))})
    gun = own_barrel(game, balance)

    statuses = slot_named(gun, "legal", "Barrel").statuses or {}

    assert statuses["Barrel.Ok"]["status"] == "natural"
    assert statuses["Barrel.Zero"] == {"status": "custom", "status_reason": WEIGHT_ZERO, "dlc": None, "unknown": None}


def test_an_empty_manufacturer_list_ignores_the_index_and_means_weight_100(game):
    # Regression: the part-level index 1 points at weight 0, but an EMPTY Manufacturers array means weight 100
    # (BLCMods parts guide). A missing Manufacturers field still falls back to the part-level index.
    empty, missing = Part("Barrel.EmptyList"), Part("Barrel.NoField")
    balance = FakeBalance("Balance.Pistol", data={
        "Barrel": part_list(entry(empty, weight=1, manufacturers=[]), entry(missing, weight=1)),
    })
    gun = own_barrel(game, balance)

    statuses = slot_named(gun, "legal", "Barrel").statuses or {}

    assert statuses["Barrel.EmptyList"]["status"] == "natural"
    assert statuses["Barrel.NoField"] == {"status": "custom", "status_reason": WEIGHT_ZERO, "dlc": None, "unknown": None}


def test_an_own_entry_for_another_manufacturer_is_custom_and_one_for_this_manufacturer_is_natural(game):
    jakobs = Part("Manufacturer.Jakobs", "ManufacturerDefinition")  # the gun's manufacturer (make_gun)
    bandit = Part("Manufacturer.Bandit", "ManufacturerDefinition")
    balance = FakeBalance(
        "Balance.Pistol",
        data={
            "Barrel": part_list(
                entry(Part("Barrel.Bandit"), manufacturers=[(bandit, 1)]),
                entry(Part("Barrel.Both"), manufacturers=[(jakobs, 2), (bandit, 1)]),
                entry(Part("Barrel.JakobsOff"), manufacturers=[(jakobs, 1)]),
            ),
        },
    )
    gun = own_barrel(game, balance)

    statuses = slot_named(gun, "legal", "Barrel").statuses or {}

    assert statuses["Barrel.Bandit"] == {
        "status": "custom",
        "status_reason": "In this item's own Barrel list, but only for other manufacturers (Bandit)",
        "dlc": None,
        "unknown": None,
    }
    assert statuses["Barrel.Both"]["status"] == "natural"
    assert statuses["Barrel.JakobsOff"] == {
        "status": "custom", "status_reason": WEIGHT_ZERO, "dlc": None, "unknown": None,
    }


# --- drop weights: DefaultWeightIndex indexes ConsolidatedAttributeInitData (UNVERIFIED in game) ----


def test_an_index_that_points_at_a_positive_weight_is_natural(game):
    balance = FakeBalance("Balance.Pistol", data={"Barrel": part_list(entry(Part("Barrel.Heavy"), weight=2))})
    gun = own_barrel(game, balance)
    assert slot_named(gun, "legal", "Barrel").statuses["Barrel.Heavy"]["status"] == "natural"


def test_an_index_out_of_range_is_unknown_weight_and_never_custom(game):
    balance = FakeBalance("Balance.Pistol", data={"Barrel": part_list(entry(Part("Barrel.Far"), weight=9))})
    gun = own_barrel(game, balance)
    assert slot_named(gun, "legal", "Barrel").statuses["Barrel.Far"] == NO_WEIGHT


def test_an_entry_whose_value_depends_on_a_runtime_object_is_unknown_weight(game):
    balance = FakeBalance("Balance.Pistol", data={"Barrel": part_list(entry(Part("Barrel.Runtime"), weight=0))})
    balance.RuntimePartListCollection.ConsolidatedAttributeInitData = [caid_entry(1.0, runtime=True)]
    gun = own_barrel(game, balance)
    assert slot_named(gun, "legal", "Barrel").statuses["Barrel.Runtime"] == NO_WEIGHT


def test_a_balance_without_a_consolidated_attribute_array_is_unknown_weight(game):
    balance = FakeBalance("Balance.Pistol", data={"Barrel": part_list(entry(Part("Barrel.Bare"), weight=0))})
    del balance.RuntimePartListCollection.ConsolidatedAttributeInitData
    gun = own_barrel(game, balance)
    assert slot_named(gun, "legal", "Barrel").statuses["Barrel.Bare"] == NO_WEIGHT


def test_an_entry_without_a_default_weight_index_is_unknown_weight(game):
    balance = FakeBalance("Balance.Pistol", data={"Barrel": part_list(entry(Part("Barrel.NoIndex"), weight=None))})
    gun = own_barrel(game, balance)
    assert slot_named(gun, "legal", "Barrel").statuses["Barrel.NoIndex"] == NO_WEIGHT


def test_a_non_integer_index_is_unknown_weight(game):
    balance = FakeBalance("Balance.Pistol", data={"Barrel": part_list(entry(Part("Barrel.Float"), weight=1.0))})
    gun = own_barrel(game, balance)
    assert slot_named(gun, "legal", "Barrel").statuses["Barrel.Float"] == NO_WEIGHT


def test_a_weight_that_scales_to_zero_is_custom_with_the_weight_reason(game):
    scaled_to_zero = SimpleNamespace(
        BaseValueConstant=2.0, BaseValueScaleConstant=0.0, BaseValueAttribute=None, InitializationDefinition=None,
    )
    balance = FakeBalance("Balance.Pistol", data={"Barrel": part_list(entry(Part("Barrel.Scaled"), weight=0))})
    balance.RuntimePartListCollection.ConsolidatedAttributeInitData = [scaled_to_zero]
    gun = own_barrel(game, balance)
    assert slot_named(gun, "legal", "Barrel").statuses["Barrel.Scaled"]["status"] == "custom"  # 2.0 x 0.0 = 0


def test_the_weight_reader_gives_none_for_anything_it_cannot_read():
    source = SimpleNamespace(ConsolidatedAttributeInitData=[caid_entry(1.0), None])
    assert gear_parts._resolve_weight(source, 0) == 1.0  # noqa: SLF001
    assert gear_parts._resolve_weight(source, 1) is None  # noqa: SLF001 - a None entry
    assert gear_parts._resolve_weight(source, True) is None  # noqa: SLF001 - a bool is not an index
    assert gear_parts._resolve_weight(None, 0) is None  # noqa: SLF001 - no source at all
    assert gear_parts._resolve_weight(SimpleNamespace(), 0) is None  # noqa: SLF001 - no array field


# --- manufacturers in an own-list entry (UNVERIFIED in game) ---------------------------------------


def test_a_default_manufacturer_entry_applies_to_every_manufacturer_with_the_part_level_index(game):
    dahl = Part("Manufacturer.Dahl", "ManufacturerDefinition")
    # The default entry's own index (5) is out of range; the part-level index (0, positive) is the one that applies
    part = Part("Barrel.Anyone")
    balance = FakeBalance(
        "Balance.Pistol", data={"Barrel": part_list(entry(part, weight=0, manufacturers=[(None, 5)]))},
    )
    jakobs_gun = own_barrel(game, balance)
    dahl_gun = own_barrel(game, balance, dahl)

    for gun in (jakobs_gun, dahl_gun):
        assert slot_named(gun, "legal", "Barrel").statuses["Barrel.Anyone"]["status"] == "natural"


def test_a_mixed_manufacturer_list_is_custom_for_its_manufacturer_and_natural_for_the_rest(game):
    bandit = Part("Manufacturer.Bandit", "ManufacturerDefinition")
    dahl = Part("Manufacturer.Dahl", "ManufacturerDefinition")
    part = Part("Barrel.Mixed")
    balance = FakeBalance(
        "Balance.Pistol",
        data={"Barrel": part_list(entry(part, weight=0, manufacturers=[(bandit, 1), (None, 5)]))},
    )
    bandit_gun = own_barrel(game, balance, bandit)
    dahl_gun = own_barrel(game, balance, dahl)

    assert slot_named(bandit_gun, "legal", "Barrel").statuses["Barrel.Mixed"] == {
        "status": "custom", "status_reason": WEIGHT_ZERO, "dlc": None, "unknown": None,
    }
    assert slot_named(dahl_gun, "legal", "Barrel").statuses["Barrel.Mixed"]["status"] == "natural"


def test_a_manufacturer_list_that_names_no_default_and_not_this_manufacturer_is_restricted(game):
    bandit = Part("Manufacturer.Bandit", "ManufacturerDefinition")
    other = entry(Part("Barrel.Other"), manufacturers=[(bandit, 2)])
    balance = FakeBalance("Balance.Pistol", data={"Barrel": part_list(other)})
    gun = own_barrel(game, balance)  # the Jakobs gun from make_gun
    assert slot_named(gun, "legal", "Barrel").statuses["Barrel.Other"] == {
        "status": "custom",
        "status_reason": "In this item's own Barrel list, but only for other manufacturers (Bandit)",
        "dlc": None,
        "unknown": None,
    }


def test_an_item_whose_manufacturer_cannot_be_read_is_unknown_weight_not_custom(game):
    bandit = Part("Manufacturer.Bandit", "ManufacturerDefinition")
    unclear = entry(Part("Barrel.Unclear"), manufacturers=[(bandit, 1)])
    balance = FakeBalance("Balance.Pistol", data={"Barrel": part_list(unclear)})
    gun = own_barrel(game, balance, manufacturer=None)
    assert slot_named(gun, "legal", "Barrel").statuses["Barrel.Unclear"] == NO_WEIGHT


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


# --- base game only with positive evidence (UNVERIFIED: the package-name table is a guess) ---------


def test_a_part_owned_only_by_a_mod_balance_is_unknown_dlc_not_base(game):
    balance = FakeBalance("Mod_Custom.Balance.X", parts={"Barrel": [Part("Mod_Custom.Barrel.Q")]})
    game.all["WeaponBalanceDefinition"] = [balance]

    status = barrel_statuses(make_gun())["Mod_Custom.Barrel.Q"]

    assert status == {"status": "unknown", "status_reason": UNKNOWN_DLC, "dlc": None, "unknown": "dlc"}


def test_a_part_owned_by_a_base_prefix_balance_without_a_dlc_set_is_custom(game):
    part = Part("GD_Weap_Pistol.Barrel.Bandit")
    balance = FakeBalance("GD_Weap_Pistol.A_Weapons.Pistol_Bandit", parts={"Barrel": [part]})
    game.all["WeaponBalanceDefinition"] = [balance]

    status = barrel_statuses(make_gun())[part.path]

    assert status["status"] == "custom"
    assert status["unknown"] is None


def test_a_part_owned_by_a_base_balance_and_a_mod_balance_is_custom_because_base_wins(game):
    part = Part("GD_Weap_Pistol.Barrel.Shared")
    base = FakeBalance("GD_Weap_Pistol.A_Weapons.Pistol_Bandit", parts={"Barrel": [part]})
    mod = FakeBalance("Mod_Custom.Balance.X", parts={"Barrel": [part]})
    game.all["WeaponBalanceDefinition"] = [base, mod]

    assert barrel_statuses(make_gun())[part.path]["status"] == "custom"


def test_a_part_owned_by_a_dlc_balance_and_a_mod_balance_is_dlc(game):
    game.pc = SimpleNamespace()
    part = Part("Barrel.Orch")
    dlc_balance = FakeBalance("Balance.Orchid", parts={"Barrel": [part]}, dlc=FakeDlc(ORCHID_NAME))
    mod = FakeBalance("Mod_Custom.Balance.X", parts={"Barrel": [part]})
    game.all["WeaponBalanceDefinition"] = [dlc_balance, mod]

    assert barrel_statuses(make_gun())[part.path]["status"] == "dlc"


def test_without_a_universe_a_mod_package_part_is_unknown_dlc():
    ref = PartRef("Mod_Custom.Barrel.Q", "WeaponPartDefinition")
    status = gear_parts.part_status(ref, slot="Barrel", weapon=True, legal_ids=set(), own_classes=set())
    assert status == {"status": "unknown", "status_reason": UNKNOWN_DLC, "dlc": None, "unknown": "dlc"}


def test_without_a_universe_a_base_package_part_is_custom():
    ref = PartRef("GD_Weap_Pistol.Barrel.Z", "WeaponPartDefinition")
    status = gear_parts.part_status(ref, slot="Barrel", weapon=True, legal_ids=set(), own_classes=set())
    assert status["status"] == "custom"


def test_base_package_names_must_not_name_a_dlc():
    assert gear_parts._is_base_package("GD_Weap_Pistol.A_Weapons.Pistol_Bandit")  # noqa: SLF001
    assert gear_parts._is_base_package("GD_Shields.Balance.X")  # noqa: SLF001
    assert not gear_parts._is_base_package("GD_Weap_Orchid.Balance.X")  # noqa: SLF001 - a DLC codename
    assert not gear_parts._is_base_package("GD_Orchid_Weapons.Balance.X")  # noqa: SLF001
    assert not gear_parts._is_base_package("Mod_Custom.Balance.X")  # noqa: SLF001
    assert not gear_parts._is_base_package("")  # noqa: SLF001


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


# --- the automatic quick check: CanBeSaved() is the only native call on an item -------------------

NATIVE_NAMES = (
    "CheckDefMatch", "SerialNumberMatchesDef", "CanBeSaved", "CreateSerialNumber", "PackSerialNumber",
    "UnpackSerialNumber", "IsDLCRequirementMet", "GetDLCRestrictedMessage",
)  # fmt: skip
SERIAL_NAMES = (
    "CreateSerialNumber", "PackSerialNumber", "UnpackSerialNumber", "SerialNumberMatchesDef", "CheckDefMatch",
)


def tripwire_gun(*, can_save: bool | None = True) -> SimpleNamespace:
    """A gun whose serial functions raise AssertionError when called. CanBeSaved is the only one allowed to run.

    RTSE's probe catches the raise, so these tests rely on spy() to prove that no serial function was called.
    """
    item = make_gun(uid=7)

    def tripwire(name: str) -> Any:
        def raise_when_called(*_args: Any) -> None:
            raise AssertionError(f"{name} must not be called on an item")

        return raise_when_called

    for name in SERIAL_NAMES:
        setattr(item, name, tripwire(name))
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


def use_backpack(monkeypatch: pytest.MonkeyPatch, item: Any, slots: list[Slot] | None = None) -> None:
    """Like test_gear_edit.use, but the item is in the backpack (not equipped)."""
    monkeypatch.setattr(gear_edit, "_find_item", lambda _params: (item, gear_inventory.BACKPACK))
    monkeypatch.setattr(gear_edit, "_slots", lambda _item, _mode: list(slots or []))


def barrel_option(part: Any) -> list[Slot]:
    return [Slot("Barrel", "BarrelPartDefinition", [PartRef(part.path, "WeaponPartDefinition")])]


def test_item_detail_and_set_part_call_only_can_be_saved(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = tripwire_gun()
    calls = spy(item, [])
    use_backpack(monkeypatch, item, barrel_option(new_part))

    gear_edit.item_detail({"id": "x"})
    result = gear_edit.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})

    assert {name for name, _args in calls} == {"CanBeSaved"}
    kept = result["applied"]["kept"]
    assert kept["quick_checks"] == [{"name": "CanBeSaved", "ok": True, "detail": "returned True"}]
    assert "save_check_available" not in kept
    assert "save_check" not in kept


def test_set_part_on_equipped_gear_calls_only_can_be_saved(game, monkeypatch):
    new_part = Part("Barrel.New")
    game.objects[("WeaponPartDefinition", "Barrel.New")] = new_part
    item = tripwire_gun()
    calls = spy(item, [])
    use(monkeypatch, item, barrel_option(new_part))

    kept = gear_edit.set_part({"id": "x", "slot": "Barrel", "part": "Barrel.New"})["applied"]["kept"]

    assert kept["quick_checks"][0]["name"] == "CanBeSaved"
    assert {name for name, _args in calls} == {"CanBeSaved"}


def test_set_level_calls_only_can_be_saved_and_reports_the_quick_check(game, monkeypatch):
    game.pc = SimpleNamespace(GetMaximumPossiblePlayerLevelCap=lambda: 80, GetMaxOverpowerLevel=lambda: 10)
    item = tripwire_gun()
    calls = spy(item, [])
    use_backpack(monkeypatch, item)

    kept = gear_edit.set_level({"id": "x", "level": 6})["applied"]["kept"]

    assert {name for name, _args in calls} == {"CanBeSaved"}
    assert kept["quick_checks"] == [{"name": "CanBeSaved", "ok": True, "detail": "returned True"}]
    assert "save_check_available" not in kept


def test_the_quick_check_is_empty_for_items_that_are_not_weapons():
    assert gear_edit._quick_checks(make_item(FakeBalance("Balance.Shield", item=True))) == []  # noqa: SLF001


def test_the_debug_dump_runs_no_serial_function(game, monkeypatch, tmp_path):
    target = tmp_path / "debug_validation.json"
    monkeypatch.setattr(gear_edit, "DEBUG_VALIDATION_FILE", target)
    item = tripwire_gun()
    calls = spy(item, [])
    use_backpack(monkeypatch, item)

    result = gear_edit.debug_validation({"id": "x"})

    assert result["saved_to"] == str(target)
    assert set(result["functions"]) == {"CanBeSaved", "IsDLCRequirementMet", "GetDLCRestrictedMessage"}  # no serial names
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


def test_the_debug_dump_counts_an_unknown_part_under_unknown(game, monkeypatch, tmp_path):
    target = tmp_path / "debug_validation.json"
    monkeypatch.setattr(gear_edit, "DEBUG_VALIDATION_FILE", target)
    unknown = {"Barrel.Q": {"status": "unknown", "status_reason": WEIGHT_UNKNOWN, "dlc": None, "unknown": "weight"}}
    slot = Slot("Barrel", "BarrelPartDefinition", [PartRef("Barrel.Q", "WeaponPartDefinition")], unknown)
    use_backpack(monkeypatch, make_gun(), [slot])

    gear_edit.debug_validation({"id": "x"})

    dump = json.loads(target.read_text(encoding="utf-8"))
    assert dump["status_counts"]["Barrel"] == {"options": 1, "unknown": 1}


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
    assert "save_check_available" not in kept
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
    assert "save_check_available" not in kept
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
    owners = [m.__name__ for m in api.ROUTE_MODULES if "/api/debug/validation" in m.GET_ROUTES]
    assert owners == ["rtse.gear_edit"]


def test_the_removed_check_part_route_is_gone():
    assert "/api/item/check_part" not in api.GET_ROUTES
    assert "/api/item/check_part" not in api.POST_ROUTES


def test_the_save_check_route_is_gone():
    assert "/api/item/save_check" not in api.GET_ROUTES
    assert not hasattr(gear_edit, "save_check_route")
    assert not hasattr(gear_edit, "_independent_def")
