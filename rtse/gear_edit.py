"""Gear edits: change an item's part, level or stat, rebuilding it from its definition data.

Every edit is written, the item is rebuilt, the game is asked whether it took the change, and the old fields
are put back if it did not.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mods_base import get_pc
from unrealsdk import find_object, logging, unreal

from . import limits, stats
from .gear_inventory import EQUIPPED, _find_item, _hidden_bones, _item_id, _note_id_change
from .gear_parts import ITEM_SLOTS, WEAPON_SLOTS, _is_weapon, _mode, _part_ref, _ref_dict, _slots
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
    seen: dict[str, Any] = {}

    def write() -> None:
        setattr(item.DefinitionData, slot.field, part)
        seen["after_write"] = _part_ref(getattr(item.DefinitionData, slot.field))

    def verify() -> str | None:
        current = _path_or_none(getattr(item.DefinitionData, slot.field))
        if current == part_id:
            return None
        return f"the {slot.name} slot holds {current!r}, not {part_id!r}"

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
    logging.info(f"RTSE: set_part {applied}")
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
    logging.info(f"RTSE: set_level {applied}")
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
    _note_id_change(params["id"], item)
    return {**_detail(item, location, _mode(params)), "applied": {"what": f"{stat} ({location})", **applied},
            "warnings": warnings}


def _warnings(message: str | None) -> list[str]:
    """A save warning from rtse/stats.py as the list the JSON carries (empty when there is none)."""
    return [message] if message else []


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = ApiError

GET_ROUTES: dict[str, Route] = {"/api/item": item_detail}
POST_ROUTES: dict[str, Route] = {
    "/api/item/set_part": set_part,
    "/api/item/set_level": set_level,
    "/api/item/set_stat": set_stat,
}
