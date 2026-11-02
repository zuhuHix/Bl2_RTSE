"""The player's character: level, experience, skill points and currencies.

Names were checked against the game's own class definitions (dev/class_index.py): level, skill points and name are
fields of the PlayerReplicationInfo; currency is GetCurrencyOnHand/AddCurrencyOnHand/SetCurrencyOnHand on the
PlayerReplicationInfo; experience is read with the controller's GetExpPoints() and written to the experience resource
pool (D_Resourcepools.PlayerPools.ExperiencePool, Startup.upk). The XP needed per level comes from the controller's
GetExpPointsRequiredForLevel(Level). None of this has run in a live game; reads are defensive and every value that
could not be settled from the game's data is listed in the JSON's "unverified" dict.

Settled from the game's data (dev/upk.py, dev/audit_names.py):
  - currency indexes: WillowInventoryDefinition.ECurrencyType (Engine.upk) value names in order: CURRENCY_Credits 0,
    CURRENCY_Eridium 1, CURRENCY_SeraphCrystals 2, CURRENCY_Reserved_A 3, ... No entry is named for Torgue tokens.
  - level cap: limits.character_level_cap (GetMaximumPossiblePlayerLevelCap on the controller).
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import logging, unreal

from . import limits

DEBUG_CHARACTER_FILE = Path(__file__).parent / "debug_character.json"

MIN_LEVEL = 1
MAX_CURRENCY = 2_000_000_000  # stay inside a signed 32-bit integer
MAX_SKILL_POINTS = 999
MAX_XP = 2_000_000_000
# The game's experience resource pool (Startup.upk; its Resource is D_Resources.Experience).
EXPERIENCE_POOL = "D_Resourcepools.PlayerPools.ExperiencePool"

# key, label, ECurrencyType index (see the module docstring). Index 3 is CURRENCY_Reserved_A in the enum: it is the
# only slot left after Seraph Crystals, but nothing in the game data names it Torgue Tokens, so it stays unverified.
CURRENCIES = (
    ("money", "Cash", 0),
    ("eridium", "Eridium", 1),
    ("seraph", "Seraph Crystals", 2),
    ("torgue", "Torgue Tokens", 3),
)
UNVERIFIED_CURRENCY = {
    "torgue": "currency index 3 is CURRENCY_Reserved_A in ECurrencyType; no game data ties it to Torgue Tokens",
}

# Where each plain value might live: (owner, attribute). The first one that exists is used. All real.
# Experience is not here: it has no field (see _xp).
FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "level": (("pri", "ExpLevel"),),
    "skill_points": (("pri", "GeneralSkillPoints"),),
    "name": (("pri", "PlayerName"),),
}


class CharacterError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def xp_for_level(level: int) -> int:
    """FALLBACK ONLY (unverified): XP needed to reach a level, used when the game's own table can't be asked."""
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
        try:
            scalars = _scalars(target)
        except Exception:  # noqa: BLE001, S112 - an object that can't be listed just doesn't vote
            continue
        for name, value in scalars.items():
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


def _number(value: Any) -> int | None:
    if isinstance(value, tuple):  # functions with out parameters return (result, *outs)
        value = value[0] if value else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _call(target: Any, name: str, kwargs: dict[str, Any], *args: Any) -> Any:
    """Calls a game function with keyword arguments (immune to parameter order), falling back to positional ones."""
    function = getattr(target, name)
    try:
        return function(**kwargs)
    except Exception:  # noqa: BLE001
        return function(*args)


def _currency(owners: dict[str, Any], index: int) -> int | None:
    pri = owners.get("pri")
    if pri is None:
        return None
    try:
        return _number(_call(pri, "GetCurrencyOnHand", {"FormOfCurrency": index}, index))
    except Exception:  # noqa: BLE001
        return None


def _xp_pool(owners: dict[str, Any]) -> Any:
    """The player's ExperiencePool resource pool (exact definition path first), or None."""
    manager = getattr(owners["pc"], "ResourcePoolManager", None)
    pools = getattr(manager, "ResourcePools", None) if manager is not None else None
    fallback = None
    for pool in pools or ():
        if pool is None:
            continue
        try:
            if pool.Definition._path_name() == EXPERIENCE_POOL:  # noqa: SLF001 - exact, from the game data
                return pool
            if fallback is None and pool.Class.Name == "ExperienceResourcePool":  # noqa: SLF001 - the real class
                fallback = pool
        except Exception:  # noqa: BLE001, S112
            continue
    return fallback


def _xp(owners: dict[str, Any]) -> tuple[int | None, str | None]:
    """Experience points: the controller's GetExpPoints() (real), else the experience pool's current value."""
    try:
        value = _number(owners["pc"].GetExpPoints())
        if value is not None:
            return value, "pc.GetExpPoints()"
    except Exception:  # noqa: BLE001, S110
        pass
    pool = _xp_pool(owners)
    if pool is not None:
        try:
            return round(float(pool.GetCurrentValue())), "xp pool.GetCurrentValue()"
        except Exception:  # noqa: BLE001, S110
            pass
    return None, None


def _xp_required(owners: dict[str, Any], level: int) -> tuple[int, str]:
    """XP needed to reach `level`: the controller's GetExpPointsRequiredForLevel(Level) (real, the game's own table),
    else the guessed formula (unverified)."""
    try:
        value = _number(_call(owners["pc"], "GetExpPointsRequiredForLevel", {"Level": level}, level))
        if value is not None:
            return value, "pc.GetExpPointsRequiredForLevel()"
    except Exception:  # noqa: BLE001, S110
        pass
    return xp_for_level(level), "fallback formula (unverified)"


def _write_xp(owners: dict[str, Any], value: int) -> str:
    """Sets experience through the experience resource pool (real: ResourcePool.SetCurrentValue(Float))."""
    pool = _xp_pool(owners)
    if pool is None:
        raise CharacterError(501, "this game build has no experience pool I can find - run the character dump")
    try:
        pool.SetCurrentValue(float(value))
    except Exception:  # noqa: BLE001
        raise CharacterError(501, "this game build won't let me set experience - run the character dump") from None
    return "xp pool.SetCurrentValue"


def read() -> dict[str, Any]:
    owners = _owners()
    sources: dict[str, str | None] = {}
    values: dict[str, Any] = {}
    for key in FIELDS:
        values[key], sources[key] = _read(owners, key)
    values["xp"], sources["xp"] = _xp(owners)

    cap, cap_source = limits.character_level_cap(owners["pc"])
    level = values["level"]
    this_xp = next_xp = None
    if level is not None:
        this_xp, sources["xp_this_level"] = _xp_required(owners, level)
        next_xp, sources["xp_next_level"] = _xp_required(owners, level + 1)
    class_key, class_source = _character_class(owners)

    unverified: dict[str, str] = dict(UNVERIFIED_CURRENCY)
    if cap_source.startswith("fallback"):
        unverified["limits.level"] = f"the game didn't report the level cap; using the fallback {cap} (unverified)"
    for key in ("xp_this_level", "xp_next_level"):
        if (sources.get(key) or "").startswith("fallback"):
            unverified[key] = "the game's GetExpPointsRequiredForLevel() couldn't be read; using the guessed XP formula"

    return {
        "class_key": class_key,
        "class_source": class_source,
        "name": None if values["name"] is None else str(values["name"]),
        "level": level,
        "xp": values["xp"],
        "xp_this_level": this_xp,
        "xp_next_level": next_xp,
        "skill_points": values["skill_points"],
        "currencies": [
            {"key": key, "label": label, "index": index, "value": _currency(owners, index)} for key, label, index in CURRENCIES
        ],
        "limits": {
            "level": [MIN_LEVEL, cap], "level_source": cap_source,
            "currency": MAX_CURRENCY, "skill_points": MAX_SKILL_POINTS,
        },
        "sources": sources,
        "unverified": unverified,
    }


def _whole(raw: Any, low: int, high: int, what: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise CharacterError(400, f"{what} must be a whole number") from None
    if not low <= value <= high:
        raise CharacterError(400, f"{what} must be {low}-{high}")
    return value


_last_write: dict[str, Any] = {}


def _restore(owners: dict[str, Any], xp: int | None, level: int | None) -> None:
    """Puts back the values read before a write that didn't stick. Best effort: a failure here is only logged."""
    if xp is not None:
        try:
            _write_xp(owners, xp)
        except CharacterError as ex:
            logging.error(f"RTSE: could not restore XP {xp}: {ex}")
    if level is not None:
        try:
            _write(owners, "level", level)
        except Exception as ex:  # noqa: BLE001
            logging.error(f"RTSE: could not restore level {level}: {ex}")


def _undo_level(owners: dict[str, Any], before: dict[str, Any], level: int, target_xp: int, xp_source: str, reason: str,
                status: int) -> CharacterError:
    """After a level write went wrong: puts the level and XP back, reads back, and returns the error to raise. The message
    says whether the restore stuck; if it didn't, the save's level and XP are not what they were, so the status is 500."""
    try:
        observed = read()
    except Exception:  # noqa: BLE001
        observed = {"level": None, "xp": None}
    _restore(owners, before["xp"], before["level"])
    try:
        restored = read()
    except Exception:  # noqa: BLE001
        restored = {"level": None, "xp": None}
    _last_write.clear()
    _last_write.update({"field": "level", "requested": level, "xp_requested": target_xp, "xp_source": xp_source,
                        "before": {"level": before["level"], "xp": before["xp"]},
                        "after": {"level": observed["level"], "xp": observed["xp"]},
                        "restored": {"level": restored["level"], "xp": restored["xp"]}})
    if restored["level"] == before["level"] and restored["xp"] == before["xp"]:
        return CharacterError(status, f"{reason}; restored the previous level and XP")
    return CharacterError(
        500, f"{reason}; restoring ALSO failed (level now {restored['level']}, XP now {restored['xp']}) - reload the save before playing",
    )


def set_field(params: dict[str, Any]) -> dict[str, Any]:
    field = params.get("field")
    owners = _owners()
    before = read()
    where: dict[str, str] = {}

    if field == "level":
        cap, _cap_source = limits.character_level_cap(owners["pc"])
        level = _whole(params.get("value"), MIN_LEVEL, cap, "level")
        target_xp, xp_source = _xp_required(owners, level)
        # Level and XP are stored separately and must move together, XP first. If no experience pool can be written,
        # no alternative sets a level to a chosen value: WillowPlayerController.ExpLevelUp(bCheated) only adds one level
        # from the XP already held, and nothing else in the class data writes XP or level, so refuse and change nothing.
        if before["xp"] is None or before["level"] is None:
            # Without the old values there is nothing to put back, so don't start a write that might need undoing.
            raise CharacterError(409, "can't read the current level/XP, so a change couldn't be undone - nothing was changed")
        where["xp"] = _write_xp(owners, target_xp)
        try:
            where["level"] = _write(owners, "level", level)
            check = read()
        except Exception as ex:  # noqa: BLE001 - whatever failed after the XP write, both fields go back
            raise _undo_level(owners, before, level, target_xp, xp_source, f"writing the level failed ({ex})",
                              ex.status if isinstance(ex, CharacterError) else 409) from ex
        if check["level"] != level or check["xp"] != target_xp:
            raise _undo_level(owners, before, level, target_xp, xp_source,
                              f"the game didn't keep level {level} with {target_xp} XP "
                              f"(it reads level {check['level']}, XP {check['xp']})", 409)
    elif field == "xp":
        where["xp"] = _write_xp(owners, _whole(params.get("value"), 0, MAX_XP, "experience"))
    elif field == "skill_points":
        where["skill_points"] = _write(owners, "skill_points", _whole(params.get("value"), 0, MAX_SKILL_POINTS, "skill points"))
    elif field in {key for key, _label, _index in CURRENCIES}:
        index = next(i for key, _label, i in CURRENCIES if key == field)
        target = _whole(params.get("value"), 0, MAX_CURRENCY, "amount")
        current = _currency(owners, index)
        if current is None:
            raise CharacterError(501, "this game build can't read that currency - run the character dump")
        pri = owners["pri"]
        if target != current:
            try:
                _call(pri, "AddCurrencyOnHand", {"FormOfCurrency": index, "AddValue": target - current}, index, target - current)
                where[field] = f"pri.AddCurrencyOnHand({index})"
            except Exception:  # noqa: BLE001
                _call(pri, "SetCurrencyOnHand", {"FormOfCurrency": index, "NewValue": target}, index, target)
                where[field] = f"pri.SetCurrencyOnHand({index})"
        else:
            where[field] = "unchanged"
    else:
        raise CharacterError(400, "unknown field")

    after = read()
    applied = {"field": field, "requested": params.get("value"), "via": where, "before": _brief(before, field), "after": _brief(after, field)}
    if field == "level":
        _last_write.clear()
        _last_write.update({"field": field, "requested": applied["requested"], "xp_requested": target_xp, "xp_source": xp_source,
                            "before": {"level": before["level"], "xp": before["xp"]},
                            "after": {"level": after["level"], "xp": after["xp"]}, "restored": None})
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
    """Saves every field of the player controller, its replication info and pawn, to find real names, plus the
    evidence behind each unverified value: the currency probe, the level cap and XP table, the experience pool's
    definition path and the level/XP before and after the last level write."""
    owners = _owners()
    out: dict[str, Any] = {name: _scalars(obj) for name, obj in owners.items() if obj is not None}
    cap, cap_source = limits.character_level_cap(owners["pc"])
    pool = _xp_pool(owners)
    out["currency_probe"] = {str(i): _currency(owners, i) for i in range(8)}
    out["candidates"] = {key: _read(owners, key) for key in FIELDS}
    out["xp"] = {
        "read": _xp(owners),
        "pool_found": pool is not None,
        "pool_definition": _definition_path_or_none(pool),
        "pool_definition_expected": EXPERIENCE_POOL,
    }
    out["level_cap"] = {"value": cap, "source": cap_source}
    out["xp_required"] = {str(level): _xp_required(owners, level) for level in range(1, cap + 2)}
    out["currency_table"] = {"enum": "WillowInventoryDefinition.ECurrencyType", "rows": list(CURRENCIES)}
    out["last_level_write"] = dict(_last_write)
    DEBUG_CHARACTER_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {
        "saved_to": str(DEBUG_CHARACTER_FILE),
        "currency_probe": out["currency_probe"],
        "candidates": {k: v[1] for k, v in out["candidates"].items()},
        "xp_pool_definition": out["xp"]["pool_definition"],
        "level_cap": out["level_cap"],
    }


def _definition_path_or_none(pool: Any) -> str | None:
    if pool is None:
        return None
    try:
        return pool.Definition._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None
