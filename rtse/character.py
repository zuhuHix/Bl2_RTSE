"""The player's character: level, experience, skill points and currencies.

Field names come from the game's classes but have not all been confirmed in a running game, so every
field lists the places it might live (candidates) and reports which one worked. Run the character dump
from the web UI if something reads as "unavailable".
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import logging, unreal

DEBUG_CHARACTER_FILE = Path(__file__).parent / "debug_character.json"

MIN_LEVEL = 1
MAX_LEVEL = 80
MAX_CURRENCY = 2_000_000_000  # stay inside a signed 32-bit integer
MAX_SKILL_POINTS = 999

# key, label, the game's currency index
CURRENCIES = (
    ("money", "Cash", 0),
    ("eridium", "Eridium", 1),
    ("seraph", "Seraph Crystals", 2),
    ("torgue", "Torgue Tokens", 3),
)

# Where each plain value might live: (owner, attribute). The first one that exists is used.
FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "level": (("pri", "ExpLevel"), ("pc", "ExpLevel")),
    "xp": (("pri", "ExpPoints"), ("pc", "ExpPoints")),
    "skill_points": (("pri", "GeneralSkillPoints"), ("pc", "GeneralSkillPoints")),
    "name": (("pri", "PlayerName"),),
}


class CharacterError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def xp_for_level(level: int) -> int:
    """Experience points needed to reach a level (level 2 = 358, level 3 = 1241, ...)."""
    return 0 if level <= 1 else math.ceil(60 * (level**2.8 - 1))


def _owners() -> dict[str, Any]:
    pc = get_pc(possibly_loading=True)
    if pc is None:
        raise CharacterError(409, "not in a save")
    return {"pc": pc, "pri": getattr(pc, "PlayerReplicationInfo", None), "pawn": getattr(pc, "Pawn", None)}


def _read(owners: dict[str, Any], key: str) -> tuple[Any, str | None]:
    for owner, attribute in FIELDS[key]:
        target = owners.get(owner)
        if target is None:
            continue
        try:
            return getattr(target, attribute), f"{owner}.{attribute}"
        except Exception:  # noqa: BLE001, S112
            continue
    return None, None


def _write(owners: dict[str, Any], key: str, value: Any) -> str:
    for owner, attribute in FIELDS[key]:
        target = owners.get(owner)
        if target is None:
            continue
        try:
            getattr(target, attribute)  # does it exist?
        except Exception:  # noqa: BLE001, S112
            continue
        setattr(target, attribute, value)
        return f"{owner}.{attribute}"
    raise CharacterError(501, f"this game build has no readable '{key}' field - run the character dump")


CLASS_PATTERN = re.compile(
    r"(?:GD_|CharClass_|Character_|CD_|Char_)(Assassin|Siren|Soldier|Mercenary|Merc|Mechromancer|Mechro|Tulip|Psycho|Lilac)(?![a-z])",
    re.IGNORECASE,
)
CLASS_KEYS = {
    "assassin": "assassin", "siren": "siren", "soldier": "soldier", "mercenary": "mercenary", "merc": "mercenary",
    "mechromancer": "mechromancer", "mechro": "mechromancer", "tulip": "mechromancer", "psycho": "psycho", "lilac": "psycho",
}
_class_cache: dict[str, Any] = {}


def _character_class(owners: dict[str, Any]) -> tuple[str | None, str | None]:
    """Which vault hunter this is, found by looking for GD_<Class> names on the player's objects."""
    pc = owners["pc"]
    cached = _class_cache.get(pc._path_name())  # noqa: SLF001
    if cached:
        return cached
    votes: dict[str, int] = {}
    where: dict[str, str] = {}
    for owner in ("pc", "pawn", "pri"):
        target = owners.get(owner)
        if target is None:
            continue
        for name, value in _scalars(target).items():
            if not isinstance(value, str):
                continue
            match = CLASS_PATTERN.search(value)
            if match:
                key = CLASS_KEYS[match.group(1).lower()]
                votes[key] = votes.get(key, 0) + 1
                where.setdefault(key, f"{owner}.{name}")
    if not votes:
        return None, None
    best = max(votes, key=votes.get)
    result = (best, where[best])
    _class_cache[pc._path_name()] = result  # noqa: SLF001
    return result


def _currency(pc: unreal.UObject, index: int) -> int | None:
    try:
        return int(pc.GetCurrencyOnHand(index))
    except Exception:  # noqa: BLE001
        return None


def read() -> dict[str, Any]:
    owners = _owners()
    pc = owners["pc"]
    sources: dict[str, str | None] = {}
    values: dict[str, Any] = {}
    for key in FIELDS:
        values[key], sources[key] = _read(owners, key)

    level = values["level"]
    class_key, class_source = _character_class(owners)
    return {
        "class_key": class_key,
        "class_source": class_source,
        "name": None if values["name"] is None else str(values["name"]),
        "level": level,
        "xp": values["xp"],
        "xp_this_level": None if level is None else xp_for_level(level),
        "xp_next_level": None if level is None else xp_for_level(level + 1),
        "skill_points": values["skill_points"],
        "currencies": [
            {"key": key, "label": label, "index": index, "value": _currency(pc, index)} for key, label, index in CURRENCIES
        ],
        "limits": {"level": [MIN_LEVEL, MAX_LEVEL], "currency": MAX_CURRENCY, "skill_points": MAX_SKILL_POINTS},
        "sources": sources,
    }


def _whole(raw: Any, low: int, high: int, what: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise CharacterError(400, f"{what} must be a whole number") from None
    if not low <= value <= high:
        raise CharacterError(400, f"{what} must be {low}-{high}")
    return value


def set_field(params: dict[str, Any]) -> dict[str, Any]:
    field = params.get("field")
    owners = _owners()
    pc = owners["pc"]
    before = read()
    where: dict[str, str] = {}

    if field == "level":
        level = _whole(params.get("value"), MIN_LEVEL, MAX_LEVEL, "level")
        # Level and XP are stored separately; keep them in step or the game snaps the level back.
        where["xp"] = _write(owners, "xp", xp_for_level(level))
        where["level"] = _write(owners, "level", level)
    elif field == "xp":
        where["xp"] = _write(owners, "xp", _whole(params.get("value"), 0, 2_000_000_000, "experience"))
    elif field == "skill_points":
        where["skill_points"] = _write(owners, "skill_points", _whole(params.get("value"), 0, MAX_SKILL_POINTS, "skill points"))
    elif field in {key for key, _label, _index in CURRENCIES}:
        index = next(i for key, _label, i in CURRENCIES if key == field)
        target = _whole(params.get("value"), 0, MAX_CURRENCY, "amount")
        current = _currency(pc, index)
        if current is None:
            raise CharacterError(501, "this game build can't read that currency - run the character dump")
        if target != current:
            pc.AddCurrencyOnHand(index, target - current)
        where[field] = f"pc.AddCurrencyOnHand({index})"
    else:
        raise CharacterError(400, "unknown field")

    after = read()
    applied = {"field": field, "requested": params.get("value"), "via": where, "before": _brief(before, field), "after": _brief(after, field)}
    logging.info(f"RTSE: character {applied}")
    return {**after, "applied": applied}


def _brief(state: dict[str, Any], field: str) -> Any:
    if field in state:
        return state[field]
    return next((c["value"] for c in state["currencies"] if c["key"] == field), None)


def _scalars(obj: Any) -> dict[str, Any]:
    """Every readable scalar field of an object, plus the names of the functions it has."""
    out: dict[str, Any] = {}
    for prop in obj.Class._fields():  # noqa: SLF001
        try:
            value = getattr(obj, prop.Name)
        except Exception as ex:  # noqa: BLE001
            out[prop.Name] = f"<error {ex!r}>"
            continue
        if value is None or isinstance(value, (bool, int, float, str)):
            out[prop.Name] = value
        elif isinstance(value, unreal.UObject):
            out[prop.Name] = value._path_name()  # noqa: SLF001
        else:
            out[prop.Name] = f"<{type(value).__name__}>"
    return out


def debug_dump() -> dict[str, Any]:
    """Saves every field of the player controller, its replication info and pawn, to find real names."""
    owners = _owners()
    out: dict[str, Any] = {name: _scalars(obj) for name, obj in owners.items() if obj is not None}
    out["currency_probe"] = {str(i): _currency(owners["pc"], i) for i in range(8)}
    out["candidates"] = {key: _read(owners, key) for key in FIELDS}
    DEBUG_CHARACTER_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_CHARACTER_FILE), "currency_probe": out["currency_probe"], "candidates": {k: v[1] for k, v in out["candidates"].items()}}
