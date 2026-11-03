"""History: the timeline of every write RTSE made (rtse/journal.py) and undo, plus the save checkpoints' routes.

Undo is an inverse operation: it replays the module's own POST route with the recorded `before` value, through the same
game-thread path, so an undo gets the same validation, rollback and read-back as the original edit. An undo runs inside
its own journal transaction (the "/api/history/undo" POST), so it is journaled like any other write and can itself be
undone (which redoes the edit).

Before anything is written, the loaded character must be the one the edit was made to (every entry records the
save file id, class and name; an undo is refused when either side can't be read), and every record's target is read
again and compared with the WHOLE state the edit left (level and XP together; every skill rank for a tree reset). If
anything differs, the undo is refused with a 409 instead of overwriting the newer value.

Undoable: gear (part, level, stat override), character fields, ammo, SDU levels and capacities, skill ranks and a skill
tree reset. Listed but NOT undoable (see NOT_UNDOABLE for the reasons shown in the UI): world edits (missions,
objectives, challenges, fast travel, playthrough), inspector writes, and checkpoint actions. Entries left by an earlier
game session are shown but never undone: the objects they name may be gone.

Nothing here has been run in a live game yet: undo calls the same routes as the pages do, so it is exactly as verified as
those routes are.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from mods_base import get_pc
from unrealsdk import logging

from . import ammo, character, checkpoints, journal, sdu, skills
from .routing import ApiError, Params, Route

DEBUG_HISTORY_FILE = Path(__file__).with_name("debug_history.json")
UNDO_ROUTE = "/api/history/undo"

WORLD_REASON = (
    "Missions, challenges, fast travel and playthrough changes ripple into other game state (objective sets, rewards, "
    "map markers, the mission log), so putting one value back isn't a safe undo. Restore a save backup instead."
)
NOT_UNDOABLE = {
    "world.": WORLD_REASON,
    "inspect.": "Inspector writes are raw field writes with no game-side checks; set the old value by hand on the "
    "Inspector page, or restore a save backup.",
    "checkpoint.": "This is a save-backup action, not a change to the game.",
}
EARLIER_REASON = "From an earlier game session: the objects it changed may not exist any more, so it can't be undone."


class HistoryError(ApiError):
    """A history route error (subclass of ApiError, so the registry passes it straight through)."""


class Refused(Exception):
    """An undo step can't be planned (a record lacks what the undo needs)."""


Steps = list[tuple[str, Params]]


class Adapter(NamedTuple):
    """How to undo one kind of record.

    steps(rec):   the POST routes (path, params) that put rec["before"] back, in order.
    current(rec): what the game holds now, in the same shape as rec["after"].
    what(rec):    a short name for messages ("Cash", "SMG ammo", ...).
    restore(rec, value): steps that put the target back to a recorded state `value` (used when an undo fails and must
                  put back what it changed); by default steps() with value as the "before".
    """

    steps: Callable[[dict[str, Any]], Steps]
    current: Callable[[dict[str, Any]], Any]
    what: Callable[[dict[str, Any]], str]
    restore: Callable[[dict[str, Any], Any], Steps] | None = None  # steps to put a recorded state back (default: steps)


# --- Comparing values -----------------------------------------------------------------------------------------------


def _same(a: Any, b: Any) -> bool:
    """Equal as recorded values: numbers within a small tolerance, dicts/lists element by element."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-5, abs_tol=1e-4)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _short(value: Any) -> str:
    if isinstance(value, dict):
        if "name" in value:
            return str(value["name"])
        return ", ".join(f"{k} {_short(v)}" for k, v in value.items())
    if isinstance(value, float):
        return f"{value:g}"
    return "nothing" if value is None else str(value)


def _params(rec: dict[str, Any]) -> dict[str, Any]:
    params = rec.get("params")
    return params if isinstance(params, dict) else {}


def _need(value: Any, what: str) -> Any:
    if value is None:
        raise Refused(f"the edit didn't record {what}")
    return value


# --- Adapters, one per kind of record --------------------------------------------------------------------------------


def _character_steps(rec: dict[str, Any]) -> Steps:
    field = rec["kind"].split(".", 1)[1]
    return [("/api/character/set", {"field": field, "value": _need(rec["before"], "the old value")})]


def _character_now(rec: dict[str, Any]) -> Any:
    return character._brief(character.read(), rec["kind"].split(".", 1)[1])  # noqa: SLF001


def _progress(value: Any, what: str) -> dict[str, Any]:
    """Level and XP move together (setting one can change the other), so their records hold both."""
    if not isinstance(value, dict) or value.get("level") is None or value.get("xp") is None:
        raise Refused(f"this edit was recorded without both the level and the XP {what}")
    return {"level": value["level"], "xp": value["xp"]}


def _progress_steps(rec: dict[str, Any]) -> Steps:
    _progress(rec["after"], "it left")
    before = _progress(rec["before"], "from before")
    # Level first (it moves XP to the start of that level), then the exact XP
    return [("/api/character/set", {"field": "level", "value": before["level"]}),
            ("/api/character/set", {"field": "xp", "value": before["xp"]})]


def _progress_now(_rec: dict[str, Any]) -> Any:
    state = character.read()
    return {"level": state.get("level"), "xp": state.get("xp")}


def _ammo_id(rec: dict[str, Any]) -> int:
    try:
        return int(_params(rec)["id"])
    except (KeyError, TypeError, ValueError):
        raise Refused("the edit didn't record which ammo pool it changed") from None


def _ammo_steps(rec: dict[str, Any]) -> Steps:
    return [("/api/ammo/set", {"id": _ammo_id(rec), "value": _need(rec["before"], "the old amount")})]


def _ammo_now(rec: dict[str, Any]) -> Any:
    index = _ammo_id(rec)
    pool = next((p for p in ammo.read()["pools"] if p["id"] == index), None)
    if pool is None or pool["label"] != rec["target"]:
        raise HistoryError(409, f"the {rec['target']} ammo pool isn't where it was; undo refused")
    return pool["value"]


def _sdu_kind(rec: dict[str, Any]) -> str:
    return str(rec["target"])


def _sdu_steps(rec: dict[str, Any]) -> Steps:
    before = rec["before"] if isinstance(rec["before"], dict) else {}
    if rec["kind"] == "sdu.ammo_max":
        return [("/api/sdu/ammo_max", {"kind": _sdu_kind(rec), "max": _need(before.get("capacity"), "the old capacity")})]
    return [("/api/sdu/set", {"kind": _sdu_kind(rec), "level": _need(before.get("level"), "the old upgrade level")})]


def _sdu_now(rec: dict[str, Any]) -> Any:
    return sdu._brief(sdu._kind_state(_sdu_kind(rec)))  # noqa: SLF001


def _skill(index: Any) -> dict[str, Any] | None:
    return next((s for s in skills.read()["skills"] if s["id"] == index), None)


def _skill_steps(rec: dict[str, Any]) -> Steps:
    index = _need(_params(rec).get("id"), "which skill it changed")
    # over_max: the old rank is one the game held, so it's allowed even when it is past the skill's normal maximum
    return [("/api/skills/set", {"id": index, "level": _need(rec["before"], "the old rank"), "over_max": True})]


def _skill_now(rec: dict[str, Any]) -> Any:
    found = _skill(_params(rec).get("id"))
    if found is None or found["name"] != rec["target"]:
        raise HistoryError(409, f"the skill {rec['target']} isn't where it was; undo refused")
    return found["level"]


def _tree(value: Any, what: str) -> dict[str, Any]:
    """A reset's state as compared: unspent points and EVERY skill's rank (not just the totals)."""
    if not isinstance(value, dict) or not isinstance(value.get("levels"), list) or value.get("skill_points") is None:
        raise Refused(f"this reset was recorded without every skill's rank {what}")
    return {"skill_points": value["skill_points"],
            "ranks": {str(s["id"]): s.get("level") for s in value["levels"] if isinstance(s, dict) and "id" in s}}


def _reset_now(_rec: dict[str, Any]) -> Any:
    state = skills.read()
    # the same skills skills.reset records: the ones the game doesn't grant by itself
    levels = [s for s in state.get("skills", []) if not (s.get("action") or s.get("hidden"))]
    return _tree({"skill_points": state.get("skill_points"), "levels": levels}, "now")


def _gear_id(rec: dict[str, Any]) -> str:
    return _need(_params(rec).get("id"), "which item it changed")


def _gear_item(rec: dict[str, Any]) -> Any:
    from .gear_inventory import _find_item  # noqa: PLC0415 - gear modules import heavy game state; load on use

    return _find_item({"id": _gear_id(rec)})[0]


def _part_field(item: Any, slot: str) -> str:
    from .gear_parts import ITEM_SLOTS, WEAPON_SLOTS, _is_weapon  # noqa: PLC0415

    weapon = _is_weapon(item)
    if slot not in (WEAPON_SLOTS if weapon else ITEM_SLOTS):
        raise HistoryError(409, f"the item has no {slot} slot any more; undo refused")
    return f"{slot}{'PartDefinition' if weapon else 'ItemPartDefinition'}"


def _part_id(value: Any) -> Any:
    return value.get("id") if isinstance(value, dict) else value


def _part_steps(rec: dict[str, Any]) -> Steps:
    slot = _need(_params(rec).get("slot"), "which slot it changed")
    # "everything" mode: the old part may have come from any mode, and set_part only accepts parts listed in its mode
    return [("/api/item/set_part", {"id": _gear_id(rec), "slot": slot, "part": _part_id(rec["before"]), "mode": "everything"})]


def _part_now(rec: dict[str, Any]) -> Any:
    item = _gear_item(rec)
    part = getattr(item.DefinitionData, _part_field(item, _params(rec).get("slot", "")))
    return {"id": None if part is None else part._path_name()}  # noqa: SLF001


def _level_steps(rec: dict[str, Any]) -> Steps:
    before = rec["before"] if isinstance(rec["before"], dict) else {}
    # The level the game shows and saves is built from the manufacturer grade (see gear_edit.set_level)
    return [("/api/item/set_level", {"id": _gear_id(rec), "level": _need(before.get("grade"), "the old level")})]


def _level_now(rec: dict[str, Any]) -> Any:
    item = _gear_item(rec)
    data = item.DefinitionData
    return {"exp_level": item.ExpLevel, "grade": data.ManufacturerGradeIndex, "game_stage": data.GameStage}


def _stat_steps(rec: dict[str, Any]) -> Steps:
    before = rec["before"] if isinstance(rec["before"], dict) else {}
    if "override" not in before:
        raise Refused("this stat edit was recorded without the old override")
    # override None: there was no override before, so the undo clears it and the game's own value comes back
    return [("/api/item/set_stat", {"id": _gear_id(rec), "stat": _params(rec).get("stat"), "value": before["override"]})]


def _stat_now(rec: dict[str, Any]) -> Any:
    item = _gear_item(rec)
    stat = _params(rec).get("stat")
    return {"value": getattr(item, stat), "base": getattr(item, f"{stat}BaseValue")}


def _named(prefix: str) -> Callable[[dict[str, Any]], str]:
    return lambda rec: f"{prefix}{rec['target']}"


def _tree_steps(target: dict[str, Any], skip: dict[str, Any] | None = None) -> Steps:
    """Sets every skill in target["levels"] to its rank (except where `skip` says it already is), then the points."""
    steps: Steps = [
        ("/api/skills/set", {"id": s["id"], "level": s.get("level"), "over_max": True})
        for s in target["levels"]
        if skip is None or s.get("level") != skip.get(str(s["id"]))
    ]
    steps.append(("/api/character/set", {"field": "skill_points", "value": target["skill_points"]}))
    return steps


def _reset_steps(rec: dict[str, Any]) -> Steps:
    after = _tree(rec["after"], "it left")
    _tree(rec["before"], "from before")
    return _tree_steps(rec["before"], after["ranks"])


def _reset_restore(_rec: dict[str, Any], value: Any) -> Steps:
    _tree(value, "to put back")
    return _tree_steps(value)


ADAPTERS: dict[str, Adapter] = {
    "character.level": Adapter(_progress_steps, _progress_now, lambda _rec: "level and XP"),
    "character.xp": Adapter(_progress_steps, _progress_now, lambda _rec: "level and XP"),
    "character.": Adapter(_character_steps, _character_now, lambda rec: rec["kind"].split(".", 1)[1].replace("_", " ")),
    "ammo.value": Adapter(_ammo_steps, _ammo_now, lambda rec: f"{rec['target']} ammo"),
    "sdu.": Adapter(_sdu_steps, _sdu_now, _named("upgrade ")),
    "skills.level": Adapter(_skill_steps, _skill_now, _named("skill ")),
    "skills.reset": Adapter(_reset_steps, _reset_now, lambda _rec: "the skill tree", _reset_restore),
    "gear.part": Adapter(_part_steps, _part_now, lambda rec: f"the {_params(rec).get('slot', 'part')} part"),
    "gear.level": Adapter(_level_steps, _level_now, lambda _rec: "the item level"),
    "gear.stat": Adapter(_stat_steps, _stat_now, lambda rec: f"the {_params(rec).get('stat', 'stat')} stat"),
}


def _adapter(kind: str) -> Adapter | None:
    exact = ADAPTERS.get(kind)
    if exact is not None:
        return exact
    return next((a for prefix, a in ADAPTERS.items() if prefix.endswith(".") and kind.startswith(prefix)), None)


def _restore_steps(adapter: Adapter, rec: dict[str, Any], value: Any) -> Steps:
    """The steps that put this record's target back to `value` (a state it recorded)."""
    if adapter.restore is not None:
        return adapter.restore(rec, value)
    return adapter.steps({**rec, "before": value})


def _why_not(rec: dict[str, Any]) -> str | None:
    """Why this record can't be undone (None when it can). Never raises."""
    kind = str(rec.get("kind", ""))
    for prefix, reason in NOT_UNDOABLE.items():
        if kind.startswith(prefix):
            return reason
    adapter = _adapter(kind)
    if adapter is None:
        return f"RTSE has no undo for {kind} changes."
    try:
        adapter.steps(rec)
    except Refused as ex:
        return f"Can't undo: {ex}."
    except Exception as ex:  # noqa: BLE001
        return f"Can't undo: {ex}."
    return None


def _comparable(rec: dict[str, Any], value: Any) -> Any:
    """The part of a recorded value an undo compares: the whole state the undo would overwrite."""
    kind = rec["kind"]
    if kind == "gear.part":
        return _part_id(value)
    if kind in ("character.level", "character.xp"):
        return _progress(value, "")
    if kind == "skills.reset":
        return value if isinstance(value, dict) and "ranks" in value else _tree(value, "")
    return value


# --- Which character an entry belongs to ----------------------------------------------------------------------------


def current_identity() -> dict[str, Any] | None:
    """The loaded character as {save_id, class, name}: the player controller's SaveGameFileId and PlayerClass, and its
    PRI's PlayerName. None unless all three can be read (UNVERIFIED in a live game, like the rest of these reads)."""
    try:
        pc = get_pc(possibly_loading=True)
        if pc is None:
            return None
        save_id = getattr(pc, "SaveGameFileId", None)
        player_class = getattr(pc, "PlayerClass", None)
        pri = getattr(pc, "PlayerReplicationInfo", None)
        name = getattr(pri, "PlayerName", None) if pri is not None else None
    except Exception:  # noqa: BLE001
        return None
    if isinstance(player_class, str):
        class_id = player_class
    else:
        path_name = getattr(player_class, "_path_name", None)
        try:
            class_id = path_name() if callable(path_name) else None
        except Exception:  # noqa: BLE001
            class_id = None
    if not isinstance(save_id, int) or isinstance(save_id, bool) or not class_id or not isinstance(name, str) or not name:
        return None
    return {"save_id": save_id, "class": str(class_id), "name": name}


# Every journal entry records the character it was made to (journal calls this when a write starts). A lambda, so
# tests can replace current_identity.
journal.identity_provider = lambda: current_identity()  # noqa: PLW0108


def _who(identity: Any) -> str:
    if not isinstance(identity, dict):
        return "an unknown character"
    return f"{identity.get('name')} ({str(identity.get('class', '')).rpartition('.')[2] or '?'}, save {identity.get('save_id')})"


def _identity_problem(entry: dict[str, Any], now: Any) -> str | None:
    """Why this entry can't be undone on the character loaded now (None when it's the same one)."""
    made = entry.get("identity")
    if not isinstance(made, dict):
        return "RTSE couldn't tell which character this change was made to, so it won't undo it on whoever is loaded now."
    if not isinstance(now, dict):
        return "RTSE can't tell which character is loaded right now, so it won't undo this change."
    if made != now:
        return f"This change was made to {_who(made)}, but {_who(now)} is loaded now."
    return None


# --- The timeline as the page sees it --------------------------------------------------------------------------------

REQUESTED_KEYS = ("value", "level", "max", "part")


def _requested(rec: dict[str, Any]) -> Any:
    params = _params(rec)
    if rec["kind"] == "skills.reset":
        return None
    return next((params[key] for key in REQUESTED_KEYS if key in params), None)


def _view_record(rec: dict[str, Any], earlier: bool) -> dict[str, Any]:
    why = EARLIER_REASON if earlier else _why_not(rec)
    return {**rec, "requested": _requested(rec), "undoable": why is None, "why_not": why}


def _view(entry: dict[str, Any], now: Any = None) -> dict[str, Any]:
    earlier = entry.get("session") == "earlier"
    records = [_view_record(rec, earlier) for rec in entry.get("records", [])]
    reason = None
    if entry.get("partial"):
        reason = PARTIAL_REASON
        records = [{**r, "undoable": False, "why_not": PARTIAL_REASON} for r in records]
    elif entry.get("undone_by") is not None:
        reason = f"Already undone (by #{entry['undone_by']})."
    elif earlier:
        reason = EARLIER_REASON
    else:
        reason = next((r["why_not"] for r in records if r["why_not"]), None) or _identity_problem(entry, now)
    return {**entry, "records": records, "undo": {"possible": reason is None, "reason": reason}}


def timeline(_params: Params | None = None, *, pending: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET /api/history. `pending` is an entry about to be committed (an undo's own response includes it)."""
    found = journal.entries()
    if pending is not None:
        found = [dict(e) for e in found] + [dict(pending)]
        current = journal.links(found)
        for e in found:
            e["undone_by"] = current[e["id"]]
    now = current_identity()
    return {"entries": [_view(e, now) for e in reversed(found)], "character": now, **journal.status()}


# --- Undo -------------------------------------------------------------------------------------------------------


def _now_of(adapter: Adapter, rec: dict[str, Any]) -> Any:
    """What the game holds now for this record, comparable with rec["after"]. Raises HistoryError(409) if unreadable."""
    try:
        return _comparable(rec, adapter.current(rec))
    except HistoryError:
        raise
    except (ApiError, Refused) as ex:
        raise HistoryError(409, f"can't read {adapter.what(rec)} now ({ex}); undo refused") from None


def undo(params: Params) -> dict[str, Any]:
    """POST /api/history/undo {"id": n}: puts back what entry n changed, after checking it is the same character and
    that nothing changed the values since."""
    entry = journal.get(params.get("id"))
    if entry is None:
        raise HistoryError(404, "that history entry doesn't exist (it may have dropped off the end of the timeline)")
    now_identity = current_identity()
    view = _view(entry, now_identity)
    if entry.get("undone_by") is not None:
        raise HistoryError(409, view["undo"]["reason"])
    problem = None if entry.get("partial") or entry.get("session") == "earlier" else _identity_problem(entry, now_identity)
    if not view["undo"]["possible"] and view["undo"]["reason"] != problem:
        raise HistoryError(400, view["undo"]["reason"])  # this kind of entry can't be undone at all
    if problem:
        raise HistoryError(409, f"{problem} Undo refused.")
    tx = journal.current()
    if tx is None or tx.label != UNDO_ROUTE:
        raise HistoryError(500, "undo must run as its own write (POST /api/history/undo)")

    records = list(reversed(entry["records"]))  # undo the last change first
    plans: list[tuple[dict[str, Any], Adapter, Steps]] = []
    for rec in records:
        adapter = _adapter(rec["kind"])
        assert adapter is not None  # noqa: S101 - _view said every record has one
        now = _now_of(adapter, rec)
        if not _same(now, _comparable(rec, rec["after"])):
            raise HistoryError(
                409,
                f"{adapter.what(rec).capitalize()} is now {_short(now)}, but this edit left "
                f"{_short(_comparable(rec, rec['after']))}; undo refused so a newer change isn't overwritten",
            )
        plans.append((rec, adapter, adapter.steps(rec)))

    from . import api  # noqa: PLC0415 - api imports this module

    tx.meta["undo_of"] = entry["id"]
    start = len(tx.records)
    done: list[dict[str, Any]] = []  # the steps that succeeded
    for index, (rec, _adapter_, steps) in enumerate(plans):
        for route, step_params in steps:
            write = api.POST_ROUTES.get(route)
            try:
                if write is None:
                    raise HistoryError(500, f"the route {route} is gone")
                result = write(step_params)
            except Exception as ex:  # noqa: BLE001 - every failure is unwound the same way
                _unwind(tx, entry, plans[: index + 1], start, len(done), ex)
            done.append({"route": route, "params": step_params, "kind": rec["kind"], "applied": result.get("applied")})
    logging.info(f"RTSE: undid history entry {entry['id']} in {len(done)} step(s)")

    pending = journal.preview(tx)
    return {
        "applied": {"undid": entry["id"], "entry": pending and _view(pending, now_identity), "steps": done},
        **timeline(pending=pending),
    }


PARTIAL_REASON = "This undo stopped halfway and couldn't be fully put back. Check these values in game by hand."


def _unwind(tx: journal.Transaction, entry: dict[str, Any], touched: list[tuple[dict[str, Any], Adapter, Steps]],
            start: int, steps_done: int, error: BaseException) -> None:
    """A step of an undo failed: puts back what the attempt changed, checks that it worked, then raises.

    1. Every record the attempt made (finished steps, and anything the failed step recorded before it raised) is put
       back, newest first.
    2. Each record the undo touched, including the one whose step failed, is read back and compared with the state the
       undo started from (its `after`). A step can change the game and then raise without recording anything, so this
       read-back is what catches that; a mismatch is put back to `after` directly.
    3. If everything reads back as it started, nothing changed. Otherwise the game really is half-changed, so an
       explicit "partial" entry lists every change made during the attempt and what still differs (journal.note_partial)
       instead of letting the abort hide it. The undo's own transaction is discarded either way (all or nothing).
    """
    from . import api  # noqa: PLC0415

    status = getattr(error, "status", 500)
    message = str(error) or repr(error)
    problems: list[str] = []

    def run(steps: Steps) -> None:
        for route, params in steps:
            api.POST_ROUTES[route](params)

    for rec in reversed(list(tx.records[start:])):
        adapter = _adapter(rec["kind"])
        try:
            if adapter is None:
                raise Refused(f"RTSE has no undo for {rec['kind']}")
            run(adapter.steps(rec))
        except Exception as ex:  # noqa: BLE001
            problems.append(f"putting back {adapter.what(rec) if adapter else rec['kind']}: {ex}")

    still: list[str] = []
    for rec, adapter, _steps in reversed(touched):
        started = _comparable(rec, rec["after"])
        try:
            if _same(_now_of(adapter, rec), started):
                continue
        except Exception as ex:  # noqa: BLE001
            still.append(f"{adapter.what(rec)} couldn't be read back ({ex})")
            continue
        failed = ""
        try:
            run(_restore_steps(adapter, rec, rec["after"]))
        except Exception as ex:  # noqa: BLE001 - a put-back that raised may still have landed: read it back
            failed = f" (putting it back raised: {ex})"
        try:
            now = _now_of(adapter, rec)
        except Exception as ex:  # noqa: BLE001
            still.append(f"{adapter.what(rec)} couldn't be read back ({ex}){failed}")
            continue
        if not _same(now, started):
            still.append(f"{adapter.what(rec)} is {_short(now)}, not {_short(started)}{failed}")

    where = f"undo of #{entry['id']} failed at step {steps_done + 1} ({message})"
    if not still:
        logging.error(f"RTSE: {where}; everything it changed was put back")
        raise HistoryError(status, f"{where}; everything it had changed was put back, so nothing changed") from None
    detail = f"{where}, and it couldn't all be put back: {'; '.join(still)}"
    if problems:
        detail += f" ({'; '.join(problems)})"
    journal.note_partial(UNDO_ROUTE, {"id": entry["id"]}, list(tx.records[start:]), detail,
                         attempted_undo_of=entry["id"], identity=tx.meta.get("identity"))
    logging.error(f"RTSE: {detail}")
    raise HistoryError(
        status,
        f"{detail}. The timeline has a 'partial' entry listing exactly what changed; check those values in game.",
    ) from None


# --- Debug dump -------------------------------------------------------------------------------------------------


def debug_dump(_params: Params) -> dict[str, Any]:
    """Saves the journal's state, the last entries, and everything checkpoints tried when finding the save."""
    data = {
        "journal": journal.status(),
        "entries": journal.entries()[-30:],
        "open_transaction": getattr(journal.current(), "label", None),
        "checkpoints": checkpoints.debug_info(),
    }
    try:
        data["checkpoint_state"] = checkpoints.state()
    except Exception as ex:  # noqa: BLE001
        data["checkpoint_state"] = f"<error {ex!r}>"
    DEBUG_HISTORY_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_HISTORY_FILE), "entries": len(data["entries"])}


# --- Routes (paths are the API; api.py collects these). The checkpoint routes come along so api.py needs one line. --

ROUTE_ERROR = ApiError  # HistoryError and checkpoints.CheckpointError are both ApiErrors

GET_ROUTES: dict[str, Route] = {
    "/api/history": timeline,
    "/api/debug/history": debug_dump,
    **checkpoints.GET_ROUTES,
}
POST_ROUTES: dict[str, Route] = {
    UNDO_ROUTE: undo,
    **checkpoints.POST_ROUTES,
}
