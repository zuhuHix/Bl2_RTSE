"""Gear part discovery: which parts each slot can take, part labels and what each part does.

Read-only. "Legal" parts mirror how the game decides what an item may have; the wider modes offer parts from
every loaded balance definition. The gear_* modules are the gear backend; api.py routes to them.

Game calls made here, both UNVERIFIED in game: IsLicensed() on a DLC set when a part's status is worked out, and
WorldInfo.GetMapName() to notice a map change. Nothing here calls a serial function or changes an item.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from enum import Enum
from typing import Any, NamedTuple

from mods_base import get_pc
from unrealsdk import find_all, find_object, logging, unreal

from .routing import ApiError, Params, Route

# --- Part slot discovery -------------------------------------------------------------------------
# "Legal" parts mirror how the game decides what an item may have: its balance definition owns a part
# list collection with one weighted list per slot. The wider modes ignore that and offer parts from
# every balance definition that is currently loaded, so you can build guns the game would never roll.

WEAPON_SLOTS = (
    "Body", "Grip", "Barrel", "Sight", "Stock", "Elemental", "Accessory1", "Accessory2", "Material",
)  # fmt: skip
ITEM_SLOTS = ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta", "Material")

# Item definitions which have no parts at all
PARTLESS_ITEM_DEFINITIONS = {
    "UsableCustomizationItemDefinition",
    "UsableItemDefinition",
    "MissionItemDefinition",
}

MODE_LEGAL = "legal"  # Only parts this exact item could roll
MODE_SAME_SLOT = "same_slot"  # Any part used in that slot by any weapon/item (e.g. every barrel)
MODE_EVERYTHING = "everything"  # Any part from any slot, in any slot
MODES = (MODE_LEGAL, MODE_SAME_SLOT, MODE_EVERYTHING)


class PartRef(NamedTuple):
    """A part identified by class and path. Never a live object: those can be freed on map change."""

    id: str
    class_name: str
    meshes: tuple[str, ...] = ()  # names of the gestalt mesh pieces this part draws


class Slot(NamedTuple):
    name: str  # Display name, e.g. "Barrel"
    field: str  # DefinitionData field, e.g. "BarrelPartDefinition"
    options: list[PartRef]
    statuses: dict[str, dict[str, Any]] | None = None  # part id -> status, status_reason, dlc (see part_status)


def _mesh_names(part: unreal.UObject) -> tuple[str, ...]:
    """The gestalt mesh pieces a part draws, e.g. ("Pistol_Barrel_Dahl",). Empty for parts without one."""
    if part.Name.endswith("_None"):
        return ()  # "no accessory" parts still name a placeholder mesh that the game does not draw
    names: list[str] = []
    try:
        main = part.GestaltModeSkeletalMeshName
        if main is not None:
            names.append(str(main))
    except AttributeError:
        pass
    try:
        names.extend(str(n) for n in part.AdditionalGestaltModeSkeletalMeshNames)
    except (AttributeError, TypeError):
        pass
    return tuple(n for n in names if n and n != "None")


def _ref(part: unreal.UObject) -> PartRef:
    return PartRef(part._path_name(), part.Class.Name, _mesh_names(part))  # noqa: SLF001


# Only name parts carry in-game text. Class data verified: these two have a `PartName` (Str) field.
# WeaponPartDefinition and ItemPartDefinition have no display-name field, so they get no label.
# The runtime text itself is unverified until read in game.
NAME_PART_CLASSES = ("WeaponNamePartDefinition", "ItemNamePartDefinition")


def _text_of(part: unreal.UObject | None) -> str | None:
    """A name part's in-game text (e.g. "Logan's Gun"), or None for any other part or when it can't be read."""
    if part is None or part.Class.Name not in NAME_PART_CLASSES:
        return None
    try:
        return str(getattr(part, "PartName", "") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def _part_label(ref: PartRef) -> str | None:
    """The in-game label for a part reference. Looked up only for name parts, so the part scan never pays for it."""
    if ref.class_name not in NAME_PART_CLASSES:
        return None
    try:
        part = find_object(ref.class_name, ref.id)
    except Exception:  # noqa: BLE001
        return None
    return _text_of(part)


def _ref_dict(ref: PartRef) -> dict[str, Any]:
    package, _, name = ref.id.rpartition(".")
    return {
        "id": ref.id,
        "name": name or ref.id,
        "group": package,
        "class": ref.class_name,
        "meshes": list(ref.meshes),
        "label": _part_label(ref),  # real in-game text, or None: the browser then infers a label from tokens
    }


def _part_ref(part: unreal.UObject | None) -> dict[str, str] | None:
    return None if part is None else _ref_dict(_ref(part))


def _weighted_parts(weighted: unreal.WrappedStruct | None) -> list[unreal.UObject]:
    if not weighted:
        return []
    return [entry.Part for entry in weighted.WeightedParts or () if entry.Part is not None]



# A balance keeps one struct per slot (<Slot>PartData: WeightedParts and bEnabled). A child balance that disables a
# slot inherits it from its BaseDefinition. Class data and modding knowledge only: UNVERIFIED in game. The entries
# carry Part, DefaultWeightIndex (a byte INDEX, not a probability) and Manufacturers (each with its own index).


def _slot_data(balance: Any, slot: str, *, weapon: bool) -> tuple[Any, Any]:
    """One balance level's struct for a slot, and the object it sits on: the part list collection, or the shield
    definition for its <Slot>Parts. (None, None) when this level has no collection for the slot."""
    if weapon:
        collection = _attr(balance, "RuntimePartListCollection")
        if collection is None:
            collection = _attr(balance, "WeaponPartListCollection")
        return (None, None) if collection is None else (_attr(collection, f"{slot}PartData"), collection)
    inventory_definition = _attr(balance, "InventoryDefinition")
    if (
        inventory_definition is not None
        and _attr(_attr(inventory_definition, "Class"), "Name") == "ShieldDefinition"
        and not _attr(balance, "PartListCollection")
    ):
        # Shields without a part list collection keep their parts on the definition itself
        return _attr(inventory_definition, f"{slot}Parts"), inventory_definition
    is_class_mod = _attr(_attr(balance, "Class"), "Name") == "ClassModBalanceDefinition"
    collection = _attr(balance, "RuntimePartListCollection" if is_class_mod else "PartListCollection")
    return (None, None) if collection is None else (_attr(collection, f"{slot}PartData"), collection)


def _as_list(value: Any) -> list[Any]:
    """An array property as a list of its non-None items; [] when it is missing or unreadable."""
    if value is None:
        return []
    try:
        return [item for item in value if item is not None]
    except Exception:  # noqa: BLE001
        return []


def _slot_enabled(data: Any) -> bool:
    """bEnabled when the struct has it. Without the field, a slot counts as enabled when it has entries."""
    if data is None:
        return False
    enabled = _attr(data, "bEnabled")
    if enabled is not None:
        return bool(enabled)
    return bool(_as_list(_attr(data, "WeightedParts")))


class _OwnList(NamedTuple):
    """One slot's WeightedParts entries, and the object whose ConsolidatedAttributeInitData their weights index into."""

    entries: list[Any]
    source: Any  # the part list collection (or shield definition) the slot came from; None when there is no list


def _slot_entries(balance: Any, slot: str, *, weapon: bool) -> _OwnList:
    """The WeightedParts entries that govern one slot: this balance's when enabled, else the first enabled parent's.

    UNVERIFIED: the inheritance through bEnabled is inferred from class data and modding knowledge.
    """
    for level in _balance_chain(balance):
        data, source = _slot_data(level, slot, weapon=weapon)
        if _slot_enabled(data):
            return _OwnList(_as_list(_attr(data, "WeightedParts")), source)
    return _OwnList([], None)


def _effective_entries(balance: Any, *, weapon: bool) -> dict[str, _OwnList]:
    return {slot: _slot_entries(balance, slot, weapon=weapon) for slot in (WEAPON_SLOTS if weapon else ITEM_SLOTS)}


def _parts_of(entries: list[Any]) -> list[unreal.UObject]:
    return [part for part in (_attr(entry, "Part") for entry in entries) if part is not None]


def _is_weapon(item: unreal.UObject) -> bool:
    return item.Class.Name == "WillowWeapon"


def _is_partless(item: unreal.UObject) -> bool:
    if _is_weapon(item):
        return False
    definition = item.DefinitionData.ItemDefinition
    return definition is not None and definition.Class.Name in PARTLESS_ITEM_DEFINITIONS


def _legal_entries(item: unreal.UObject) -> dict[str, _OwnList]:
    """Each slot's own WeightedParts entries for this item's balance, inherited parts included. {} without a balance."""
    balance = item.DefinitionData.BalanceDefinition
    if balance is None:
        return {}
    return _effective_entries(balance, weapon=_is_weapon(item))


def _manufacturer_names(manufacturers: list[Any]) -> str:
    named = [m for m in manufacturers if _attr(m, "Manufacturer") is not None]
    names = [_text(_attr(_attr(m, "Manufacturer"), "Name")) for m in named]
    return ", ".join([n for n in names if n][:3]) or "unknown"


def _resolve_weight(source: Any, index: Any) -> float | None:
    """The drop weight a DefaultWeightIndex points at: the ConsolidatedAttributeInitData entry at that index, scaled.

    The index is an index into that array, not a probability. None when anything is missing or unreadable, when the
    index is not an int or is out of range, or when the entry depends on a runtime value (BaseValueAttribute or
    InitializationDefinition set). UNVERIFIED: the index-to-entry reading and BaseValueConstant x BaseValueScaleConstant
    come from class data and the BLCMods wiki page "Understanding Item and Weapon Parts Definitions", not the game.
    """
    try:
        caid = list(source.ConsolidatedAttributeInitData)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(caid):
        return None
    entry = caid[index]
    if entry is None or _attr(entry, "BaseValueAttribute") is not None:
        return None
    if _attr(entry, "InitializationDefinition") is not None:
        return None
    constant = _plain_number(_attr(entry, "BaseValueConstant"))
    scale = _plain_number(_attr(entry, "BaseValueScaleConstant"))
    return None if constant is None or scale is None else constant * scale


def _entry_outcome(entry: Any, item: unreal.UObject, slot: str, source: Any) -> tuple[str, str]:
    """How one own-list entry stands on this item: ("natural", ""), ("custom", reason) or ("unknown", reason).

    Which index applies: an entry for this item's manufacturer uses that entry's DefaultWeightIndex. With no
    manufacturer list, or with a default entry (Manufacturer None, which applies to every manufacturer), the
    part-level DefaultWeightIndex applies. A list that names only other manufacturers means the part cannot drop
    on this item.
    UNVERIFIED: see _resolve_weight.
    """
    index = _attr(entry, "DefaultWeightIndex")
    manufacturers = _as_list(_attr(entry, "Manufacturers"))
    if manufacturers:
        mine = _path_of(_attr(item.DefinitionData, "ManufacturerDefinition"))
        matching = [m for m in manufacturers if mine is not None and _path_of(_attr(m, "Manufacturer")) == mine]
        has_default = any(_attr(m, "Manufacturer") is None for m in manufacturers)
        if matching:
            index = _attr(matching[0], "DefaultWeightIndex")
        elif not has_default:
            if mine is None:
                index = None  # this item's manufacturer cannot be read, so its weight cannot be told
            else:
                names = _manufacturer_names(manufacturers)
                return "custom", f"In this item's own {slot} list, but only for other manufacturers ({names})"
    weight = _resolve_weight(source, index)
    if weight is not None and weight > 0:
        return STATUS_NATURAL, ""
    if weight == 0:
        return STATUS_CUSTOM, (
            f"In this item's own {slot} list, but its drop weight is 0, so it never drops naturally (unverified)"
        )
    return STATUS_UNKNOWN, f"In this item's own {slot} list, but its drop weight could not be read (unverified)"


def _own_problems(own: _OwnList | None, item: unreal.UObject, slot: str) -> dict[str, tuple[str, str]]:
    """Own-list parts that are not natural on this item: part id -> (STATUS_CUSTOM | STATUS_UNKNOWN, reason).

    A part is natural when any of its entries is. Otherwise unknown beats custom: an unreadable weight is not evidence
    that the part can never drop. An entry that raises counts as unknown, never as custom.
    """
    outcomes: dict[str, list[tuple[str, str]]] = {}
    for entry in (own.entries if own is not None else []):
        part_id = _path_of(_attr(entry, "Part"))
        if part_id is None:
            continue
        try:
            outcome = _entry_outcome(entry, item, slot, own.source)
        except Exception:  # noqa: BLE001
            reason = f"In this item's own {slot} list, but its drop weight could not be read (unverified)"
            outcome = (STATUS_UNKNOWN, reason)
        outcomes.setdefault(part_id, []).append(outcome)
    problems: dict[str, tuple[str, str]] = {}
    for part_id, found in outcomes.items():
        if any(kind == STATUS_NATURAL for kind, _reason in found):
            continue
        unknown = [outcome for outcome in found if outcome[0] == STATUS_UNKNOWN]
        problems[part_id] = unknown[0] if unknown else found[0]
    return problems


# Every part currently loaded, by kind then slot. Built once because scanning every balance
# definition is slow; stored as paths only. Cleared by the "rescan" route.
_universe: dict[bool, dict[str, dict[str, PartRef]]] = {}
_owners: dict[bool, dict[str, dict[str, None]]] = {}
_universe_meta: dict[bool, dict[str, Any]] = {}  # per kind: when it was built (monotonic) and the map then

# Words in game object names that say nothing about which gun they are
_NAME_NOISE = {"title", "prefix", "legendary", "unique", "rare", "weapon", "weapons", "balance", "name"}


def _humanize(name: str) -> str:
    """"Title_Legendary_LogansGun" -> "Logans Gun"."""
    words: list[str] = []
    for token in name.split("_"):
        words.extend(re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", token))
    kept = [w for w in words if not w.isdigit() and w.lower() not in _NAME_NOISE]
    return " ".join(kept) or name


def _title_name(title: unreal.UObject) -> str:
    """A name part's in-game text (e.g. "Logan's Gun"), or a tidied-up object name if it can't be read."""
    try:
        text = str(getattr(title, "PartName", "") or "").strip()
    except Exception:  # noqa: BLE001
        text = ""
    return text or _humanize(title.Name)


def _balance_label(balance: unreal.UObject, *, weapon: bool) -> str:
    """The gun a balance definition makes. Unique guns have exactly one title part, which is their name."""
    try:
        use_runtime = weapon or balance.Class.Name == "ClassModBalanceDefinition"
        collection = balance.RuntimePartListCollection if use_runtime else balance.PartListCollection
        titles = _weighted_parts(getattr(collection, "TitlePartData", None)) if collection is not None else []
        if len(titles) == 1:
            return _title_name(titles[0])
    except Exception:  # noqa: BLE001, S110
        pass  # fall back to the balance's own name
    return _humanize(balance.Name)


def _balances(weapon: bool) -> Iterator[unreal.UObject]:
    classes = ("WeaponBalanceDefinition",) if weapon else ("InventoryBalanceDefinition", "ClassModBalanceDefinition")
    for class_name in classes:
        for balance in find_all(class_name):
            if not balance.Name.startswith("Default__"):
                yield balance


def _map_name() -> str | None:
    """The name of the loaded map, to notice a map change. UNVERIFIED: WorldInfo.GetMapName(False) is named from class
    data and its runtime result is not confirmed in game. None when it cannot be read; never raises."""
    try:
        pc = get_pc(possibly_loading=True)
        if pc is None:
            return None
        value = pc.WorldInfo.GetMapName(False)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(value, tuple):
        value = value[0] if value else None
    return _text(value) or None


def _clear_caches() -> None:
    """Forgets every part and DLC cache. Used by the rescan route and when the map changes."""
    _universe.clear()
    _owners.clear()
    _part_dlc.clear()
    _balance_dlc_cache.clear()
    _dlc_cache.clear()
    _universe_meta.clear()


def _part_universe(*, weapon: bool) -> dict[str, dict[str, PartRef]]:
    cached = _universe.get(weapon)
    if cached is not None:
        built_on = _universe_meta.get(weapon, {}).get("map")
        now = _map_name()
        if now is None or now == built_on:
            return cached
        logging.info(f"RTSE: map changed from {built_on} to {now}: rebuilding the part caches")
        _clear_caches()

    universe: dict[str, dict[str, PartRef]] = {s: {} for s in (WEAPON_SLOTS if weapon else ITEM_SLOTS)}
    owners: dict[str, dict[str, None]] = {}  # part id -> names of the guns/items that can roll it
    # part id -> {"base": a base-game owner, "dlcs": {id: DlcRef}, "unknown": an owner that says neither}
    dlc_entries: dict[str, dict[str, Any]] = {}
    scanned = 0
    for balance in _balances(weapon):
        try:
            lists = _effective_entries(balance, weapon=weapon)
        except Exception:  # noqa: BLE001, S112
            continue  # An odd balance definition shouldn't stop the whole scan
        scanned += 1
        label = _balance_label(balance, weapon=weapon)
        balance_path = _path_of(balance) or ""
        balance_dlc = _balance_dlc(balance)  # once per balance; cached
        for slot, own in lists.items():
            for part in _parts_of(own.entries):
                ref = _ref(part)
                universe[slot].setdefault(ref.id, ref)
                owners.setdefault(ref.id, {})[label] = None
                kind, dlc = _owner_dlc(balance_dlc, balance_path, ref.id)
                entry = dlc_entries.setdefault(ref.id, {"base": False, "dlcs": {}, "unknown": False})
                if kind == "dlc" and dlc is not None:
                    entry["dlcs"].setdefault(dlc.id, dlc)
                elif kind == "base":
                    entry["base"] = True
                else:
                    entry["unknown"] = True

    logging.info(
        f"RTSE: scanned {scanned} {'weapon' if weapon else 'item'} balances, "
        f"{sum(len(v) for v in universe.values())} part entries",
    )
    _universe[weapon] = universe
    _owners[weapon] = owners
    _part_dlc[weapon] = dlc_entries
    _universe_meta[weapon] = {"built": time.monotonic(), "map": _map_name()}
    return universe


def _mode(params: Params) -> str:
    mode = params.get("mode", MODE_LEGAL)
    if mode not in MODES:
        raise ApiError(400, f"mode must be one of {', '.join(MODES)}")
    return mode


# --- Part status: natural / DLC / custom / unsupported -------------------------------------------
# UNVERIFIED in game: DLC sets are read from class data (DlcItemSet, BaseDefinition), IsLicensed() is a game call
# and the package-name table is a guess. Nothing in this section has been run against the real game yet.

STATUS_NATURAL = "natural"  # in this item's own balance part list for the slot: could drop naturally
STATUS_DLC = "dlc"  # only used by DLC gear
STATUS_CUSTOM = "custom"  # comes from another balance, not this item's own list
STATUS_UNSUPPORTED = "unsupported"  # the wrong part family for this item
# Not known yet: "weight" (own list, drop weight unreadable) or "dlc" (see the "unknown" field of the status)
STATUS_UNKNOWN = "unknown"

MAX_BASE_LEVELS = 6  # BaseDefinition links followed when looking for a DLC set (also stops a loop)


class DlcRef(NamedTuple):
    id: str
    name: str
    licensed: bool | None  # IsLicensed() for the local player; None when unknown
    source: str  # "DlcItemSet" (read from the balance) or "package name (guess)"


def dlc_dict(ref: DlcRef | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    return {"name": ref.name, "licensed": ref.licensed, "source": ref.source}


# Hand-written, NOT verified in game: a guess from package names. Each key is a token of a part's package name,
# e.g. "GD_Orchid_Weapons" -> "Orchid". Used only when no balance information is loaded for a part.
DLC_PACKAGE_HINTS = {
    "Orchid": "Captain Scarlett and Her Pirate's Booty",
    "Iris": "Mr. Torgue's Campaign of Carnage",
    "Sage": "Sir Hammerlock's Big Game Hunt",
    "Aster": "Tiny Tina's Assault on Dragon Keep",
    "Anemone": "Commander Lilith & the Fight for Sanctuary",
    "Lobelia": "Ultimate Vault Hunter Upgrade Pack 2 (Digistruct Peak)",
    "Tulip": "Mechromancer Pack",
    "Lilac": "Psycho Pack",
    "Flax": "T.K. Baha's Bloody Harvest",
}

# Hand-written, NOT verified in game: a guess from Borderlands 2 package names. Only positive evidence that a balance
# is base game. DLC packages use codenames like GD_Orchid_ / GD_Iris_ / GD_Sage_ / GD_Aster_ (see DLC_PACKAGE_HINTS).
BASE_GAME_PACKAGE_PREFIXES = ("GD_Weap_", "GD_Shields", "GD_GrenadeMods", "GD_Artifacts", "GD_ClassMods")


def _is_base_package(path: str) -> bool:
    """True when the first dotted segment starts with a base-game prefix and names no DLC token. UNVERIFIED."""
    package = path.partition(".")[0]
    if not package.startswith(BASE_GAME_PACKAGE_PREFIXES):
        return False
    return not any(token in DLC_PACKAGE_HINTS for token in package.split("_"))

# Caches for the DLC lookups, cleared by rescan_parts. Balances by path, DLC sets by path (IsLicensed runs once each).
_balance_dlc_cache: dict[str, DlcRef | None] = {}
_dlc_cache: dict[str, DlcRef] = {}
# Per kind (weapon or item) and part id: whether a non-DLC balance owns the part, and the DLC sets that do.
_part_dlc: dict[bool, dict[str, dict[str, Any]]] = {}


def _attr(obj: Any, name: str) -> Any:
    """getattr that never raises: a missing or unreadable field is None."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001
        return None


def _path_of(obj: Any) -> str | None:
    try:
        return obj._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None


def _text(value: Any) -> str:
    try:
        return str(value).strip() if value is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _balance_chain(balance: Any) -> Iterator[Any]:
    """The balance, then its BaseDefinition parents: at most MAX_BASE_LEVELS, stopping at None or at a loop."""
    seen: set[Any] = set()
    current = balance
    for _ in range(MAX_BASE_LEVELS):
        if current is None:
            return
        key = _path_of(current) or id(current)
        if key in seen:
            return
        seen.add(key)
        yield current
        current = _attr(current, "BaseDefinition")


def _licensed(dlc: Any) -> bool | None:
    """IsLicensed() for the local player. None when there is no player or the game call fails. UNVERIFIED."""
    try:
        pc = get_pc(possibly_loading=True)
        if pc is None:
            return None
        value = dlc.IsLicensed(pc)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(value, tuple):
        value = value[0] if value else None
    return None if value is None else bool(value)


def _dlc_name(dlc: Any) -> str:
    package = _attr(dlc, "PackageDef")
    for text in (
        _text(_attr(dlc, "ContentDisplayName")),
        _text(_attr(package, "PackageDisplayName")),
        _text(_attr(package, "DLCName")),
        _text(_attr(dlc, "Name")),
    ):
        if text:
            return text
    return "unnamed DLC"


def _dlc_ref(dlc: Any) -> DlcRef:
    key = _path_of(dlc)
    if key is not None and key in _dlc_cache:
        return _dlc_cache[key]
    ref = DlcRef(id=key or f"object:{id(dlc)}", name=_dlc_name(dlc), licensed=_licensed(dlc), source="DlcItemSet")
    if key is not None:
        _dlc_cache[key] = ref
    return ref


def _balance_dlc(balance: Any) -> DlcRef | None:
    """The DLC set a balance belongs to: its own DlcItemSet, else its BaseDefinition's. None for base-game balances.

    UNVERIFIED: the field names come from class data, the runtime values are not confirmed in game.
    Never raises. Cached per balance path until rescan_parts.
    """
    try:
        key = _path_of(balance)
        if key is not None and key in _balance_dlc_cache:
            return _balance_dlc_cache[key]
        found: DlcRef | None = None
        for level in _balance_chain(balance):
            dlc_set = _attr(level, "DlcItemSet")
            if dlc_set is not None:
                found = _dlc_ref(dlc_set)
                break
        if key is not None:
            _balance_dlc_cache[key] = found
        return found
    except Exception:  # noqa: BLE001
        return None


def item_dlc(item: unreal.UObject) -> DlcRef | None:
    """The DLC set of an item's own balance. None when it is base game or unknown. UNVERIFIED (see _balance_dlc)."""
    try:
        balance = item.DefinitionData.BalanceDefinition
    except Exception:  # noqa: BLE001
        return None
    return None if balance is None else _balance_dlc(balance)


def balance_chain_view(balance: Any) -> list[dict[str, Any]]:
    """The balance and its BaseDefinition parents, with each DlcItemSet path, for the debug dump."""
    view: list[dict[str, Any]] = []
    for level in _balance_chain(balance):
        dlc_set = _attr(level, "DlcItemSet")
        view.append({"balance": _path_of(level) or _text(_attr(level, "Name")), "DlcItemSet": _path_of(dlc_set)})
    return view


def _dlc_from_package(part_id: str) -> DlcRef | None:
    """A guess from the part's package name (its first dotted segment, e.g. "GD_Orchid_Weapons"). UNVERIFIED."""
    package = part_id.partition(".")[0]
    for token in package.split("_"):
        name = DLC_PACKAGE_HINTS.get(token)
        if name is not None:
            return DlcRef(id=f"package:{token}", name=name, licensed=None, source="package name (guess)")
    return None


def _owner_dlc(balance_dlc: DlcRef | None, balance_path: str, part_id: str) -> tuple[str, DlcRef | None]:
    """What one balance that can roll a part says about DLC: ("dlc", ref), ("base", None) or ("unknown", None).

    Its DlcItemSet first, then a DLC hint in its package name, then one in the part's package name. It counts as base
    game only with a base-game package name (positive evidence). UNVERIFIED: all of this is a guess from names.
    """
    if balance_dlc is not None:
        return "dlc", balance_dlc
    hinted = _dlc_from_package(balance_path) or _dlc_from_package(part_id)
    if hinted is not None:
        return "dlc", hinted
    if _is_base_package(balance_path):
        return "base", None
    return "unknown", None


def part_dlc(part_id: str, *, weapon: bool) -> tuple[str, DlcRef | None]:
    """Whether a part is base game, DLC or unknown: ("base", None), ("dlc", ref) or ("unknown", None). UNVERIFIED.

    Judged from every loaded balance that can roll it. Base wins over DLC; unknown only when no owner says base or DLC.
    Without a scanned universe for this part (legal mode, or a part no loaded balance uses) it uses the package names.
    """
    entry = _part_dlc.get(weapon, {}).get(part_id)
    if entry is None:
        hinted = _dlc_from_package(part_id)
        if hinted is not None:
            return "dlc", hinted
        return ("base", None) if _is_base_package(part_id) else ("unknown", None)
    if entry["base"]:
        return "base", None
    dlc = next(iter(entry["dlcs"].values()), None)
    if dlc is not None:
        return "dlc", dlc
    return "unknown", None


_EQUIPABLE_FAMILIES = (
    "ShieldPartDefinition",
    "GrenadeModPartDefinition",
    "ClassModPartDefinition",
    "ArtifactPartDefinition",
)
_WEAPON_PART_CLASSES = ("WeaponPartDefinition", "WeaponNamePartDefinition")


def _family_problem(ref: PartRef, *, weapon: bool, own_classes: set[str]) -> str | None:
    """Why a part is the wrong family for this item, or None. Not checked in game: these rules come from class names."""
    cls = ref.class_name
    if weapon and cls not in _WEAPON_PART_CLASSES:
        return f"a {cls} is not a weapon part (not checked in game)"
    if not weapon and cls in _WEAPON_PART_CLASSES:
        return "a weapon part on an item (not checked in game)"
    if not weapon and cls in _EQUIPABLE_FAMILIES:
        others = sorted(c for c in own_classes if c in _EQUIPABLE_FAMILIES and c != cls)
        if others:
            return f"a {cls}, but this item's own parts here are {others[0]} (not checked in game)"
    return None


def _status(status: str, reason: str, dlc: DlcRef | None = None, unknown: str | None = None) -> dict[str, Any]:
    """One part's status. "unknown" says what is unknown ("weight" or "dlc") and is None for every other status."""
    return {"status": status, "status_reason": reason, "dlc": dlc_dict(dlc), "unknown": unknown}


def part_status(
    ref: PartRef,
    *,
    slot: str,
    weapon: bool,
    legal_ids: set[str],
    own_classes: set[str],
    own_problems: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """How one part stands on one item's slot.

    Precedence: natural > unsupported > own-list custom > unknown weight > dlc > unknown dlc > custom.
    legal_ids are the part ids in this item's own balance list for the slot; own_classes are the class names of those
    parts; own_problems maps own-list part ids that are not natural to (STATUS_CUSTOM | STATUS_UNKNOWN, reason).
    The DLC, weight and family results are UNVERIFIED in game.
    """
    problem = (own_problems or {}).get(ref.id) if ref.id in legal_ids else None
    if ref.id in legal_ids and problem is None:
        return _status(STATUS_NATURAL, f"In this item's own balance list for the {slot} slot")
    family = _family_problem(ref, weapon=weapon, own_classes=own_classes)
    if family is not None:
        return _status(STATUS_UNSUPPORTED, family[:1].upper() + family[1:])
    if problem is not None:
        kind, reason = problem
        if kind == STATUS_UNKNOWN:
            return _status(STATUS_UNKNOWN, reason, unknown="weight")
        return _status(STATUS_CUSTOM, reason)
    kind, dlc = part_dlc(ref.id, weapon=weapon)
    if kind == "dlc" and dlc is not None:
        if dlc.source == "DlcItemSet":
            return _status(STATUS_DLC, f"Only used by DLC gear: {dlc.name}", dlc)
        return _status(STATUS_DLC, f"Looks like DLC content from its package name: {dlc.name} (guess)", dlc)
    if kind == "unknown":
        return _status(STATUS_UNKNOWN, "From another balance; whether it needs a DLC could not be told", unknown="dlc")
    return _status(STATUS_CUSTOM, f"From another balance, not this item's own {slot} slot")


def _slots(item: unreal.UObject, mode: str) -> list[Slot]:
    if _is_partless(item):
        return []

    weapon = _is_weapon(item)
    entries = _legal_entries(item)
    legal = {slot: [_ref(p) for p in _parts_of(own.entries)] for slot, own in entries.items()}
    if mode == MODE_LEGAL and not any(legal.values()):
        return []

    universe = _part_universe(weapon=weapon) if mode != MODE_LEGAL else {}
    suffix = "PartDefinition" if weapon else "ItemPartDefinition"
    data = item.DefinitionData

    slots: list[Slot] = []
    for name in WEAPON_SLOTS if weapon else ITEM_SLOTS:
        field = f"{name}{suffix}"
        options: dict[str, PartRef] = {}
        if mode == MODE_SAME_SLOT:
            options.update(universe[name])
        elif mode == MODE_EVERYTHING:
            for refs in universe.values():
                options.update(refs)
        for ref in legal.get(name, ()):
            options.setdefault(ref.id, ref)
        current = getattr(data, field)
        if current is not None:  # Always keep the current part selectable, even if it is unusual
            options.setdefault(_ref(current).id, _ref(current))

        ordered = sorted(options.values(), key=lambda r: (r.id.rpartition(".")[2].lower(), r.id))
        here = legal.get(name, ())
        legal_ids = {r.id for r in here}
        own_classes = {r.class_name for r in here}
        own_problems = _own_problems(entries.get(name), item, name)
        statuses: dict[str, dict[str, Any]] = {}
        for ref in ordered:
            try:
                statuses[ref.id] = part_status(
                    ref, slot=name, weapon=weapon, legal_ids=legal_ids, own_classes=own_classes,
                    own_problems=own_problems,
                )
            except Exception:  # noqa: BLE001, S112
                continue  # one odd part must never break the picker
        slots.append(Slot(name, field, ordered, statuses))
    return slots


MAX_EFFECTS = 40


def _struct_summary(struct: Any, *, paths: bool = False) -> dict[str, Any]:
    """Flattens the simple fields of a struct, e.g. one attribute effect of a part.

    paths=True keeps object fields as full paths (the stable id of an attribute) instead of bare names.
    """
    out: dict[str, Any] = {}
    for prop in struct._type._fields():  # noqa: SLF001
        try:
            value = getattr(struct, prop.Name)
        except Exception:  # noqa: BLE001, S112
            continue
        if isinstance(value, unreal.UObject):
            out[prop.Name] = value._path_name() if paths else value.Name  # noqa: SLF001
        elif isinstance(value, Enum):
            out[prop.Name] = value.name
        elif value is None or isinstance(value, (bool, int, float, str)):
            out[prop.Name] = value
        elif type(value).__name__ in ("WrappedStruct", "WrappedArray"):
            out[prop.Name] = _value_summary(value, depth=2)
    return out


# Hand-written, NOT verified in game: the labels below are guesses for common weapon attributes. The key is
# the attribute object's own name (last dotted segment of its path, lower-cased), e.g. "WeaponDamage" from
# "D_Attributes.Weapon.WeaponDamage". Anything not listed is split into words from its name instead.
ATTRIBUTE_LABELS: dict[str, str] = {
    "weapondamage": "Weapon damage",
    "meleedamage": "Melee damage",
    "criticaldamage": "Critical damage",
    "firerate": "Fire rate",
    "fireinterval": "Fire interval",
    "reloadtime": "Reload time",
    "reloadspeed": "Reload speed",
    "clipsize": "Magazine size",
    "magazinesize": "Magazine size",
    "accuracy": "Accuracy",
    "spread": "Spread",
    "recoil": "Recoil",
    "projectilespeed": "Projectile speed",
    "statuseffectchance": "Status effect chance",
    "statuseffectdamage": "Status effect damage",
}


def _attribute_label(attribute: str | None) -> str | None:
    """A readable name for an attribute path: the hand-written table first, then its object name split into words."""
    if not attribute:
        return None
    leaf = attribute.rpartition(".")[2] or attribute
    known = ATTRIBUTE_LABELS.get(leaf.lower())
    if known:
        return known
    return " ".join(re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", leaf)) or leaf


def _plain_number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _exact_effect(summary: dict[str, Any]) -> dict[str, Any] | None:
    """Reads the fields the class data names, or returns None when they are not all there.

    Field names verified in class data: AttributeEffectData.AttributeToModify (Object, its path is the id),
    AttributeEffectData.ModifierType (Byte) and AttributeEffectData.BaseModifierValue (Struct), which should
    hold AttributeInitializationData.BaseValueConstant and .BaseValueScaleConstant (Float). The runtime
    values and the meaning of each ModifierType number are UNVERIFIED, so "value" (constant x scale) is
    not confirmed in game: "verified" is always False for now.
    """
    attribute = summary.get("AttributeToModify")
    base = summary.get("BaseModifierValue")
    if not isinstance(attribute, str) or not isinstance(base, dict) or "ModifierType" not in summary:
        return None
    if "BaseValueConstant" not in base and "BaseValueScaleConstant" not in base:
        return None
    constant = _plain_number(base.get("BaseValueConstant"))
    scale = _plain_number(base.get("BaseValueScaleConstant"))
    kind = summary.get("ModifierType")
    return {
        "attribute": attribute,
        "attribute_label": _attribute_label(attribute),
        "type": kind if kind is None or isinstance(kind, str) else str(kind),
        "value": constant * scale if constant is not None and scale is not None else None,
        "constant": constant,
        "scale": scale,
        "verified": False,  # the runtime value and the ModifierType meanings are not confirmed in game
        "fields_found": True,  # the real field names were found in the summary
    }


def _effect_view(summary: dict[str, Any]) -> dict[str, Any]:
    """Picks out the attribute, how it is modified and by how much; keeps the raw summary for display.

    Reads the class-data fields first (see _exact_effect). Only when they are missing does it fall back to
    scanning keys by name, which is a guess; that result has "fields_found": False. "verified" is False either way.
    """
    exact = _exact_effect(summary)
    if exact is not None:
        return {**exact, "raw": summary}
    attribute = next((v for k, v in summary.items() if "Attribute" in k and isinstance(v, str)), None)
    kind = next((v for k, v in summary.items() if "Type" in k and isinstance(v, str)), None)
    value = next(
        (v for k, v in summary.items() if "Value" in k and isinstance(v, (int, float)) and not isinstance(v, bool)),
        None,
    )
    return {
        "attribute": attribute,
        "attribute_label": _attribute_label(attribute),
        "type": kind,
        "value": value,
        "constant": None,
        "scale": None,
        "raw": summary,
        "verified": False,  # a guess from key names, so not confirmed in game either
        "fields_found": False,
    }


def _part_object(params: Params) -> unreal.UObject:
    part_id, class_name = params.get("id"), params.get("cls")
    if not isinstance(part_id, str) or not isinstance(class_name, str):
        raise ApiError(400, "'id' and 'cls' are required")
    try:
        part = find_object(class_name, part_id)
    except Exception:  # noqa: BLE001
        part = None
    if part is None:
        raise ApiError(404, "that part is not loaded right now")
    return part


def _part_effects(part: unreal.UObject) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    for prop in part.Class._fields():  # noqa: SLF001
        if "Effect" not in prop.Name:
            continue
        try:
            elements = list(getattr(part, prop.Name))
        except Exception:  # noqa: BLE001, S112
            continue  # Not an array
        for element in elements[:MAX_EFFECTS]:
            try:
                effects.append({"source": prop.Name, **_effect_view(_struct_summary(element, paths=True))})
            except Exception:  # noqa: BLE001, S112
                continue
    return effects


def part_info(params: Params) -> dict[str, Any]:
    """What a part does, read from the part's own attribute-effect lists."""
    part = _part_object(params)
    return {"id": params["id"], "effects": _part_effects(part)}



MAX_ARRAY_ITEMS = 600


def _value_summary(value: Any, depth: int = 0) -> Any:
    """Any property value as JSON: scalars, object paths, structs and (bounded) arrays."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, unreal.UObject):
        return value._path_name()  # noqa: SLF001
    if isinstance(value, Enum):
        return value.name
    kind = type(value).__name__
    if kind == "WrappedStruct":  # check before arrays: arrays also expose a `_type`
        return _struct_summary(value) if depth < 4 else "<struct>"
    if kind == "WrappedArray":
        items = list(value)
        return {
            "length": len(items),
            "items": [_value_summary(v, depth + 1) for v in items[:MAX_ARRAY_ITEMS]],
        }
    return f"<{kind}>"



def rescan_parts(_params: Params) -> dict[str, Any]:
    """Forgets the cached part universe so newly loaded parts (e.g. after a map change) show up."""
    _clear_caches()
    return {"ok": True}


def parts_cache_info(*, weapon: bool) -> dict[str, Any]:
    """How old the part universe for one kind of gear is, and the map it was built on. Reads the cache only."""
    meta = _universe_meta.get(weapon)
    if _universe.get(weapon) is None or meta is None:
        return {"scanned": False, "age_s": None, "map": None}
    return {"scanned": True, "age_s": round(time.monotonic() - meta["built"], 1), "map": meta["map"]}


def part_owners(params: Params) -> dict[str, Any]:
    """Which guns (or gear) can roll each part: {part id: [how many, first few names...]}."""
    weapon = params.get("kind", "weapon") != "item"
    _part_universe(weapon=weapon)
    return {"owners": {pid: [len(names), *list(names)[:4]] for pid, names in _owners[weapon].items()}}


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = ApiError

GET_ROUTES: dict[str, Route] = {"/api/part": part_info, "/api/part_owners": part_owners}
POST_ROUTES: dict[str, Route] = {"/api/parts/rescan": rescan_parts}
