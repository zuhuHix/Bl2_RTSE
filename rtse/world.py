"""World progress: missions, challenges, fast-travel stations and the playthrough (Normal / TVHM / UVHM).

The game-side names here were checked against the class definitions extracted from the installed game (run
dev/audit_names.py): every field, function and parameter name below EXISTS with the type used. What that does
not prove is behaviour - nobody has run this in a live game. Still guesses:
  - whether the playthrough number counts from 0 or from 1 (FIRST_RAW; the raw number is reported so it can be checked)
  - how GRI.SetCurrentPlaythrough behaves for a client (it may be server-only or may not persist to the save)
  - whether writing a mission's Status (directly, or through MissionTracker.SetMissionStatus) re-triggers the game's
    mission logic (rewards, objectives, the HUD) or only changes the stored value
  - that challenge levels are numbered from 0, and that ServerCompleteChallenge works from the client
  - which arguments RegisterStationForPlayer wants (DiscoveredBy, bFromLoad) and what Behavior_RegisterStationDefinition
    shows on screen
  - that pc.LocalChallengeDataCache holds every challenge the character has (not only a recent subset)
Functions are called with keyword arguments named like the real parameters first, then positionally. The result of
every write is read back and reported, and each of the four sections is independent: if the game doesn't expose one,
only that section reports "unavailable". Writes the game has no function for (resetting a challenge, locking a
station again) answer 501 instead of guessing. Run the world dump from the web UI to see the real values.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, NamedTuple

from mods_base import get_pc
from unrealsdk import find_all, logging, unreal

DEBUG_WORLD_FILE = Path(__file__).parent / "debug_world.json"

MAX_BULK = 1000  # most missions / challenges / stations one request may touch

# ---------- playthrough ----------

PLAYTHROUGHS = (("Normal", "Normal"), ("TVHM", "True Vault Hunter Mode"), ("UVHM", "Ultimate Vault Hunter Mode"))
FIRST_RAW = 0  # the game's own number for playthrough 1 (guess: it counts from zero; the dump shows the raw numbers)
PLAYTHROUGH_READERS = (("pc", "GetCurrentPlaythrough"), ("gri", "GetCurrPlaythrough"))  # called functions, no arguments
PLAYTHROUGH_FIELDS = (("gri", "CurrentPlaythrough"),)  # read if no function answers; written (then NotifyPlaythroughChanged) if the setters don't take
PLAYTHROUGH_SETTERS = (("gri", "SetCurrentPlaythrough"),)  # (PrimaryWPC, InCurrPlaythrough)

# Objects reached from the player controller, tried in order. Keys are owner names: pc is the controller itself,
# gri the WillowGameReplicationInfo, missions its MissionTracker. WorldInfo.Game is server-side only (often None).
OWNER_PATHS: dict[str, tuple[str, ...]] = {
    "gri": ("WorldInfo.GRI",),
    "missions": ("WorldInfo.GRI.MissionTracker",),
    "game": ("WorldInfo.Game",),
}

# ---------- missions ----------

# EMissionStatus in the game's order (MS_NotStarted .. MS_Failed), and what we call each state.
STATUS_ORDER = ("not_started", "active", "objectives_done", "ready", "complete", "failed")
STATUS_ENUM_NAMES = {
    "not_started": ("MS_NotStarted",),
    "active": ("MS_Active",),
    "objectives_done": ("MS_RequiredObjectivesComplete",),
    "ready": ("MS_ReadyToTurnIn",),
    "complete": ("MS_Complete",),
    "failed": ("MS_Failed",),
}
STATUS_LABELS = {
    "not_started": "Not started", "active": "Active", "objectives_done": "Objectives done",
    "ready": "Ready to turn in", "complete": "Complete", "failed": "Failed", "unknown": "Unknown",
}
WRITABLE_STATUSES = ("not_started", "active", "ready", "complete")
# pc.MissionPlaythroughs: one MissionPlaythroughData {PlayThroughNumber, MissionList, ...} per playthrough, whose
# MissionList holds IMission.MissionData {MissionDef, Status, ...}. The tracker's own MissionList is the fallback.
MISSION_PLAYTHROUGH_LISTS = (("pc", "MissionPlaythroughs"),)
MISSION_FLAT_LISTS = (("missions", "MissionList"),)
MISSION_GROUP_ENTRIES = ("MissionList",)  # the entries inside one playthrough group
MISSION_PT_NUMBER = ("PlayThroughNumber",)
MISSION_DEF = ("MissionDef",)
MISSION_STATUS = ("Status",)
MISSION_NAME = ("MissionName",)  # on the MissionDefinition
MISSION_PLOT_FLAG = ("bPlotCritical",)
MISSION_SETTERS = (("missions", "SetMissionStatus"),)  # (InMission, MissionStatus, WillowPC), current playthrough only
# Package prefixes of DLC missions (guesses from memory of the game's package names).
DLC_PACKAGES = (
    ("gd_aster", "Tiny Tina's Assault on Dragon Keep"), ("gd_sage", "Hammerlock's Big Game Hunt"),
    ("gd_iris", "Mr. Torgue's Campaign of Carnage"), ("gd_orchid", "Captain Scarlett's Pirate Booty"),
    ("gd_anemone", "Headhunter Pack"), ("gd_lilac", "Psycho Pack"), ("gd_dandelion", "Other DLC"),
)

# ---------- challenges ----------

# pc.LocalChallengeDataCache holds ChallengeDefinition.ChallengeData {ChallengeDefinition, PCOwner}; the progress is
# not in the struct, it is read through pc functions (IsChallengeComplete, GetChallengeTotalProgress, ...).
CHALLENGE_LISTS = (("pc", "LocalChallengeDataCache"),)
CHALLENGE_DEF = ("ChallengeDefinition",)
CHALLENGE_NAME = ("ChallengeName",)  # on the ChallengeDefinition
CHALLENGE_DESC = ("Description",)
CHALLENGE_DEF_GOAL = ("GoalValue",)
CHALLENGE_LEVELS = ("Levels",)  # ConditionLevel entries; completing a challenge completes each level
CHALLENGE_CATEGORY = ("ChallengeCategoryDef",)  # object with .CategoryName
CHALLENGE_CATEGORY_NAME = ("CategoryName",)

# ---------- fast travel ----------

STATION_CLASSES = ("FastTravelStationDefinition",)
STATION_LISTS: tuple[tuple[str, str], ...] = ()  # the game keeps no readable visited-list on the controller
STATION_NAME = ("StationDisplayName",)  # on the definition (TravelStationDefinition)
STATION_LEVEL = ("StationLevelName",)
STATION_GETTER = ("pc", "IsStationDiscovered")  # (StationDefinition) -> Bool
STATION_SETTERS = (("pc", "Behavior_RegisterStationDefinition"), ("pc", "RegisterStationForPlayer"))  # tried in order

class WorldError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _Unavailable(Exception):
    """One section of the page can't be read in this game build."""


# ---------- small helpers ----------


def _walk(root: Any, path: str) -> Any:
    target = root
    for part in path.split("."):
        if target is None:
            return None
        try:
            target = getattr(target, part)
        except Exception:  # noqa: BLE001
            return None
    return target


def _owners() -> dict[str, Any]:
    pc = get_pc(possibly_loading=True)
    if pc is None:
        raise WorldError(409, "not in a save")
    owners: dict[str, Any] = {"pc": pc}
    for key, paths in OWNER_PATHS.items():
        for path in paths:
            target = _walk(pc, path)
            if target is not None:
                owners[key] = target
                break
    return owners


def _first(obj: Any, names: tuple[str, ...]) -> tuple[Any, str | None]:
    """The first of these attributes that exists and isn't null, with the name that worked."""
    if obj is None:
        return None, None
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:  # noqa: BLE001, S112
            continue
        if value is not None:
            return value, name
    return None, None


def _candidates(owners: dict[str, Any], specs: tuple[tuple[str, str], ...]) -> list[tuple[str, Any]]:
    """Every (owner, attribute) in the list that exists on this game build, as (source, value)."""
    found = []
    for owner, attribute in specs:
        value, _name = _first(owners.get(owner), (attribute,))
        if value is not None:
            found.append((f"{owner}.{attribute}", value))
    return found


def _items(array: Any) -> list[Any]:
    try:
        return [item for item in array if item is not None]
    except Exception:  # noqa: BLE001
        return []


def _call(target: Any, name: str, kwargs: dict[str, Any] | None = None, args: tuple[Any, ...] = ()) -> Any:
    """Calls a game function by name with keyword arguments (immune to parameter order), falling back to positional ones."""
    function = getattr(target, name)
    if kwargs:
        try:
            return function(**kwargs)
        except Exception:  # noqa: BLE001
            if not args:
                raise
    return function(*args)


def _path(obj: Any) -> str:
    try:
        return obj._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return str(obj)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, unreal.UObject):
        return _path(value)
    text = str(value).strip()
    return text or None


def _clean(path: str) -> str:
    """A readable name from an object path: 'GD_Episode01.M_Ep1_ClearingBerg' -> 'Ep1 Clearing Berg'."""
    name = path.rsplit(".", 1)[-1]
    name = re.sub(r"^(?:M|Ch|Challenge|FT|Station)_", "", name)
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name.replace("_", " "))
    return name.strip() or path


def _whole(raw: Any, low: int, high: int, what: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise WorldError(400, f"{what} must be a whole number") from None
    if not low <= value <= high:
        raise WorldError(400, f"{what} must be {low}-{high}")
    return value


def _require_confirm(params: dict[str, Any], what: str) -> None:
    if params.get("confirm") is not True:
        raise WorldError(400, f"{what} needs confirmation - send {{\"confirm\": true}}")


def _ids(params: dict[str, Any]) -> list[str]:
    raw = params.get("ids")
    if raw is None and params.get("id") is not None:
        raw = [params["id"]]
    if not isinstance(raw, list) or not raw or not all(isinstance(i, str) and i for i in raw):
        raise WorldError(400, "send 'id' (a string) or 'ids' (a non-empty list of strings)")
    if len(raw) > MAX_BULK:
        raise WorldError(400, f"at most {MAX_BULK} at once")
    return list(dict.fromkeys(raw))


def _section(build: Callable[[dict[str, Any]], dict[str, Any]], owners: dict[str, Any]) -> dict[str, Any]:
    """Runs one section's reader. A failure becomes that section's 'unavailable' state; nothing else is touched."""
    try:
        return {"available": True, "error": None, **build(owners)}
    except _Unavailable as ex:
        return {"available": False, "error": str(ex)}
    except Exception as ex:  # noqa: BLE001
        return {"available": False, "error": f"{type(ex).__name__}: {ex}"}


# ---------- playthrough ----------


def _read_playthrough(owners: dict[str, Any]) -> tuple[int | None, str | None]:
    """The game's raw playthrough number and where it came from."""
    for owner, function in PLAYTHROUGH_READERS:
        target = owners.get(owner)
        if target is None:
            continue
        try:
            return int(_call(target, function)), f"{owner}.{function}()"
        except Exception:  # noqa: BLE001, S112
            continue
    for owner, attribute in PLAYTHROUGH_FIELDS:
        value, _name = _first(owners.get(owner), (attribute,))
        try:
            if value is not None:
                return int(value), f"{owner}.{attribute}"
        except (TypeError, ValueError):
            continue
    return None, None


def _current_playthrough(owners: dict[str, Any]) -> int | None:
    raw, _source = _read_playthrough(owners)
    if raw is None:
        return None
    number = raw - FIRST_RAW + 1
    return number if 1 <= number <= len(PLAYTHROUGHS) else None


def _playthrough_section(owners: dict[str, Any]) -> dict[str, Any]:
    raw, source = _read_playthrough(owners)
    if raw is None:
        raise _Unavailable("this game build doesn't expose the current playthrough")
    number = raw - FIRST_RAW + 1
    return {
        "current": number if 1 <= number <= len(PLAYTHROUGHS) else None,
        "raw": raw,
        "source": source,
        "options": [{"number": i + 1, "short": short, "name": name} for i, (short, name) in enumerate(PLAYTHROUGHS)],
    }


def set_playthrough(params: dict[str, Any]) -> dict[str, Any]:
    _require_confirm(params, "switching playthrough")
    target = _whole(params.get("playthrough"), 1, len(PLAYTHROUGHS), "playthrough")
    owners = _owners()
    raw_before, source = _read_playthrough(owners)
    if raw_before is None:
        raise WorldError(501, "this game build can't read the playthrough - run the world dump")
    raw_target = target - 1 + FIRST_RAW
    pc = owners["pc"]
    via = None
    for owner, function in PLAYTHROUGH_SETTERS:
        obj = owners.get(owner)
        if obj is None:
            continue
        try:
            _call(obj, function, {"PrimaryWPC": pc, "InCurrPlaythrough": raw_target}, (pc, raw_target))
        except Exception:  # noqa: BLE001, S112
            continue
        via = f"{owner}.{function}()"
        if _read_playthrough(owners)[0] == raw_target:
            break
    if _read_playthrough(owners)[0] != raw_target:  # the setter didn't take (or wasn't there): write the field, then tell the game
        for owner, attribute in PLAYTHROUGH_FIELDS:
            obj = owners.get(owner)
            if obj is None or _first(obj, (attribute,))[1] is None:
                continue
            try:
                setattr(obj, attribute, raw_target)
            except Exception:  # noqa: BLE001, S112
                continue
            via = f"{owner}.{attribute}"
            try:
                _call(obj, "NotifyPlaythroughChanged")
                via += " + NotifyPlaythroughChanged()"
            except Exception:  # noqa: BLE001, S110
                pass
            if _read_playthrough(owners)[0] == raw_target:
                break
    if via is None:
        raise WorldError(501, "this game build has no usable playthrough setter - run the world dump")
    section = _section(_playthrough_section, owners)
    applied = {
        "what": "playthrough", "requested": target, "via": via, "read_from": source,
        "before": raw_before - FIRST_RAW + 1, "after": section.get("current"),
    }
    logging.info(f"RTSE: world {applied}")
    return {"playthrough": section, "applied": applied}


# ---------- missions ----------


class _Mission(NamedTuple):
    pt: int
    entry: Any
    definition: Any
    id: str
    status_field: str


def _status_key(raw: Any) -> str:
    if raw is None:
        return "unknown"
    name = getattr(raw, "name", None)
    text = (name if isinstance(name, str) else str(raw)).lower().replace("_", "")
    for needle, key in (
        ("notstarted", "not_started"), ("requiredobjective", "objectives_done"), ("readytoturnin", "ready"),
        ("complete", "complete"), ("failed", "failed"), ("active", "active"),
    ):
        if needle in text:
            return key
    try:
        return STATUS_ORDER[int(raw)]
    except (TypeError, ValueError, IndexError):
        return "unknown"


def _status_value(sample: Any, key: str) -> Any:
    """What to assign for a status: a member of the same enum as the value we read, else a number."""
    members = getattr(type(sample), "__members__", None)
    if members:
        for name in STATUS_ENUM_NAMES[key]:
            if name in members:
                return members[name]
        for name, member in members.items():
            if _status_key(name) == key:
                return member
    if isinstance(sample, str):
        return STATUS_ENUM_NAMES[key][0]
    return STATUS_ORDER.index(key)


def _mission_entry(entry: Any, pt: int) -> _Mission | None:
    definition, _name = _first(entry, MISSION_DEF)
    if definition is None:
        return None
    for field in MISSION_STATUS:
        try:
            getattr(entry, field)
        except Exception:  # noqa: BLE001, S112
            continue
        return _Mission(pt, entry, definition, _path(definition), field)
    return None


def _mission_refs(owners: dict[str, Any]) -> tuple[list[_Mission], str]:
    """Every mission the game tracks, for every playthrough, and which list they came from."""
    for source, groups in _candidates(owners, MISSION_PLAYTHROUGH_LISTS):
        refs: list[_Mission] = []
        for position, group in enumerate(_items(groups)):
            entries, _name = _first(group, MISSION_GROUP_ENTRIES)
            if entries is None:
                continue
            number, _name = _first(group, MISSION_PT_NUMBER)
            pt = number - FIRST_RAW + 1 if isinstance(number, int) and 0 <= number - FIRST_RAW < len(PLAYTHROUGHS) else position + 1
            refs += [ref for entry in _items(entries) if (ref := _mission_entry(entry, pt))]
        if refs:
            return refs, source
    pt = _current_playthrough(owners) or 1
    for source, entries in _candidates(owners, MISSION_FLAT_LISTS):
        refs = [ref for entry in _items(entries) if (ref := _mission_entry(entry, pt))]
        if refs:
            return refs, source
    raise _Unavailable("this game build doesn't expose the mission list")


def _mission_row(ref: _Mission) -> dict[str, Any]:
    name, _field = _first(ref.definition, MISSION_NAME)
    plot, _field = _first(ref.definition, MISSION_PLOT_FLAG)
    low = ref.id.lower()
    group, kind = "Side missions", "side"
    dlc = next((label for prefix, label in DLC_PACKAGES if low.startswith(prefix)), None)
    if dlc:
        group, kind = dlc, "dlc"
    elif plot is True or "episode" in low:
        group, kind = "Main story", "main"
    try:
        status = _status_key(getattr(ref.entry, ref.status_field))
    except Exception:  # noqa: BLE001
        status = "unknown"
    return {"id": ref.id, "name": _text(name) or _clean(ref.id), "type": kind, "group": group, "status": status}


def _missions_section(owners: dict[str, Any]) -> dict[str, Any]:
    refs, source = _mission_refs(owners)
    by_pt: dict[int, list[dict[str, Any]]] = {}
    for ref in refs:
        by_pt.setdefault(ref.pt, []).append(_mission_row(ref))
    playthroughs = []
    for pt in sorted(by_pt):
        counts: dict[str, int] = {}
        for row in by_pt[pt]:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        playthroughs.append({"number": pt, "rows": by_pt[pt], "counts": counts})
    return {"playthroughs": playthroughs, "current": _current_playthrough(owners), "source": source, "statuses": STATUS_LABELS}


def _mission_status(ref: _Mission) -> str:
    try:
        return _status_key(getattr(ref.entry, ref.status_field))
    except Exception:  # noqa: BLE001
        return "unknown"


def _write_mission(owners: dict[str, Any], ref: _Mission, key: str, current: int | None) -> str | None:
    """Sets one mission's status; returns how it was done, or None if the readback never matched.

    The tracker is the world's own (current playthrough) state, so for the current playthrough it is asked first;
    whatever it leaves behind in the stored entry is then checked, and the entry is written directly if it differs.
    """
    pc = owners.get("pc")
    sample = getattr(ref.entry, ref.status_field)
    values = [_status_value(sample, key)]
    if STATUS_ORDER.index(key) not in values:
        values.append(STATUS_ORDER.index(key))  # a plain number, if the enum member isn't accepted
    called = None
    if current is None or ref.pt == current:
        for owner, function in MISSION_SETTERS:
            target = owners.get(owner)
            if target is None:
                continue
            for value in values:
                try:
                    _call(target, function, {"InMission": ref.definition, "MissionStatus": value, "WillowPC": pc}, (ref.definition, value, pc))
                except Exception:  # noqa: BLE001, S112
                    continue
                called = f"{owner}.{function}()"
                break
            if called:
                break
        if called and _mission_status(ref) == key:
            return called
    try:
        setattr(ref.entry, ref.status_field, values[0])
        if _mission_status(ref) == key:
            return f"{called} + entry.{ref.status_field}" if called else f"entry.{ref.status_field}"
    except Exception:  # noqa: BLE001, S110
        pass
    return None


def _counts(refs: list[_Mission]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ref in refs:
        key = _mission_status(ref)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _mission_targets(owners: dict[str, Any], params: dict[str, Any], wanted: list[str] | None) -> tuple[list[_Mission], int]:
    try:
        refs, _source = _mission_refs(owners)
    except _Unavailable as ex:
        raise WorldError(501, f"{ex} - run the world dump") from None
    current = _current_playthrough(owners)
    raw_pt = params.get("playthrough")
    pt = _whole(raw_pt, 1, len(PLAYTHROUGHS), "playthrough") if raw_pt is not None else current
    if pt is None:
        raise WorldError(400, "say which playthrough (1-3); the game's current one can't be read")
    pool = [ref for ref in refs if ref.pt == pt]
    if not pool:
        raise WorldError(404, f"no missions found for playthrough {pt}")
    if wanted is None:
        return pool, pt
    known = {ref.id: ref for ref in pool}
    missing = [i for i in wanted if i not in known]
    if missing:
        raise WorldError(404, f"unknown mission: {missing[0]}" + (f" (+{len(missing) - 1} more)" if len(missing) > 1 else ""))
    return [known[i] for i in wanted], pt


def _apply_missions(owners: dict[str, Any], targets: list[_Mission], pt: int, key: str) -> dict[str, Any]:
    before = _counts(targets)
    single_before = _mission_status(targets[0]) if len(targets) == 1 else None
    current = _current_playthrough(owners)
    vias: set[str] = set()
    failed: list[str] = []
    changed = 0
    for ref in targets:
        if _mission_status(ref) == key:
            continue
        via = _write_mission(owners, ref, key, current)
        if via:
            vias.add(via)
            changed += 1
        else:
            failed.append(ref.id)
    after = _counts(targets)
    one = len(targets) == 1
    applied = {
        "what": "mission" if one else "missions", "playthrough": pt, "requested": key, "count": len(targets), "changed": changed,
        "failed": failed[:20], "via": sorted(vias),
        "before": single_before if one else before, "after": _mission_status(targets[0]) if one else after,
    }
    if one:
        applied["name"] = _mission_row(targets[0])["name"]
    logging.info(f"RTSE: world {applied}")
    return {"missions": _section(_missions_section, owners), "applied": applied}


def set_mission(params: dict[str, Any]) -> dict[str, Any]:
    _require_confirm(params, "changing mission state")
    key = params.get("status")
    if key not in WRITABLE_STATUSES:
        raise WorldError(400, f"status must be one of {', '.join(WRITABLE_STATUSES)}")
    wanted = _ids(params)
    owners = _owners()
    targets, pt = _mission_targets(owners, params, wanted)
    return _apply_missions(owners, targets, pt, key)


def reset_missions(params: dict[str, Any]) -> dict[str, Any]:
    """Puts missions back to 'not started': the listed ids, or every mission in the playthrough with {"all": true}."""
    _require_confirm(params, "resetting missions")
    wanted = None if params.get("all") is True else _ids(params)
    owners = _owners()
    targets, pt = _mission_targets(owners, params, wanted)
    return _apply_missions(owners, targets, pt, "not_started")


# ---------- challenges ----------


class _Challenge(NamedTuple):
    entry: Any
    definition: Any
    id: str


def _challenge_entry(entry: Any) -> _Challenge | None:
    definition, _name = _first(entry, CHALLENGE_DEF)
    if definition is None:
        return None
    return _Challenge(entry, definition, _path(definition))


def _challenge_refs(owners: dict[str, Any]) -> tuple[list[_Challenge], str]:
    for source, entries in _candidates(owners, CHALLENGE_LISTS):
        refs = [ref for entry in _items(entries) if (ref := _challenge_entry(entry))]
        if refs:
            return refs, source
    raise _Unavailable("this game build doesn't expose the challenge list")


def _whole_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _challenge_progress(owners: dict[str, Any], definition: Any) -> tuple[int | None, int | None]:
    """(current, target) over all levels from pc.GetChallengeTotalProgress; the out values follow the return value."""
    try:
        result = _call(owners["pc"], "GetChallengeTotalProgress", {"ChalDef": definition}, (definition,))
    except Exception:  # noqa: BLE001
        return None, None
    numbers = [v for v in (result if isinstance(result, tuple) else (result,)) if _whole_number(v)]
    return (numbers[-2], numbers[-1]) if len(numbers) >= 2 else (None, None)


def _challenge_done(owners: dict[str, Any], definition: Any) -> bool | None:
    try:
        return bool(_call(owners["pc"], "IsChallengeComplete", {"ChalDef": definition}, (definition,)))
    except Exception:  # noqa: BLE001
        return None


def _challenge_row(owners: dict[str, Any], ref: _Challenge) -> dict[str, Any]:
    name, _field = _first(ref.definition, CHALLENGE_NAME)
    desc, _field = _first(ref.definition, CHALLENGE_DESC)
    category, _field = _first(_first(ref.definition, CHALLENGE_CATEGORY)[0], CHALLENGE_CATEGORY_NAME)
    progress, goal = _challenge_progress(owners, ref.definition)
    if goal is None:
        value, _field = _first(ref.definition, CHALLENGE_DEF_GOAL)
        goal = value if _whole_number(value) else None
    done = _challenge_done(owners, ref.definition)
    if done is None and progress is not None and goal:
        done = progress >= goal
    package = _clean(ref.id.split(".")[0].removeprefix("GD_")) if "." in ref.id else ""
    group = _text(category) or package or "Challenges"
    return {
        "id": ref.id, "name": _text(name) or _clean(ref.id), "desc": _text(desc) or "", "group": group,
        "progress": progress, "goal": goal, "done": done,
    }


def _challenges_section(owners: dict[str, Any]) -> dict[str, Any]:
    refs, source = _challenge_refs(owners)
    # can_reset: the game has no per-challenge reset (only PrestigeResetChallenges, which resets everything)
    return {"rows": [_challenge_row(owners, ref) for ref in refs], "source": source, "can_reset": False}


def _complete_challenge(owners: dict[str, Any], ref: _Challenge) -> list[str]:
    """Completes each level of a challenge that isn't done yet through pc.ServerCompleteChallenge. Returns the calls made."""
    pc = owners["pc"]
    levels, _field = _first(ref.definition, CHALLENGE_LEVELS)
    calls: list[str] = []
    for level in range(max(1, len(_items(levels)))):  # levels are assumed to count from 0
        try:
            if _call(pc, "IsChallengeLevelComplete", {"ChalDef": ref.definition, "LevelIdx": level}, (ref.definition, level)) is True:
                continue
        except Exception:  # noqa: BLE001, S110
            pass  # can't tell: complete it anyway, the readback decides
        _call(pc, "ServerCompleteChallenge", {"ChalDef": ref.definition, "LevelIdx": level}, (ref.definition, level))
        calls.append("pc.ServerCompleteChallenge()")
    return calls


def set_challenge(params: dict[str, Any]) -> dict[str, Any]:
    action = params.get("action")
    if action not in ("complete", "reset"):
        raise WorldError(400, "action must be 'complete' or 'reset'")
    if action == "reset":
        raise WorldError(501, "resetting challenges isn't supported: the game has no function to reset a single challenge (only a full prestige reset)")
    wanted = _ids(params)
    owners = _owners()
    try:
        refs, _source = _challenge_refs(owners)
    except _Unavailable as ex:
        raise WorldError(501, f"{ex} - run the world dump") from None
    known = {ref.id: ref for ref in refs}
    missing = [i for i in wanted if i not in known]
    if missing:
        raise WorldError(404, f"unknown challenge: {missing[0]}")
    before = [_challenge_row(owners, known[i]) for i in wanted]
    vias: set[str] = set()
    failed: list[str] = []
    for challenge_id in wanted:
        ref = known[challenge_id]
        if _challenge_done(owners, ref.definition) is True:
            continue
        try:
            vias.update(_complete_challenge(owners, ref))
        except Exception:  # noqa: BLE001
            failed.append(challenge_id)
            continue
        if _challenge_done(owners, ref.definition) is not True:
            failed.append(challenge_id)
    after = [_challenge_row(owners, known[i]) for i in wanted]
    one = len(wanted) == 1
    applied = {
        "what": "challenge" if one else "challenges", "requested": action, "count": len(wanted), "failed": failed[:20],
        "via": sorted(vias),
        "before": (before[0]["done"], before[0]["progress"]) if one else sum(1 for r in before if r["done"]),
        "after": (after[0]["done"], after[0]["progress"]) if one else sum(1 for r in after if r["done"]),
    }
    if one:
        applied["name"] = after[0]["name"]
    logging.info(f"RTSE: world {applied}")
    return {"challenges": _section(_challenges_section, owners), "applied": applied}


# ---------- fast travel stations ----------


def _station_defs() -> list[Any]:
    defs: list[Any] = []
    seen: set[str] = set()
    for class_name in STATION_CLASSES:
        try:
            found = list(find_all(class_name))
        except Exception:  # noqa: BLE001, S112
            continue
        for obj in found:
            path = _path(obj)
            if "Default__" in path or path in seen:
                continue
            seen.add(path)
            defs.append(obj)
    return defs


def _station_state(owners: dict[str, Any], definition: Any) -> bool | None:
    """Whether pc.IsStationDiscovered says the station is unlocked; None if it can't answer."""
    owner, function = STATION_GETTER
    target = owners.get(owner)
    if target is None:
        return None
    try:
        return bool(_call(target, function, {"StationDefinition": definition}, (definition,)))
    except Exception:  # noqa: BLE001
        return None


def _station_row(owners: dict[str, Any], definition: Any) -> dict[str, Any]:
    path = _path(definition)
    name, _field = _first(definition, STATION_NAME)
    level, _field = _first(definition, STATION_LEVEL)
    return {"id": path, "name": _text(name) or _clean(path), "level": _text(level) or "", "visited": _station_state(owners, definition)}


def _stations_section(owners: dict[str, Any]) -> dict[str, Any]:
    defs = _station_defs()
    if not defs:
        raise _Unavailable("no fast-travel station definitions were found in this game build")
    rows = sorted((_station_row(owners, d) for d in defs), key=lambda r: r["name"].lower())
    warning = None
    if all(r["visited"] is None for r in rows):
        warning = "stations were found but whether they are unlocked can't be read, so they show as unknown"
    return {"rows": rows, "warning": warning, "can_lock": False}  # the game has no function to un-discover a station


def _station_calls(function: str, pc: Any, definition: Any) -> list[tuple[dict[str, Any], tuple[Any, ...]]]:
    """(keyword, positional) argument sets to try for one registering function."""
    if function == "Behavior_RegisterStationDefinition":
        return [({"TravelDefinition": definition, "bSetAsLastVisited": False}, (definition, False))]
    if function == "RegisterStationForPlayer":  # DiscoveredBy is a guess: the replication info, else the controller
        who = [w for w in (_first(pc, ("PlayerReplicationInfo",))[0], pc) if w is not None]
        return [
            ({"ActivatedStationDefinition": definition, "ActivatedStation": None, "DiscoveredBy": w, "bFromLoad": True, "bSetAsLastVisited": False},
             (definition, None, w, True, False))
            for w in who
        ]
    return []


def _write_station(owners: dict[str, Any], definition: Any) -> str | None:
    """Unlocks one station, trying each registering function in turn until the readback agrees. Returns how, or None."""
    for owner, function in STATION_SETTERS:
        target = owners.get(owner)
        if target is None:
            continue
        for kwargs, args in _station_calls(function, owners.get("pc"), definition):
            try:
                _call(target, function, kwargs, args)
            except Exception:  # noqa: BLE001, S112
                continue
            if _station_state(owners, definition) is True:
                return f"{owner}.{function}()"
    return None


def unlock_stations(params: dict[str, Any]) -> dict[str, Any]:
    """Unlocks fast-travel stations: {"id": ...}, {"ids": [...]} or {"all": true}. Locking again isn't supported."""
    visited = params.get("visited", True)
    if not isinstance(visited, bool):
        raise WorldError(400, "'visited' must be true or false")
    if not visited:
        raise WorldError(501, "locking stations again isn't supported: the game has no function to un-discover a station")
    owners = _owners()
    defs = _station_defs()
    if not defs:
        raise WorldError(501, "no fast-travel station definitions were found in this game build - run the world dump")
    by_id = {_path(d): d for d in defs}
    if params.get("all") is True:
        wanted = list(by_id)
    else:
        wanted = _ids(params)
        missing = [i for i in wanted if i not in by_id]
        if missing:
            raise WorldError(404, f"unknown station: {missing[0]}")
    before = sum(1 for i in wanted if _station_state(owners, by_id[i]) is True)
    vias: set[str] = set()
    failed: list[str] = []
    for station_id in wanted:
        definition = by_id[station_id]
        if _station_state(owners, definition) is True:
            continue
        via = _write_station(owners, definition)
        if via:
            vias.add(via)
        else:
            failed.append(station_id)
    after = sum(1 for i in wanted if _station_state(owners, by_id[i]) is True)
    applied = {
        "what": "stations", "requested": "unlock", "count": len(wanted), "failed": failed[:20],
        "via": sorted(vias), "before": before, "after": after,
    }
    if len(wanted) == 1:
        applied["name"] = _station_row(owners, by_id[wanted[0]])["name"]
    logging.info(f"RTSE: world {applied}")
    return {"stations": _section(_stations_section, owners), "applied": applied}


# ---------- read ----------

SECTIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "playthrough": _playthrough_section,
    "missions": _missions_section,
    "challenges": _challenges_section,
    "stations": _stations_section,
}


def read(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """All four sections, or just one with {"section": "missions"}. Each one succeeds or fails on its own."""
    owners = _owners()
    only = (params or {}).get("section")
    if only is not None and only not in SECTIONS:
        raise WorldError(400, f"unknown section (use {', '.join(SECTIONS)})")
    return {name: _section(build, owners) for name, build in SECTIONS.items() if only in (None, name)}


# ---------- debug ----------


def _fields(obj: Any) -> list[Any]:
    try:
        return list(obj.Class._fields() if isinstance(obj, unreal.UObject) else obj._type._fields())  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return []


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, unreal.UObject):
        return _path(value)
    if isinstance(value, tuple):  # a call with out parameters: (return value, *outs)
        return [_scalar(v) for v in value]
    if hasattr(value, "name") and isinstance(getattr(value, "name"), str):  # an enum member
        return f"{type(value).__name__}.{value.name} ({int(value) if hasattr(value, '__int__') else '?'})"
    try:
        return f"<{type(value).__name__} x{len(value)}>"
    except Exception:  # noqa: BLE001
        return f"<{type(value).__name__}>"


def _describe(obj: Any, keywords: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Class name plus each field's kind and value. With keywords, only fields whose name contains one."""
    if obj is None:
        return {"value": None}
    out: dict[str, Any] = {}
    for prop in _fields(obj):
        name = getattr(prop, "Name", None)
        if not name or (keywords and not any(word in name.lower() for word in keywords)):
            continue
        entry: dict[str, Any] = {"kind": type(prop).__name__}
        try:
            entry["value"] = _scalar(getattr(obj, name))
        except Exception as ex:  # noqa: BLE001
            entry["value"] = f"<error {ex!r}>"
        out[name] = entry
    cls = getattr(getattr(obj, "Class", None), "Name", None)
    return {"class": cls or type(obj).__name__, "path": _path(obj) if isinstance(obj, unreal.UObject) else None, "fields": out}


def _guard(build: Callable[[], Any]) -> Any:
    try:
        return build()
    except Exception as ex:  # noqa: BLE001
        return f"<error {ex!r}>"


def debug_dump() -> dict[str, Any]:
    """Saves the real values behind every section, with samples, to rtse/debug_world.json."""
    owners = _owners()
    pc = owners["pc"]
    out: dict[str, Any] = {
        "owners": {key: _path(obj) for key, obj in owners.items()},
        "missing_owners": [key for key in OWNER_PATHS if key not in owners],
        "first_raw_assumed": FIRST_RAW,
    }
    out["gri_fields"] = _guard(lambda: _describe(owners.get("gri"), ("playthrough", "missiontracker")))
    out["pc_matching_fields"] = _guard(lambda: _describe(pc, ("mission", "challenge", "station", "teleport", "playthrough")))

    def playthrough() -> dict[str, Any]:
        reads: dict[str, Any] = {}
        for owner, function in PLAYTHROUGH_READERS:
            reads[f"{owner}.{function}()"] = _guard(lambda o=owner, f=function: _scalar(_call(owners[o], f)))
        for owner, attribute in PLAYTHROUGH_FIELDS:
            reads[f"{owner}.{attribute}"] = _guard(lambda o=owner, a=attribute: _scalar(getattr(owners[o], a)))
        reads["gri.PlaythroughOverride"] = _guard(lambda: _scalar(owners["gri"].PlaythroughOverride))
        reads["pc.IsPlaythroughComplete(0, 1, 2)"] = _guard(
            lambda: [_scalar(_call(pc, "IsPlaythroughComplete", {"PlayThroughNumber": n}, (n,))) for n in range(3)])
        return {"candidates": reads, "chosen": _read_playthrough(owners), "first_raw_assumed": FIRST_RAW}

    def missions() -> dict[str, Any]:
        tracker = owners.get("missions")
        groups = _items(getattr(pc, "MissionPlaythroughs", None))
        data: dict[str, Any] = {
            "tracker_found": tracker is not None,
            "tracker_mission_count": _guard(lambda: len(_items(tracker.MissionList))) if tracker is not None else None,
            "tracker_active_mission": _guard(lambda: _scalar(tracker.ActiveMission)) if tracker is not None else None,
            "playthrough_groups": len(groups),
        }
        samples = []
        for group in groups[:4]:
            entries = _items(getattr(group, "MissionList", None))
            samples.append({
                "PlayThroughNumber": _guard(lambda g=group: _scalar(g.PlayThroughNumber)),
                "ActiveMission": _guard(lambda g=group: _scalar(g.ActiveMission)),
                "FilteredMissions": _guard(lambda g=group: _scalar(g.FilteredMissions)),
                "MissionList": f"{len(entries)} entries",
                "first_entries": [_guard(lambda e=e: {
                    "MissionDef": _scalar(e.MissionDef), "Status": _scalar(e.Status), "bFiltered": _scalar(e.bFiltered),
                    "bInitialized": _scalar(e.bInitialized),
                    "tracker.GetMissionStatus": _scalar(_call(tracker, "GetMissionStatus", {"InMission": e.MissionDef}, (e.MissionDef,))) if tracker is not None else None,
                }) for e in entries[:5]],
            })
        data["groups"] = samples
        try:
            refs, source = _mission_refs(owners)
            data.update({
                "source": source, "count": len(refs),
                "per_playthrough": {str(pt): sum(1 for r in refs if r.pt == pt) for pt in sorted({r.pt for r in refs})},
                "status_counts": _counts(refs), "sample_rows": [_mission_row(r) for r in refs[:8]],
            })
        except _Unavailable as ex:
            data["source"] = str(ex)
        return data

    def challenges() -> dict[str, Any]:
        cache = _items(getattr(pc, "LocalChallengeDataCache", None))
        sample = []
        for entry in cache[:8]:
            definition = _first(entry, CHALLENGE_DEF)[0]
            if definition is None:
                sample.append({"entry": _guard(lambda e=entry: _describe(e))})
                continue
            sample.append({
                "id": _path(definition),
                "name": _guard(lambda d=definition: _scalar(d.ChallengeName)),
                "GoalValue": _guard(lambda d=definition: _scalar(d.GoalValue)),
                "levels": _guard(lambda d=definition: len(_items(d.Levels))),
                "IsChallengeComplete": _guard(lambda d=definition: _scalar(_call(pc, "IsChallengeComplete", {"ChalDef": d}, (d,)))),
                "GetChallengeTotalProgress (return, current, target)": _guard(lambda d=definition: _scalar(_call(pc, "GetChallengeTotalProgress", {"ChalDef": d}, (d,)))),
                "GetCurrentChallengeLevel": _guard(lambda d=definition: _scalar(_call(pc, "GetCurrentChallengeLevel", {"ChallengeDef": d}, (d,)))),
                "GetHighestChallengeLevelComplete": _guard(lambda d=definition: _scalar(_call(pc, "GetHighestChallengeLevelComplete", {"ChalDef": d}, (d,)))),
                "PlayerHasChallenge": _guard(lambda d=definition: _scalar(_call(pc, "PlayerHasChallenge", {"ChalDef": d}, (d,)))),
            })
        return {
            "LocalChallengeDataCache": len(cache), "TrackedChallenges": _guard(lambda: len(_items(pc.TrackedChallenges))),
            "sample": sample, "sample_rows": [_challenge_row(owners, r) for r in _challenge_refs(owners)[0][:8]],
        }

    def stations() -> dict[str, Any]:
        defs = _station_defs()
        counts = {}
        for class_name in (*STATION_CLASSES, "TravelStationDefinition", "LevelTravelStationDefinition"):
            counts[class_name] = _guard(lambda c=class_name: sum(1 for _ in find_all(c)))
        return {
            "class_counts": counts, "defs_found": len(defs), "sample_defs": [_describe(d) for d in defs[:3]],
            "IsStationDiscovered (first 8)": [
                {"id": _path(d), "raw": _guard(lambda d=d: _scalar(_call(pc, "IsStationDiscovered", {"StationDefinition": d}, (d,))))} for d in defs[:8]],
            "LastVisitedTeleporter": _guard(lambda: _scalar(pc.LastVisitedTeleporter)),
            "sample_rows": [_station_row(owners, d) for d in defs[:8]],
        }

    out["playthrough"] = _guard(playthrough)
    out["missions"] = _guard(missions)
    out["challenges"] = _guard(challenges)
    out["stations"] = _guard(stations)
    DEBUG_WORLD_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {
        "saved_to": str(DEBUG_WORLD_FILE),
        "sections": {name: not (isinstance(out[name], str) and out[name].startswith("<error")) for name in SECTIONS},
    }
