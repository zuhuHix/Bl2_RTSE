"""Farm guard and farm tracker: protects a farm's mission state, and counts attempts and drops while you farm.

A farm profile (rtse/farm_profiles.json) names one mission whose progress ruins a farm, e.g. Doctor's Orders, where
each recorder picked up lowers the number of the best loot midget spawns in the Wildlife Exploitation Preserve.
Enabling a profile snapshots that mission's full stored state (world.mission_snapshot: Status, ObjectivesProgress,
ActiveObjectiveSet, SubObjectiveSets, playthrough and the save identity: save file number + class + character name,
plus the save GUID when readable). No snapshot or restore happens unless the playthrough and the save are identified.
When the game then changes the mission, an alert says what that costs ("Picking up this recorder would reduce the
midget spawns.") and offers:
  keep         accept the change; the snapshot is retaken from the new state
  undo         put the snapshot back (world.restore_mission_snapshot: all or nothing; a failed or partial write is rolled
               back, the alert stays open with "failed" set to the reason)
  undo_reload  undo, then save-quit and load straight back in (EXPERIMENTAL, off by default; see quick reload)
  dismiss      close an alert about a change that was blocked (nothing to undo)
With the profile's auto_block on (off by default), the change is blocked in a pre-hook instead (the game function
never runs), so there is nothing to undo.
No undo after a turn-in: once ANY current record of the guarded mission (tracker or saved copy) is Complete, its
rewards are already given and can't be taken back, so the alert offers keep only (with "note" saying why) and resolve
refuses undo, checked live. A record whose status can't be read turns undo off too. A mission that is already Complete
can't be snapshotted (enable / retake answer 409: the farm is over), and Keep after a turn-in switches the guard off.

Game hooks (names checked with dev/class_index.py; what they do at runtime is NOT verified in a live game):
  WillowGame.MissionTracker:SetMissionStatus(InMission, MissionStatus, WillowPC)  pre + post
  WillowGame.MissionTracker:UpdateObjective(MissionObjective, ObjectiveBit)      pre + post
  WillowGame.MissionTracker:DecrementObjective(Objective)                        pre + post
  WillowGame.WillowPlayerController:ServerCompleteMission(Mission, MissionDirector)  pre + post (turning a mission in)
  WillowGame.WillowPickup:GiveTo(P, bReady, Waypoint)                           pre + post (anything picked up)
  WillowGame.FrontendGFxMovie:Start                                             post (only for the quick reload)
Hooks run on the game thread for every mission update and pickup, so they stay cheap: a path-name lookup against the
enabled profiles, nothing more unless the mission is guarded. They never raise (errors are logged, rate limited).
install_hooks() first removes this module's hooks, and every hook has the fixed id HOOK_ID, so enabling twice, F8
restarts and mod reloads never leave a hook registered twice. The events below are published whether or not a
profile is enabled; other features subscribe to them.

Events (see rtse/events.py):
  MISSION_CHANGED {"mission": path, "status": status key after the change ("active", "complete", ... or "unknown"),
                   "objective": objective path | None, "change": "status" | "objective" | "decrement" | "complete",
                   "name": readable mission name, "guarded": bool}
  PICKUP          {"item": readable name, "kind": "weapon" | "item" | "mission_item" | "ammo" | "money" | "usable",
                   "amount": None, "definition": definition path | None, "rarity": int | None}

Co-op: only the host's (or a solo) game runs the mission logic. Farm writes and auto-block need a CONFIRMED
NM_Standalone or NM_ListenServer (WorldInfo.NetMode); a client, a dedicated server or an unreadable mode fails closed:
writes refused (423) with the reason, and the hooks don't guard or block anything.

Quick reload (experimental, settings.experimental_reload): restore, then pc.ReturnToTitleScreen(bSkipSave=False) so the
game saves the restored state, then FrontendGFxMovie.LaunchSaveGame(<same playthrough>) once the main menu starts.
That's the pattern of the SDK "Map Reloader" mod. If the relaunch doesn't happen, press Continue yourself. reload_safety()
must confirm a safe state (host, pawn spawned, no loading screen, not waiting for a save id, save manager idle) before
anything is restored, and again on the tick that quits; if it can't, the reload is refused (or skipped, after the undo).

Farm tracker: one session at a time. attempts (+1 by hand, and +1 whenever a fresh map is noticed: WorldInfo.
TimeSeconds going backwards, unverified), a timer, and the matching drops picked up (profile "drops": kinds and
min_rarity, where rarity is the game's RarityLevel: 1 white .. 5 orange, unverified).

Routes:
  GET  /api/farm                      the whole state (profiles, alerts, session, coop, writes, settings, hooks, reload)
  GET  /api/debug/farm                saves rtse/debug_farm.json (hooks, recent hook calls, raw mission data)
  POST /api/farm/enable               {"profile": id, "enabled": bool, "resnapshot"?: bool}
  POST /api/farm/snapshot             {"profile": id}  retake the snapshot from the current state
  POST /api/farm/settings             {"experimental_reload"?: bool, "profile"?: id, "auto_block"?: bool}
  POST /api/farm/resolve              {"alert": id, "action": "keep" | "undo" | "undo_reload" | "dismiss"}
  POST /api/farm/session              {"action": "start" | "stop" | "reset" | "attempt", "profile"?: id}
Every route answers with the same state as GET /api/farm, plus "applied" on writes.
An alert is {"id", "profile", "profile_name", "mission", "mission_name", "change": "objective" | "decrement" | "status" |
"complete" | "pickup", "objective" (path) | None, "objective_name" | None, "item" (a blocked pickup's name) | None,
"blocked", "message", "before", "after" ({"status", "progress", "active_set"} | None), "at", "count", "actions"}.
"""

from __future__ import annotations

import collections
import json
import time
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import hooks as _hooks
from unrealsdk import logging
from unrealsdk.hooks import Type, add_hook, remove_hook

from . import events, gamethread, world
from .routing import Route

PROFILES_FILE = Path(__file__).parent / "farm_profiles.json"
DEBUG_FILE = Path(__file__).parent / "debug_farm.json"
HOOK_ID = "rtse_farmguard"
BLOCK = getattr(_hooks, "Block", None)  # returned from a pre-hook, the game function doesn't run

NM_CLIENT = 3  # ENetMode: NM_Standalone, NM_DedicatedServer, NM_ListenServer, NM_Client
HOST_MODES = (0, 2)  # solo, co-op host: the only modes in which farm guard writes or blocks
NO_UNDO_AFTER_TURN_IN = "Rewards already given can't be taken back, so undo is off once the mission is turned in."
NO_UNDO_UNREADABLE = "RTSE can't read this mission's state in every record right now, so undo is off (it can't rule out a turn-in)."
MAX_ALERTS = 20
MAX_DROPS = 200
MAX_TRACE = 100
RELOAD_TIMEOUT = 180.0  # seconds: a pending quick reload older than this is dropped
ACTIONS = ("keep", "undo", "undo_reload", "dismiss")
KIND_BY_CLASS = {"WillowWeapon": "weapon", "WillowMissionItem": "mission_item", "WillowUsableItem": "usable"}


class FarmError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# ---------- state ----------

_profiles: list[dict[str, Any]] = []
_by_mission: dict[str, dict[str, Any]] = {}  # mission path -> profile, ENABLED profiles only (what the hooks read)
_state: dict[str, dict[str, Any]] = {}  # profile id -> {"enabled", "auto_block", "snapshot", "snapshot_at"}
_alerts: list[dict[str, Any]] = []
_next_alert = 1
_settings = {"experimental_reload": False}
_session: dict[str, Any] = {}
_reload: dict[str, Any] = {"pending": None, "last": None}
_installed: list[tuple[str, Any]] = []
_failed: list[str] = []
_hook_errors: collections.Counter[str] = collections.Counter()
_trace: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX_TRACE)
_pending_pickups: dict[str, dict[str, Any]] = {}
_restoring = False  # our own restore calls SetMissionStatus: the hooks must not react to it
_last_world_time: float | None = None


def _reset_session(profile: str | None = None) -> None:
    _session.clear()
    _session.update({"running": False, "profile": profile, "started": None, "elapsed_before": 0.0, "attempts": 0, "drops": [], "pickups": 0})


_reset_session()


def load_profiles(path: Path = PROFILES_FILE) -> list[dict[str, Any]]:
    """Reads the profile file; keeps any enabled state for profiles that still exist."""
    global _profiles  # noqa: PLW0603
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = [p for p in data.get("profiles", []) if isinstance(p, dict) and p.get("id") and p.get("mission")]
    _profiles = profiles
    for profile in profiles:
        _state.setdefault(profile["id"], {"enabled": False, "auto_block": False, "snapshot": None, "snapshot_at": None})
    for stale in [key for key in _state if key not in {p["id"] for p in profiles}]:
        del _state[stale]
    _rebuild_index()
    return profiles


def _rebuild_index() -> None:
    _by_mission.clear()
    for profile in _profiles:
        if _state[profile["id"]]["enabled"]:
            _by_mission[profile["mission"]] = profile


def _profile(profile_id: Any) -> dict[str, Any]:
    if not _profiles:
        load_profiles()
    for profile in _profiles:
        if profile["id"] == profile_id:
            return profile
    raise FarmError(404, "unknown farm profile")


# ---------- small game helpers (never raise) ----------


def _path(obj: Any) -> str | None:
    if obj is None:
        return None
    try:
        return obj._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return str(obj)


def _mission_of(objective_path: str | None) -> str | None:
    """'GD_X.M_Mission:Objective' -> 'GD_X.M_Mission' (an objective is a sub-object of its mission)."""
    if not objective_path:
        return None
    return objective_path.split(":", 1)[0]


def _net_mode(pc: Any) -> int | None:
    try:
        raw = pc.WorldInfo.NetMode
    except Exception:  # noqa: BLE001
        return None
    name = getattr(raw, "name", None)
    if isinstance(name, str):
        return {"NM_Standalone": 0, "NM_DedicatedServer": 1, "NM_ListenServer": 2, "NM_Client": 3}.get(name)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _mode_problem(mode: int | None) -> str:
    """'' when this game runs the missions (solo or co-op host); otherwise why farm guard is off. Unknown fails closed."""
    if mode in HOST_MODES:
        return ""
    if mode == NM_CLIENT:
        return "you're a client in someone else's co-op game; only the host's game runs the missions, so farm guard is off"
    if mode is None:
        return "RTSE can't tell whether this is your own game or someone else's co-op game (WorldInfo.NetMode unreadable), so farm guard is off"
    return f"this isn't a solo or hosted game (net mode {mode}), so farm guard is off"


def coop_state() -> dict[str, Any]:
    """{"in_game", "net_mode", "client", "host", "players", "reason"}.

    host is True only for a CONFIRMED solo (NM_Standalone) or co-op host (NM_ListenServer) game; farm writes and
    auto-block need it. A client, a dedicated server or an unreadable mode gets host False and a reason."""
    pc = get_pc(possibly_loading=True)
    if pc is None:
        return {"in_game": False, "net_mode": None, "client": False, "host": False, "players": None, "reason": "not in a save"}
    mode = _net_mode(pc)
    players = None
    try:
        players = sum(1 for pri in pc.WorldInfo.GRI.PRIArray if pri is not None)
    except Exception:  # noqa: BLE001, S110
        pass
    reason = _mode_problem(mode)
    return {"in_game": True, "net_mode": mode, "client": mode == NM_CLIENT, "host": not reason, "players": players, "reason": reason}


def _require_writable() -> None:
    coop = coop_state()
    if not coop["in_game"]:
        raise FarmError(409, "not in a save")
    if not coop["host"]:
        raise FarmError(423, coop["reason"])


def _is_host() -> bool:
    """Cheap check for the hooks: True only when the mode is confirmed solo or host."""
    try:
        pc = get_pc(possibly_loading=True)
        return pc is not None and _net_mode(pc) in HOST_MODES
    except Exception:  # noqa: BLE001
        return False


def reload_safety() -> tuple[bool, str]:
    """Whether it's safe to save-quit right now: host, pawn spawned, no loading screen, not saving. Unknown = not safe.

    The save manager's CurrentState is treated as idle when its enum name says Idle/None, or (no name) when it is 0
    (a guess: the enum's values weren't readable offline)."""
    pc = get_pc(possibly_loading=True)
    if pc is None:
        return False, "not in a save"
    problem = _mode_problem(_net_mode(pc))
    if problem:
        return False, problem
    if getattr(pc, "Pawn", None) is None:
        return False, "your character isn't spawned right now (loading, or in Fight For Your Life?)"
    try:
        if pc.IsLoadingMoviePlaying():
            return False, "a loading screen is up"
    except Exception:  # noqa: BLE001
        return False, "RTSE can't tell whether a loading screen is up"
    if getattr(pc, "bWaitingForSaveGameId", None) is not False:
        return False, "the game is still setting up the save (or RTSE can't tell)"
    try:
        state = pc.GetWillowGlobals().GetWillowSaveGameManager().CurrentState
        name = getattr(state, "name", None)
        idle = any(word in name.lower() for word in ("idle", "none")) if isinstance(name, str) else int(state) == 0
    except Exception:  # noqa: BLE001
        return False, "RTSE can't tell whether the game is saving"
    if not idle:
        return False, "the game is saving or loading"
    return True, ""


def _status_now(tracker: Any, mission: Any) -> str:
    try:
        return world._status_key(tracker.GetMissionStatus(mission))  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return "unknown"


def _readable(path: str | None) -> str | None:
    """'GD_X.M_Mission' -> 'Mission'; an objective 'GD_X.M_Mission:CollectNotes' -> 'Collect Notes'."""
    if not path:
        return None
    return world._clean(path.rsplit(":", 1)[-1] if ":" in path else path)  # noqa: SLF001


def _note_world() -> None:
    """Counts an attempt when a fresh map is noticed (the world clock went backwards). Cheap; never raises."""
    global _last_world_time  # noqa: PLW0603
    try:
        pc = get_pc(possibly_loading=True)
        now = float(pc.WorldInfo.TimeSeconds) if pc is not None else None
    except Exception:  # noqa: BLE001
        return
    if now is None:
        return
    if _last_world_time is not None and now + 1.0 < _last_world_time and _session.get("running"):
        _session["attempts"] += 1
    _last_world_time = now


def _hook_error(name: str, ex: BaseException) -> None:
    _hook_errors[name] += 1
    count = _hook_errors[name]
    if count <= 3 or count % 100 == 0:
        logging.error(f"RTSE: farm guard hook {name} failed ({count}x): {ex!r}")


def _traced(hook: str, **fields: Any) -> None:
    _trace.append({"t": round(time.time(), 3), "hook": hook, **fields})


# ---------- guard logic ----------


def _guards(profile: dict[str, Any], change: str, objective: str | None) -> bool:
    guard = profile.get("guard") or {}
    if change in ("objective", "decrement", "pickup"):
        wanted = guard.get("objectives")
        return wanted is None or objective is None or objective in wanted
    if change == "complete":
        return guard.get("complete", True) is not False
    return guard.get("status", True) is not False


def _consequence(profile: dict[str, Any], change: str) -> str:
    texts = profile.get("consequence") or {}
    key = "objective" if change == "decrement" else change
    return texts.get(key) or texts.get("objective") or f"This changes {profile.get('mission_name') or profile['mission']}, which this farm depends on."


def _summary(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {"status": state.get("status"), "progress": state.get("progress"), "active_set": state.get("active_set")}


def _primary(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    entries = snapshot.get("entries") or {}
    return entries.get("tracker") or entries.get("saved")


def _add_alert(profile: dict[str, Any], change: str, *, objective: str | None = None, item: str | None = None, blocked: bool = False,
               after: dict[str, Any] | None = None) -> dict[str, Any]:
    """Opens an alert, or folds the change into the profile's open alert of the same kind (blocked or not)."""
    global _next_alert  # noqa: PLW0603
    now = time.time()
    for alert in _alerts:
        if alert["profile"] == profile["id"] and alert["blocked"] == blocked:
            alert.update({"count": alert["count"] + 1, "at": now, "change": change, "objective": objective or alert["objective"],
                          "objective_name": _readable(objective) or alert["objective_name"], "item": item or alert["item"],
                          "message": _consequence(profile, change)})
            if after is not None:
                alert["after"] = _summary(after)
            _set_actions(alert, profile)
            return alert
    alert = {
        "id": _next_alert, "profile": profile["id"], "profile_name": profile.get("name"), "mission": profile["mission"],
        "mission_name": profile.get("mission_name") or _readable(profile["mission"]), "change": change,
        "objective": objective, "objective_name": _readable(objective), "item": item, "blocked": blocked,
        "message": _consequence(profile, change), "before": _summary(_primary(_state[profile["id"]]["snapshot"])),
        "after": _summary(after), "at": now, "count": 1,
        "actions": [], "note": None, "failed": None,
    }
    _set_actions(alert, profile)
    _next_alert += 1
    _alerts.append(alert)
    del _alerts[:-MAX_ALERTS]
    logging.info(f"RTSE: farm guard alert: {alert['mission_name']}: {alert['message']}{' (blocked)' if blocked else ''}")
    return alert


def _undo_blocker(profile: dict[str, Any], change: str) -> str | None:
    """Why undo must be off right now, or None. Reads EVERY current record of the mission (tracker and saved copy):
    a turn-in, any record that is Complete (whatever the snapshot's status), or any record whose status can't be read
    (or no records at all) turns undo off. Unknown means no undo."""
    if change == "complete":
        return NO_UNDO_AFTER_TURN_IN
    records = world.mission_records(profile["mission"])
    if not records:
        return NO_UNDO_UNREADABLE
    for state in records.values():
        if "status" in (state.get("unreadable") or ()) or state.get("status") not in world.STATUS_ORDER:
            return NO_UNDO_UNREADABLE
    if any(state["status"] == "complete" for state in records.values()):  # whatever the snapshot said
        return NO_UNDO_AFTER_TURN_IN
    return None


def _set_actions(alert: dict[str, Any], profile: dict[str, Any]) -> None:
    if alert["blocked"]:
        alert.update({"actions": ["dismiss"], "note": None})
        return
    blocker = _undo_blocker(profile, alert["change"])
    if blocker:
        alert.update({"actions": ["keep"], "note": blocker})
    else:
        alert.update({"actions": ["keep", "undo", "undo_reload"], "note": None})


def _check(profile: dict[str, Any], change: str, objective: str | None) -> None:
    """After a guarded mission changed: alert if its state no longer matches the snapshot."""
    snapshot = _state[profile["id"]]["snapshot"]
    if not snapshot:
        return
    now = world.mission_state(profile["mission"])
    before = _primary(snapshot)
    if now is None or before is None:
        return
    if (now.get("status"), now.get("progress"), now.get("active_set"), now.get("sub_sets")) != (
        before.get("status"), before.get("progress"), before.get("active_set"), before.get("sub_sets")):
        _add_alert(profile, change, objective=objective, after=now)


def _guarded(mission: str | None, change: str, objective: str | None) -> dict[str, Any] | None:
    profile = _by_mission.get(mission) if mission else None
    if profile is None or not _guards(profile, change, objective):
        return None
    return profile


def _pre(change: str, mission_obj: Any, objective_obj: Any) -> Any:
    """Shared pre-hook: block a guarded change when the profile's auto_block is on."""
    if _restoring or not _by_mission:
        return None
    objective = _path(objective_obj)
    mission = _path(mission_obj) if mission_obj is not None else _mission_of(objective)
    profile = _guarded(mission, change, objective)
    if profile is None or not _state[profile["id"]]["auto_block"] or BLOCK is None or not _is_host():
        return None
    _add_alert(profile, change, objective=objective, blocked=True)
    _traced(f"pre:{change}", mission=mission, objective=objective, blocked=True)
    return BLOCK


def _post(change: str, tracker: Any, mission_obj: Any, objective_obj: Any) -> None:
    """Shared post-hook: publish MISSION_CHANGED, then check a guarded mission against its snapshot."""
    if _restoring:
        return
    _note_world()
    objective = _path(objective_obj)
    mission = _path(mission_obj) if mission_obj is not None else _mission_of(objective)
    profile = _guarded(mission, change, objective)
    if mission_obj is None and tracker is not None and objective_obj is not None:
        mission_obj = getattr(objective_obj, "Outer", None)
    status = _status_now(tracker, mission_obj) if tracker is not None and mission_obj is not None else "unknown"
    if status == "unknown" and profile is not None:  # guarded missions only: a list walk is too slow for every update
        status = (world.mission_state(mission) or {}).get("status", "unknown")
    events.publish(events.MISSION_CHANGED, {
        "mission": mission, "status": status, "objective": objective, "change": change,
        "name": _readable(mission), "guarded": profile is not None,
    })
    _traced(f"post:{change}", mission=mission, objective=objective, status=status, guarded=profile is not None)
    if profile is not None and _is_host():
        _check(profile, change, objective)


def _tracker() -> Any:
    try:
        return get_pc(possibly_loading=True).WorldInfo.GRI.MissionTracker
    except Exception:  # noqa: BLE001
        return None


# --- hook callbacks: (obj, args, ret, func); each catches everything ---


def _pre_status(_obj: Any, args: Any, _ret: Any, _func: Any) -> Any:
    try:
        return _pre("status", args.InMission, None)
    except Exception as ex:  # noqa: BLE001
        _hook_error("SetMissionStatus pre", ex)
    return None


def _post_status(obj: Any, args: Any, _ret: Any, _func: Any) -> None:
    try:
        _post("status", obj, args.InMission, None)
    except Exception as ex:  # noqa: BLE001
        _hook_error("SetMissionStatus post", ex)


def _pre_objective(_obj: Any, args: Any, _ret: Any, _func: Any) -> Any:
    try:
        return _pre("objective", None, args.MissionObjective)
    except Exception as ex:  # noqa: BLE001
        _hook_error("UpdateObjective pre", ex)
    return None


def _post_objective(obj: Any, args: Any, _ret: Any, _func: Any) -> None:
    try:
        _post("objective", obj, None, args.MissionObjective)
    except Exception as ex:  # noqa: BLE001
        _hook_error("UpdateObjective post", ex)


def _pre_decrement(_obj: Any, args: Any, _ret: Any, _func: Any) -> Any:
    try:
        return _pre("decrement", None, args.Objective)
    except Exception as ex:  # noqa: BLE001
        _hook_error("DecrementObjective pre", ex)
    return None


def _post_decrement(obj: Any, args: Any, _ret: Any, _func: Any) -> None:
    try:
        _post("decrement", obj, None, args.Objective)
    except Exception as ex:  # noqa: BLE001
        _hook_error("DecrementObjective post", ex)


def _pre_complete(_obj: Any, args: Any, _ret: Any, _func: Any) -> Any:
    try:
        return _pre("complete", args.Mission, None)
    except Exception as ex:  # noqa: BLE001
        _hook_error("ServerCompleteMission pre", ex)
    return None


def _post_complete(_obj: Any, args: Any, _ret: Any, _func: Any) -> None:
    try:
        _post("complete", _tracker(), args.Mission, None)
    except Exception as ex:  # noqa: BLE001
        _hook_error("ServerCompleteMission post", ex)


def _pickup_info(pickup: Any) -> dict[str, Any] | None:
    inventory = getattr(pickup, "Inventory", None)
    if inventory is None:
        return None
    cls = getattr(getattr(inventory, "Class", None), "Name", None) or type(inventory).__name__
    data = getattr(inventory, "DefinitionData", None)
    definition = None
    for field in ("WeaponTypeDefinition", "ItemDefinition"):
        value = getattr(data, field, None) if data is not None else None
        if value is not None:
            definition = _path(value)
            break
    balance = _path(getattr(data, "BalanceDefinition", None)) if data is not None else None
    kind = KIND_BY_CLASS.get(cls, "item")
    low = (definition or "").lower()
    if kind == "usable":
        kind = "money" if "money" in low or "cash" in low else "ammo" if "ammo" in low else "usable"
    name = None
    for getter in ("GetShortHumanReadableName", "GetHumanReadableName"):
        try:
            name = getattr(inventory, getter)() or None
        except Exception:  # noqa: BLE001, S112
            continue
        if name:
            break
    rarity = None
    for source, field in ((pickup, "InventoryRarityLevel"), (inventory, "RarityLevel")):
        try:
            value = int(getattr(source, field))
        except Exception:  # noqa: BLE001, S112
            continue
        if value > 0:
            rarity = value
            break
    return {"item": name or _readable(definition) or cls, "kind": kind, "amount": None, "definition": definition,
            "balance": balance, "rarity": rarity}


def _pickup_profile(info: dict[str, Any]) -> dict[str, Any] | None:
    paths = (info.get("definition") or "", info.get("balance") or "")
    for profile in _by_mission.values():
        if any(pattern and any(pattern in p for p in paths) for pattern in profile.get("pickups") or ()):
            return profile
    return None


def _pre_pickup(obj: Any, _args: Any, _ret: Any, _func: Any) -> Any:
    try:
        info = _pickup_info(obj)
        if info is None:
            return None
        key = _path(obj) or str(id(obj))
        _pending_pickups[key] = info
        if len(_pending_pickups) > 32:  # a pickup whose post-hook never came
            _pending_pickups.pop(next(iter(_pending_pickups)))
        if _by_mission and not _restoring:
            profile = _pickup_profile(info)
            if profile is not None and _state[profile["id"]]["auto_block"] and BLOCK is not None and _is_host():
                _pending_pickups.pop(key, None)
                _add_alert(profile, "pickup", item=info["item"], blocked=True)
                _traced("pre:pickup", item=info["item"], blocked=True)
                return BLOCK
    except Exception as ex:  # noqa: BLE001
        _hook_error("GiveTo pre", ex)
    return None


def _matches_drop(info: dict[str, Any]) -> bool:
    profile_id = _session.get("profile")
    filters = {"kinds": ["weapon", "item"], "min_rarity": 3}
    if profile_id:
        try:
            filters = {**filters, **(_profile(profile_id).get("drops") or {})}
        except FarmError:
            pass
    if info["kind"] not in filters["kinds"]:
        return False
    return info.get("rarity") is not None and info["rarity"] >= filters["min_rarity"]


def _post_pickup(obj: Any, _args: Any, _ret: Any, _func: Any) -> None:
    try:
        info = _pending_pickups.pop(_path(obj) or str(id(obj)), None)
        if info is None:
            return
        _note_world()
        events.publish(events.PICKUP, {k: info[k] for k in ("item", "kind", "amount", "definition", "rarity")})
        _traced("post:pickup", item=info["item"], kind=info["kind"], rarity=info["rarity"])
        if _session.get("running"):
            _session["pickups"] += 1
            if _matches_drop(info):
                _session["drops"].append({**{k: info[k] for k in ("item", "kind", "definition", "rarity")}, "at": time.time(), "attempt": _session["attempts"]})
                del _session["drops"][:-MAX_DROPS]
    except Exception as ex:  # noqa: BLE001
        _hook_error("GiveTo post", ex)


def _on_frontend_start(obj: Any, _args: Any, _ret: Any, _func: Any) -> None:
    """Quick reload, step 2: the main menu started after ReturnToTitleScreen; load the same playthrough."""
    try:
        pending = _reload.get("pending")
        if not pending:
            return
        _reload["pending"] = None
        if time.time() - pending["at"] > RELOAD_TIMEOUT:
            _reload["last"] = "the relaunch timed out; press Continue yourself"
            return
        world._call(obj, "LaunchSaveGame", {"PlayThrough": pending["pt_raw"], "PlayersWithoutDlc": []}, (pending["pt_raw"], []))  # noqa: SLF001
        _reload["last"] = f"relaunched playthrough {pending['pt_raw'] - world.FIRST_RAW + 1}"
        logging.info(f"RTSE: farm guard quick reload: {_reload['last']}")
    except Exception as ex:  # noqa: BLE001
        _reload["last"] = f"the relaunch failed ({type(ex).__name__}); press Continue yourself"
        _hook_error("FrontendGFxMovie.Start post", ex)


HOOKS = (
    ("WillowGame.MissionTracker:SetMissionStatus", Type.PRE, _pre_status),
    ("WillowGame.MissionTracker:SetMissionStatus", Type.POST, _post_status),
    ("WillowGame.MissionTracker:UpdateObjective", Type.PRE, _pre_objective),
    ("WillowGame.MissionTracker:UpdateObjective", Type.POST, _post_objective),
    ("WillowGame.MissionTracker:DecrementObjective", Type.PRE, _pre_decrement),
    ("WillowGame.MissionTracker:DecrementObjective", Type.POST, _post_decrement),
    ("WillowGame.WillowPlayerController:ServerCompleteMission", Type.PRE, _pre_complete),
    ("WillowGame.WillowPlayerController:ServerCompleteMission", Type.POST, _post_complete),
    ("WillowGame.WillowPickup:GiveTo", Type.PRE, _pre_pickup),
    ("WillowGame.WillowPickup:GiveTo", Type.POST, _post_pickup),
    ("WillowGame.FrontendGFxMovie:Start", Type.POST, _on_frontend_start),
)


def install_hooks() -> None:
    """Registers every hook once (removing ours first, so calling it again never doubles them). Loads the profiles."""
    remove_hooks()
    try:
        load_profiles()
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: farm profiles could not be read: {ex!r}")
    _failed.clear()
    for path, kind, callback in HOOKS:
        try:
            add_hook(path, kind, HOOK_ID, callback)
        except Exception as ex:  # noqa: BLE001
            _failed.append(path)
            logging.error(f"RTSE: could not hook {path}: {ex!r}")
        else:
            _installed.append((path, kind))
    logging.info(f"RTSE: farm guard hooks installed: {len(_installed)} of {len(HOOKS)}")


def remove_hooks() -> None:
    """Removes every hook this module may have added (safe to call when none are installed)."""
    for path, kind, _callback in HOOKS:
        try:
            remove_hook(path, kind, HOOK_ID)
        except Exception:  # noqa: BLE001, S110
            pass
    _installed.clear()


# ---------- reading the state ----------


def _snapshot_view(snapshot: dict[str, Any] | None, taken_at: float | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    identity = snapshot.get("identity") or {}
    return {"taken_at": taken_at, "playthrough": snapshot.get("playthrough"), "character": snapshot.get("character"),
            "save_id": identity.get("save_id"), "player_class": identity.get("player_class"),
            **(_summary(_primary(snapshot)) or {})}


def _profile_view(profile: dict[str, Any], in_game: bool) -> dict[str, Any]:
    state = _state[profile["id"]]
    live = world.mission_state(profile["mission"]) if in_game else None
    snap = _primary(state["snapshot"])
    changed = None
    if live is not None and snap is not None:
        changed = _summary(live) != _summary(snap) or live.get("sub_sets") != snap.get("sub_sets")
    return {
        **{k: profile.get(k) for k in ("id", "name", "area", "map", "dlc", "mission", "mission_name", "description", "unverified")},
        "consequence": (profile.get("consequence") or {}).get("objective"),
        "guard": profile.get("guard") or {}, "drops": profile.get("drops") or {}, "can_block_pickups": bool(profile.get("pickups")),
        "enabled": state["enabled"], "auto_block": state["auto_block"],
        "snapshot": _snapshot_view(state["snapshot"], state["snapshot_at"]),
        "live": _summary(live), "found": live is not None if in_game else None, "changed": changed,
        "open_alerts": sum(1 for a in _alerts if a["profile"] == profile["id"]),
    }


def _session_view() -> dict[str, Any]:
    elapsed = _session["elapsed_before"]
    if _session["running"] and _session["started"] is not None:
        elapsed += time.time() - _session["started"]
    return {**{k: _session[k] for k in ("running", "profile", "attempts", "pickups")}, "elapsed": round(elapsed, 1),
            "drops": list(reversed(_session["drops"][-50:])), "drop_count": len(_session["drops"])}


def read(_params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not _profiles:
        load_profiles()
    _note_world()
    coop = coop_state()
    in_game = coop["in_game"]
    return {
        "profiles": [_profile_view(p, in_game) for p in _profiles],
        "alerts": list(reversed(_alerts)),
        "session": _session_view(),
        "coop": coop,
        "writes": {"allowed": coop["host"], "reason": coop["reason"]},
        "settings": dict(_settings),
        "hooks": {"installed": len(_installed), "total": len(HOOKS), "failed": list(_failed), "can_block": BLOCK is not None},
        "reload": {"pending": _reload["pending"] is not None, "last": _reload["last"]},
        "in_game": in_game,
    }


# ---------- writes ----------


def _bool(params: dict[str, Any], key: str) -> bool:
    value = params.get(key)
    if not isinstance(value, bool):
        raise FarmError(400, f"send '{key}' as true or false")
    return value


def _read_snapshot(profile: dict[str, Any]) -> dict[str, Any]:
    try:
        return world.mission_snapshot(profile["mission"])
    except world.WorldError as ex:
        raise FarmError(ex.status, str(ex)) from None


def _is_complete(snapshot: dict[str, Any]) -> bool:
    return any((state or {}).get("status") == "complete" for state in (snapshot.get("entries") or {}).values())


def _farm_over(profile: dict[str, Any]) -> str:
    name = profile.get("mission_name") or _readable(profile["mission"])
    return f"{name} is already turned in on this playthrough, so this farm is over and there's nothing to guard."


def _take_snapshot(profile: dict[str, Any]) -> dict[str, Any]:
    """Snapshots the profile's mission. Refused (409) once any record of it is Complete: the farm is over."""
    snapshot = _read_snapshot(profile)
    if _is_complete(snapshot):
        raise FarmError(409, _farm_over(profile))
    _state[profile["id"]].update({"snapshot": snapshot, "snapshot_at": time.time()})
    return snapshot


def _close_alerts(profile_id: str) -> None:
    _alerts[:] = [a for a in _alerts if a["profile"] != profile_id]


def set_enabled(params: dict[str, Any]) -> dict[str, Any]:
    profile = _profile(params.get("profile"))
    enabled = _bool(params, "enabled")
    state = _state[profile["id"]]
    applied: dict[str, Any] = {"what": "enabled", "profile": profile["id"], "before": state["enabled"], "requested": enabled}
    if enabled:
        _require_writable()
        if not state["enabled"] or state["snapshot"] is None or params.get("resnapshot") is True:
            _take_snapshot(profile)
            applied["snapshot"] = "taken"
        else:
            applied["snapshot"] = "kept"  # enabling an enabled profile is a no-op
        state["enabled"] = True
    else:
        state.update({"enabled": False, "snapshot": None, "snapshot_at": None})
        _close_alerts(profile["id"])
    _rebuild_index()
    applied["after"] = state["enabled"]
    return {**read(), "applied": applied}


def retake_snapshot(params: dict[str, Any]) -> dict[str, Any]:
    profile = _profile(params.get("profile"))
    _require_writable()
    if not _state[profile["id"]]["enabled"]:
        raise FarmError(409, "enable the profile first")
    snapshot = _take_snapshot(profile)
    _close_alerts(profile["id"])
    return {**read(), "applied": {"what": "snapshot", "profile": profile["id"], "after": _summary(_primary(snapshot))}}


def set_settings(params: dict[str, Any]) -> dict[str, Any]:
    applied: dict[str, Any] = {"what": "settings"}
    if "experimental_reload" in params:
        applied["experimental_reload"] = {"before": _settings["experimental_reload"], "after": _bool(params, "experimental_reload")}
        _settings["experimental_reload"] = params["experimental_reload"]
    if "auto_block" in params:
        profile = _profile(params.get("profile"))
        value = _bool(params, "auto_block")
        if value and BLOCK is None:
            raise FarmError(501, "this SDK can't block game functions, so auto-block isn't available")
        applied["auto_block"] = {"profile": profile["id"], "before": _state[profile["id"]]["auto_block"], "after": value}
        _state[profile["id"]]["auto_block"] = value
    if len(applied) == 1:
        raise FarmError(400, "send 'experimental_reload', or 'profile' with 'auto_block'")
    return {**read(), "applied": applied}


def _restore(profile: dict[str, Any]) -> dict[str, Any]:
    global _restoring  # noqa: PLW0603
    snapshot = _state[profile["id"]]["snapshot"]
    if not snapshot:
        raise FarmError(409, "this profile has no snapshot to put back")
    _restoring = True
    try:
        return world.restore_mission_snapshot(snapshot)
    except world.WorldError as ex:
        raise FarmError(ex.status, str(ex)) from None
    finally:
        _restoring = False


def _quick_reload() -> None:
    """Quick reload, step 1 (runs on the next game tick, after the HTTP answer went out)."""
    try:
        pc = get_pc(possibly_loading=True)
        if pc is None:
            _reload.update({"pending": None, "last": "not in a save any more"})
            return
        raw, _source = world._read_playthrough({"pc": pc, "gri": getattr(pc.WorldInfo, "GRI", None)})  # noqa: SLF001
        if raw is None:
            _reload.update({"pending": None, "last": "the playthrough couldn't be read; reload by hand"})
            return
        safe, why = reload_safety()  # checked again: things can change between the click and this tick
        if not safe:
            _reload.update({"pending": None, "last": f"the undo was done, but the quick reload was skipped: {why}. Save-quit and continue by hand."})
            return
        _reload["pending"] = {"pt_raw": raw, "at": time.time()}
        world._call(pc, "ReturnToTitleScreen", {"bSkipSave": False, "bRemoveSplitPlayer": False}, (False, False))  # noqa: SLF001
        _reload["last"] = "saved and quit to the title screen; relaunching"
    except Exception as ex:  # noqa: BLE001
        _reload.update({"pending": None, "last": f"the quick reload failed ({type(ex).__name__}: {ex})"})
        logging.error(f"RTSE: farm guard quick reload failed: {ex!r}")


def resolve(params: dict[str, Any]) -> dict[str, Any]:
    action = params.get("action")
    if action not in ACTIONS:
        raise FarmError(400, f"action must be one of {', '.join(ACTIONS)}")
    alert = next((a for a in _alerts if a["id"] == params.get("alert")), None)
    if alert is None:
        raise FarmError(404, "that alert is gone (already resolved?)")
    if action not in alert["actions"]:
        raise FarmError(400, f"this alert can't be resolved with '{action}'")
    if action == "undo_reload" and not _settings["experimental_reload"]:
        raise FarmError(409, "quick reload is experimental: turn it on in the farm settings first")
    profile = _profile(alert["profile"])
    applied: dict[str, Any] = {"what": action, "alert": alert["id"], "profile": profile["id"]}
    if action in ("keep", "undo", "undo_reload"):
        _require_writable()
    if action in ("undo", "undo_reload"):
        blocker = _undo_blocker(profile, alert["change"])  # checked live on every record, not from the alert
        if blocker:
            live = world.mission_state(profile["mission"])
            if live is not None:
                alert["after"] = _summary(live)
            _set_actions(alert, profile)  # now offers keep only
            raise FarmError(409, blocker)
        if action == "undo_reload":
            safe, why = reload_safety()
            if not safe:
                raise FarmError(409, f"quick reload isn't safe right now: {why}. Nothing was changed; try again, or use Undo and reload by hand.")
    if action == "keep":
        snapshot = _read_snapshot(profile)
        if _is_complete(snapshot):  # turned in: nothing left to guard, so the guard switches itself off
            _state[profile["id"]].update({"enabled": False, "snapshot": None, "snapshot_at": None})
            _rebuild_index()
            applied.update({"after": None, "guard": "off", "note": _farm_over(profile)})
        else:
            _state[profile["id"]].update({"snapshot": snapshot, "snapshot_at": time.time()})
            applied["after"] = _summary(_primary(snapshot))
    elif action in ("undo", "undo_reload"):
        try:
            applied["restore"] = _restore(profile)
        except FarmError as ex:
            alert["failed"] = str(ex)  # the alert stays open, with the reason on it
            raise
        if action == "undo_reload":
            gamethread.submit(_quick_reload)
            _reload["last"] = "quick reload queued"
            applied["reload"] = "queued"
    if action == "dismiss":
        _alerts.remove(alert)
    else:
        _close_alerts(profile["id"])
    return {**read(), "applied": applied}


def session(params: dict[str, Any]) -> dict[str, Any]:
    action = params.get("action")
    now = time.time()
    if action == "start":
        profile_id = params.get("profile")
        if profile_id is not None and profile_id != _session["profile"]:
            _profile(profile_id)
            _reset_session(profile_id)  # a different farm starts from zero
        if not _session["running"]:  # starting a running session is a no-op
            _session.update({"running": True, "started": now})
    elif action == "stop":
        if _session["running"]:
            _session["elapsed_before"] += now - (_session["started"] or now)
            _session.update({"running": False, "started": None})
    elif action == "reset":
        _reset_session(params.get("profile", _session["profile"]))
    elif action == "attempt":
        _session["attempts"] += 1
    else:
        raise FarmError(400, "action must be start, stop, reset or attempt")
    return {**read(), "applied": {"what": f"session {action}", "after": _session_view()}}


# ---------- debug ----------


def debug_dump() -> dict[str, Any]:
    """Saves what's needed to check the guard in game to rtse/debug_farm.json."""
    if not _profiles:
        load_profiles()
    out: dict[str, Any] = {
        "hooks": {"installed": [f"{p} ({'pre' if k == Type.PRE else 'post'})" for p, k in _installed], "failed": list(_failed),
                  "errors": dict(_hook_errors), "can_block": BLOCK is not None},
        "coop": world._guard(coop_state),  # noqa: SLF001
        "recent_hook_calls": list(_trace), "alerts": list(_alerts), "session": _session_view(), "reload": dict(_reload),
        "profiles": {},
    }
    for profile in _profiles:
        def detail(p: dict[str, Any] = profile) -> dict[str, Any]:
            owners = world._owners()  # noqa: SLF001
            entries = world._snapshot_entries(owners, p["mission"])  # noqa: SLF001
            data: dict[str, Any] = {"found_in": sorted(entries), "entries": {s: world._describe(e) for s, e in entries.items()}}  # noqa: SLF001
            if entries:
                definition = world._first(next(iter(entries.values())), world.MISSION_DEF)[0]  # noqa: SLF001
                data["objectives"] = [{"id": world._path(o), "count": world._guard(lambda o=o: int(o.ObjectiveCount))}  # noqa: SLF001
                                      for o in world._items(world._first(definition, world.OBJECTIVE_DEFS)[0])]  # noqa: SLF001
                data["sets"] = [world._path(s) for s in world._items(world._first(definition, world.OBJECTIVE_SET_DEFS)[0])]  # noqa: SLF001
                data["snapshot_now"] = world.mission_snapshot(p["mission"])
            return data

        out["profiles"][profile["id"]] = {"state": {k: v for k, v in _state[profile["id"]].items()}, "game": world._guard(detail)}  # noqa: SLF001
    DEBUG_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_FILE), "hooks": out["hooks"]["installed"], "profiles": sorted(out["profiles"])}


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = FarmError

GET_ROUTES: dict[str, Route] = {
    "/api/farm": read,
    "/api/debug/farm": lambda _params: debug_dump(),
}
POST_ROUTES: dict[str, Route] = {
    "/api/farm/enable": set_enabled,
    "/api/farm/snapshot": retake_snapshot,
    "/api/farm/settings": set_settings,
    "/api/farm/resolve": resolve,
    "/api/farm/session": session,
}
