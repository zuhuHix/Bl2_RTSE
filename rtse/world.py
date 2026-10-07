"""World progress: missions, challenges, fast-travel stations and the playthrough (Normal / TVHM / UVHM).

None of the game-side names here have been confirmed in a running game. Every value lists the places it
might live (candidates), they are tried in order, and the result of each write is read back and reported.
Each of the four sections is independent: if the game doesn't expose one, only that section reports
"unavailable" and the others still work. Run the world dump from the web UI to find the real names.
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
FIRST_RAW = 0  # the game's own number for playthrough 1 (guess: it counts from zero)
PLAYTHROUGH_READERS = (("pc", "GetCurrentPlaythrough"), ("game", "GetCurrentPlaythrough"))  # called functions
PLAYTHROUGH_FIELDS = (("pc", "PlayThroughNumber"), ("game", "PlayThroughNumber"), ("pc", "PlaythroughNumber"), ("pc", "CurrentPlaythrough"))
PLAYTHROUGH_SETTERS = (("pc", "SetCurrentPlaythrough"), ("game", "SetCurrentPlaythrough"), ("pc", "SetPlaythrough"))

# Objects hanging off the player controller that might own the data, tried in order. Keys are owner names.
OWNER_PATHS: dict[str, tuple[str, ...]] = {
    "game": ("WorldInfo.Game",),
    "missions": ("MissionTracker", "MissionPlaylist"),
    "challenges": ("ChallengeTracker", "ChallengeHandler"),
}

# ---------- missions ----------

# EMissionStatus in the order the game numbers it (guess), and what we call each state.
STATUS_ORDER = ("not_started", "active", "objectives_done", "ready", "complete", "failed")
STATUS_ENUM_NAMES = {
    "not_started": ("MS_NotStarted",),
    "active": ("MS_Active",),
    "objectives_done": ("MS_RequiredObjectivesComplete",),
    "ready": ("MS_ReadyToTurnIn",),
    "complete": ("MS_Complete", "MS_Completed"),
    "failed": ("MS_Failed",),
}
STATUS_LABELS = {
    "not_started": "Not started", "active": "Active", "objectives_done": "Objectives done",
    "ready": "Ready to turn in", "complete": "Complete", "failed": "Failed", "unknown": "Unknown",
}
WRITABLE_STATUSES = ("not_started", "active", "ready", "complete")
MISSION_PLAYTHROUGH_LISTS = (("pc", "MissionPlaythroughs"), ("missions", "MissionPlaythroughs"))  # one group per playthrough
MISSION_FLAT_LISTS = (("missions", "MissionList"), ("pc", "MissionList"), ("missions", "Missions"), ("missions", "MissionStatusList"))
MISSION_GROUP_ENTRIES = ("MissionList", "Missions")  # the entries inside one playthrough group
MISSION_PT_NUMBER = ("PlayThroughNumber", "PlaythroughNumber")
MISSION_DEF = ("MissionDef", "MissionDefinition", "Mission", "Def")
MISSION_STATUS = ("Status", "MissionStatus", "State")
MISSION_NAME = ("MissionName", "Title", "DisplayName", "MissionTitle")
MISSION_PLOT_FLAG = ("bPlotCritical", "bIsPlotMission")
MISSION_SETTERS = (("missions", "SetMissionStatus"), ("pc", "SetMissionStatus"))  # (definition, status), tried after a direct write
# Package prefixes of DLC missions (guesses from memory of the game's package names).
DLC_PACKAGES = (
    ("gd_aster", "Tiny Tina's Assault on Dragon Keep"), ("gd_sage", "Hammerlock's Big Game Hunt"),
    ("gd_iris", "Mr. Torgue's Campaign of Carnage"), ("gd_orchid", "Captain Scarlett's Pirate Booty"),
    ("gd_anemone", "Headhunter Pack"), ("gd_lilac", "Psycho Pack"), ("gd_dandelion", "Other DLC"),
)

# ---------- challenges ----------

CHALLENGE_LISTS = (
    ("challenges", "ChallengeList"), ("pc", "ChallengeList"), ("challenges", "Challenges"), ("pc", "Challenges"),
    ("challenges", "ChallengeData"), ("pc", "ChallengeDataList"),
)
CHALLENGE_DEF = ("ChallengeDef", "ChallengeDefinition", "Challenge", "Def")
CHALLENGE_PROGRESS = ("Progress", "CurrentValue", "Value", "Count")
CHALLENGE_DONE = ("bCompleted", "bIsComplete", "bComplete", "Completed", "bDone")
CHALLENGE_ENTRY_GOAL = ("Goal", "GoalValue", "Target")
CHALLENGE_NAME = ("ChallengeName", "DisplayName", "Title")
CHALLENGE_DESC = ("ChallengeDescription", "Description")
CHALLENGE_DEF_GOAL = ("Goal", "GoalValue", "TargetValue")
CHALLENGE_LEVELS = ("Levels", "ChallengeLevels")  # the last level's goal is the challenge's goal
CHALLENGE_LEVEL_GOAL = ("Goal", "GoalValue", "LevelGoal", "TargetValue")
CHALLENGE_CATEGORY = ("Category", "ChallengeCategory")

# ---------- fast travel ----------

STATION_CLASSES = ("FastTravelStationDefinition",)
STATION_LISTS = (  # lists of visited stations (strings, objects, or structs holding both)
    ("pc", "VisitedTeleporters"), ("pc", "VisitedFastTravelStations"), ("pc", "FastTravelStationsVisited"),
    ("pc", "ActiveFastTravelStations"), ("pc", "FastTravelStations"),
)
STATION_NAME = ("StationDisplayName", "DisplayName", "StationName", "LevelDisplayName")
STATION_LEVEL = ("LevelName", "StationLevelName", "LevelPackageName")
STATION_FLAGS = ("bHasBeenVisited", "bVisited")  # on the definition itself
STATION_GETTER = ("pc", "GetFastTravelStationVisited")
STATION_SETTER = ("pc", "SetFastTravelStationVisited")
STATION_ENTRY_DEF = ("StationDef", "Station", "FastTravelStation", "Def")
STATION_ENTRY_FLAG = ("bVisited", "bHasBeenVisited", "Visited")


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


def _set_first(obj: Any, names: tuple[str, ...], value: Any) -> str | None:
    """Writes the first of these attributes that exists. Returns its name, or None if none exists."""
    for name in names:
        try:
            getattr(obj, name)  # does it exist?
        except Exception:  # noqa: BLE001, S112
            continue
        setattr(obj, name, value)
        return name
    return None


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
            return int(getattr(target, function)()), f"{owner}.{function}()"
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
    via = None
    for owner, attribute in PLAYTHROUGH_FIELDS:
        obj = owners.get(owner)
        if obj is None or _first(obj, (attribute,))[1] is None:
            continue
        try:
            setattr(obj, attribute, raw_target)
        except Exception:  # noqa: BLE001, S112
            continue
        via = f"{owner}.{attribute}"
        if _read_playthrough(owners)[0] == raw_target:
            break
    else:
        for owner, function in PLAYTHROUGH_SETTERS:
            obj = owners.get(owner)
            if obj is None:
                continue
            try:
                getattr(obj, function)(raw_target)
            except Exception:  # noqa: BLE001, S112
                continue
            via = f"{owner}.{function}()"
            if _read_playthrough(owners)[0] == raw_target:
                break
    if via is None:
        raise WorldError(501, "this game build has no writable playthrough field - run the world dump")
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
            pt = number + 1 if isinstance(number, int) and 0 <= number < len(PLAYTHROUGHS) else position + 1
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


def _write_mission(owners: dict[str, Any], ref: _Mission, key: str) -> str | None:
    """Sets one mission's status; returns how it was done, or None if the readback never matched."""
    try:
        setattr(ref.entry, ref.status_field, _status_value(getattr(ref.entry, ref.status_field), key))
        if _mission_status(ref) == key:
            return f"entry.{ref.status_field}"
    except Exception:  # noqa: BLE001, S110
        pass
    for owner, function in MISSION_SETTERS:
        target = owners.get(owner)
        if target is None:
            continue
        try:
            getattr(target, function)(ref.definition, _status_value(getattr(ref.entry, ref.status_field), key))
        except Exception:  # noqa: BLE001, S112
            continue
        if _mission_status(ref) == key:
            return f"{owner}.{function}()"
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
    vias: set[str] = set()
    failed: list[str] = []
    changed = 0
    for ref in targets:
        if _mission_status(ref) == key:
            continue
        via = _write_mission(owners, ref, key)
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


def _challenge_goal(ref: _Challenge) -> float | None:
    for obj, names in ((ref.entry, CHALLENGE_ENTRY_GOAL), (ref.definition, CHALLENGE_DEF_GOAL)):
        value, _name = _first(obj, names)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    levels, _name = _first(ref.definition, CHALLENGE_LEVELS)
    last = (_items(levels) or [None])[-1]
    value, _name = _first(last, CHALLENGE_LEVEL_GOAL)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _challenge_row(ref: _Challenge) -> dict[str, Any]:
    name, _field = _first(ref.definition, CHALLENGE_NAME)
    desc, _field = _first(ref.definition, CHALLENGE_DESC)
    category, _field = _first(ref.definition, CHALLENGE_CATEGORY)
    progress, _field = _first(ref.entry, CHALLENGE_PROGRESS)
    flag, _field = _first(ref.entry, CHALLENGE_DONE)
    goal = _challenge_goal(ref)
    progress = progress if isinstance(progress, (int, float)) and not isinstance(progress, bool) else None
    if isinstance(flag, bool):
        done = flag
    else:
        done = None if progress is None or not goal else progress >= goal
    package = _clean(ref.id.split(".")[0].removeprefix("GD_")) if "." in ref.id else ""
    group = _text(category) or package or "Challenges"
    return {
        "id": ref.id, "name": _text(name) or _clean(ref.id), "desc": _text(desc) or "", "group": group,
        "progress": progress, "goal": goal, "done": done,
    }


def _challenges_section(owners: dict[str, Any]) -> dict[str, Any]:
    refs, source = _challenge_refs(owners)
    return {"rows": [_challenge_row(ref) for ref in refs], "source": source}


def _write_challenge(ref: _Challenge, complete: bool) -> list[str]:
    """Marks a challenge done (or not). Returns the fields written; empty if it has none we know."""
    written: list[str] = []
    if name := _set_first(ref.entry, CHALLENGE_DONE, complete):
        written.append(name)
    progress, name = _first(ref.entry, CHALLENGE_PROGRESS)
    goal = _challenge_goal(ref)
    if name and isinstance(progress, (int, float)) and (goal is not None or not complete):
        value = goal if complete else 0
        setattr(ref.entry, name, type(progress)(value))
        written.append(name)
    return written


def set_challenge(params: dict[str, Any]) -> dict[str, Any]:
    action = params.get("action")
    if action not in ("complete", "reset"):
        raise WorldError(400, "action must be 'complete' or 'reset'")
    if action == "reset":
        _require_confirm(params, "resetting challenges")
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
    want_done = action == "complete"
    before = [_challenge_row(known[i]) for i in wanted]
    fields: set[str] = set()
    failed: list[str] = []
    for challenge_id in wanted:
        ref = known[challenge_id]
        try:
            written = _write_challenge(ref, want_done)
        except Exception:  # noqa: BLE001
            written = []
        fields.update(written)
        row = _challenge_row(ref)
        ok = row["done"] is True if want_done else (row["done"] is False or row["progress"] == 0)
        if not written or not ok:
            failed.append(challenge_id)
    after = [_challenge_row(known[i]) for i in wanted]
    one = len(wanted) == 1
    applied = {
        "what": "challenge" if one else "challenges", "requested": action, "count": len(wanted), "failed": failed[:20],
        "via": sorted(f"entry.{f}" for f in fields),
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


def _station_keys(definition: Any) -> set[str]:
    """Every string the game might use to refer to this station in a list of visited ones."""
    keys = {_path(definition).lower()}
    for names in (("Name",), STATION_NAME, STATION_LEVEL):
        value, _name = _first(definition, names)
        if value is not None:
            keys.add(str(value).lower())
    return keys


def _entry_matches(entry: Any, keys: set[str]) -> bool:
    if isinstance(entry, str):
        return entry.lower() in keys
    if isinstance(entry, unreal.UObject):
        return _path(entry).lower() in keys
    target, _name = _first(entry, STATION_ENTRY_DEF)
    return target is not None and _path(target).lower() in keys


def _list_state(array: Any, definition: Any) -> bool | None:
    keys = _station_keys(definition)
    try:
        for entry in _items(array):
            if _entry_matches(entry, keys):
                flag, _name = _first(entry, STATION_ENTRY_FLAG)
                return flag if isinstance(flag, bool) else True
        return False
    except Exception:  # noqa: BLE001
        return None


def _station_state(owners: dict[str, Any], definition: Any) -> bool | None:
    """Whether the station is unlocked, asking every way we know. True if any says so; None if none can answer."""
    answers: list[bool] = []
    owner, function = STATION_GETTER
    target = owners.get(owner)
    if target is not None:
        try:
            answers.append(bool(getattr(target, function)(definition)))
        except Exception:  # noqa: BLE001, S110
            pass
    value, _name = _first(definition, STATION_FLAGS)
    if isinstance(value, bool):
        answers.append(value)
    for _source, array in _candidates(owners, STATION_LISTS):
        state = _list_state(array, definition)
        if state is not None:
            answers.append(state)
    if not answers:
        return None
    return any(answers)


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
    return {"rows": rows, "warning": warning}


def _list_write(array: Any, definition: Any, visited: bool) -> bool:
    keys = _station_keys(definition)
    items = _items(array)
    matches = [i for i, entry in enumerate(items) if _entry_matches(entry, keys)]
    if visited:
        if matches:  # already listed; if it carries a visited flag, set it (the readback decides if that was enough)
            for i in matches:
                _set_first(items[i], STATION_ENTRY_FLAG, True)
            return True
        sample = items[0] if items else None
        if isinstance(sample, str):
            array.append(_path(definition) if "." in sample else str(getattr(definition, "Name", _path(definition))))
            return True
        if sample is None or isinstance(sample, unreal.UObject):
            try:
                array.append(definition)
                return True
            except Exception:  # noqa: BLE001
                array.append(str(getattr(definition, "Name", _path(definition))))
                return True
        return False  # a list of structs we can't build
    for i in reversed(matches):
        flag = _set_first(items[i], STATION_ENTRY_FLAG, False)
        if flag is None:
            try:
                array.pop(i)
            except Exception:  # noqa: BLE001
                del array[i]
    return True


def _write_station(owners: dict[str, Any], definition: Any, visited: bool) -> str | None:
    """Unlocks (or re-locks) one station, trying each way in turn until the readback agrees. Returns how, or None."""
    owner, function = STATION_SETTER
    target = owners.get(owner)
    if target is not None:
        try:
            getattr(target, function)(definition, visited)
            if _station_state(owners, definition) == visited:
                return f"{owner}.{function}()"
        except Exception:  # noqa: BLE001, S110
            pass
    if _first(definition, STATION_FLAGS)[1]:
        try:
            _set_first(definition, STATION_FLAGS, visited)
            if _station_state(owners, definition) == visited:
                return f"definition.{_first(definition, STATION_FLAGS)[1]}"
        except Exception:  # noqa: BLE001, S110
            pass
    for source, array in _candidates(owners, STATION_LISTS):
        try:
            if _list_write(array, definition, visited) and _station_state(owners, definition) == visited:
                return source
        except Exception:  # noqa: BLE001, S112
            continue
    return None


def unlock_stations(params: dict[str, Any]) -> dict[str, Any]:
    """Unlocks fast-travel stations: {"id": ...}, {"ids": [...]} or {"all": true}. {"visited": false} locks them again."""
    visited = params.get("visited", True)
    if not isinstance(visited, bool):
        raise WorldError(400, "'visited' must be true or false")
    if not visited:
        _require_confirm(params, "locking stations again")
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
        if _station_state(owners, definition) == visited:
            continue
        via = _write_station(owners, definition, visited)
        if via:
            vias.add(via)
        else:
            failed.append(station_id)
    after = sum(1 for i in wanted if _station_state(owners, by_id[i]) is True)
    applied = {
        "what": "stations", "requested": "unlock" if visited else "lock", "count": len(wanted), "failed": failed[:20],
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


def _sample(items: list[Any], count: int = 4) -> list[dict[str, Any]]:
    out = []
    for entry in items[:count]:
        described = _describe(entry)
        for field, value in list(described["fields"].items()):
            nested = None
            try:
                nested = getattr(entry, field)
            except Exception:  # noqa: BLE001, S112
                continue
            if isinstance(nested, unreal.UObject) and field in {*MISSION_DEF, *CHALLENGE_DEF, *STATION_ENTRY_DEF}:
                value["object"] = _describe(nested)
        out.append(described)
    return out


def _guard(build: Callable[[], Any]) -> Any:
    try:
        return build()
    except Exception as ex:  # noqa: BLE001
        return f"<error {ex!r}>"


def debug_dump() -> dict[str, Any]:
    """Saves the real field names behind every section, with sample entries, to rtse/debug_world.json."""
    owners = _owners()
    words = ("mission", "challenge", "travel", "station", "teleport", "playthrough", "visited")
    out: dict[str, Any] = {"owners": {key: _path(obj) for key, obj in owners.items()}}
    out["matching_fields"] = {key: _guard(lambda o=obj: _describe(o, words)) for key, obj in owners.items() if key in ("pc", "game")}
    out["trackers"] = {key: _guard(lambda o=owners[key]: _describe(o)) for key in ("missions", "challenges") if key in owners}

    def playthrough() -> dict[str, Any]:
        reads: dict[str, Any] = {}
        for owner, function in PLAYTHROUGH_READERS:
            reads[f"{owner}.{function}()"] = _guard(lambda o=owner, f=function: _scalar(getattr(owners[o], f)()))
        for owner, attribute in PLAYTHROUGH_FIELDS:
            reads[f"{owner}.{attribute}"] = _guard(lambda o=owner, a=attribute: _scalar(getattr(owners[o], a)))
        return {"candidates": reads, "chosen": _read_playthrough(owners), "first_raw_assumed": FIRST_RAW}

    def missions() -> dict[str, Any]:
        refs, source = _mission_refs(owners)
        return {
            "source": source, "count": len(refs), "per_playthrough": {str(pt): sum(1 for r in refs if r.pt == pt) for pt in {r.pt for r in refs}},
            "status_counts": _counts(refs), "sample": _sample([r.entry for r in refs]),
            "sample_rows": [_mission_row(r) for r in refs[:8]],
        }

    def challenges() -> dict[str, Any]:
        refs, source = _challenge_refs(owners)
        return {"source": source, "count": len(refs), "sample": _sample([r.entry for r in refs]), "sample_rows": [_challenge_row(r) for r in refs[:8]]}

    def stations() -> dict[str, Any]:
        defs = _station_defs()
        lists = {}
        for source, array in _candidates(owners, STATION_LISTS):
            items = _items(array)
            lists[source] = {"length": len(items), "sample": [_scalar(i) if not _fields(i) or isinstance(i, unreal.UObject) else _describe(i) for i in items[:6]]}
        counts = {}
        for class_name in (*STATION_CLASSES, "LevelTravelStationDefinition", "TeleporterDefinition"):
            counts[class_name] = _guard(lambda c=class_name: sum(1 for _ in find_all(c)))
        return {
            "class_counts": counts, "visited_lists": lists, "defs_found": len(defs), "sample_defs": [_describe(d) for d in defs[:3]],
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
