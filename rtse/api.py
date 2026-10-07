"""API routes. Every route runs on the game thread and returns JSON-serialisable data.

Read routes (GET) and write routes (POST) both take one dict: the query string or the JSON body.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from collections.abc import Iterator
from typing import Any, Callable, NamedTuple

from mods_base import get_pc
from unrealsdk import find_all, find_object, logging, unreal

from . import ammo, character, stats

Params = dict[str, Any]

DEBUG_DUMP_FILE = Path(__file__).parent / "debug_item.json"
DEBUG_INVENTORY_FILE = Path(__file__).parent / "debug_inventory.json"
DEBUG_PART_FILE = Path(__file__).parent / "debug_part.json"
DEBUG_MESH_FILE = Path(__file__).parent / "debug_meshes.json"
DEBUG_GESTALT_FILE = Path(__file__).parent / "debug_gestalt.json"
GESTALTS_FILE = Path(__file__).parent / "gestalts.json"
COVERAGE_FILE = Path(__file__).parent / "debug_coverage.json"
MODELS_MANIFEST = Path(__file__).parent / "web" / "models" / "parts.json"

MIN_LEVEL = 1
MAX_LEVEL = 100


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


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


def _ref_dict(ref: PartRef) -> dict[str, str]:
    package, _, name = ref.id.rpartition(".")
    return {"id": ref.id, "name": name or ref.id, "group": package, "class": ref.class_name, "meshes": list(ref.meshes)}


def _part_ref(part: unreal.UObject | None) -> dict[str, str] | None:
    return None if part is None else _ref_dict(_ref(part))


def _weighted_parts(weighted: unreal.WrappedStruct | None) -> list[unreal.UObject]:
    if not weighted:
        return []
    return [entry.Part for entry in weighted.WeightedParts or () if entry.Part is not None]


def _weapon_part_lists(balance: unreal.UObject) -> dict[str, list[unreal.UObject]]:
    collection = balance.RuntimePartListCollection
    if collection is None:
        return {}
    return {s: _weighted_parts(getattr(collection, f"{s}PartData")) for s in WEAPON_SLOTS}


def _item_part_lists(balance: unreal.UObject) -> dict[str, list[unreal.UObject]]:
    inventory_definition = balance.InventoryDefinition
    if (
        inventory_definition is not None
        and inventory_definition.Class.Name == "ShieldDefinition"
        and not balance.PartListCollection
    ):
        # Shields without a part list collection keep their parts on the definition itself
        return {s: _weighted_parts(getattr(inventory_definition, f"{s}Parts")) for s in ITEM_SLOTS}

    collection = (
        balance.RuntimePartListCollection
        if balance.Class.Name == "ClassModBalanceDefinition"
        else balance.PartListCollection
    )
    if collection is None:
        return {}
    return {s: _weighted_parts(getattr(collection, f"{s}PartData")) for s in ITEM_SLOTS}


def _is_weapon(item: unreal.UObject) -> bool:
    return item.Class.Name == "WillowWeapon"


def _is_partless(item: unreal.UObject) -> bool:
    if _is_weapon(item):
        return False
    definition = item.DefinitionData.ItemDefinition
    return definition is not None and definition.Class.Name in PARTLESS_ITEM_DEFINITIONS


def _legal_part_lists(item: unreal.UObject) -> dict[str, list[unreal.UObject]]:
    balance = item.DefinitionData.BalanceDefinition
    if balance is None:
        return {}
    return _weapon_part_lists(balance) if _is_weapon(item) else _item_part_lists(balance)


# Every part currently loaded, by kind then slot. Built once because scanning every balance
# definition is slow; stored as paths only. Cleared by the "rescan" route.
_universe: dict[bool, dict[str, dict[str, PartRef]]] = {}
_owners: dict[bool, dict[str, dict[str, None]]] = {}

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


def _part_universe(*, weapon: bool) -> dict[str, dict[str, PartRef]]:
    cached = _universe.get(weapon)
    if cached is not None:
        return cached

    universe: dict[str, dict[str, PartRef]] = {s: {} for s in (WEAPON_SLOTS if weapon else ITEM_SLOTS)}
    owners: dict[str, dict[str, None]] = {}  # part id -> names of the guns/items that can roll it
    scanned = 0
    for balance in _balances(weapon):
        try:
            lists = _weapon_part_lists(balance) if weapon else _item_part_lists(balance)
        except Exception:  # noqa: BLE001, S112
            continue  # An odd balance definition shouldn't stop the whole scan
        scanned += 1
        label = _balance_label(balance, weapon=weapon)
        for slot, parts in lists.items():
            for part in parts:
                ref = _ref(part)
                universe[slot].setdefault(ref.id, ref)
                owners.setdefault(ref.id, {})[label] = None

    logging.info(
        f"RTSE: scanned {scanned} {'weapon' if weapon else 'item'} balances, "
        f"{sum(len(v) for v in universe.values())} part entries",
    )
    _universe[weapon] = universe
    _owners[weapon] = owners
    return universe


def _mode(params: Params) -> str:
    mode = params.get("mode", MODE_LEGAL)
    if mode not in MODES:
        raise ApiError(400, f"mode must be one of {', '.join(MODES)}")
    return mode


def _slots(item: unreal.UObject, mode: str) -> list[Slot]:
    if _is_partless(item):
        return []

    weapon = _is_weapon(item)
    legal = {slot: [_ref(p) for p in parts] for slot, parts in _legal_part_lists(item).items()}
    if mode == MODE_LEGAL and not legal:
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
        slots.append(Slot(name, field, ordered))
    return slots


# --- Inventory access ----------------------------------------------------------------------------

MAX_CHAIN_LENGTH = 64  # Guards against a corrupt linked list looping forever

EQUIPPED = "Equipped"
BACKPACK = "Backpack"


def _manager() -> unreal.UObject:
    pc = get_pc(possibly_loading=True)
    manager = pc.GetPawnInventoryManager() if pc is not None else None
    if manager is None:
        raise ApiError(409, "not in a save")
    return manager


def _walk_chain(first: unreal.UObject | None) -> list[unreal.UObject]:
    """Follows an Unreal inventory linked list, where each item's `Inventory` is the next one."""
    items: list[unreal.UObject] = []
    item = first
    while item is not None and len(items) < MAX_CHAIN_LENGTH:
        items.append(item)
        item = item.Inventory
    return items


def _item_id(item: unreal.UObject) -> str:
    return item._path_name()  # noqa: SLF001


def _inventory(manager: unreal.UObject) -> tuple[list[tuple[unreal.UObject, str]], list[str]]:
    """Returns every editable item as (item, location), equipped first, plus any lookup warnings."""
    warnings: list[str] = []

    backpack = [item for item in manager.Backpack if item is not None]
    backpack_ids = {_item_id(item) for item in backpack}

    equipped: list[unreal.UObject] = []
    seen: set[str] = set()
    for chain_name in ("InventoryChain", "ItemChain"):
        try:
            chain = _walk_chain(getattr(manager, chain_name))
        except Exception as ex:  # noqa: BLE001
            warnings.append(f"{chain_name}: {ex!r}")
            continue
        for item in chain:
            item_id = _item_id(item)
            if item_id in backpack_ids or item_id in seen or not hasattr(item, "DefinitionData"):
                continue
            seen.add(item_id)
            equipped.append(item)

    return [(i, EQUIPPED) for i in equipped] + [(i, BACKPACK) for i in backpack], warnings


# The game can replace or re-parent an item's object when it is rebuilt, which changes its path name
# (our id). Remember old -> new so a page still holding the old id keeps working.
_id_aliases: dict[str, str] = {}
MAX_ALIAS_HOPS = 8


def _resolve_alias(item_id: str) -> str:
    for _ in range(MAX_ALIAS_HOPS):
        newer = _id_aliases.get(item_id)
        if newer is None or newer == item_id:
            break
        item_id = newer
    return item_id


def _note_id_change(old_id: str, item: unreal.UObject) -> None:
    new_id = _item_id(item)
    if new_id != old_id:
        _id_aliases[old_id] = new_id
        logging.info(f"RTSE: item id changed after edit: {old_id} -> {new_id}")


def _find_item(params: Params) -> tuple[unreal.UObject, str]:
    item_id = params.get("id")
    if not isinstance(item_id, str):
        raise ApiError(400, "'id' is required")
    item_id = _resolve_alias(item_id)
    items, _warnings = _inventory(_manager())
    for item, location in items:
        if _item_id(item) == item_id:
            return item, location
    raise ApiError(404, f"that item is no longer in your inventory ({item_id})")


def equipped_weapons() -> list[unreal.UObject]:
    """Weapons the player currently has equipped. Empty when not in a save."""
    try:
        found, _warnings = _inventory(_manager())
    except ApiError:
        return []
    return [item for item, location in found if location == EQUIPPED and _is_weapon(item)]


def _reinitialize(item: unreal.UObject, *, equipped: bool) -> None:
    """Makes the game rebuild the item from its (edited) definition data."""
    data = item.DefinitionData
    if _is_weapon(item):
        data.TitlePartDefinition = None
        data.PrefixPartDefinition = None
        # Initializing a weapon without a weapon type crashes the game
        can_initialize = data.WeaponTypeDefinition is not None
    else:
        data.TitleItemNamePartDefinition = None
        data.PrefixItemNamePartDefinition = None
        can_initialize = data.ItemDefinition is not None

    if can_initialize:
        item.InitializeInternal(True)
        if not equipped:
            # Unreadying would holster/unequip gear the player is using
            item.Unready(True)
    item.DefinitionData.UniqueId = item.GenerateUniqueID()


# --- Routes --------------------------------------------------------------------------------------


def ping(_params: Params) -> dict[str, Any]:
    return {"ok": True, "in_game": get_pc(possibly_loading=True) is not None}


def _weapon_type_path(item: unreal.UObject) -> str | None:
    """The weapon's type definition path (it names the gun type, e.g. ...WeaponType_Jakobs_Sniper)."""
    if not _is_weapon(item):
        return None
    try:
        weapon_type = item.DefinitionData.WeaponTypeDefinition
        return None if weapon_type is None else weapon_type._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None


def _current_build(item: unreal.UObject) -> list[dict[str, Any]]:
    """The parts on an item right now (name and meshes only), so the list can draw a picture of it."""
    if _is_partless(item):
        return []
    weapon = _is_weapon(item)
    suffix = "PartDefinition" if weapon else "ItemPartDefinition"
    build: list[dict[str, Any]] = []
    for name in WEAPON_SLOTS if weapon else ITEM_SLOTS:
        try:
            part = getattr(item.DefinitionData, f"{name}{suffix}")
        except Exception:  # noqa: BLE001, S112
            continue
        if part is not None:
            ref = _ref_dict(_ref(part))
            build.append({"slot": name, "name": ref["name"], "group": ref["group"], "meshes": ref["meshes"]})
    return build


def items(_params: Params) -> dict[str, Any]:
    found, warnings = _inventory(_manager())
    return {
        "items": [
            {
                "id": _item_id(item),
                "class": item.Class.Name,
                "name": item.GetHumanReadableName(),
                "location": location,
                "level": getattr(item, "ExpLevel", None),
                "rarity": getattr(item, "RarityLevel", None),
                "type_path": _weapon_type_path(item),
                "build": _current_build(item),
                "hide_bones": _hidden_bones(item),
            }
            for item, location in found
        ],
        "warnings": warnings,
    }


def item_detail(params: Params) -> dict[str, Any]:
    item, location = _find_item(params)
    return _detail(item, location, _mode(params))


def _hidden_bones(item: unreal.UObject) -> list[str]:
    """Bones the game hides on the built weapon mesh, e.g. a moon clip. Empty for non-weapons."""
    if not _is_weapon(item):
        return []
    weapon_type = item.DefinitionData.WeaponTypeDefinition
    names: list[str] = []
    for field in ("BoneToHideOnMesh", "AdditionalBoneToHideOnMesh"):
        try:
            value = str(getattr(weapon_type, field))
        except AttributeError:
            continue
        if value and value != "None":
            names.append(value)
    return names


def _detail(item: unreal.UObject, location: str, mode: str) -> dict[str, Any]:
    data = item.DefinitionData
    return {
        "hide_bones": _hidden_bones(item),
        "id": _item_id(item),
        "location": location,
        "mode": mode,
        "class": item.Class.Name,
        "name": item.GetHumanReadableName(),
        "level": item.ExpLevel,
        "grade": data.ManufacturerGradeIndex,
        "game_stage": data.GameStage,
        "stats": stats.read_stats(item),
        "slots": [
            {
                "slot": slot.name,
                "current": _part_ref(getattr(data, slot.field)),
                "options": [_ref_dict(ref) for ref in slot.options],
            }
            for slot in _slots(item, mode)
        ],
    }


def set_part(params: Params) -> dict[str, Any]:
    item, location = _find_item(params)
    slot = next((s for s in _slots(item, _mode(params)) if s.name == params.get("slot")), None)
    if slot is None:
        raise ApiError(400, "unknown slot for this item")

    part_id = params.get("part")
    part: unreal.UObject | None = None
    if part_id is not None:
        ref = next((r for r in slot.options if r.id == part_id), None)
        if ref is None:
            raise ApiError(400, "that part is not available in this mode")
        try:
            part = find_object(ref.class_name, ref.id)
        except Exception:  # noqa: BLE001
            part = None
        if part is None:
            raise ApiError(409, "that part is not loaded right now - try rescanning parts")

    before = _part_ref(getattr(item.DefinitionData, slot.field))
    old_fingerprint = stats.fingerprint(item)
    setattr(item.DefinitionData, slot.field, part)
    stats.move_overrides(old_fingerprint, stats.fingerprint(item))
    after_write = _part_ref(getattr(item.DefinitionData, slot.field))
    _reinitialize(item, equipped=location == EQUIPPED)
    after_init = _part_ref(getattr(item.DefinitionData, slot.field))
    applied = {
        "what": f"{slot.name} ({location})",
        "requested": part_id,
        "before": before and before["name"],
        "after": {
            "after_write": after_write and after_write["name"],
            "after_rebuild": after_init and after_init["name"],
        },
    }
    logging.info(f"RTSE: set_part {applied}")
    _note_id_change(params["id"], item)
    return {**_detail(item, location, _mode(params)), "applied": applied}


def set_level(params: Params) -> dict[str, Any]:
    item, location = _find_item(params)
    try:
        level = int(params["level"])
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, "'level' must be an integer") from None
    if not MIN_LEVEL <= level <= MAX_LEVEL:
        raise ApiError(400, f"level must be {MIN_LEVEL}-{MAX_LEVEL}")

    data = item.DefinitionData
    before = {"exp_level": item.ExpLevel, "grade": data.ManufacturerGradeIndex, "game_stage": data.GameStage}

    # The level the game shows and saves is built from the manufacturer grade; game stage is a
    # separate stored value (both are written to the save). Keep them in step.
    old_fingerprint = stats.fingerprint(item)
    data.GameStage = level
    data.ManufacturerGradeIndex = level
    stats.move_overrides(old_fingerprint, stats.fingerprint(item))
    _reinitialize(item, equipped=location == EQUIPPED)
    if item.ExpLevel != level:
        item.ExpLevel = level

    data = item.DefinitionData
    applied = {
        "what": f"level ({location})",
        "requested": level,
        "before": before,
        "after": {"exp_level": item.ExpLevel, "grade": data.ManufacturerGradeIndex, "game_stage": data.GameStage},
    }
    logging.info(f"RTSE: set_level {applied}")
    _note_id_change(params["id"], item)
    return {**_detail(item, location, _mode(params)), "applied": applied}


def set_stat(params: Params) -> dict[str, Any]:
    item, location = _find_item(params)
    stat = params.get("stat")
    if stat not in stats.STAT_NAMES:
        raise ApiError(400, "unknown stat")
    if not _is_weapon(item):
        raise ApiError(400, "only weapons have editable stats")

    raw = params.get("value")
    try:
        value = None if raw is None else float(raw)
        applied = stats.set_override(item, stat, value)
    except (TypeError, ValueError) as ex:
        raise ApiError(400, str(ex)) from None

    if value is None:
        # Removing an override: rebuild the weapon so the game restores the real value
        _reinitialize(item, equipped=location == EQUIPPED)
        applied["after"] = {
            "value": getattr(item, stat),
            "base": getattr(item, f"{stat}BaseValue"),
        }
    logging.info(f"RTSE: set_stat {applied}")
    _note_id_change(params["id"], item)
    return {**_detail(item, location, _mode(params)), "applied": {"what": f"{stat} ({location})", **applied}}


MAX_EFFECTS = 40


def _struct_summary(struct: Any) -> dict[str, Any]:
    """Flattens the simple fields of a struct, e.g. one attribute effect of a part."""
    out: dict[str, Any] = {}
    for prop in struct._type._fields():  # noqa: SLF001
        try:
            value = getattr(struct, prop.Name)
        except Exception:  # noqa: BLE001, S112
            continue
        if isinstance(value, unreal.UObject):
            out[prop.Name] = value.Name
        elif isinstance(value, Enum):
            out[prop.Name] = value.name
        elif value is None or isinstance(value, (bool, int, float, str)):
            out[prop.Name] = value
        elif type(value).__name__ in ("WrappedStruct", "WrappedArray"):
            out[prop.Name] = _value_summary(value, depth=2)
    return out


def _effect_view(summary: dict[str, Any]) -> dict[str, Any]:
    """Picks out the attribute, how it is modified and by how much; keeps the rest for display."""
    attribute = next((v for k, v in summary.items() if "Attribute" in k and isinstance(v, str)), None)
    kind = next((v for k, v in summary.items() if "Type" in k and isinstance(v, str)), None)
    value = next(
        (v for k, v in summary.items() if "Value" in k and isinstance(v, (int, float)) and not isinstance(v, bool)),
        None,
    )
    return {"attribute": attribute, "type": kind, "value": value, "raw": summary}


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
                effects.append({"source": prop.Name, **_effect_view(_struct_summary(element))})
            except Exception:  # noqa: BLE001, S112
                continue
    return effects


def part_info(params: Params) -> dict[str, Any]:
    """What a part does, read from the part's own attribute-effect lists."""
    part = _part_object(params)
    return {"id": params["id"], "effects": _part_effects(part)}


def debug_part(params: Params) -> dict[str, Any]:
    """Dumps a part's simple fields and raw effects, to refine how effects are read."""
    part = _part_object(params)
    simple: dict[str, Any] = {}
    for prop in part.Class._fields():  # noqa: SLF001
        try:
            value = getattr(part, prop.Name)
        except Exception:  # noqa: BLE001, S112
            continue
        if isinstance(value, unreal.UObject):
            simple[prop.Name] = value._path_name()  # noqa: SLF001
        elif value is None or isinstance(value, (bool, int, float, str)):
            simple[prop.Name] = value
        elif isinstance(value, Enum):
            simple[prop.Name] = value.name
        else:
            simple[prop.Name] = f"<{type(value).__name__}>"
    out = {"id": params["id"], "fields": simple, "effects": _part_effects(part)}
    DEBUG_PART_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_PART_FILE), "effects": len(out["effects"])}


MAX_SOCKETS = 80


def _object_summary(obj: Any) -> dict[str, Any]:
    """Simple fields of an object, with struct fields flattened one level (e.g. a socket's location)."""
    out: dict[str, Any] = {}
    for prop in obj.Class._fields():  # noqa: SLF001
        try:
            value = getattr(obj, prop.Name)
        except Exception:  # noqa: BLE001, S112
            continue
        if isinstance(value, unreal.UObject):
            out[prop.Name] = value._path_name()  # noqa: SLF001
        elif isinstance(value, Enum):
            out[prop.Name] = value.name
        elif value is None or isinstance(value, (bool, int, float, str)):
            out[prop.Name] = value
        elif hasattr(value, "_type"):
            try:
                out[prop.Name] = _struct_summary(value)
            except Exception:  # noqa: BLE001, S110
                pass
    return out


def _mesh_report(component: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": component._path_name(), "fields": _object_summary(component)}  # noqa: SLF001
    mesh = getattr(component, "SkeletalMesh", None)
    if mesh is None:
        return entry
    entry["skeletal_mesh"] = mesh._path_name()  # noqa: SLF001
    entry["mesh_fields"] = _object_summary(mesh)
    try:
        entry["sockets"] = [_object_summary(s) for s in list(mesh.Sockets)[:MAX_SOCKETS]]
    except Exception as ex:  # noqa: BLE001
        entry["sockets"] = f"<error {ex!r}>"
    try:
        entry["bones"] = [_struct_summary(b) for b in list(mesh.RefSkeleton)[:MAX_SOCKETS]]
    except Exception as ex:  # noqa: BLE001
        entry["bones"] = f"<error {ex!r}>"
    return entry


def debug_meshes(params: Params) -> dict[str, Any]:
    """For one weapon: which mesh each part uses, plus the built weapon mesh with its sockets and bones."""
    item, _location = _find_item(params)
    out: dict[str, Any] = {"item": item.GetHumanReadableName(), "class": item.Class.Name, "components": {}, "parts": {}}

    for name in ("FirstPersonMesh", "ThirdPersonMesh"):
        try:
            component = getattr(item, name)
            out["components"][name] = None if component is None else _mesh_report(component)
        except Exception as ex:  # noqa: BLE001
            out["components"][name] = f"<error {ex!r}>"

    data = item.DefinitionData
    for prop in data._type._fields():  # noqa: SLF001
        try:
            part = getattr(data, prop.Name)
        except Exception:  # noqa: BLE001, S112
            continue
        if not isinstance(part, unreal.UObject):
            continue
        summary = _object_summary(part)
        mesh_refs = {k: v for k, v in summary.items() if "Mesh" in k and isinstance(v, str)}
        out["parts"][prop.Name] = {"path": part._path_name(), "class": part.Class.Name, "mesh_refs": mesh_refs, "fields": summary}  # noqa: SLF001

    DEBUG_MESH_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_MESH_FILE), "parts": len(out["parts"])}


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


def _deep_summary(obj: Any) -> dict[str, Any]:
    """Every data property of an object, arrays included, without the built-in functions."""
    out: dict[str, Any] = {}
    for prop in obj.Class._fields():  # noqa: SLF001
        try:
            value = _value_summary(getattr(obj, prop.Name))
        except Exception as ex:  # noqa: BLE001
            value = f"<error {ex!r}>"
        if isinstance(value, str) and value.startswith(("<BoundFunction", "<UnrealEnum", "<WrappedMultiMap")):
            continue
        out[prop.Name] = value
    return out


def debug_gestalt(params: Params) -> dict[str, Any]:
    """Dumps the weapon type's gestalt definition: how part names map onto the shared gestalt mesh."""
    item, _location = _find_item(params)
    weapon_type = item.DefinitionData.WeaponTypeDefinition
    if weapon_type is None:
        raise ApiError(400, "this weapon has no weapon type")
    gestalt = weapon_type.GestaltMesh
    if gestalt is None:
        raise ApiError(404, "this weapon type has no gestalt mesh")

    out: dict[str, Any] = {
        "weapon_type": weapon_type._path_name(),  # noqa: SLF001
        "gestalt": gestalt._path_name(),  # noqa: SLF001
        "gestalt_class": gestalt.Class.Name,
        "definition": _deep_summary(gestalt),
    }
    for name in ("SkeletalMesh", "GestaltSkeletalMesh"):
        mesh = getattr(gestalt, name, None)
        if mesh is not None:
            out[f"{name}_object"] = _deep_summary(mesh)
    DEBUG_GESTALT_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_GESTALT_FILE), "fields": len(out["definition"])}


def export_gestalts(_params: Params) -> dict[str, Any]:
    """Writes every gestalt mesh definition: part name -> triangle slice, sockets and part bounds.

    A gestalt is one shared mesh per gun type holding every part; this table says which slice of its
    triangle list is which part, which is what a 3D builder needs.
    """
    out: dict[str, Any] = {}
    for definition in find_all("GestaltSkeletalMeshDefinition"):
        if definition.Name.startswith("Default__"):
            continue
        entry: dict[str, Any] = {"path": definition._path_name()}  # noqa: SLF001
        for field in ("GestaltInfos", "GestaltSocketMappings", "GestaltPartBounds", "GestaltAccessoryNames"):
            try:
                entry[field] = _value_summary(getattr(definition, field))
            except Exception as ex:  # noqa: BLE001
                entry[field] = f"<error {ex!r}>"
        mesh = getattr(definition, "GestaltSkeletalMesh", None)
        if mesh is not None:
            entry["mesh"] = mesh._path_name()  # noqa: SLF001
            try:
                entry["mesh_sockets"] = [_object_summary(s) for s in list(mesh.Sockets)[:200]]
            except Exception as ex:  # noqa: BLE001
                entry["mesh_sockets"] = f"<error {ex!r}>"
        # Keyed by full path: the game holds more than one version (e.g. Weap_Pistol and
        # Remaster_Weap_Pistol) with the same short name and different geometry.
        out[definition._path_name()] = entry  # noqa: SLF001
    GESTALTS_FILE.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return {"saved_to": str(GESTALTS_FILE), "gestalts": sorted(out)}


def export_coverage(_params: Params) -> dict[str, Any]:
    """Compares every part the game knows about with the meshes we have models for."""
    manifest = json.loads(MODELS_MANIFEST.read_text(encoding="utf-8"))
    known = {name for model in manifest.values() for name in model["parts"]}

    report: dict[str, Any] = {"models": {key: len(model["parts"]) for key, model in manifest.items()}}
    for kind, weapon in (("weapon", True), ("item", False)):
        parts: dict[str, PartRef] = {}
        for refs in _part_universe(weapon=weapon).values():
            parts.update(refs)
        with_meshes = [ref for ref in parts.values() if ref.meshes]
        uncovered = [ref for ref in with_meshes if any(m not in known for m in ref.meshes)]
        report[kind] = {
            "parts_total": len(parts),
            "parts_naming_a_mesh": len(with_meshes),
            "parts_without_mesh_name": len(parts) - len(with_meshes),
            "parts_whose_mesh_we_lack": len(uncovered),
            "meshes_we_lack": sorted({m for ref in uncovered for m in ref.meshes if m not in known}),
            "examples": [{"part": ref.id, "meshes": list(ref.meshes)} for ref in uncovered[:120]],
        }
    COVERAGE_FILE.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return {
        "saved_to": str(COVERAGE_FILE),
        "weapon_parts": report["weapon"]["parts_total"],
        "weapon_missing": report["weapon"]["parts_whose_mesh_we_lack"],
        "item_parts": report["item"]["parts_total"],
        "item_missing": report["item"]["parts_whose_mesh_we_lack"],
    }


def rescan_parts(_params: Params) -> dict[str, Any]:
    """Forgets the cached part universe so newly loaded parts (e.g. after a map change) show up."""
    _universe.clear()
    _owners.clear()
    return {"ok": True}


def part_owners(params: Params) -> dict[str, Any]:
    """Which guns (or gear) can roll each part: {part id: [how many, first few names...]}."""
    weapon = params.get("kind", "weapon") != "item"
    _part_universe(weapon=weapon)
    return {"owners": {pid: [len(names), *list(names)[:4]] for pid, names in _owners[weapon].items()}}


def debug_item(params: Params) -> dict[str, Any]:
    """Dumps every readable scalar field of an item and its DefinitionData, to find real field names."""
    item, _location = _find_item(params)

    def dump(owner_type: Any, obj: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for prop in owner_type._fields():  # noqa: SLF001
            name = prop.Name
            try:
                value = getattr(obj, name)
            except Exception as ex:  # noqa: BLE001
                out[name] = f"<error {ex!r}>"
                continue
            if value is None or isinstance(value, (bool, int, float, str)):
                out[name] = value
            elif isinstance(value, unreal.UObject):
                out[name] = value._path_name()  # noqa: SLF001
            else:
                out[name] = f"<{type(value).__name__}>"
        return out

    data = item.DefinitionData
    result = {
        "name": item.GetHumanReadableName(),
        "item": dump(item.Class, item),
        "definition_data": dump(data._type, data),  # noqa: SLF001
    }
    DEBUG_DUMP_FILE.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_DUMP_FILE), "fields": len(result["item"]) + len(result["definition_data"])}


def debug_inventory(_params: Params) -> dict[str, Any]:
    """Dumps the inventory manager's fields and chains, to verify where equipped gear lives."""
    manager = _manager()
    out: dict[str, Any] = {"fields": {}, "chains": {}}
    for prop in manager.Class._fields():  # noqa: SLF001
        name = prop.Name
        try:
            value = getattr(manager, name)
        except Exception as ex:  # noqa: BLE001
            out["fields"][name] = f"<error {ex!r}>"
            continue
        if value is None or isinstance(value, (bool, int, float, str)):
            out["fields"][name] = value
        elif isinstance(value, unreal.UObject):
            out["fields"][name] = value._path_name()  # noqa: SLF001
        else:
            out["fields"][name] = f"<{type(value).__name__}>"

    for chain_name in ("InventoryChain", "ItemChain"):
        try:
            out["chains"][chain_name] = [
                {"class": i.Class.Name, "path": _item_id(i), "name": i.GetHumanReadableName()}
                for i in _walk_chain(getattr(manager, chain_name))
            ]
        except Exception as ex:  # noqa: BLE001
            out["chains"][chain_name] = f"<error {ex!r}>"
    out["backpack_count"] = len([i for i in manager.Backpack if i is not None])

    DEBUG_INVENTORY_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_INVENTORY_FILE), "chains": {k: len(v) if isinstance(v, list) else v for k, v in out["chains"].items()}}


def _module_route(action: Callable[[Params], dict[str, Any]]) -> Callable[[Params], dict[str, Any]]:
    def route(params: Params) -> dict[str, Any]:
        try:
            return action(params)
        except (character.CharacterError, ammo.AmmoError) as ex:
            raise ApiError(ex.status, str(ex)) from None

    return route


Route = Callable[[Params], dict[str, Any]]

GET_ROUTES: dict[str, Route] = {
    "/api/ping": ping,
    "/api/items": items,
    "/api/item": item_detail,
    "/api/debug/item": debug_item,
    "/api/debug/inventory": debug_inventory,
    "/api/part": part_info,
    "/api/part_owners": part_owners,
    "/api/debug/part": debug_part,
    "/api/debug/meshes": debug_meshes,
    "/api/debug/gestalt": debug_gestalt,
    "/api/debug/gestalts": export_gestalts,
    "/api/debug/coverage": export_coverage,
    "/api/character": _module_route(lambda _params: character.read()),
    "/api/debug/character": _module_route(lambda _params: character.debug_dump()),
    "/api/ammo": _module_route(lambda _params: ammo.read()),
    "/api/debug/ammo": _module_route(lambda _params: ammo.debug_dump()),
}
POST_ROUTES: dict[str, Route] = {
    "/api/item/set_part": set_part,
    "/api/item/set_level": set_level,
    "/api/parts/rescan": rescan_parts,
    "/api/item/set_stat": set_stat,
    "/api/character/set": _module_route(character.set_field),
    "/api/ammo/set": _module_route(ammo.set_value),
}
