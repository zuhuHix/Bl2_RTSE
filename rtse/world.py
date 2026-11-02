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
from . import journal
from .routing import Route

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
# Objectives. Real: MissionDefinition.ObjectiveDefs (every MissionObjectiveDefinition of a mission, each with ObjectiveCount),
# .ObjectiveSetDefs (the sets, each with ObjectiveDefinitions) and the stored entry's ObjectivesProgress (an array of numbers) and
# ActiveObjectiveSet. A GUESS: that ObjectivesProgress[i] belongs to ObjectiveDefs[i]. Each answer says how many numbers the game
# holds against how many objectives the mission has, so a mismatch is visible. Whether writing a number makes the mission log / HUD
# notice is also unknown; the number is read back after every write.
OBJECTIVE_DEFS = ("ObjectiveDefs",)
OBJECTIVE_SET_DEFS = ("ObjectiveSetDefs",)
OBJECTIVE_SET_OBJECTIVES = ("ObjectiveDefinitions",)
OBJECTIVE_PROGRESS = ("ObjectivesProgress",)
OBJECTIVE_ACTIVE_SET = ("ActiveObjectiveSet",)
OBJECTIVE_COUNT = ("ObjectiveCount",)
OBJECTIVE_OPTIONAL = ("bObjectiveIsOptional",)
OBJECTIVE_NAME_CALL = "GetObjectiveName"
MAX_OBJECTIVE_PROGRESS = 9999
TRACK_SETTERS = (("missions", "SetActiveMission"),)  # (InMission, bFromActivation, WillowPC)

# Package prefixes of DLC missions (guesses from memory of the game's package names).
DLC_PACKAGES = (
    ("gd_aster", "Tiny Tina's Assault on Dragon Keep"), ("gd_sage", "Hammerlock's Big Game Hunt"),
    ("gd_iris", "Mr. Torgue's Campaign of Carnage"), ("gd_orchid", "Captain Scarlett's Pirate Booty"),
    ("gd_anemone", "Headhunter Pack"), ("gd_lilac", "Psycho Pack"), ("gd_dandelion", "Other DLC"),
)

# ---------- mission objectives ----------


def _find_mission(owners: dict[str, Any], params: dict[str, Any]) -> _Mission:
    target = params.get("id")
    if not isinstance(target, str) or not target:
        raise WorldError(400, "send 'id' (the mission's id)")
    targets, _pt = _mission_targets(owners, params, [target])
    return targets[0]


def _objective_label(definition: Any, index: int) -> str:
    try:
        text = getattr(definition, OBJECTIVE_NAME_CALL)()
        if isinstance(text, tuple):
            text = text[0]
        text = _text(text)
        if text:
            return text
    except Exception:  # noqa: BLE001, S110
        pass
    return _clean(_path(definition)) or f"Objective {index + 1}"


def _progress_list(ref: _Mission) -> list[int] | None:
    raw, _name = _first(ref.entry, OBJECTIVE_PROGRESS)
    if raw is None:
        return None
    try:
        return [int(v) for v in raw]
    except Exception:  # noqa: BLE001
        return None


def _objectives_view(owners: dict[str, Any], ref: _Mission) -> dict[str, Any]:
    defs, _name = _first(ref.definition, OBJECTIVE_DEFS)
    objectives = _items(defs)
    progress = _progress_list(ref)
    index_of = {_path(o): i for i, o in enumerate(objectives)}
    active, _name = _first(ref.entry, OBJECTIVE_ACTIVE_SET)
    active_id = _path(active) if active is not None else None
    sets = []
    set_defs, _name = _first(ref.definition, OBJECTIVE_SET_DEFS)
    for set_def in _items(set_defs):
        members, _name = _first(set_def, OBJECTIVE_SET_OBJECTIVES)
        sets.append({
            "id": _path(set_def), "name": _clean(_path(set_def)), "active": _path(set_def) == active_id,
            "objectives": [index_of[_path(m)] for m in _items(members) if _path(m) in index_of],
        })
    rows = []
    for i, definition in enumerate(objectives):
        target, _name = _first(definition, OBJECTIVE_COUNT)
        optional, _name = _first(definition, OBJECTIVE_OPTIONAL)
        rows.append({
            "index": i, "id": _path(definition), "name": _objective_label(definition, i),
            "target": int(target) if isinstance(target, int) else None, "optional": optional is True,
            "progress": progress[i] if progress is not None and i < len(progress) else None,
        })
    tracker = owners.get("missions")
    tracked = None
    if tracker is not None:
        active_mission, _name = _first(tracker, ("ActiveMission",))
        tracked = active_mission is not None and _path(active_mission) == ref.id
    row = _mission_row(ref)
    note = None
    if progress is not None and len(progress) != len(rows):
        note = f"The game holds {len(progress)} progress numbers for {len(rows)} objectives, so the pairing below is a guess."
    return {
        "mission": {"id": ref.id, "name": row["name"], "playthrough": ref.pt, "status": row["status"]},
        "objectives": rows, "sets": sets, "active_set": active_id, "tracked": tracked,
        "progress_len": None if progress is None else len(progress), "note": note,
    }


def mission_objectives(params: dict[str, Any]) -> dict[str, Any]:
    owners = _owners()
    return _objectives_view(owners, _find_mission(owners, params))


def set_objective(params: dict[str, Any]) -> dict[str, Any]:
    """Sets one objective's progress number (for example 2 of a 4-part objective)."""
    owners = _owners()
    ref = _find_mission(owners, params)
    before = _objectives_view(owners, ref)
    index = _whole(params.get("index"), 0, 10_000, "index")
    value = _whole(params.get("progress"), 0, MAX_OBJECTIVE_PROGRESS, "progress")
    if index >= len(before["objectives"]):
        raise WorldError(404, f"this mission has {len(before['objectives'])} objectives")
    array, _name = _first(ref.entry, OBJECTIVE_PROGRESS)
    if array is None:
        raise WorldError(501, "this game build keeps no objective progress for this mission - run the world dump")
    try:
        while len(array) <= index:  # a mission that never started may hold fewer numbers than it has objectives
            array.append(0)
        array[index] = value
    except Exception as ex:  # noqa: BLE001
        raise WorldError(501, f"the game refused the write ({type(ex).__name__}: {ex}) - run the world dump") from None
    after = _objectives_view(owners, ref)
    applied = {
        "what": "objective", "mission": before["mission"]["name"], "objective": before["objectives"][index]["name"],
        "index": index, "requested": value, "before": before["objectives"][index]["progress"],
        "after": after["objectives"][index]["progress"], "via": f"entry.{OBJECTIVE_PROGRESS[0]}[{index}]",
    }
    logging.info(f"RTSE: world {applied}")
    journal.record("world.objective", target=f"{ref.id}#{index}", before=applied["before"], after=applied["after"])
    return {**after, "applied": applied}


def set_objective_set(params: dict[str, Any]) -> dict[str, Any]:
    """Moves a mission to another of its objective sets (a later or earlier stage of the mission)."""
    owners = _owners()
    ref = _find_mission(owners, params)
    before = _objectives_view(owners, ref)
    wanted = params.get("set")
    set_defs, _name = _first(ref.definition, OBJECTIVE_SET_DEFS)
    match = next((d for d in _items(set_defs) if _path(d) == wanted), None)
    if match is None:
        raise WorldError(404, "this mission has no objective set like that")
    try:
        setattr(ref.entry, OBJECTIVE_ACTIVE_SET[0], match)
    except Exception as ex:  # noqa: BLE001
        raise WorldError(501, f"the game refused the write ({type(ex).__name__}: {ex}) - run the world dump") from None
    after = _objectives_view(owners, ref)
    applied = {
        "what": "objective set", "mission": before["mission"]["name"], "requested": wanted, "before": before["active_set"],
        "after": after["active_set"], "via": f"entry.{OBJECTIVE_ACTIVE_SET[0]}",
    }
    logging.info(f"RTSE: world {applied}")
    journal.record("world.objective_set", target=ref.id, before=applied["before"], after=applied["after"])
    return {**after, "applied": applied}


def track_mission(params: dict[str, Any]) -> dict[str, Any]:
    """Makes this the mission shown on the HUD (the active mission) - current playthrough only."""
    owners = _owners()
    ref = _find_mission(owners, params)
    current = _current_playthrough(owners)
    if current is not None and ref.pt != current:
        raise WorldError(400, "only a mission of the playthrough you're in can be tracked")
    before = _objectives_view(owners, ref)
    called = None
    for owner, function in TRACK_SETTERS:
        target = owners.get(owner)
        if target is None:
            continue
        try:
            _call(target, function, {"InMission": ref.definition, "bFromActivation": False, "WillowPC": owners["pc"]}, (ref.definition, False, owners["pc"]))
            called = f"{owner}.{function}()"
            break
        except Exception:  # noqa: BLE001, S112
            continue
    if called is None:
        raise WorldError(501, "this game build has no way to change the tracked mission - run the world dump")
    after = _objectives_view(owners, ref)
    applied = {"what": "tracked mission", "mission": before["mission"]["name"], "requested": True, "before": before["tracked"], "after": after["tracked"], "via": called}
    logging.info(f"RTSE: world {applied}")
    journal.record("world.track", target=ref.id, before=applied["before"], after=applied["after"])
    return {**after, "applied": applied}


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
    journal.record("world.playthrough", target="playthrough", before=applied["before"], after=applied["after"])
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


def _record_missions(kind: str, targets: list[_Mission], result: dict[str, Any]) -> None:
    """Journals a mission write, but only when the game state actually changed."""
    applied = result["applied"]
    if applied["changed"]:
        journal.record(kind, target=[ref.id for ref in targets], before=applied["before"], after=applied["after"])


def set_mission(params: dict[str, Any]) -> dict[str, Any]:
    _require_confirm(params, "changing mission state")
    key = params.get("status")
    if key not in WRITABLE_STATUSES:
        raise WorldError(400, f"status must be one of {', '.join(WRITABLE_STATUSES)}")
    wanted = _ids(params)
    owners = _owners()
    targets, pt = _mission_targets(owners, params, wanted)
    result = _apply_missions(owners, targets, pt, key)
    _record_missions("world.mission", targets, result)
    return result


def reset_missions(params: dict[str, Any]) -> dict[str, Any]:
    """Puts missions back to 'not started': the listed ids, or every mission in the playthrough with {"all": true}."""
    _require_confirm(params, "resetting missions")
    wanted = None if params.get("all") is True else _ids(params)
    owners = _owners()
    targets, pt = _mission_targets(owners, params, wanted)
    result = _apply_missions(owners, targets, pt, "not_started")
    _record_missions("world.missions_reset", targets, result)
    return result


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
    if vias:
        journal.record("world.challenge", target=wanted, before=applied["before"], after=applied["after"])
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
    if vias:
        journal.record("world.stations", target=wanted, before=applied["before"], after=applied["after"])
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


# ---------- mission snapshots (used by farmguard.py) ----------
#
# A mission's stored state lives in two places for the playthrough you're in: the MissionTracker's MissionList (the
# live world state) and pc.MissionPlaythroughs[current].MissionList (the copy the save is written from). Both hold
# IMission.MissionData {MissionDef, Status, ObjectivesProgress, ActiveObjectiveSet, SubObjectiveSets, ...} (names
# checked with dev/class_index.py). A snapshot reads both; a restore writes both. UNVERIFIED: that writing these
# fields is enough for the game to forget the progress (rather than only the HUD), and that the save picks up the
# restored tracker values.
#
# Fail closed: no snapshot and no restore unless the current playthrough AND the save are identified. The save
# identity is pc.SaveGameFileId (the save file's number) + pc.PlayerClass (path) + the character name, plus the
# cached PlayerSaveGame's SaveGuid when it can be read. A restore resolves every object first (nothing is written if
# one is missing), and if a write raises or the read-back doesn't match, every entry is put back to what it held just
# before the restore, and the restore fails with the reason.

SNAPSHOT_SOURCES = ("tracker", "saved")
SNAPSHOT_SET_CLASSES = ("MissionObjectiveSetDefinition", "MissionObjectiveSetCollectionDefinition", "MissionObjectiveSetBranchingDefinition")
SAVE_ID_FIELDS = (("pc", "SaveGameFileId"), ("pc", "SaveGameName"), ("pc", "PlayerClass"))  # Int, Str, Object on WillowPlayerController
SNAPSHOT_FIELDS = ("status", "progress", "active_set", "sub_sets")


def save_identity(owners: dict[str, Any] | None = None) -> dict[str, Any]:
    """{"save_id", "save_name", "player_class", "character", "save_guid"} of the save being played; values may be None.

    save_id (pc.SaveGameFileId), player_class and character are required to identify a save; save_guid is extra.
    """
    owners = owners or _owners()
    pc = owners["pc"]
    out: dict[str, Any] = {"save_id": None, "save_name": None, "player_class": None, "character": None, "save_guid": None}
    try:
        value = pc.SaveGameFileId
        out["save_id"] = int(value) if value is not None and int(value) >= 0 else None
    except Exception:  # noqa: BLE001, S110
        pass
    out["save_name"] = _text(_first(pc, ("SaveGameName",))[0])
    player_class, _name = _first(pc, ("PlayerClass",))
    out["player_class"] = _path(player_class) if player_class is not None else None
    out["character"] = _text(_first(_first(pc, ("PlayerReplicationInfo",))[0], ("PlayerName",))[0])
    try:
        save = pc.GetCachedSaveGame()
        guid = save.SaveGuid if save is not None else None
        if guid is not None:
            text = "-".join(f"{int(getattr(guid, part)) & 0xFFFFFFFF:08X}" for part in ("A", "B", "C", "D"))
            out["save_guid"] = text if text != "00000000-00000000-00000000-00000000" else None
    except Exception:  # noqa: BLE001, S110
        pass
    return out


def _identity_problem(identity: dict[str, Any]) -> str | None:
    missing = [label for key, label in (("save_id", "save file number"), ("player_class", "class"), ("character", "character name"))
               if identity.get(key) in (None, "")]
    if missing:
        return f"the save can't be identified (no {', '.join(missing)}), so RTSE won't snapshot or restore missions"
    return None


def _same_save(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Every identity field both sides know must match (and the required ones are known on both, see _identity_problem)."""
    return all(a.get(key) == b.get(key) for key in ("save_id", "save_name", "player_class", "character", "save_guid")
               if a.get(key) is not None and b.get(key) is not None)


def _known_playthrough(owners: dict[str, Any]) -> int:
    current = _current_playthrough(owners)
    if current is None:
        raise WorldError(409, "the current playthrough can't be read, so RTSE won't snapshot or restore missions")
    return current


def _snapshot_entries(owners: dict[str, Any], mission_id: str) -> dict[str, Any]:
    """{"tracker": entry, "saved": entry} for mission_id in the current playthrough (a source is missing if not found).

    The saved copy is only looked up when the current playthrough is known (never "the first one")."""
    found: dict[str, Any] = {}
    tracker = owners.get("missions")
    for entry in _items(_first(tracker, MISSION_FLAT_LISTS[0][1:])[0]) if tracker is not None else []:
        definition, _name = _first(entry, MISSION_DEF)
        if definition is not None and _path(definition) == mission_id:
            found["tracker"] = entry
            break
    current = _current_playthrough(owners)
    if current is None:
        return found
    for group in _items(_first(owners.get("pc"), (MISSION_PLAYTHROUGH_LISTS[0][1],))[0]):
        number, _name = _first(group, MISSION_PT_NUMBER)
        if not isinstance(number, int) or number - FIRST_RAW + 1 != current:
            continue
        for entry in _items(_first(group, MISSION_GROUP_ENTRIES)[0]):
            definition, _name = _first(entry, MISSION_DEF)
            if definition is not None and _path(definition) == mission_id:
                found["saved"] = entry
                break
        break
    return found


def _entry_state(entry: Any) -> dict[str, Any]:
    """One MissionData entry as plain JSON: status, objective numbers, active set and sub-sets (paths)."""
    state: dict[str, Any] = {"status": "unknown", "status_raw": None, "progress": None, "active_set": None, "sub_sets": None}
    try:
        raw = getattr(entry, MISSION_STATUS[0])
        state["status"] = _status_key(raw)
        state["status_raw"] = int(raw)
    except Exception:  # noqa: BLE001, S110
        pass
    raw_progress, _name = _first(entry, OBJECTIVE_PROGRESS)
    if raw_progress is not None:
        try:
            state["progress"] = [int(v) for v in raw_progress]
        except Exception:  # noqa: BLE001, S110
            pass
    active, _name = _first(entry, OBJECTIVE_ACTIVE_SET)
    state["active_set"] = _path(active) if active is not None else None
    subs, name = _first(entry, ("SubObjectiveSets",))
    if name is not None:
        state["sub_sets"] = [_path(s) for s in _items(subs)]
    return state


def mission_snapshot(mission_id: str) -> dict[str, Any]:
    """The mission's full stored state in the current playthrough, as JSON (see the section comment).

    {"mission", "name", "playthrough", "character", "identity": save_identity(), "entries": {"tracker": state | None,
     "saved": state | None}, "set_classes": {set path: class name}}. Raises WorldError 409 when the playthrough or the
    save can't be identified, 404 when the mission isn't in this playthrough.
    """
    owners = _owners()
    _known_playthrough(owners)
    identity = save_identity(owners)
    problem = _identity_problem(identity)
    if problem:
        raise WorldError(409, problem)
    entries = _snapshot_entries(owners, mission_id)
    if not entries:
        raise WorldError(404, "that mission isn't in this playthrough's mission list (is its DLC installed?)")
    return _build_snapshot(owners, mission_id, entries, identity)


def _build_snapshot(owners: dict[str, Any], mission_id: str, entries: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    entry = entries.get("tracker", entries.get("saved"))
    definition, _name = _first(entry, MISSION_DEF)
    name, _field = _first(definition, MISSION_NAME)
    set_classes: dict[str, str] = {}
    for source_entry in entries.values():
        objects = [_first(source_entry, OBJECTIVE_ACTIVE_SET)[0], *_items(_first(source_entry, ("SubObjectiveSets",))[0])]
        for obj in objects:
            if obj is not None:
                set_classes[_path(obj)] = getattr(getattr(obj, "Class", None), "Name", None) or type(obj).__name__
    return {
        "mission": mission_id, "name": _text(name) or _clean(mission_id), "playthrough": _current_playthrough(owners),
        "character": identity.get("character"), "identity": identity,
        "entries": {source: _entry_state(entries[source]) if source in entries else None for source in SNAPSHOT_SOURCES},
        "set_classes": set_classes,
    }


def mission_state(mission_id: str) -> dict[str, Any] | None:
    """The live state (the tracker's entry, else the saved one) in the same shape as a snapshot entry, or None."""
    try:
        entries = _snapshot_entries(_owners(), mission_id)
    except WorldError:
        return None
    entry = entries.get("tracker", entries.get("saved"))
    return _entry_state(entry) if entry is not None else None


def _resolve_set(definition: Any, path: str | None, classes: dict[str, str]) -> Any:
    """The objective-set object behind a snapshot path: one of the mission's own sets, else found by class and path."""
    if path is None:
        return None
    for set_def in _items(_first(definition, OBJECTIVE_SET_DEFS)[0]):
        if _path(set_def) == path:
            return set_def
    for class_name in dict.fromkeys((classes.get(path), *SNAPSHOT_SET_CLASSES)):
        if not class_name:
            continue
        try:
            from unrealsdk import find_object  # noqa: PLC0415

            found = find_object(class_name, path)
        except Exception:  # noqa: BLE001
            found = None
        if found is not None:
            return found
    raise WorldError(409, f"the objective set {path} can't be found in the game any more - take a new snapshot")


def _write_progress(entry: Any, values: list[int]) -> None:
    try:
        setattr(entry, OBJECTIVE_PROGRESS[0], list(values))
        return
    except Exception:  # noqa: BLE001, S110
        pass
    array = getattr(entry, OBJECTIVE_PROGRESS[0])  # element by element, if the whole array can't be assigned
    while len(array) > len(values):
        array.pop()
    for i, value in enumerate(values):
        if i < len(array):
            array[i] = value
        else:
            array.append(value)


class _Plan(NamedTuple):
    """What to write into one entry. Objects are already resolved; a field left as None is not written."""

    source: str
    entry: Any
    progress: list[int] | None
    set_active: bool
    active_set: Any
    sub_sets: list[Any] | None
    status: str | None


def _plan_from_snapshot(source: str, entry: Any, want: dict[str, Any], definition: Any, classes: dict[str, str]) -> _Plan:
    """Resolves every reference the snapshot names (raises WorldError before anything is written)."""
    progress = want.get("progress")
    sub_sets = want.get("sub_sets")
    status = want.get("status")
    return _Plan(
        source, entry, list(progress) if progress is not None else None, "active_set" in want,
        _resolve_set(definition, want.get("active_set"), classes) if "active_set" in want else None,
        [_resolve_set(definition, p, classes) for p in sub_sets] if sub_sets is not None else None,
        status if status in STATUS_ORDER else None,
    )


def _plan_from_entry(source: str, entry: Any) -> _Plan:
    """What the entry holds right now, with its live objects (for a rollback; nothing to resolve)."""
    state = _entry_state(entry)
    subs, name = _first(entry, ("SubObjectiveSets",))
    return _Plan(source, entry, state["progress"], True, _first(entry, OBJECTIVE_ACTIVE_SET)[0],
                 list(_items(subs)) if name is not None else None, state["status"] if state["status"] in STATUS_ORDER else None)


def _apply_plan(owners: dict[str, Any], plan: _Plan, definition: Any) -> list[str]:
    """Writes one entry; returns how each field was written. Raises on the first write the game refuses."""
    via: list[str] = []
    entry, source = plan.entry, plan.source
    if plan.progress is not None:
        _write_progress(entry, plan.progress)
        via.append(f"{source}.{OBJECTIVE_PROGRESS[0]}")
    if plan.set_active:
        setattr(entry, OBJECTIVE_ACTIVE_SET[0], plan.active_set)
        via.append(f"{source}.{OBJECTIVE_ACTIVE_SET[0]}")
    if plan.sub_sets is not None:
        setattr(entry, "SubObjectiveSets", list(plan.sub_sets))
        via.append(f"{source}.SubObjectiveSets")
    key = plan.status
    if key is not None and _status_key(getattr(entry, MISSION_STATUS[0])) != key:
        sample = getattr(entry, MISSION_STATUS[0])
        tracker = owners.get("missions")
        if source == "tracker" and tracker is not None:
            try:
                value = _status_value(sample, key)
                _call(tracker, MISSION_SETTERS[0][1], {"InMission": definition, "MissionStatus": value, "WillowPC": owners["pc"]}, (definition, value, owners["pc"]))
                via.append(f"missions.{MISSION_SETTERS[0][1]}()")
            except Exception:  # noqa: BLE001, S110
                pass  # the direct write below still sets it
        if _status_key(getattr(entry, MISSION_STATUS[0])) != key:
            setattr(entry, MISSION_STATUS[0], _status_value(sample, key))
            via.append(f"{source}.{MISSION_STATUS[0]}")
    return via


def _mismatch(entries: dict[str, Any], wanted: dict[str, dict[str, Any]]) -> list[str]:
    out = []
    for source, entry in entries.items():
        state, want = _entry_state(entry), wanted.get(source) or {}
        for field in SNAPSHOT_FIELDS:
            if field in want and want[field] is not None and state.get(field) != want[field]:
                out.append(f"{source}.{field}")
    return out


def _notify(owners: dict[str, Any], definition: Any, via: list[str]) -> None:
    tracker = owners.get("missions")
    if tracker is None:
        return
    try:
        _call(tracker, "OnMissionObjectivesChanged", {"MissionThatChanged": definition}, (definition,))  # HUD / log refresh
        via.append("missions.OnMissionObjectivesChanged()")
    except Exception:  # noqa: BLE001, S110
        pass


def restore_mission_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Puts a mission back to a snapshot taken by mission_snapshot(): all of it, or none of it.

    Refused (409) unless the current playthrough is known and equals the snapshot's, and the save identity matches.
    Every object is resolved before the first write. If a write raises, or the read-back doesn't match the snapshot,
    every entry is rolled back to what it held just before, and WorldError 500 says what failed (and whether the rollback
    worked). On success returns {"mission", "name", "before": {source: state}, "after": {source: state}, "matches": True,
    "mismatch": [], "via": [...]} and journals one world.restore record whose undo puts the pre-restore state back.
    """
    owners = _owners()
    mission_id = snapshot["mission"]
    current = _known_playthrough(owners)
    if snapshot.get("playthrough") != current:
        raise WorldError(409, f"the snapshot is from playthrough {snapshot.get('playthrough')} but you're in playthrough {current}")
    identity = save_identity(owners)
    problem = _identity_problem(identity)
    if problem:
        raise WorldError(409, problem)
    taken = snapshot.get("identity") or {}
    if _identity_problem(taken) or not _same_save(taken, identity):
        who = taken.get("character") or snapshot.get("character") or "another save"
        raise WorldError(409, f"the snapshot belongs to {who} (save {taken.get('save_id')}), not this save - take a new one")
    entries = _snapshot_entries(owners, mission_id)
    if not entries:
        raise WorldError(404, "that mission isn't in this playthrough's mission list any more")
    definition, _name = _first(entries.get("tracker", entries.get("saved")), MISSION_DEF)
    classes = snapshot.get("set_classes") or {}
    wanted_raw = snapshot.get("entries") or {}
    wanted = {source: wanted_raw.get(source) or wanted_raw.get("tracker") or wanted_raw.get("saved") or {} for source in entries}
    # 1. preflight: resolve everything, and capture what's there now (for the rollback and the journal's undo)
    plans = [_plan_from_snapshot(source, entry, wanted[source], definition, classes) for source, entry in entries.items() if wanted[source]]
    rollback = [_plan_from_entry(source, entry) for source, entry in entries.items()]
    prior = _build_snapshot(owners, mission_id, entries, identity)
    before = {source: _entry_state(entry) for source, entry in entries.items()}
    # 2. write; 3. read back. Any failure rolls every entry back.
    via: list[str] = []
    failure = None
    try:
        for plan in plans:
            via += _apply_plan(owners, plan, definition)
    except Exception as ex:  # noqa: BLE001
        failure = f"the game refused a write ({type(ex).__name__}: {ex})"
    if failure is None:
        mismatch = _mismatch(entries, wanted)
        if mismatch:
            failure = f"the game didn't keep {', '.join(mismatch)}"
    if failure is not None:
        rollback_errors = []
        for plan in rollback:
            try:
                _apply_plan(owners, plan, definition)
            except Exception as ex:  # noqa: BLE001
                rollback_errors.append(f"{plan.source} ({type(ex).__name__})")
        left = _mismatch(entries, before)
        _notify(owners, definition, [])
        outcome = ("and putting the mission back as it was ALSO failed for " + ", ".join(rollback_errors + left) + " - run the farm dump"
                   if rollback_errors or left else "so the mission was put back exactly as it was before the undo")
        logging.error(f"RTSE: world restore of {mission_id} failed: {failure}; {outcome}")
        raise WorldError(500, f"undo failed: {failure}, {outcome}")
    _notify(owners, definition, via)
    after = {source: _entry_state(entry) for source, entry in entries.items()}
    name = snapshot.get("name") or _clean(mission_id)
    applied = {"mission": mission_id, "name": name, "before": before, "after": after, "matches": True, "mismatch": [], "via": via}
    logging.info(f"RTSE: world restore {applied}")
    journal.record("world.restore", target=mission_id, before=before, after=after, undo=lambda: restore_mission_snapshot(prior))
    return applied


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = WorldError

GET_ROUTES: dict[str, Route] = {
    "/api/world": read,
    "/api/debug/world": lambda _params: debug_dump(),
    "/api/world/objectives": mission_objectives,
}
POST_ROUTES: dict[str, Route] = {
    "/api/world/mission": set_mission,
    "/api/world/missions/reset": reset_missions,
    "/api/world/objective": set_objective,
    "/api/world/objective_set": set_objective_set,
    "/api/world/track": track_mission,
    "/api/world/challenge": set_challenge,
    "/api/world/stations": unlock_stations,
    "/api/world/playthrough": set_playthrough,
}
