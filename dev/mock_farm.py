"""Mock of the farm guard endpoints (rtse/farmguard.py), for front-end work. Loaded by dev/mock_server.py.

The profiles come from rtse/farm_profiles.json, the file the backend reads. Everything else is kept in memory and starts
as a demo: Doctor's Orders is guarded and has changed (one open alert), and a farm session is running with four drops.

    MOCK_FARM=client   a co-op client: every POST is refused with the backend's reason (writes.allowed is false)
    MOCK_FARM=blocked  auto-block is on for the WEP profile and a change was blocked (one alert, dismiss only)

The mock server maps only LookupError (404) and ValueError (400), so the backend's 409 and 423 come out as ValueError here.
The backend's 'applied' keys are copied; nothing here is checked against the real game.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from pathlib import Path

PROFILES_FILE = Path(__file__).resolve().parent.parent / "rtse" / "farm_profiles.json"
MODE = os.environ.get("MOCK_FARM", "")
POST_ROUTES = {"/api/farm/enable", "/api/farm/snapshot", "/api/farm/settings", "/api/farm/resolve", "/api/farm/session"}
ACTIONS = ("keep", "undo", "undo_reload", "dismiss")
COOP_REASON = "you're a client in someone else's co-op game; only the host's game runs the missions, so farm guard is off"
MAX_ALERTS = 20
HOOKS = [  # what rtse/farmguard.py installs (HOOKS): path (pre|post)
    "WillowGame.MissionTracker:SetMissionStatus (pre)", "WillowGame.MissionTracker:SetMissionStatus (post)",
    "WillowGame.MissionTracker:UpdateObjective (pre)", "WillowGame.MissionTracker:UpdateObjective (post)",
    "WillowGame.MissionTracker:DecrementObjective (pre)", "WillowGame.MissionTracker:DecrementObjective (post)",
    "WillowGame.WillowPlayerController:ServerCompleteMission (pre)", "WillowGame.WillowPlayerController:ServerCompleteMission (post)",
    "WillowGame.WillowPickup:GiveTo (pre)", "WillowGame.WillowPickup:GiveTo (post)",
    "WillowGame.FrontendGFxMovie:Start (post)",
]

PROFILES = [p for p in json.loads(PROFILES_FILE.read_text(encoding="utf-8")).get("profiles", [])
            if isinstance(p, dict) and p.get("id") and p.get("mission")]
BY_ID = {p["id"]: p for p in PROFILES}
WEP, COWBOY = "wep_loot_midgets", "ffs_space_cowboy_potty"
WEP_SET = "GD_Z2_DoctorsOrders.M_DoctorsOrders:ObjectiveSet_Notes"

NOW = time.time()
SETTINGS = {"experimental_reload": False}
RELOAD = {"pending": None, "last": None}
STATE = {p["id"]: {"enabled": False, "auto_block": False, "snapshot": None, "snapshot_at": None} for p in PROFILES}
LIVE = {  # the live mission state per profile: {status, progress, active_set, sub_sets}
    WEP: {"status": "active", "progress": [1], "active_set": WEP_SET, "sub_sets": []},
    COWBOY: {"status": "active", "progress": [3], "active_set": None, "sub_sets": []},
}
ALERTS: list[dict] = []
NEXT_ALERT = [1]
SESSION: dict = {}


def _snapshot_of(tracker: dict, playthrough: int = 1) -> dict:
    """Shaped like world.mission_snapshot(): entries.tracker is the mission state."""
    return {"mission": BY_ID[WEP]["mission"], "name": BY_ID[WEP]["mission_name"], "playthrough": playthrough,
            "character": "Axton", "entries": {"tracker": copy.deepcopy(tracker), "saved": None}, "set_classes": {}}


def _reset(profile_id) -> None:
    SESSION.clear()
    SESSION.update({"running": False, "profile": profile_id, "started": None, "elapsed_before": 0.0, "attempts": 0, "drops": [], "pickups": 0})


def _demo() -> None:
    """The starting state for screenshots (see the module docstring for the two MOCK_FARM variants)."""
    before = {"status": "active", "progress": [0], "active_set": WEP_SET, "sub_sets": []}
    if MODE == "blocked":
        LIVE[WEP] = copy.deepcopy(before)  # the game never made the change: live still matches the snapshot
    else:
        LIVE[WEP] = {"status": "active", "progress": [1], "active_set": WEP_SET, "sub_sets": []}
    STATE[WEP].update(enabled=True, auto_block=MODE == "blocked", snapshot=_snapshot_of(before), snapshot_at=NOW - 1500)
    if MODE == "blocked":
        _add_alert(WEP, "pickup", item="Slag experiment notes", blocked=True)["at"] = NOW - 40
    else:
        _add_alert(WEP, "objective", objective="GD_Z2_DoctorsOrders.M_DoctorsOrders:CollectNotes", after=LIVE[WEP])["at"] = NOW - 140
    _reset(WEP)
    SESSION.update(running=True, started=NOW - 95, elapsed_before=540.0, attempts=4, pickups=37, drops=[
        {"item": "Jakobs Hellfire Pistol", "kind": "weapon", "definition": "GD_Weap_Pistol.A_Weapons.Pistol_Jakobs_Hellfire", "rarity": 3, "at": NOW - 480, "attempt": 1},
        {"item": "Siren's Mirage Shield", "kind": "item", "definition": "GD_Itempool_Shields.A_Item.Shield_Mirage", "rarity": 4, "at": NOW - 330, "attempt": 2},
        {"item": "Torgue Hand Cannon", "kind": "weapon", "definition": "GD_Weap_Launchers.A_Weapons.Launcher_TorgueHandCannon", "rarity": 5, "at": NOW - 150, "attempt": 3},
        {"item": "Bouncing Betty Grenade Mod", "kind": "item", "definition": "GD_Itempool_GrenadeMods.A_Item.GrenadeMod_BouncingBetty", "rarity": 3, "at": NOW - 40, "attempt": 4},
    ])


def _readable(path: str | None) -> str | None:
    """'GD_X.M_Mission:CollectNotes' -> 'Collect Notes'."""
    if not path:
        return None
    last = path.rsplit(":", 1)[-1]
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", last).replace("_", " ")


def _summary(state: dict | None) -> dict | None:
    if state is None:
        return None
    return {"status": state.get("status"), "progress": state.get("progress"), "active_set": state.get("active_set")}


def _primary(snapshot: dict | None) -> dict | None:
    if not snapshot:
        return None
    entries = snapshot.get("entries") or {}
    return entries.get("tracker") or entries.get("saved")


def _consequence(profile: dict, change: str) -> str:
    texts = profile.get("consequence") or {}
    key = "objective" if change == "decrement" else change
    return texts.get(key) or texts.get("objective") or f"This changes {profile.get('mission_name') or profile['mission']}, which this farm depends on."


def _add_alert(pid: str, change: str, *, objective: str | None = None, item: str | None = None, blocked: bool = False, after: dict | None = None) -> dict:
    """Like farmguard._add_alert: folds into the profile's open alert of the same kind, else opens one."""
    profile = BY_ID[pid]
    now = time.time()
    for alert in ALERTS:
        if alert["profile"] == pid and alert["blocked"] == blocked:
            alert.update(count=alert["count"] + 1, at=now, change=change, objective=objective or alert["objective"],
                         objective_name=_readable(objective) or alert["objective_name"], message=_consequence(profile, change))
            if after is not None:
                alert["after"] = _summary(after)
            return alert
    alert = {
        "id": NEXT_ALERT[0], "profile": pid, "profile_name": profile.get("name"), "mission": profile["mission"],
        "mission_name": profile.get("mission_name"), "change": change, "objective": objective, "objective_name": _readable(objective), "item": item,
        "blocked": blocked, "message": _consequence(profile, change), "before": _summary(_primary(STATE[pid]["snapshot"])),
        "after": _summary(after), "at": now, "count": 1, "actions": ["dismiss"] if blocked else ["keep", "undo", "undo_reload"],
    }
    NEXT_ALERT[0] += 1
    ALERTS.append(alert)
    del ALERTS[:-MAX_ALERTS]
    return alert


def _close(pid: str) -> None:
    ALERTS[:] = [a for a in ALERTS if a["profile"] != pid]


def _take(pid: str) -> None:
    STATE[pid].update(snapshot=_snapshot_of(LIVE[pid]), snapshot_at=time.time())


def _restore(pid: str) -> dict:
    snapshot = STATE[pid]["snapshot"]
    if not snapshot:
        raise ValueError("this profile has no snapshot to put back")
    LIVE[pid] = copy.deepcopy(_primary(snapshot))
    return {"mission": snapshot["mission"], "name": snapshot["name"], "matches": True, "mismatch": []}


def _profile(profile_id) -> dict:
    if profile_id not in BY_ID:
        raise LookupError("unknown farm profile")
    return BY_ID[profile_id]


def _bool(params: dict, key: str) -> bool:
    value = params.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"send '{key}' as true or false")
    return value


def _session_view() -> dict:
    elapsed = SESSION["elapsed_before"]
    if SESSION["running"] and SESSION["started"] is not None:
        elapsed += time.time() - SESSION["started"]
    return {**{k: SESSION[k] for k in ("running", "profile", "attempts", "pickups")}, "elapsed": round(elapsed, 1),
            "drops": list(reversed(SESSION["drops"][-50:])), "drop_count": len(SESSION["drops"])}


def _profile_view(p: dict) -> dict:
    pid = p["id"]
    state, live = STATE[pid], LIVE[pid]
    snap = _primary(state["snapshot"])
    changed = None
    if snap is not None:
        changed = _summary(live) != _summary(snap) or live.get("sub_sets") != snap.get("sub_sets")
    snapshot = None
    if state["snapshot"]:
        snapshot = {"taken_at": state["snapshot_at"], "playthrough": state["snapshot"].get("playthrough"),
                    "character": state["snapshot"].get("character"), **_summary(snap)}
    return {
        **{k: p.get(k) for k in ("id", "name", "area", "map", "dlc", "mission", "mission_name", "description", "unverified")},
        "consequence": (p.get("consequence") or {}).get("objective"),
        "guard": p.get("guard") or {}, "drops": p.get("drops") or {}, "can_block_pickups": bool(p.get("pickups")),
        "enabled": state["enabled"], "auto_block": state["auto_block"], "snapshot": snapshot,
        "live": _summary(live), "found": True, "changed": changed,
        "open_alerts": sum(1 for a in ALERTS if a["profile"] == pid),
    }


def read() -> dict:
    client = MODE == "client"
    return {
        "profiles": [_profile_view(p) for p in PROFILES],
        "alerts": list(reversed(ALERTS)),
        "session": _session_view(),
        "coop": {"in_game": True, "net_mode": 3 if client else 2, "client": client, "players": 2 if client else 1,
                 "reason": COOP_REASON if client else ""},
        "writes": {"allowed": not client, "reason": COOP_REASON if client else ""},
        "settings": dict(SETTINGS),
        "hooks": {"installed": len(HOOKS), "total": len(HOOKS), "failed": [], "can_block": True},
        "reload": dict(RELOAD),
        "in_game": True,
    }


def _enable(params: dict) -> dict:
    pid = _profile(params.get("profile"))["id"]
    enabled = _bool(params, "enabled")
    state = STATE[pid]
    applied = {"what": "enabled", "profile": pid, "before": state["enabled"], "requested": enabled}
    if enabled:
        if not state["enabled"] or state["snapshot"] is None or params.get("resnapshot") is True:
            _take(pid)
            applied["snapshot"] = "taken"
        else:
            applied["snapshot"] = "kept"  # enabling an enabled profile is a no-op
        state["enabled"] = True
        if (_primary(state["snapshot"]) or {}).get("status") == "complete":
            applied["warning"] = f"{BY_ID[pid].get('mission_name')} is already complete, so this farm is probably over on this playthrough."
    else:
        state.update(enabled=False, snapshot=None, snapshot_at=None)
        _close(pid)
    applied["after"] = state["enabled"]
    return {**read(), "applied": applied}


def _retake(params: dict) -> dict:
    pid = _profile(params.get("profile"))["id"]
    if not STATE[pid]["enabled"]:
        raise ValueError("enable the profile first")
    _take(pid)
    _close(pid)
    return {**read(), "applied": {"what": "snapshot", "profile": pid, "after": _summary(_primary(STATE[pid]["snapshot"]))}}


def _settings(params: dict) -> dict:
    applied: dict = {"what": "settings"}
    if "experimental_reload" in params:
        applied["experimental_reload"] = {"before": SETTINGS["experimental_reload"], "after": _bool(params, "experimental_reload")}
        SETTINGS["experimental_reload"] = params["experimental_reload"]
    if "auto_block" in params:
        pid = _profile(params.get("profile"))["id"]
        value = _bool(params, "auto_block")
        applied["auto_block"] = {"profile": pid, "before": STATE[pid]["auto_block"], "after": value}
        STATE[pid]["auto_block"] = value
    if len(applied) == 1:
        raise ValueError("send 'experimental_reload', or 'profile' with 'auto_block'")
    return {**read(), "applied": applied}


def _resolve(params: dict) -> dict:
    action = params.get("action")
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    alert = next((a for a in ALERTS if a["id"] == params.get("alert")), None)
    if alert is None:
        raise LookupError("that alert is gone (already resolved?)")
    if action not in alert["actions"]:
        raise ValueError(f"this alert can't be resolved with '{action}'")
    if action == "undo_reload" and not SETTINGS["experimental_reload"]:
        raise ValueError("quick reload is experimental: turn it on in the farm settings first")
    pid = alert["profile"]
    applied: dict = {"what": action, "alert": alert["id"], "profile": pid}
    if action == "keep":
        _take(pid)
        applied["after"] = _summary(_primary(STATE[pid]["snapshot"]))
    elif action in ("undo", "undo_reload"):
        applied["restore"] = _restore(pid)
        if action == "undo_reload":
            RELOAD["last"] = "quick reload queued"
            applied["reload"] = "queued"
    if action == "dismiss":
        ALERTS.remove(alert)
    else:
        _close(pid)
    return {**read(), "applied": applied}


def _session_cmd(params: dict) -> dict:
    action = params.get("action")
    now = time.time()
    if action == "start":
        pid = params.get("profile")
        if pid is not None and pid != SESSION["profile"]:
            _profile(pid)
            _reset(pid)  # a different farm starts from zero
        if not SESSION["running"]:
            SESSION.update(running=True, started=now)
    elif action == "stop":
        if SESSION["running"]:
            SESSION["elapsed_before"] += now - (SESSION["started"] or now)
            SESSION.update(running=False, started=None)
    elif action == "reset":
        _reset(params.get("profile", SESSION["profile"]))
    elif action == "attempt":
        SESSION["attempts"] += 1
    else:
        raise ValueError("action must be start, stop, reset or attempt")
    return {**read(), "applied": {"what": f"session {action}", "after": _session_view()}}


_reset(None)
_demo()


def handle(path: str, params: dict, shared: dict) -> dict | None:
    """Routes by path: the mock server gives no method, and every POST route is a path of its own."""
    if path == "/api/farm":
        return read()
    if path == "/api/debug/farm":
        return {"saved_to": "(mock) rtse/debug_farm.json", "hooks": list(HOOKS), "profiles": sorted(BY_ID)}
    if path not in POST_ROUTES:
        return None
    if MODE == "client":  # a co-op client's writes are refused, whatever the request
        raise ValueError(COOP_REASON)
    if path == "/api/farm/enable":
        return _enable(params)
    if path == "/api/farm/snapshot":
        return _retake(params)
    if path == "/api/farm/settings":
        return _settings(params)
    if path == "/api/farm/resolve":
        return _resolve(params)
    return _session_cmd(params)
