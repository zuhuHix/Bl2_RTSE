"""Gear inventory: finds the items the player owns (equipped, backpack and bank) and the ids they are known by.

Also the inventory write routes: duplicate, delete, move between backpack and bank, and export/import of the whole
inventory as item codes. codes.py and bank.py import this module at their top, so they are imported here inside the
functions that need them (see the import notes on each route).
"""

from __future__ import annotations

from typing import Any

from mods_base import get_pc
from unrealsdk import logging, unreal

from . import journal, stats
from .gear_parts import ITEM_SLOTS, PARTLESS_ITEM_DEFINITIONS, WEAPON_SLOTS, _is_weapon, _ref, _ref_dict
from .routing import ApiError, Params, Route

# --- Inventory access ----------------------------------------------------------------------------

MAX_CHAIN_LENGTH = 64  # Guards against a corrupt linked list looping forever

EQUIPPED = "Equipped"
BACKPACK = "Backpack"
BANK = "Bank"


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


def used_unique_ids() -> set[int]:
    """Every nonzero UniqueId an item or bank entry holds (backpack, equipped, bank). Parts that cannot be read are left out."""
    from . import bank  # local: bank imports this module

    used: set[int] = set()
    try:
        found, _warnings = _inventory(_manager())
        used.update(stats.unique_id(item) for item, _location in found)
    except ApiError:
        pass  # not in a save: the backpack and equipped items are unknown
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: reading the inventory UniqueIds failed: {ex!r}")
    used.update(bank.bank_unique_ids())
    used.discard(0)
    return used


def backpack_item(unique_id: int) -> unreal.UObject | None:
    """The backpack item with this UniqueId, or None."""
    for item in _manager().Backpack:
        if item is not None and stats.unique_id(item) == unique_id:
            return item
    return None


def mark_of(item: unreal.UObject) -> int:
    """The item's Mark (1 = standard). UNVERIFIED property name; 1 when it cannot be read."""
    try:
        return int(item.Mark) or 1
    except Exception:  # noqa: BLE001
        return 1


# --- Cards and builds (shared with codes.py, which builds the same shape from a definition) ----------


def _weapon_type_path(item: unreal.UObject) -> str | None:
    """The weapon's type definition path (it names the gun type, e.g. ...WeaponType_Jakobs_Sniper)."""
    if not _is_weapon(item):
        return None
    try:
        weapon_type = item.DefinitionData.WeaponTypeDefinition
        return None if weapon_type is None else weapon_type._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None


def _partless_definition(data: Any) -> bool:
    definition = data.ItemDefinition
    return definition is not None and definition.Class.Name in PARTLESS_ITEM_DEFINITIONS


def build_of(data: Any, weapon: bool) -> list[dict[str, Any]]:
    """The parts a definition has, by slot (name and meshes only). `data` is a live DefinitionData or a copy of one."""
    if not weapon and _partless_definition(data):
        return []
    suffix = "PartDefinition" if weapon else "ItemPartDefinition"
    build: list[dict[str, Any]] = []
    for name in WEAPON_SLOTS if weapon else ITEM_SLOTS:
        try:
            part = getattr(data, f"{name}{suffix}")
        except Exception:  # noqa: BLE001, S112
            continue
        if part is not None:
            ref = _ref_dict(_ref(part))
            build.append({"slot": name, "name": ref["name"], "group": ref["group"], "meshes": ref["meshes"]})
    return build


def hidden_bones_of(weapon_type: Any) -> list[str]:
    """Bones the game hides on the built weapon mesh (e.g. a moon clip), from its weapon type. Empty when there is none."""
    if weapon_type is None:
        return []
    names: list[str] = []
    for field in ("BoneToHideOnMesh", "AdditionalBoneToHideOnMesh"):
        try:
            value = str(getattr(weapon_type, field))
        except AttributeError:
            continue
        if value and value != "None":
            names.append(value)
    return names


def _hidden_bones(item: unreal.UObject) -> list[str]:
    """Bones the game hides on the built weapon mesh. Empty for non-weapons."""
    if not _is_weapon(item):
        return []
    return hidden_bones_of(item.DefinitionData.WeaponTypeDefinition)


def card_of(item: unreal.UObject, location: str, card_id: str | None = None) -> dict[str, Any]:
    """The gear card for a live item (the shape /api/items and /api/bank return)."""
    return {
        "id": card_id or _item_id(item),
        "class": item.Class.Name,
        "name": item.GetHumanReadableName(),
        "location": location,
        "level": getattr(item, "ExpLevel", None),
        "rarity": getattr(item, "RarityLevel", None),
        "type_path": _weapon_type_path(item),
        "build": build_of(item.DefinitionData, _is_weapon(item)),
        "hide_bones": _hidden_bones(item),
        "unique_id": stats.unique_id(item),
    }


def items(_params: Params) -> dict[str, Any]:
    found, warnings = _inventory(_manager())
    return {"items": [card_of(item, location) for item, location in found], "warnings": warnings}


# --- Helpers for the write routes ----------------------------------------------------------------


def _need_id(params: Params) -> str:
    item_id = params.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise ApiError(400, "'id' is required")
    return item_id


def _game_failed(action: str, ex: Exception) -> ApiError:
    return ApiError(409, f"{action} failed in the game ({ex})")


def remove_from_backpack(manager: unreal.UObject, item: unreal.UObject) -> None:
    """Removes a backpack item and checks it left the backpack. Raises ApiError (409) otherwise."""
    item_id = _item_id(item)
    try:
        manager.RemoveFromInventory(item, False)  # UNVERIFIED call
    except Exception as ex:  # noqa: BLE001
        raise _game_failed("removing the item", ex) from None
    if any(other is not None and _item_id(other) == item_id for other in manager.Backpack):
        raise ApiError(409, "the game did not remove the item from the backpack")


def _code_for_removal(source: Any, name: str, warnings: list[str]) -> str | None:
    """The code of an item about to be removed (so the removal can be undone), or None with a warning."""
    from . import codes  # local: codes imports this module

    try:
        code, _header, code_warnings = codes.export_code(source)
    except Exception as ex:  # noqa: BLE001
        warnings.append(f"{name}: no code could be made ({ex}), so it cannot be restored by undo")
        return None
    warnings.extend(f"{name}: {w}" for w in code_warnings)
    return code


# --- Routes: read ----------------------------------------------------------------------------------


def inventory_export(_params: Params) -> dict[str, Any]:
    """Every item (equipped, backpack, bank) as a code, for saving to a file. Items with no code are skipped with a warning."""
    from . import bank, codes  # local: both import this module

    found, warnings = _inventory(_manager())
    rows: list[dict[str, Any]] = []
    for item, location in found:
        card = card_of(item, location)
        row = _export_row(card, item, warnings, codes)
        if row is not None:
            rows.append(row)
    try:
        entries = bank.bank_entries()
    except ApiError as ex:
        warnings.append(f"bank: {ex}")
        entries = []
    for index, entry in entries:
        card, warning = bank.entry_card(index, entry)
        if warning:
            warnings.append(f"{card['name']}: {warning}")
        row = _export_row(card, bank.export_source(entry), warnings, codes)
        if row is not None:
            rows.append(row)
    return {"format": "rtse-inventory", "version": 1, "items": rows, "warnings": warnings}


def _export_row(card: dict[str, Any], source: Any, warnings: list[str], codes: Any) -> dict[str, Any] | None:
    try:
        code, _header, code_warnings = codes.export_code(source)
    except Exception as ex:  # noqa: BLE001
        warnings.append(f"{card['name']}: no code could be made ({ex}), not exported")
        return None
    warnings.extend(f"{card['name']}: {w}" for w in code_warnings)
    return {
        "location": card["location"], "name": card["name"], "class": card["class"], "level": card["level"],
        "rarity": card["rarity"], "unique_id": card["unique_id"], "code": code,
    }  # fmt: skip


# --- Routes: writes ----------------------------------------------------------------------------------


def duplicate(params: Params) -> dict[str, Any]:
    """Copies an item into the backpack (a bank item copies into the bank), with a fresh UniqueId."""
    from . import bank, codes  # local: both import this module

    item_id = _need_id(params)
    if item_id.startswith("bank:"):
        index, entry = bank.find_bank_entry(item_id)
        definition, is_weapon = bank.entry_definition(entry)
        source_name, mark, destination = bank.entry_card(index, entry)[0]["name"], bank.entry_mark(entry), "bank"
    else:
        item, _location = _find_item(params)
        definition, is_weapon = item.DefinitionData, _is_weapon(item)
        source_name, mark, destination = item.GetHumanReadableName(), mark_of(item), "backpack"
    try:
        unique_id = codes.fresh_unique_id(used_unique_ids())
        copy = codes.copy_definition(definition, is_weapon)
        copy.UniqueId = unique_id  # UNVERIFIED: writing a struct field
    except Exception as ex:  # noqa: BLE001
        raise _game_failed(f"copying {source_name}", ex) from None
    problem = codes.place_definition(copy, is_weapon, destination, mark)
    if problem:
        raise ApiError(409, problem)
    added = codes.find_added(destination, unique_id)
    if added is None:
        raise ApiError(409, f"the game did not add the copy of {source_name} to the {destination}")
    journal.record(
        "inventory.add", target=added["id"], before=None,
        after={"source": item_id, "unique_id": unique_id},
        undo=lambda: codes.undo_add(destination, unique_id),
    )  # fmt: skip
    return {"ok": True, "id": added["id"], "name": added["name"], "unique_id": unique_id, "destination": destination}


def delete(params: Params) -> dict[str, Any]:
    """Deletes a backpack or bank item. The code is made first, so the reply can carry it for restoring."""
    from . import bank, codes  # local: both import this module

    item_id = _need_id(params)
    warnings: list[str] = []
    if item_id.startswith("bank:"):
        index, entry = bank.find_bank_entry(item_id)
        name = bank.entry_card(index, entry)[0]["name"]
        unique_id = bank.entry_unique_id(entry)
        destination = "bank"
        code = _code_or_refuse(bank.export_source(entry), name, warnings, codes)
        warnings.extend(bank.remove_bank_entry(index, entry))
    else:
        item, location = _find_item(params)
        if location == EQUIPPED:
            raise ApiError(409, "unequip it first")
        name, unique_id, destination = item.GetHumanReadableName(), stats.unique_id(item), "backpack"
        code = _code_or_refuse(item, name, warnings, codes)
        remove_from_backpack(_manager(), item)
    journal.record(
        "inventory.delete", target=item_id, before={"name": name, "unique_id": unique_id, "code": code}, after=None,
        undo=lambda: codes.restore_code(code, destination),
    )  # fmt: skip
    return {"ok": True, "deleted": {"name": name, "unique_id": unique_id, "code": code}, "warnings": warnings}


def _code_or_refuse(source: Any, name: str, warnings: list[str], codes: Any) -> str:
    try:
        code, _header, code_warnings = codes.export_code(source)
    except Exception as ex:  # noqa: BLE001
        raise ApiError(409, f"could not make a code for {name}, so it was not deleted ({ex})") from None
    warnings.extend(f"{name}: {w}" for w in code_warnings)
    return code


def move(params: Params) -> dict[str, Any]:
    """Moves an item between the backpack and the bank. Its UniqueId stays the same."""
    to = params.get("to")
    if to not in ("bank", "backpack"):
        raise ApiError(400, "'to' must be bank or backpack")
    item_id = _need_id(params)
    if item_id.startswith("bank:"):
        from . import bank  # local: bank imports this module

        index, entry = bank.find_bank_entry(item_id)
        if to == "bank":
            raise ApiError(400, "that item is already in the bank")
        result = _bank_to_backpack(index, entry)
        origin = BANK
    else:
        item, location = _find_item(params)
        if location == EQUIPPED:
            raise ApiError(409, "unequip it first")
        if to == "backpack":
            raise ApiError(400, "that item is already in the backpack")
        result = _backpack_to_bank(item)
        origin = BACKPACK
    unique_id = result.pop("_unique_id")
    journal.record(
        "inventory.move", target=result["id"], before={"location": origin, "unique_id": unique_id},
        after={"location": to}, undo=lambda: _move_by_uid(unique_id, origin),
    )  # fmt: skip
    return {"ok": True, "id": result["id"], "name": result["name"], "to": to}


def _backpack_to_bank(item: unreal.UObject) -> dict[str, Any]:
    from . import bank, codes  # local: both import this module

    unique_id, name = stats.unique_id(item), item.GetHumanReadableName()
    try:
        copy = codes.copy_definition(item.DefinitionData, _is_weapon(item))
    except Exception as ex:  # noqa: BLE001
        raise _game_failed(f"reading {name}", ex) from None
    if not bank.add_def_to_bank(copy, _is_weapon(item)):
        raise ApiError(409, "the bank is full or refused the item")
    try:
        remove_from_backpack(_manager(), item)
    except ApiError:
        _undo_quietly(lambda: bank.remove_bank_entry(*bank.find_by_unique_id(unique_id)), "bank copy")
        raise
    added = codes.find_added("bank", unique_id)
    return {"id": added["id"] if added else None, "name": name, "_unique_id": unique_id}


def _bank_to_backpack(index: int, entry: Any) -> dict[str, Any]:
    from . import bank, codes  # local: both import this module

    name = bank.entry_card(index, entry)[0]["name"]
    unique_id = bank.entry_unique_id(entry)
    definition, is_weapon = bank.entry_definition(entry)
    try:
        copy = codes.copy_definition(definition, is_weapon)
    except Exception as ex:  # noqa: BLE001
        raise _game_failed(f"reading {name}", ex) from None
    problem = codes.place_definition(copy, is_weapon, "backpack", bank.entry_mark(entry))
    if problem:
        raise ApiError(409, problem)
    if codes.find_added("backpack", unique_id) is None:
        raise ApiError(409, f"the game did not add {name} to the backpack; it is still in the bank")
    try:
        bank.remove_bank_entry(index, entry)
    except ApiError:
        _undo_quietly(lambda: codes.undo_add("backpack", unique_id), "backpack copy")
        raise
    added = codes.find_added("backpack", unique_id)
    return {"id": added["id"] if added else None, "name": name, "_unique_id": unique_id}


def _move_by_uid(unique_id: int, to: str) -> None:
    """Undo of a move: finds the item by UniqueId wherever it is now and moves it to `to`."""
    from . import bank  # local: bank imports this module

    if to == BACKPACK:
        found = bank.find_by_unique_id(unique_id)
        if found is not None:
            _bank_to_backpack(*found)
        return
    item = backpack_item(unique_id)
    if item is not None:
        _backpack_to_bank(item)


def _undo_quietly(action: Any, what: str) -> None:
    try:
        action()
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: could not undo the {what} after a failed move: {ex!r}")


def inventory_import(params: Params) -> dict[str, Any]:
    """Imports codes (from inventory_export or a list of codes). replace=true first removes the backpack and/or bank
    items of the places that receive items (never equipped items). Nothing is removed unless every code is valid."""
    from . import bank, codes  # local: both import this module

    entries = params.get("items")
    destination = params.get("destination", "backpack")
    if not isinstance(entries, list):
        raise ApiError(400, "'items' must be a list")
    if destination not in ("backpack", "bank", "original"):
        raise ApiError(400, "destination must be backpack, bank or original")
    if len(entries) > codes.MAX_CODES:
        raise ApiError(400, f"at most {codes.MAX_CODES} items at a time")
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        code = entry.get("code") if isinstance(entry, dict) else None
        if not isinstance(code, str):
            raise ApiError(400, "each item needs a code")
        if destination == "original":
            target = "bank" if entry.get("location") == BANK else "backpack"
        else:
            target = destination
        pairs.append((code, target))

    warnings: list[str] = []
    removed: list[dict[str, Any]] = []
    if params.get("replace"):
        receivers = {target for _code, target in pairs}
        codes.check_codes([code for code, _target in pairs])
        manager = _manager()
        if "backpack" in receivers:
            backpack_items = [item for item in manager.Backpack if item is not None]
            room = manager.GetEmptyBackpackSlots() + len(backpack_items)  # UNVERIFIED call
            needed = sum(1 for _code, target in pairs if target == "backpack")
            if room < needed:
                raise ApiError(409, f"the backpack would have room for {room} after the replace; the import has {needed}")
            removed += _clear_backpack(manager, warnings, codes)
        if "bank" in receivers:
            capacity = bank.capacity()
            needed = sum(1 for _code, target in pairs if target == "bank")
            if capacity is not None and capacity < needed:
                raise ApiError(409, f"the bank holds {capacity} items after the replace; the import has {needed}")
            removed += _clear_bank(warnings, bank, codes)

    result = codes.import_pairs(pairs)
    result["removed"] = len(removed)
    result["warnings"] = warnings
    if removed:
        journal.record(
            "inventory.replace", target=destination, before={"removed": removed}, after={"imported": result["imported"]},
            undo=lambda: _restore_removed(removed),
        )  # fmt: skip
    return result


def _clear_backpack(manager: unreal.UObject, warnings: list[str], codes: Any) -> list[dict[str, Any]]:
    removed: list[dict[str, Any]] = []
    for item in [i for i in manager.Backpack if i is not None]:
        name = item.GetHumanReadableName()
        code = _code_for_removal(item, name, warnings)
        try:
            remove_from_backpack(manager, item)
        except ApiError as ex:
            raise ApiError(ex.status, f"{ex} (the replace stopped after removing {len(removed)} items)") from None
        if code is not None:
            removed.append({"code": code, "name": name, "location": BACKPACK})
    return removed


def _clear_bank(warnings: list[str], bank: Any, codes: Any) -> list[dict[str, Any]]:
    removed: list[dict[str, Any]] = []
    for _ in range(codes.MAX_CODES):
        entries = bank.bank_entries()
        if not entries:
            return removed
        index, entry = entries[-1]
        name = bank.entry_card(index, entry)[0]["name"]
        code = _code_for_removal(bank.export_source(entry), name, warnings)
        try:
            warnings.extend(bank.remove_bank_entry(index, entry))
        except ApiError as ex:
            raise ApiError(ex.status, f"{ex} (the replace stopped after removing {len(removed)} bank items)") from None
        if code is not None:
            removed.append({"code": code, "name": name, "location": BANK})
    raise ApiError(409, "the bank still holds items after the replace; reload and try again")


def _restore_removed(removed: list[dict[str, Any]]) -> None:
    from . import codes  # local: codes imports this module

    for row in removed:
        codes.restore_code(row["code"], "bank" if row["location"] == BANK else "backpack")


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = ApiError

GET_ROUTES: dict[str, Route] = {"/api/items": items, "/api/inventory/export": inventory_export}
POST_ROUTES: dict[str, Route] = {
    "/api/inventory/duplicate": duplicate,
    "/api/inventory/delete": delete,
    "/api/inventory/move": move,
    "/api/inventory/import": inventory_import,
}
