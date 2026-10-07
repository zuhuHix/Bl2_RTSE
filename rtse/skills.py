"""The player's skill tree: every skill of the class with its points and maximum.

NOTHING HERE HAS BEEN CONFIRMED IN A RUNNING GAME. Every class, field and function name below is a guess
from memory of the Borderlands 2 SDK, so each thing lists the places it might live (candidates), is tried
in order, and reports which one worked. If the tree can't be read the page shows "unavailable" instead of
failing. Run the skills dump from the web UI to find the real names.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from unrealsdk import logging, unreal

from . import character

DEBUG_SKILLS_FILE = Path(__file__).parent / "debug_skills.json"

TIER_UNLOCK_POINTS = 5  # a tier unlocks once the tiers below it hold 5 points each (as in the game; used only to show "locked")
MAX_GRADE_FALLBACK = 5  # used only when a skill's own maximum can't be read

# Where the tree might live: (owner, attribute), tried in order. The tree is an object with a list of skills.
TREE_SOURCES: tuple[tuple[str, str], ...] = (
    ("pc", "PlayerSkillTree"),
    ("pc", "SkillTree"),
    ("pc", "PlayerSkills"),
    ("pawn", "SkillTree"),
    ("pri", "SkillTree"),
)
# What the list of skills inside the tree might be called. The tree itself is also tried as a list.
LIST_FIELDS = ("Skills", "SkillList", "SkillItems", "Items")
# A list entry is either the skill's definition, or a record that points at one (and holds the points).
DEFINITION_FIELDS = ("Definition", "SkillDefinition", "Skill", "SkillDef")
GRADE_FIELDS = ("Grade", "CurrentGrade", "SkillGrade", "Rank", "Level")  # points put into the skill
GRADE_GETTERS = ("GetSkillGrade", "GetSkillRank", "GetSkillLevel")  # called as (definition) on the controller or tree
GRADE_SETTERS = ("SetSkillGrade", "SetSkillRank", "SetSkillLevel", "ForceSkillGrade")  # called as (definition, points)
MAX_FIELDS = ("MaxGrade", "MaxRank", "MaxLevel")
TIER_FIELDS = ("Tier", "SkillTier", "TierIndex")
BRANCH_FIELDS = ("Branch", "SkillBranch", "BranchDefinition")
NAME_FIELDS = ("SkillName", "DisplayName", "SkillDisplayName")
BRANCH_NAME_FIELDS = ("BranchName", "DisplayName", "Name")
NOTIFY_FUNCTIONS = ("NotifySkillRankChanged", "OnSkillGradeChanged", "UpdateSkillGrade")  # called as (definition, points)
RESET_FUNCTIONS = ("ResetSkillTree", "ResetSkills", "RespecSkills")  # called with no arguments


class SkillsError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _Found:
    """Where the tree was found: its owner objects, the tree itself and the list of skill entries."""

    def __init__(self, owners: dict[str, Any], tree: Any, source: str, list_field: str | None, entries: list[Any]) -> None:
        self.owners, self.tree, self.source, self.list_field, self.entries = owners, tree, source, list_field, entries


def _owners() -> dict[str, Any]:
    try:
        return character._owners()  # noqa: SLF001
    except character.CharacterError as ex:
        raise SkillsError(ex.status, str(ex)) from None


def _first(targets: list[tuple[str, Any]], fields: tuple[str, ...]) -> tuple[Any, str | None]:
    """The first of these fields that reads as something other than None, on any of these objects."""
    for owner, target in targets:
        if target is None:
            continue
        for field in fields:
            try:
                value = getattr(target, field)
            except Exception:  # noqa: BLE001, S112
                continue
            if value is not None:
                return value, f"{owner}.{field}"
    return None, None


def _entries_of(candidate: Any) -> list[Any] | None:
    """A candidate tree or list as a plain list of entries, or None if it isn't one that holds anything."""
    try:
        entries = [entry for entry in candidate if entry is not None]
    except Exception:  # noqa: BLE001
        return None
    return entries or None


def _locate(owners: dict[str, Any], tried: list[str] | None = None) -> tuple[_Found | None, str]:
    """Finds the skill list. Returns it, or None with the reason it couldn't be found."""
    reason = "this game build has no skill tree at any place RTSE knows to look"
    for owner, attribute in TREE_SOURCES:
        target = owners.get(owner)
        if target is None:
            continue
        try:
            tree = getattr(target, attribute)
        except Exception:  # noqa: BLE001
            if tried is not None:
                tried.append(f"{owner}.{attribute}: no such field")
            continue
        if tree is None:
            if tried is not None:
                tried.append(f"{owner}.{attribute}: empty")
            continue
        for list_field in (*LIST_FIELDS, None):
            try:
                entries = _entries_of(tree if list_field is None else getattr(tree, list_field))
            except Exception:  # noqa: BLE001
                continue
            if entries:
                if tried is not None:
                    tried.append(f"{owner}.{attribute}.{list_field or '(itself)'}: {len(entries)} entries")
                return _Found(owners, tree, f"{owner}.{attribute}", list_field, entries), ""
        if tried is not None:
            tried.append(f"{owner}.{attribute}: found, but no list of skills inside it")
        reason = f"found {owner}.{attribute} but couldn't find a list of skills in it - run the skills dump"
    return None, reason


def _path(obj: Any) -> str | None:
    try:
        return obj._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None


def _pretty(raw: str) -> str:
    """'Skill_FoxMasterMind' -> 'Fox Master Mind'."""
    text = re.sub(r"^(?:Skill|Branch)_", "", raw).replace("_", " ")
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text).strip() or raw


def _text(obj: Any, fields: tuple[str, ...]) -> str | None:
    value, _where = _first([("obj", obj)], fields)
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text != "None" else None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _grade(found: _Found, entry: Any, definition: Any) -> tuple[int | None, str | None]:
    """The points in one skill, and how they were read ("field:Grade" or "call:pc.GetSkillGrade")."""
    for field in GRADE_FIELDS:
        try:
            value = _to_int(getattr(entry, field))
        except Exception:  # noqa: BLE001, S112
            continue
        if value is not None:
            return value, f"field:{field}"
    for owner, target in (("pc", found.owners.get("pc")), ("tree", found.tree)):
        for getter in GRADE_GETTERS:
            try:
                value = _to_int(getattr(target, getter)(definition))
            except Exception:  # noqa: BLE001, S112
                continue
            if value is not None:
                return value, f"call:{owner}.{getter}"
    return None, None


def _describe(found: _Found, index: int, entry: Any) -> dict[str, Any]:
    definition, _where = _first([("entry", entry)], DEFINITION_FIELDS)
    if definition is None:
        definition = entry
    targets = [("definition", definition), ("entry", entry)]
    path = _path(definition)
    parts = (path or "").split(".")

    name = _text(definition, NAME_FIELDS) or _pretty(getattr(definition, "Name", None) or parts[-1] or f"Skill {index}")
    level, grade_via = _grade(found, entry, definition)
    max_raw, _ = _first(targets, MAX_FIELDS)
    tier_raw, _ = _first(targets, TIER_FIELDS)

    branch_raw, _ = _first(targets, BRANCH_FIELDS)
    branch = None
    if isinstance(branch_raw, str):
        branch = branch_raw
    elif branch_raw is not None:
        branch = _text(branch_raw, BRANCH_NAME_FIELDS) or getattr(branch_raw, "Name", None)
    if not branch and len(parts) >= 3:
        branch = parts[-2]  # GD_Assassin_Skills.Sniping.Velocity -> Sniping

    return {
        "id": index,
        "path": path,
        "name": name,
        "branch": re.sub(r"[^a-z0-9]+", "", str(branch or "skills").lower()),
        "branch_label": _pretty(str(branch)) if branch else "Skills",
        "tier": _to_int(tier_raw),
        "level": level,
        "max": _to_int(max_raw),
        "grade_via": grade_via,
    }


def _classify(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turns each skill's raw tier into a 0-based row, adds `locked`, and returns one summary per branch."""
    branches: dict[str, dict[str, Any]] = {}
    for skill in skills:
        branch = branches.setdefault(skill["branch"], {"key": skill["branch"], "label": skill["branch_label"], "points": 0, "tiers": set()})
        branch["points"] += skill["level"] or 0
        if skill["tier"] is not None:
            branch["tiers"].add(skill["tier"])
    for skill in skills:
        tiers = sorted(branches[skill["branch"]]["tiers"])
        row = tiers.index(skill["tier"]) if skill["tier"] in tiers else None
        points_below = sum(s["level"] or 0 for s in skills if s["branch"] == skill["branch"] and s["tier"] is not None and s["tier"] < (skill["tier"] or 0))
        skill["row"] = row
        skill["requires"] = None if row is None else row * TIER_UNLOCK_POINTS
        skill["locked"] = None if row is None else points_below < row * TIER_UNLOCK_POINTS
    return [{"key": b["key"], "label": b["label"], "points": b["points"], "rows": len(b["tiers"])} for b in branches.values()]


def _state(found: _Found | None, reason: str, owners: dict[str, Any]) -> dict[str, Any]:
    points, points_source = character._read(owners, "skill_points")  # noqa: SLF001
    try:
        class_key, _class_source = character._character_class(owners)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        class_key = None
    out: dict[str, Any] = {
        "available": found is not None,
        "reason": reason or None,
        "class_key": class_key,
        "skill_points": points,
        "limits": {"skill_points": character.MAX_SKILL_POINTS, "tier_unlock": TIER_UNLOCK_POINTS},
        "skills": [],
        "branches": [],
        "spent": 0,
        "sources": {"tree": found and found.source, "list": found and found.list_field, "skill_points": points_source},
    }
    if found is None:
        return out
    skills = [_describe(found, index, entry) for index, entry in enumerate(found.entries)]
    out["branches"] = _classify(skills)
    out["skills"] = skills
    out["spent"] = sum(skill["level"] or 0 for skill in skills)
    unreadable = [skill["name"] for skill in skills if skill["level"] is None]
    if unreadable:
        out["reason"] = f"couldn't read the points of {len(unreadable)} skills (e.g. {unreadable[0]}) - run the skills dump"
    return out


def read() -> dict[str, Any]:
    owners = _owners()
    found, reason = _locate(owners)
    return _state(found, reason, owners)


def _whole(raw: Any, what: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise SkillsError(400, f"{what} must be a whole number") from None


def _pick(found: _Found, raw: Any) -> int:
    """Which entry a request means: a list index, or a skill's path name."""
    if isinstance(raw, str) and not raw.isdigit():
        for index, entry in enumerate(found.entries):
            definition, _where = _first([("entry", entry)], DEFINITION_FIELDS)
            if _path(definition if definition is not None else entry) == raw:
                return index
        raise SkillsError(400, "unknown skill")
    try:
        index = int(raw)
    except (TypeError, ValueError):
        raise SkillsError(400, "unknown skill") from None
    if not 0 <= index < len(found.entries):
        raise SkillsError(400, "unknown skill")
    return index


def _definition(entry: Any) -> Any:
    definition, _where = _first([("entry", entry)], DEFINITION_FIELDS)
    return entry if definition is None else definition


def _notify(found: _Found, definition: Any, level: int) -> str | None:
    """Tells the game a skill's points changed so it re-applies the skill's effects. Which call does that is a guess."""
    for owner, target in (("pc", found.owners.get("pc")), ("tree", found.tree)):
        for name in NOTIFY_FUNCTIONS:
            try:
                getattr(target, name)(definition, level)
                return f"{owner}.{name}"
            except Exception:  # noqa: BLE001, S112
                continue
    return None


def _store(found: _Found, entry: Any, definition: Any, level: int, grade_via: str | None) -> tuple[str, str | None]:
    """Writes one skill's points. Returns how it was written and whether the game was told."""
    via = None
    if grade_via and grade_via.startswith("field:"):
        field = grade_via.split(":", 1)[1]
        try:
            setattr(entry, field, level)
            via = f"entry.{field}"
        except Exception:  # noqa: BLE001
            via = None
    if via is None or _grade(found, entry, definition)[0] != level:
        for owner, target in (("pc", found.owners.get("pc")), ("tree", found.tree)):
            for setter in GRADE_SETTERS:
                try:
                    getattr(target, setter)(definition, level)
                except Exception:  # noqa: BLE001, S112
                    continue
                if _grade(found, entry, definition)[0] == level:
                    return f"{owner}.{setter}", _notify(found, definition, level)
    if via is None:
        raise SkillsError(501, "this game build can't write skill points - run the skills dump")
    return via, _notify(found, definition, level)


def set_level(params: dict[str, Any]) -> dict[str, Any]:
    owners = _owners()
    found, reason = _locate(owners)
    if found is None:
        raise SkillsError(501, f"skill tree unavailable: {reason}")
    index = _pick(found, params.get("id"))
    entry = found.entries[index]
    before = _describe(found, index, entry)
    level = _whole(params.get("level"), "level")
    top = before["max"] if before["max"] is not None else MAX_GRADE_FALLBACK
    if not 0 <= level <= top:
        raise SkillsError(400, f"{before['name']} must be 0-{top}")
    if before["level"] is None:
        raise SkillsError(501, "can't read this skill's points - run the skills dump")

    # Deliberately ignores the tier lock: the game only checks it when a point is spent through the menu.
    via, notified = _store(found, entry, _definition(entry), level, before["grade_via"])
    state = read()
    after = next((s for s in state["skills"] if s["id"] == index), None)
    applied = {
        "name": before["name"], "requested": level, "via": via, "notified": notified,
        "before": before["level"], "after": after and after["level"],
    }
    logging.info(f"RTSE: skills {applied}")
    return {**state, "applied": applied}


def reset(params: dict[str, Any]) -> dict[str, Any]:
    if params.get("confirm") is not True:
        raise SkillsError(400, 'resetting the skill tree needs {"confirm": true}')
    owners = _owners()
    found, reason = _locate(owners)
    if found is None:
        raise SkillsError(501, f"skill tree unavailable: {reason}")
    before = _state(found, "", owners)
    if not any(s["level"] for s in before["skills"]):
        return {**before, "applied": {"via": None, "before": 0, "after": 0, "refunded": 0, "refund_via": None, "skill_points": before["skill_points"]}}

    # The game's own reset is tried first (it should also undo the skills' effects); anything it leaves is zeroed by hand.
    via = None
    for owner in ("pc", "tree", "pri"):
        target = found.tree if owner == "tree" else owners.get(owner)
        for name in RESET_FUNCTIONS:
            try:
                getattr(target, name)()
                via = f"{owner}.{name}"
            except Exception:  # noqa: BLE001, S112
                continue
            break
        if via:
            break
    found, _reason = _locate(owners)
    if found is None:
        raise SkillsError(501, "the skill tree disappeared while resetting - run the skills dump")

    zeroed = 0
    for index, entry in enumerate(found.entries):
        info = _describe(found, index, entry)
        if info["level"]:
            _store(found, entry, _definition(entry), 0, info["grade_via"])
            zeroed += 1
    if zeroed:
        via = f"{via} + entry writes" if via else "entry writes"

    # Points handed back: whatever the game didn't refund itself is added to the unspent total.
    after = read()
    refunded, refund_via = 0, None
    if params.get("refund", True) and before["skill_points"] is not None and after["skill_points"] is not None:
        wanted = min(before["skill_points"] + before["spent"], character.MAX_SKILL_POINTS)
        if after["skill_points"] < wanted:
            try:
                refund_via = character._write(owners, "skill_points", wanted)  # noqa: SLF001
                refunded = wanted - after["skill_points"]
            except character.CharacterError:
                refund_via = None
        else:
            refund_via = "game"
    state = read()
    applied = {
        "via": via, "before": before["spent"], "after": state["spent"], "refunded": refunded, "refund_via": refund_via,
        "skill_points": state["skill_points"],
    }
    logging.info(f"RTSE: skills reset {applied}")
    return {**state, "applied": applied}


def _class_info(obj: Any) -> dict[str, Any]:
    """The class name, every field (with its type) and every function name of a game object."""
    out: dict[str, Any] = {"class": None, "fields": [], "functions": []}
    try:
        out["class"] = obj.Class.Name
    except Exception:  # noqa: BLE001
        out["class"] = type(obj).__name__
    try:
        for prop in obj.Class._fields():  # noqa: SLF001
            try:
                kind = prop.Class.Name
            except Exception:  # noqa: BLE001
                kind = type(prop).__name__
            out["fields"].append(f"{prop.Name}: {kind}")
    except Exception as ex:  # noqa: BLE001
        out["fields"] = [f"<error {ex!r}>"]
    try:
        out["functions"] = sorted(str(f.Name) for f in obj.Class._methods())  # noqa: SLF001
    except Exception:  # noqa: BLE001
        try:
            out["functions"] = sorted(n for n in dir(obj) if not n.startswith("_") and callable(getattr(obj, n, None)))
        except Exception as ex:  # noqa: BLE001
            out["functions"] = [f"<error {ex!r}>"]
    return out


def _raw_scalars(obj: Any) -> dict[str, Any]:
    """Every readable scalar of an object or struct. Structs have no Class, so this reads whichever kind it is."""
    if isinstance(obj, unreal.UObject):
        try:
            return character._scalars(obj)  # noqa: SLF001
        except Exception as ex:  # noqa: BLE001
            return {"<error>": repr(ex)}
    out: dict[str, Any] = {}
    try:
        props = list(obj._type._fields())  # noqa: SLF001
    except Exception as ex:  # noqa: BLE001
        return {"<error>": repr(ex)}
    for prop in props:
        try:
            value = getattr(obj, prop.Name)
        except Exception as ex:  # noqa: BLE001
            out[prop.Name] = f"<error {ex!r}>"
            continue
        if value is None or isinstance(value, (bool, int, float, str)):
            out[prop.Name] = value
        elif isinstance(value, unreal.UObject):
            out[prop.Name] = _path(value)
        else:
            out[prop.Name] = f"<{type(value).__name__}>"
    return out


def debug_dump() -> dict[str, Any]:
    """Saves what the skill tree really looks like, to replace the guessed names above."""
    owners = _owners()
    tried: list[str] = []
    found, reason = _locate(owners, tried)
    out: dict[str, Any] = {"found": found and found.source, "list_field": found and found.list_field, "reason": reason, "tried": tried}

    # Anything on the controller, pawn and replication info with "skill" in its name: where the tree really is.
    out["skill_like_fields"] = {}
    for name, obj in owners.items():
        if obj is None:
            continue
        info = _class_info(obj)
        out["skill_like_fields"][name] = {
            "class": info["class"],
            "fields": [f for f in info["fields"] if "skill" in f.lower()],
            "functions": [f for f in info["functions"] if "skill" in f.lower()],
        }
    out["candidates"] = {f"{owner}.{attr}": ("present" if getattr(owners.get(owner), attr, None) is not None else "missing") for owner, attr in TREE_SOURCES}
    out["skill_points"] = character._read(owners, "skill_points")  # noqa: SLF001

    if found is not None:
        out["tree"] = {"source": found.source, **_class_info(found.tree), "scalars": _raw_scalars(found.tree)}
        entries: list[dict[str, Any]] = []
        for index, entry in enumerate(found.entries):
            definition = _definition(entry)
            item: dict[str, Any] = {"index": index, "type": type(entry).__name__, "entry": _raw_scalars(entry), "described": _describe(found, index, entry)}
            if definition is not entry:
                item["definition_path"] = _path(definition)
                item["definition"] = _raw_scalars(definition)
                if index == 0:
                    out["definition_class"] = _class_info(definition)
            if index == 0:
                out["entry_class"] = _class_info(entry) if isinstance(entry, unreal.UObject) else {"type": type(entry).__name__}
            entries.append(item)
        out["entries"] = entries
    DEBUG_SKILLS_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_SKILLS_FILE), "found": out["found"], "skills": len(out.get("entries", [])), "reason": reason or None}
