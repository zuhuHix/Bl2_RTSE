"""Gear part discovery: which parts each slot can take, part labels and what each part does.

Read-only. "Legal" parts mirror how the game decides what an item may have; the wider modes offer parts from
every loaded balance definition. The gear_* modules are the gear backend; api.py routes to them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from enum import Enum
from typing import Any, NamedTuple

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
    _universe.clear()
    _owners.clear()
    return {"ok": True}


def part_owners(params: Params) -> dict[str, Any]:
    """Which guns (or gear) can roll each part: {part id: [how many, first few names...]}."""
    weapon = params.get("kind", "weapon") != "item"
    _part_universe(weapon=weapon)
    return {"owners": {pid: [len(names), *list(names)[:4]] for pid, names in _owners[weapon].items()}}


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = ApiError

GET_ROUTES: dict[str, Route] = {"/api/part": part_info, "/api/part_owners": part_owners}
POST_ROUTES: dict[str, Route] = {"/api/parts/rescan": rescan_parts}
