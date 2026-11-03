"""Gear inventory: finds the items the player owns (equipped, backpack and bank) and the ids they are known by.

Also the inventory write routes: duplicate, delete, move between backpack and bank, and export/import of the whole
inventory as item codes. codes.py and bank.py import this module at their top, so they are imported here inside the
functions that need them (see the import notes on each route).

The writes add or copy first, verify the result by UniqueId, and only then remove; on a mismatch they undo and answer
409 (naming the code of anything that could not be put back). The JSON import only ever adds items.
"""

from __future__ import annotations

from collections import Counter
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


def _backpack_ids(manager: unreal.UObject) -> list[str]:
    return [_item_id(item) for item in manager.Backpack if item is not None]


def _code_or_name(item: unreal.UObject) -> str:
    """The item's code (UNVERIFIED CreateSerialNumber), else its name: what a user needs to get it back."""
    from . import codes  # local: codes imports this module

    try:
        return codes.export_code(item)[0]
    except Exception:  # noqa: BLE001
        try:
            return f"{item.GetHumanReadableName()} (no code could be made)"
        except Exception:  # noqa: BLE001
            return "an item (no code could be made)"


def remove_from_backpack(manager: unreal.UObject, item: unreal.UObject) -> None:
    """Removes a backpack item and checks the backpack lost exactly it: the ids before, minus the target, must equal
    the ids after. Raises ApiError 409 otherwise; when other items vanished too, the message names them by the codes
    taken just before the removal (UNVERIFIED: RemoveFromInventory, CreateSerialNumber)."""
    item_id = _item_id(item)
    others = [other for other in manager.Backpack if other is not None and _item_id(other) != item_id]
    backups = {_item_id(other): _code_or_name(other) for other in others}
    before = Counter(_backpack_ids(manager))
    try:
        manager.RemoveFromInventory(item, False)  # UNVERIFIED call
    except Exception as ex:  # noqa: BLE001
        raise _game_failed("removing the item", ex) from None
    now = Counter(_backpack_ids(manager))
    expected = before - Counter({item_id: 1})
    if now == expected:
        return
    if now == before:
        raise ApiError(409, "the game did not remove the item from the backpack")
    problems = []
    if now[item_id] > expected[item_id]:
        problems.append("the item is still in the backpack")
    vanished = expected - now
    if vanished:
        lost = [backups.get(other_id, other_id) for other_id in vanished.elements()]
        problems.append(f"other items vanished from the backpack, re-import them: {', '.join(lost)}")
    if now - expected - Counter({item_id: now[item_id]}):
        problems.append("items RTSE did not expect appeared in the backpack")
    raise ApiError(409, f"removing the item went wrong: {'; '.join(problems)}")


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
#
# Every destructive step follows one rule: add or copy first, verify the exact result by UniqueId
# (codes.add_verified, bank.remove_exact, remove_from_backpack), only then remove the source; on any mismatch,
# undo what was done and answer 409. Nothing is removed unless its code passed codes.verified_backup.


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
    added = codes.add_verified(copy, is_weapon, destination, unique_id, mark)
    journal.record(
        "inventory.add", target=added["id"], before=None,
        after={"source": item_id, "unique_id": unique_id},
        undo=lambda: codes.undo_add(destination, unique_id),
    )  # fmt: skip
    return {"ok": True, "id": added["id"], "name": added["name"], "unique_id": unique_id, "destination": destination}


def _bank_definition(entry: Any, name: str, action: str) -> tuple[Any, bool]:
    """A bank entry's (definition, is_weapon), or the refusal a delete or move gives when it cannot be read."""
    from . import bank  # local: bank imports this module

    try:
        return bank.entry_definition(entry)
    except ApiError as ex:
        raise ApiError(409, f"could not make a restorable code for {name}, so it was not {action} ({ex})") from None


def delete(params: Params) -> dict[str, Any]:
    """Deletes a backpack or bank item. Refused unless the item's code passes codes.verified_backup, so the code the
    reply carries (and the undo re-imports) really gives the same item back."""
    from . import bank, codes  # local: both import this module

    item_id = _need_id(params)
    if item_id.startswith("bank:"):
        index, entry = bank.find_bank_entry(item_id)
        name = bank.entry_card(index, entry)[0]["name"]
        unique_id = bank.entry_unique_id(entry)
        destination = "bank"
        definition, is_weapon = _bank_definition(entry, name, "deleted")
        code = codes.verified_backup(bank.export_source(entry), definition, is_weapon, name=name)
        bank.remove_exact(index, entry)
    else:
        item, location = _find_item(params)
        if location == EQUIPPED:
            raise ApiError(409, "unequip it first")
        name, unique_id, destination = item.GetHumanReadableName(), stats.unique_id(item), "backpack"
        code = codes.verified_backup(item, item.DefinitionData, _is_weapon(item), name=name)
        manager = _manager()
        try:
            remove_from_backpack(manager, item)
        except ApiError as ex:
            if _item_id(item) in _backpack_ids(manager):
                raise
            raise ApiError(409, f"{ex}; {name} itself was removed, its code to restore it: {code}") from None
    journal.record(
        "inventory.delete", target=item_id, before={"name": name, "unique_id": unique_id, "code": code}, after=None,
        undo=lambda: codes.restore_code(code, destination),
    )  # fmt: skip
    return {"ok": True, "deleted": {"name": name, "unique_id": unique_id, "code": code}, "warnings": []}


def move(params: Params) -> dict[str, Any]:
    """Moves an item between the backpack and the bank. Its UniqueId stays the same.

    The copy is added and verified at the destination first; only then is the source removed (verified too). If the
    source removal fails while the source is still there, the copy is removed again. Success means both ends were
    verified, and the returned id is the verified destination id.
    """
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

    manager = _manager()
    unique_id, name, weapon = stats.unique_id(item), item.GetHumanReadableName(), _is_weapon(item)
    item_id = _item_id(item)
    code = codes.verified_backup(item, item.DefinitionData, weapon, name=name, action="moved")
    try:
        copy = codes.copy_definition(item.DefinitionData, weapon)
    except Exception as ex:  # noqa: BLE001
        raise _game_failed(f"reading {name}", ex) from None
    added = codes.add_verified(copy, weapon, "bank", unique_id, mark_of(item))
    try:
        remove_from_backpack(manager, item)
    except ApiError as ex:
        if item_id not in _backpack_ids(manager):
            raise ApiError(
                409, f"{ex}; {name} itself left the backpack and is in the bank now (its code: {code})",
            ) from None
        try:
            bank.remove_exact(*bank.find_bank_entry(added["id"]))
        except ApiError as ex2:
            raise ApiError(
                409, f"{ex}; the bank copy could not be removed again ({ex2}), so {name} may be in both places",
            ) from None
        raise ApiError(409, f"{ex}; the bank copy was removed again and {name} stays in the backpack") from None
    return {"id": added["id"], "name": name, "_unique_id": unique_id}


def _bank_uid_count(unique_id: int) -> int | None:
    from . import bank  # local: bank imports this module

    try:
        return sum(1 for snap in bank.snapshot() if snap.unique_id == unique_id)
    except Exception:  # noqa: BLE001
        return None


def _bank_to_backpack(index: int, entry: Any) -> dict[str, Any]:
    from . import bank, codes  # local: both import this module

    name = bank.entry_card(index, entry)[0]["name"]
    unique_id = bank.entry_unique_id(entry)
    definition, is_weapon = _bank_definition(entry, name, "moved")
    code = codes.verified_backup(bank.export_source(entry), definition, is_weapon, name=name, action="moved")
    try:
        copy = codes.copy_definition(definition, is_weapon)
    except Exception as ex:  # noqa: BLE001
        raise _game_failed(f"reading {name}", ex) from None
    in_bank = _bank_uid_count(unique_id)
    added = codes.add_verified(copy, is_weapon, "backpack", unique_id, bank.entry_mark(entry))
    try:
        bank.remove_exact(index, entry)
    except ApiError as ex:
        still = _bank_uid_count(unique_id)
        if in_bank is None or still is None or still < in_bank:
            raise ApiError(
                409, f"{ex}; {name} is in the backpack now and may be gone from the bank (its code: {code})",
            ) from None
        manager = _manager()
        copy_item = next((i for i in manager.Backpack if i is not None and _item_id(i) == added["id"]), None)
        try:
            if copy_item is None:
                raise ApiError(409, "the copy is no longer in the backpack")
            remove_from_backpack(manager, copy_item)
        except ApiError as ex2:
            raise ApiError(
                409, f"{ex}; the backpack copy could not be removed again ({ex2}), so {name} may be in both places",
            ) from None
        raise ApiError(409, f"{ex}; the backpack copy was removed again and {name} stays in the bank") from None
    return {"id": added["id"], "name": name, "_unique_id": unique_id}


def _move_by_uid(unique_id: int, to: str) -> None:
    """Undo of a move: finds the item by UniqueId wherever it is now and moves it to `to` (verified the same way)."""
    from . import bank  # local: bank imports this module

    if to == BACKPACK:
        found = bank.find_by_unique_id(unique_id)
        if found is not None:
            _bank_to_backpack(*found)
        return
    item = backpack_item(unique_id)
    if item is not None:
        _backpack_to_bank(item)


REPLACE_REMOVED = "replace was removed: imports only add items; delete items separately"
EQUIPPED_CHOICES = ("skip", "backpack")
EQUIPPED_SKIPPED = "equipped item skipped"


def inventory_import(params: Params) -> dict[str, Any]:
    """Imports codes (from inventory_export or a list of codes). ADDITIVE ONLY: nothing is ever removed.

    destination "original" sends Bank rows to the bank and Backpack rows to the backpack; "backpack" / "bank" send
    every row there. Equipped rows are skipped (equipped "skip", the default: the item is still equipped, so importing
    it again would duplicate it) or go to the backpack (equipped "backpack"). One call imports at most one chunk
    (codes.Budget); the caller re-sends items[processed:] until remaining is 0. Skipped rows do not use the budget.
    """
    from . import codes  # local: codes imports this module

    if "replace" in params and params["replace"] is not False:
        raise ApiError(400, REPLACE_REMOVED)
    entries = params.get("items")
    destination = params.get("destination", "backpack")
    equipped = params.get("equipped", "skip")
    if not isinstance(entries, list):
        raise ApiError(400, "'items' must be a list")
    if destination not in ("backpack", "bank", "original"):
        raise ApiError(400, "destination must be backpack, bank or original")
    if equipped not in EQUIPPED_CHOICES:
        raise ApiError(400, "equipped must be skip or backpack")
    if len(entries) > codes.MAX_CODES:
        raise ApiError(400, f"at most {codes.MAX_CODES} items at a time")
    rows: list[tuple[str, str | None]] = []  # (code, destination), None = skipped
    for entry in entries:
        code = entry.get("code") if isinstance(entry, dict) else None
        if not isinstance(code, str):
            raise ApiError(400, "each item needs a code")
        location = entry.get("location")
        if location == EQUIPPED:
            target = "backpack" if equipped == "backpack" else None
        elif destination == "original":
            target = "bank" if location == BANK else "backpack"
        else:
            target = destination
        rows.append((code, target))

    batch, budget = codes.ImportBatch(), codes.Budget()
    for code, target in rows:
        if budget.spent():
            break
        if target is None:
            batch.skip({"code": code, "ok": False, "skipped": True, "error": EQUIPPED_SKIPPED, "id": None, "name": None,
                        "unique_id": None})  # fmt: skip
            continue
        budget.step()
        batch.run(code, target)
    result = batch.summary(len(rows))
    result["skipped"] = batch.skipped
    result["warnings"] = [f"{batch.skipped} equipped items were skipped (they are still equipped)"] if batch.skipped else []
    return result


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = ApiError

GET_ROUTES: dict[str, Route] = {"/api/items": items, "/api/inventory/export": inventory_export}
POST_ROUTES: dict[str, Route] = {
    "/api/inventory/duplicate": duplicate,
    "/api/inventory/delete": delete,
    "/api/inventory/move": move,
    "/api/inventory/import": inventory_import,
}
