"""Gear inventory: finds the items the player owns (equipped and backpack) and the ids they are known by."""

from __future__ import annotations

from typing import Any

from mods_base import get_pc
from unrealsdk import logging, unreal

from .gear_parts import ITEM_SLOTS, WEAPON_SLOTS, _is_partless, _is_weapon, _ref, _ref_dict
from .routing import ApiError, Params

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
