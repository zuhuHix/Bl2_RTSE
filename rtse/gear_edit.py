"""Gear edits: change an item's part, level or stat, rebuilding it from its definition data.

Every edit is written, the item is rebuilt, the game is asked whether it took the change, and the old fields
are put back if it did not.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import find_object, logging, unreal

from . import journal, limits, stats
from .gear_inventory import EQUIPPED, _find_item, _hidden_bones, _item_id, _note_id_change
from .gear_parts import (
    ITEM_SLOTS,
    STATUS_UNSUPPORTED,
    WEAPON_SLOTS,
    Slot,
    _is_weapon,
    _legal_part_lists,
    _mode,
    _part_ref,
    _ref,
    _ref_dict,
    _slots,
    balance_chain_view,
    dlc_dict,
    item_dlc,
    part_status,
)
from .routing import ApiError, Params, Route

MIN_LEVEL = 1  # The gear level cap is limits.gear_level_cap(pc): character cap + Overpower levels, not 100

def _reinitialize(item: unreal.UObject, *, equipped: bool, reroll_names: bool = True) -> None:
    """Makes the game rebuild the item from its (edited) definition data.

    reroll_names=True clears the title and prefix (so the game picks new ones) and gives the item a new
    UniqueId. reroll_names=False keeps both and leaves the UniqueId alone; restoring a snapshot uses it.
    """
    data = item.DefinitionData
    if _is_weapon(item):
        if reroll_names:
            data.TitlePartDefinition = None
            data.PrefixPartDefinition = None
        # Initializing a weapon without a weapon type crashes the game
        can_initialize = data.WeaponTypeDefinition is not None
    else:
        if reroll_names:
            data.TitleItemNamePartDefinition = None
            data.PrefixItemNamePartDefinition = None
        can_initialize = data.ItemDefinition is not None

    if can_initialize:
        item.InitializeInternal(True)
        if not equipped:
            # Unreadying would holster/unequip gear the player is using
            item.Unready(True)
    if reroll_names:
        item.DefinitionData.UniqueId = item.GenerateUniqueID()


# Fields a rebuild reads and a part or level edit writes. Everything else on the item is left alone.
_WEAPON_REBUILD_FIELDS = (
    "WeaponTypeDefinition", "BalanceDefinition", "ManufacturerDefinition",
    *(f"{s}PartDefinition" for s in WEAPON_SLOTS), "PrefixPartDefinition", "TitlePartDefinition",
)  # fmt: skip
_ITEM_REBUILD_FIELDS = (
    "ItemDefinition", "BalanceDefinition", "ManufacturerDefinition",
    *(f"{s}ItemPartDefinition" for s in ITEM_SLOTS), "PrefixItemNamePartDefinition", "TitleItemNamePartDefinition",
)  # fmt: skip
_LEVEL_FIELDS = ("GameStage", "ManufacturerGradeIndex", "UniqueId")


def _path_or_none(obj: unreal.UObject | None) -> str | None:
    return None if obj is None else obj._path_name()  # noqa: SLF001


def _snapshot(item: unreal.UObject) -> dict[str, Any]:
    """Every field a rebuild or a part/level edit can change, so a failed edit can be put back exactly.

    Fields this game build does not have are skipped. Any other read error stops the edit before it starts.
    """
    data = item.DefinitionData
    fields = _WEAPON_REBUILD_FIELDS if _is_weapon(item) else _ITEM_REBUILD_FIELDS
    snapshot: dict[str, Any] = {}
    for field in (*fields, *_LEVEL_FIELDS):
        try:
            snapshot[field] = getattr(data, field)
        except AttributeError:
            continue
    try:
        snapshot["ExpLevel"] = item.ExpLevel
    except AttributeError:
        pass
    return snapshot


def _restore(item: unreal.UObject, snapshot: dict[str, Any], *, equipped: bool) -> None:
    for field, value in snapshot.items():
        if field != "ExpLevel":
            setattr(item.DefinitionData, field, value)
    # Rebuild with the old UniqueId already back in place, so the stat-override hooks find the right gun
    _reinitialize(item, equipped=equipped, reroll_names=False)
    if "UniqueId" in snapshot:
        item.DefinitionData.UniqueId = snapshot["UniqueId"]
    if "ExpLevel" in snapshot:
        item.ExpLevel = snapshot["ExpLevel"]


def _edit_with_rollback(
    item: unreal.UObject,
    *,
    equipped: bool,
    what: str,
    write: Callable[[], None],
    verify: Callable[[], str | None],
    after_rebuild: Callable[[], None] | None = None,
) -> None:
    """Writes an edit, rebuilds the item and checks the game took it.

    verify() returns None when the item is as requested, else a description of the problem. If the write or
    the rebuild raises, or the check fails, every field from before is put back and an ApiError is raised.
    Overrides are not moved here: callers do that only after this returns normally.
    """
    snapshot = _snapshot(item)
    try:
        write()
        _reinitialize(item, equipped=equipped)
        if after_rebuild is not None:
            after_rebuild()
        problem = verify()
    except Exception as ex:  # noqa: BLE001
        problem = f"the game raised {ex!r}"
    if problem is None:
        return

    logging.error(f"RTSE: {what} rejected: {problem}")
    try:
        _restore(item, snapshot, equipped=equipped)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: restoring {what} also failed: {ex!r} (fields {sorted(snapshot)})")
        raise ApiError(
            500,
            f"the change failed AND restoring failed - reload the save before playing ({problem})",
        ) from None
    raise ApiError(409, f"the game did not accept that change; the previous parts were restored ({problem})")


def _gear_limits() -> dict[str, Any]:
    """The gear level range for the game as it is right now (limits.py explains the fallbacks)."""
    return limits.gear_level_cap(get_pc(possibly_loading=True))


def _level_range_message(gear: dict[str, Any]) -> str:
    return (
        f"level must be {gear['min']}-{gear['max']} (gear is level {gear['character_max']} + OP n, "
        f"so {gear['max']} = level {gear['character_max']} OP{gear['overpower_max']})"
    )



# --- Save checks and part checks (UNVERIFIED) ----------------------------------------------------
# Every game call in this section is named from class data; what it does at runtime has never been run in game.
# Results carry "verified": False. The checks only read: nothing on the live item is written by them.

UNVERIFIED_NOTE = "Function names exist in the game's class data; what they do at runtime is unverified."
VALIDATION_FUNCTIONS = (
    "CheckDefMatch", "SerialNumberMatchesDef", "CanBeSaved", "CreateSerialNumber", "PackSerialNumber",
    "UnpackSerialNumber", "IsDLCRequirementMet", "GetDLCRestrictedMessage",
)  # fmt: skip
DEBUG_VALIDATION_FILE = Path(__file__).parent / "debug_validation.json"

# Per game function name (and "struct copy"): how often RTSE used it, whether it exists, and what came back.
_probe_log: dict[str, dict[str, Any]] = {}


def _probe_entry(name: str) -> dict[str, Any]:
    return _probe_log.setdefault(
        name, {"exists": False, "calls": 0, "errors": 0, "last_result": None, "last_error": None},
    )


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 200 else text[:197] + "..."


def _probe(obj: Any, name: str, *args: Any) -> tuple[bool, Any]:
    """Calls a game function if the object has it. Returns (called, value).

    A missing function gives (False, None). When the call raises, the value is the exception itself, so callers
    report it as a failed check. Never raises; every call is logged in _probe_log.
    """
    entry = _probe_entry(name)
    try:
        function = getattr(obj, name, None)
    except Exception:  # noqa: BLE001
        function = None
    if not callable(function):
        entry["exists"] = False
        return False, None
    entry["exists"] = True
    entry["calls"] += 1
    try:
        value = function(*args)
    except Exception as ex:  # noqa: BLE001
        entry["errors"] += 1
        entry["last_error"] = repr(ex)
        return True, ex
    entry["last_result"] = _short(value)
    return True, value


def _has_function(obj: Any, name: str) -> bool:
    try:
        return callable(getattr(obj, name, None))
    except Exception:  # noqa: BLE001
        return False


def _unwrap(value: Any) -> Any:
    """A game function with out parameters returns (result, *outs). This takes the result."""
    if isinstance(value, tuple):
        return value[0] if value else None
    return value


def _unpack_result(value: Any) -> tuple[Any, Any]:
    """(ok, unpacked definition) from an UnpackSerialNumber result. Without out values there is no definition."""
    if isinstance(value, tuple):
        return (value[0] if value else None), (value[1] if len(value) > 1 else None)
    return bool(value), None


def _plain(value: Any) -> Any:
    """A JSON-friendly stand-in: object paths for objects, scalars as they are, anything else as short text."""
    if isinstance(value, unreal.UObject):
        return _path_or_none(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return _short(value)


def _same_value(a: Any, b: Any) -> bool:
    return _plain(a) == _plain(b)


def _def_fields(item: unreal.UObject) -> tuple[str, ...]:
    """The DefinitionData fields a serial carries: the same ones _snapshot covers."""
    fields = _WEAPON_REBUILD_FIELDS if _is_weapon(item) else _ITEM_REBUILD_FIELDS
    return (*fields, *_LEVEL_FIELDS)


def _field_values(data: Any, fields: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in fields:
        try:
            values[field] = getattr(data, field)
        except AttributeError:
            continue  # this build does not have the field
    return values


def _note_struct_copy(method: str, error: str | None = None) -> None:
    entry = _probe_entry("struct copy")
    entry["exists"] = True
    entry["calls"] += 1
    entry["last_result"] = method
    entry["last_error"] = error


def _copy_def(item: unreal.UObject, changes: dict[str, Any]) -> Any:
    """A COPY of the item's DefinitionData with `changes` applied. The live item is never changed.

    UNVERIFIED: unrealsdk.make_struct builds a fresh struct from the live fields, so nothing is ever written to the
    live item. Only when make_struct is missing or fails is copy.copy tried; if a write to that "copy" shows up on
    the live struct (it shares storage), the originals are put back at once and the check is abandoned. The method
    used is recorded in _probe_log["struct copy"].
    """
    data = item.DefinitionData
    make = getattr(unrealsdk, "make_struct", None)
    make_error: str | None = None
    if make is not None:
        values = _field_values(data, _def_fields(item))
        values.update(changes)
        struct_name = "WeaponDefinitionData" if _is_weapon(item) else "ItemDefinitionData"
        try:
            made = make(struct_name, **values)
        except Exception as ex:  # noqa: BLE001
            make_error = f"make_struct raised {ex!r}"
        else:
            _note_struct_copy("make_struct")
            return made
    try:
        copied = copy.copy(data)
    except Exception as ex:  # noqa: BLE001
        message = f"cannot copy the definition ({make_error or 'no make_struct'}; copy.copy raised {ex!r})"
        _note_struct_copy("none", message)
        raise RuntimeError(message) from None
    if copied is data:
        message = f"cannot copy the definition ({make_error or 'no make_struct'}; copy.copy returned the live struct)"
        _note_struct_copy("none", message)
        raise RuntimeError(message)
    originals = {field: getattr(data, field) for field in changes}
    for field, value in changes.items():
        setattr(copied, field, value)
    if all(_same_value(getattr(data, field), originals[field]) for field in changes):
        _note_struct_copy("copy.copy", make_error)
        return copied
    for field, value in originals.items():
        setattr(data, field, value)  # the "copy" shared the live struct: put the originals back
    message = "cannot copy the definition (copy.copy shared the live struct; originals restored)"
    _note_struct_copy("none", message)
    raise RuntimeError(message)


def _hypothetical_def(item: unreal.UObject, field: str, part: unreal.UObject | None) -> Any:
    """The definition the item would have with `field` set to `part` (None removes it). A copy; the item is untouched."""
    return _copy_def(item, {field: part})


def _compare_defs(expected: Any, got: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """The fields where two definitions differ. Objects compare by path. UniqueId is skipped (a serial may lack it)."""
    differences: list[dict[str, Any]] = []
    for field in fields:
        if field == "UniqueId":
            continue
        try:
            before, after = getattr(expected, field), getattr(got, field)
        except AttributeError:
            continue
        if not _same_value(before, after):
            differences.append({"field": field, "expected": _plain(before), "got": _plain(after)})
    return differences


def _skipped(name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": None, "detail": detail}


def _check(name: str, called: bool, value: Any) -> dict[str, Any]:
    """One yes/no game check. A missing function, a raised error and a None result all give ok None."""
    if not called:
        return _skipped(name, "not available on this object")
    if isinstance(value, Exception):
        return _skipped(name, f"the game raised {value!r}")
    result = _unwrap(value)
    if result is None:
        return _skipped(name, "the game returned nothing")
    ok = bool(result)
    return {"name": name, "ok": ok, "detail": f"returned {ok}"}


def _unchecked(reason: str) -> dict[str, Any]:
    """A SaveCheck that could not be made: survives is None."""
    return {
        "survives": None,
        "summary": f"Could not check: {reason}",
        "verified": False,
        "checks": [_skipped("serial round-trip", reason)],
        "lost": [],
    }


def _make_serial(item: unreal.UObject, definition: Any) -> tuple[Any, dict[str, Any] | None]:
    """A serial for the live item (definition None) or for a definition. Returns (serial, None) or (None, a skip)."""
    name = "serial round-trip"
    if definition is None:
        called, raw = _probe(item, "CreateSerialNumber")
    else:
        called, raw = _probe(item, "PackSerialNumber", definition)
    if not called:
        return None, _skipped(name, "not available on this object")
    if isinstance(raw, Exception):
        return None, _skipped(name, f"the game raised {raw!r}")
    serial = _unwrap(raw)
    if serial is None:
        return None, _skipped(name, "the game returned no serial number")
    return serial, None


def _round_trip(item: unreal.UObject, target: Any, serial: Any) -> tuple[dict[str, Any], Any, list[dict[str, Any]]]:
    """Unpacks the serial and compares the definition it gives back with `target`.

    Returns (the check, the unpacked definition or None, the fields lost). An unpack that needs an out parameter
    (TypeError) is retried with a copy to unpack into, never the live struct.
    """
    name = "serial round-trip"
    called, value = _probe(item, "UnpackSerialNumber", serial)
    if called and isinstance(value, TypeError):
        try:
            seed = _copy_def(item, {})
        except Exception as ex:  # noqa: BLE001
            return _skipped(name, f"could not make a copy to unpack into ({ex})"), None, []
        called, value = _probe(item, "UnpackSerialNumber", serial, seed)
    if not called:
        return _skipped(name, "not available on this object"), None, []
    if isinstance(value, Exception):
        return _skipped(name, f"the game raised {value!r}"), None, []
    ok, unpacked = _unpack_result(value)
    if ok is False:
        return {"name": name, "ok": False, "detail": "UnpackSerialNumber returned False"}, None, []
    if unpacked is None:
        return _skipped(name, "the game gave no definition back to compare"), None, []
    lost = _compare_defs(target, unpacked, _def_fields(item))
    detail = "nothing lost" if not lost else "lost " + ", ".join(d["field"] for d in lost)
    return {"name": name, "ok": not lost, "detail": detail}, unpacked, lost


def _save_check(item: unreal.UObject, definition: Any) -> dict[str, Any]:
    target = item.DefinitionData if definition is None else definition
    checks: list[dict[str, Any]] = []
    serial, skipped = _make_serial(item, definition)
    unpacked: Any = None
    lost: list[dict[str, Any]] = []
    if serial is None:
        checks.append(skipped)
        checks.append(_skipped("SerialNumberMatchesDef", "no serial number to test"))
        checks.append(_skipped("CheckDefMatch", "no serial number to test"))
    else:
        round_trip, unpacked, lost = _round_trip(item, target, serial)
        checks.append(round_trip)
        called, value = _probe(item, "SerialNumberMatchesDef", serial, target, False, False)
        checks.append(_check("SerialNumberMatchesDef", called, value))
        if unpacked is None:
            checks.append(_skipped("CheckDefMatch", "nothing unpacked to compare"))
        else:
            called, value = _probe(item, "CheckDefMatch", unpacked, target, False, False)
            checks.append(_check("CheckDefMatch", called, value))
    if definition is None and _is_weapon(item):  # CanBeSaved is about the live weapon only
        called, value = _probe(item, "CanBeSaved")
        checks.append(_check("CanBeSaved", called, value))

    failed = [c for c in checks if c["ok"] is False]
    if failed:
        survives: bool | None = False
    elif checks[0]["ok"] is True:
        survives = True
    else:
        survives = None

    if survives is False:
        if lost:
            summary = "Lost in a serial round-trip: " + ", ".join(d["field"] for d in lost) + " (unverified)"
        elif failed[0]["name"] == "CanBeSaved":
            summary = "CanBeSaved() returned False (unverified meaning)"
        elif failed[0]["name"] == "serial round-trip":
            summary = "The serial did not unpack (unverified)"
        else:
            summary = f"{failed[0]['name']}() returned False (unverified)"
    elif survives is True:
        summary = "Survived a serial round-trip (unverified)"
    else:
        reason = next((c["detail"] for c in checks if c["ok"] is None), "no checks ran")
        summary = f"Could not check: {reason}"
    return {
        "survives": survives,
        "summary": summary,
        "verified": False,
        "checks": checks,
        "lost": lost,
    }


def save_check(item: unreal.UObject, definition: Any = None) -> dict[str, Any]:
    """Would the game keep this definition through a serial round-trip?

    definition None checks the live item (after an apply); a definition checks that one (before an apply).
    UNVERIFIED: built on game function names from class data. Never raises: a failure comes back as survives None.
    """
    try:
        return _save_check(item, definition)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: save check failed: {ex!r}")
        return _unchecked(f"the check itself failed ({ex!r})")


def _status_of(item: unreal.UObject, slot: Slot, ref: Any) -> dict[str, Any]:
    """The status of an option: from the slot if it was computed, else worked out now. Never raises."""
    known = (slot.statuses or {}).get(ref.id)
    if known is not None:
        return known
    try:
        legal = [_ref(p) for p in _legal_part_lists(item).get(slot.name, [])]
        return part_status(
            ref,
            slot=slot.name,
            weapon=_is_weapon(item),
            legal_ids={r.id for r in legal},
            own_classes={r.class_name for r in legal},
        )
    except Exception:  # noqa: BLE001
        return {"status": None, "status_reason": None, "dlc": None}


def check_part(params: Params) -> dict[str, Any]:
    """Would this part survive a serial round-trip? Read-only: the item is not changed. UNVERIFIED (see save_check).

    slot and part are as set_part takes them; an empty part checks removing the slot's part.
    """
    item, _location = _find_item(params)
    mode = _mode(params)
    slot = next((s for s in _slots(item, mode) if s.name == params.get("slot")), None)
    if slot is None:
        raise ApiError(400, "unknown slot for this item")

    part_id = params.get("part") or None
    ref = None
    if part_id is not None:
        ref = next((r for r in slot.options if r.id == part_id), None)
        if ref is None:
            raise ApiError(400, "that part is not available in this mode")
    result: dict[str, Any] = {"slot": slot.name, "part": part_id, "status": None, "status_reason": None, "dlc": None}

    part: unreal.UObject | None = None
    if ref is not None:
        info = _status_of(item, slot, ref)
        result.update(status=info["status"], status_reason=info["status_reason"], dlc=info["dlc"])
        try:
            part = find_object(ref.class_name, ref.id)
        except Exception:  # noqa: BLE001
            part = None
        if part is None:
            return {
                **result,
                "status": STATUS_UNSUPPORTED,
                "status_reason": "Not loaded right now - try rescanning parts",
                "dlc": None,
                "save_check": _unchecked("the part is not loaded right now - try rescanning parts"),
            }

    try:
        hypothetical = _hypothetical_def(item, slot.field, part)
    except Exception as ex:  # noqa: BLE001
        return {**result, "save_check": _unchecked(f"could not make a copy to test ({ex})")}
    return {**result, "save_check": save_check(item, hypothetical)}


def _item_dlc_view(item: unreal.UObject) -> dict[str, Any] | None:
    try:
        return dlc_dict(item_dlc(item))
    except Exception:  # noqa: BLE001
        return None


def _balance_chain_view(item: unreal.UObject) -> list[dict[str, Any]]:
    try:
        return balance_chain_view(item.DefinitionData.BalanceDefinition)
    except Exception:  # noqa: BLE001
        return []


def _current_pc() -> Any:
    try:
        return get_pc(possibly_loading=True)
    except Exception:  # noqa: BLE001
        return None


def _result_view(called: bool, value: Any) -> dict[str, Any]:
    return {"called": called, "result": _short(value) if called else None}


def debug_validation(params: Params) -> dict[str, Any]:
    """Writes what this item's game calls, DLC data and part statuses look like to DEBUG_VALIDATION_FILE.

    For a person to read after trying it in game. UNVERIFIED: see UNVERIFIED_NOTE.
    """
    item, location = _find_item(params)
    mode = _mode(params)
    functions = {name: _has_function(item, name) for name in VALIDATION_FUNCTIONS}
    pc = _current_pc()

    counts: dict[str, dict[str, int]] = {}
    for slot in _slots(item, mode):
        tally: dict[str, int] = {"options": len(slot.options)}
        for info in (slot.statuses or {}).values():
            key = info.get("status") or "unknown"
            tally[key] = tally.get(key, 0) + 1
        counts[slot.name] = tally

    payload = {
        "note": UNVERIFIED_NOTE,
        "item": {
            "name": item.GetHumanReadableName(),
            "class": item.Class.Name,
            "id": _item_id(item),
            "location": location,
        },
        "mode": mode,
        "functions": functions,
        "save_check": save_check(item),
        "item_dlc": _item_dlc_view(item),
        "balance_chain": _balance_chain_view(item),
        "IsDLCRequirementMet": _result_view(*_probe(item, "IsDLCRequirementMet", pc)),
        "GetDLCRestrictedMessage": _result_view(*_probe(item, "GetDLCRestrictedMessage", pc, False)),
        "status_counts": counts,
    }
    payload["probe_log"] = {name: dict(entry) for name, entry in _probe_log.items()}
    try:
        DEBUG_VALIDATION_FILE.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except OSError as ex:
        raise ApiError(500, f"could not write the validation dump ({ex})") from None
    logging.info(f"RTSE: validation dump written to {DEBUG_VALIDATION_FILE}")
    return {"saved_to": str(DEBUG_VALIDATION_FILE), "functions": functions}


def item_detail(params: Params) -> dict[str, Any]:
    item, location = _find_item(params)
    return _detail(item, location, _mode(params))



def _detail(item: unreal.UObject, location: str, mode: str) -> dict[str, Any]:
    data = item.DefinitionData
    gear = _gear_limits()
    return {
        "hide_bones": _hidden_bones(item),
        "id": _item_id(item),
        "location": location,
        "mode": mode,
        "class": item.Class.Name,
        "name": item.GetHumanReadableName(),
        "level": item.ExpLevel,
        "overpower": limits.overpower_of(item.ExpLevel, gear["character_max"]),
        "limits": gear,
        "grade": data.ManufacturerGradeIndex,
        "game_stage": data.GameStage,
        "stats": stats.read_stats(item),
        "override_scope": stats.override_scope(item),
        "dlc": _item_dlc_view(item),
        "slots": [_slot_view(data, slot) for slot in _slots(item, mode)],
    }


def _slot_view(data: Any, slot: Slot) -> dict[str, Any]:
    """One slot for the picker. Each option (and the current part, when it is one of them) gains its part status."""
    statuses = slot.statuses or {}
    current = _part_ref(getattr(data, slot.field))
    if current is not None and current["id"] in statuses:
        current = {**current, **statuses[current["id"]]}
    return {
        "slot": slot.name,
        "current": current,
        "options": [{**_ref_dict(ref), **statuses.get(ref.id, {})} for ref in slot.options],
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
    seen: dict[str, Any] = {}

    def write() -> None:
        setattr(item.DefinitionData, slot.field, part)
        seen["after_write"] = _part_ref(getattr(item.DefinitionData, slot.field))

    def verify() -> str | None:
        current = _path_or_none(getattr(item.DefinitionData, slot.field))
        if current == part_id:
            return None
        return f"the {slot.name} slot holds {current!r}, not {part_id!r}"

    before_view = _capture_before(item)
    old_identity = stats.identity(item)
    _edit_with_rollback(
        item,
        equipped=location == EQUIPPED,
        what=f"{slot.name} ({location})",
        write=write,
        verify=verify,
    )
    # The game rebuilt the gun: its overrides follow it to the new identity, then get applied to it again
    warnings = _warnings(stats.move_overrides(old_identity, stats.identity(item)))
    stats.reapply(item, reason="changing a part")
    after_init = _part_ref(getattr(item.DefinitionData, slot.field))
    applied = {
        "what": f"{slot.name} ({location})",
        "requested": part_id,
        "before": before and before["name"],
        "after": {
            "after_write": seen["after_write"] and seen["after_write"]["name"],
            "after_rebuild": after_init and after_init["name"],
        },
    }
    applied["kept"] = _kept_view(item, before_view, edited=slot.name)
    logging.info(f"RTSE: set_part {applied}")
    journal.record("gear.part", target=params["id"], before=before, after=after_init)
    _note_id_change(params["id"], item)
    return {**_detail(item, location, _mode(params)), "applied": applied, "warnings": warnings}


def set_level(params: Params) -> dict[str, Any]:
    item, location = _find_item(params)
    try:
        level = int(params["level"])
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, "'level' must be an integer") from None
    gear = _gear_limits()
    if not MIN_LEVEL <= level <= gear["max"]:
        raise ApiError(400, _level_range_message(gear))

    data = item.DefinitionData
    before = {"exp_level": item.ExpLevel, "grade": data.ManufacturerGradeIndex, "game_stage": data.GameStage}

    def write() -> None:
        # The level the game shows and saves is built from the manufacturer grade; game stage is a
        # separate stored value (both are written to the save). Keep them in step.
        item.DefinitionData.GameStage = level
        item.DefinitionData.ManufacturerGradeIndex = level

    def fix_exp_level() -> None:
        if item.ExpLevel != level:
            item.ExpLevel = level

    def verify() -> str | None:
        if item.ExpLevel != level:
            return f"ExpLevel is {item.ExpLevel}, not {level}"
        if item.DefinitionData.GameStage != level:
            return f"GameStage is {item.DefinitionData.GameStage}, not {level}"
        if item.DefinitionData.ManufacturerGradeIndex != level:
            return f"ManufacturerGradeIndex is {item.DefinitionData.ManufacturerGradeIndex}, not {level}"
        return None

    before_view = _capture_before(item)
    old_identity = stats.identity(item)
    _edit_with_rollback(
        item,
        equipped=location == EQUIPPED,
        what=f"level ({location})",
        write=write,
        verify=verify,
        after_rebuild=fix_exp_level,
    )
    warnings = _warnings(stats.move_overrides(old_identity, stats.identity(item)))
    stats.reapply(item, reason="changing the level")

    data = item.DefinitionData
    applied = {
        "what": f"level ({location})",
        "requested": level,
        "before": before,
        "after": {"exp_level": item.ExpLevel, "grade": data.ManufacturerGradeIndex, "game_stage": data.GameStage},
    }
    applied["kept"] = _kept_view(item, before_view, edited=None)
    logging.info(f"RTSE: set_level {applied}")
    journal.record("gear.level", target=params["id"], before=before, after=applied["after"])
    _note_id_change(params["id"], item)
    return {**_detail(item, location, _mode(params)), "applied": applied, "warnings": warnings}


def set_stat(params: Params) -> dict[str, Any]:
    item, location = _find_item(params)
    stat = params.get("stat")
    if stat not in stats.STAT_NAMES:
        raise ApiError(400, "unknown stat")
    if not _is_weapon(item):
        raise ApiError(400, "only weapons have editable stats")

    raw = params.get("value")
    old_identity = stats.identity(item)
    try:
        value = None if raw is None else float(raw)
        applied = stats.set_override(item, stat, value)
    except stats.StatWriteError as ex:
        raise ApiError(409 if ex.restored else 500, str(ex)) from None
    except (TypeError, ValueError) as ex:
        raise ApiError(400, str(ex)) from None
    warnings = _warnings(applied.pop("warning", None))

    if value is None:
        # Removing an override: rebuild the weapon so the game restores the real value. The rebuild gives
        # the gun a new UniqueId, so its own overrides move with it.
        _reinitialize(item, equipped=location == EQUIPPED)
        warnings += _warnings(stats.move_overrides(old_identity, stats.identity(item)))
        stats.reapply(item, reason="clearing an override")
        applied["after"] = {
            "value": getattr(item, stat),
            "base": getattr(item, f"{stat}BaseValue"),
        }
    logging.info(f"RTSE: set_stat {applied}")
    journal.record("gear.stat", target=params["id"], before=applied["before"], after=applied["after"])
    _note_id_change(params["id"], item)
    return {**_detail(item, location, _mode(params)), "applied": {"what": f"{stat} ({location})", **applied},
            "warnings": warnings}


def _part_names(item: unreal.UObject) -> dict[str, str | None]:
    """Each slot's part name, None where the slot is empty."""
    weapon = _is_weapon(item)
    suffix = "PartDefinition" if weapon else "ItemPartDefinition"
    data = item.DefinitionData
    names: dict[str, str | None] = {}
    for slot in WEAPON_SLOTS if weapon else ITEM_SLOTS:
        part = getattr(data, f"{slot}{suffix}", None)
        names[slot] = None if part is None else str(part.Name)
    return names


def _capture_before(item: unreal.UObject) -> dict[str, Any]:
    """The item's name, part names and stats before an edit, so the reply can say what the game kept. Never raises."""
    try:
        return {"name": item.GetHumanReadableName(), "parts": _part_names(item), "stats": stats.read_stats(item)}
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: could not record the item before the edit: {ex!r}")
        return {"error": f"could not read the item before the edit ({ex!r})"}


def _kept_view(item: unreal.UObject, before: dict[str, Any], *, edited: str | None) -> dict[str, Any]:
    """What the game kept of an edit: the name, every slot's part, the stats that changed and a save check.

    Never raises: a failure comes back as {"error": ...} and the edit itself is unaffected.
    """
    if "error" in before:
        return {"error": before["error"]}
    try:
        now_parts = _part_names(item)
        parts = [
            {
                "slot": slot,
                "before": before["parts"].get(slot),
                "after": now,
                "changed": before["parts"].get(slot) != now,
                "edited": slot == edited,
            }
            for slot, now in now_parts.items()
        ]
        now_stats = {s["name"]: s for s in stats.read_stats(item)}
        changed = [
            {"name": s["name"], "label": s["label"], "before": s["value"], "after": now_stats[s["name"]]["value"]}
            for s in before["stats"]
            if s["name"] in now_stats and now_stats[s["name"]]["value"] != s["value"]
        ]
        return {
            "name": {"before": before["name"], "after": item.GetHumanReadableName()},
            "parts": parts,
            "stats": changed,
            "save_check": save_check(item),
        }
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: could not describe what the game kept: {ex!r}")
        return {"error": f"could not describe what the game kept ({ex!r})"}


def _warnings(message: str | None) -> list[str]:
    """A save warning from rtse/stats.py as the list the JSON carries (empty when there is none)."""
    return [message] if message else []


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = ApiError

GET_ROUTES: dict[str, Route] = {
    "/api/item": item_detail,
    "/api/item/check_part": check_part,
    "/api/debug/validation": debug_validation,
}
POST_ROUTES: dict[str, Route] = {
    "/api/item/set_part": set_part,
    "/api/item/set_level": set_level,
    "/api/item/set_stat": set_stat,
}
