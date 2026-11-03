"""Mock of the history and save-backup routes (rtse/history.py, rtse/checkpoints.py), for front-end work.

Loaded by dev/mock_server.py as dev/mock_history.py. Everything lives in memory: the save, backup and log paths
are invented and nothing is read from or written to disk. mock_server maps LookupError to 404 and ValueError to
400 and has no 409 or 423, so the refusals the contract says are 409 or 423 come back as 400 with the same sentence.
Follows api_contract.md plus api_delta_r1.md (verified, pinned, matches_current, auto.skipped/blocking, pending
status, last_restore status, restore_gate, partial entries, skip_backup and unpin).
"""

from __future__ import annotations

import json
import time

LIMIT = 200
DROPPED = 0
LOG_FILE = "C:/Users/you/Documents/My Games/RTSE/history.json"
SAVE_DIR = "C:/Users/you/Documents/My Games/Borderlands 2/WillowGame/SaveData/76561198000000000"
CHARACTER_FILE = f"{SAVE_DIR}/Save000A.sav"  # the save RTSE "located now" (guessed, so not verified)
PROFILE_FILE = f"{SAVE_DIR}/profile.bin"
OTHER_CHARACTER_FILE = f"{SAVE_DIR}/Save0003.sav"  # another character's save, in the same folder
BACKUPS_DIR = "C:/Users/you/Documents/My Games/RTSE/backups"
RETENTION_RANGE = [1, 200]

NOW = time.time()
WORLD_WHY = "Missions, challenges, fast travel and playthrough changes ripple into other game state, so they can't be undone safely here."
EARLIER_WHY = "Recorded in an earlier game session; undo only works for changes made in this session."
PARTIAL_WHY = "This undo stopped halfway, so it can't be undone or redone. The records show what changed, in order."

# The gate and the live state. The restore is applied only at the main menu, so at_main_menu is False in the mock.
AT_MAIN_MENU = False
CHARACTER_LOADED = True
RESTORE_RULE = (
    "A staged restore is copied into the save folder only when the game is at the main menu and the save file "
    "has not changed for 5 seconds (so the game's own save-on-quit has finished). RTSE re-checks the copied files "
    "5 seconds later."
)

# What the game holds now, per "kind:target". Undo checks these against each record's `after` before it writes.
# The cash line is deliberately off: something changed it after the cash entry, so undoing that entry is refused.
# The skill tree was reset (entry 10) and only Velocity came back (entry 11, partial), so Tank is still 0.
CURRENT = {
    "character.money:money": 6000000,
    "ammo.value:SMG": 600,
    "gear.part:barrel": {"name": "Hyperion barrel", "id": 812},
    "gear.stat:damage": 120,
    "skills.level:Velocity": 3,
    "skills.level:Tank": 0,
    "sdu.backpack:backpack": 1,
}


def _rec(kind, target, requested, before, after, route, undoable=True, why_not=None):
    return {"kind": kind, "target": target, "requested": requested, "before": before, "after": after,
            "route": route, "undoable": undoable, "why_not": why_not}


def _entry(id_, minutes_ago, label, params, records, undo_possible=True, undo_reason=None, session="current",
           undone_by=None, undo_of=None, partial=False, error=None):
    for record in records:
        record.setdefault("params", params)
    entry = {"id": id_, "time": NOW - minutes_ago * 60, "label": label, "params": params, "session": session,
             "records": records, "undo": {"possible": undo_possible, "reason": undo_reason},
             "undone_by": undone_by, "undo_of": undo_of, "error": error}
    if partial:
        entry["partial"] = True
        entry["attempted_undo_of"] = params.get("id")
    return entry


ENTRIES = [
    _entry(1, 1900, "/api/ammo/set", {"id": 1, "value": 300},
           [_rec("ammo.value", "SMG", None, 120, 300, "/api/ammo/set")],
           undo_possible=False, undo_reason=EARLIER_WHY, session="earlier"),
    _entry(2, 260, "/api/character/set", {"field": "money", "value": 5000000},
           [_rec("character.money", "money", 5000000, 1200000, 5000000, "/api/character/set")]),
    _entry(3, 210, "/api/ammo/set", {"id": 1, "value": 900},
           [_rec("ammo.value", "SMG", 900, 400, 600, "/api/ammo/set")]),
    _entry(4, 150, "/api/item/set_part", {"id": 3, "slot": "barrel", "part": 812},
           [_rec("gear.part", "barrel", {"name": "Hyperion barrel", "id": 812}, {"name": "Jakobs barrel", "id": 401},
                 {"name": "Hyperion barrel", "id": 812}, "/api/item/set_part")]),
    _entry(5, 120, "/api/item/set_stat", {"id": 3, "stat": "damage", "value": 120},
           [_rec("gear.stat", "damage", 120, 100, 120, "/api/item/set_stat")]),
    _entry(6, 80, "/api/skills/set", {"id": "velocity", "level": 3},
           [_rec("skills.level", "Velocity", 3, 1, 3, "/api/skills/set")]),
    _entry(7, 50, "/api/world/mission", {"id": "fishing"},
           [_rec("world.mission", "Fishing for Trouble", "complete", "active", "complete", "/api/world/mission",
                 undoable=False, why_not=WORLD_WHY)],
           undo_possible=False, undo_reason=WORLD_WHY),
    _entry(8, 30, "/api/sdu/set", {"kind": "backpack", "level": 2},
           [_rec("sdu.backpack", "backpack", 2, 1, 2, "/api/sdu/set")], undone_by=9),
    _entry(9, 12, "/api/history/undo", {"id": 8},
           [{**_rec("sdu.backpack", "backpack", 1, 2, 1, "/api/sdu/set"), "params": {"kind": "backpack", "level": 1}}],
           undo_of=8),
    # a skill tree reset, and an undo of it (entry 11) that stopped halfway; the reset itself stays in effect
    _entry(10, 10, "/api/skills/reset", {"tree": "Siren"},
           [_rec("skills.level", "Velocity", 0, 3, 0, "/api/skills/reset"),
            _rec("skills.level", "Tank", 0, 2, 0, "/api/skills/reset")],
           undone_by=None),  # a partial undo doesn't undo the original (as in rtse/history.py)
    _entry(11, 5, "/api/history/undo", {"id": 10},
           [_rec("skills.level", "Velocity", 3, 0, 3, "/api/skills/reset", undoable=False, why_not=PARTIAL_WHY),
            _rec("skills.level", "Tank", 2, 0, 0, "/api/skills/reset", undoable=False, why_not=PARTIAL_WHY)],
           undo_possible=False, undo_reason=PARTIAL_WHY, partial=True,
           error="Put Velocity back to 3, but couldn't put Tank back to 2: the game refused the write, so the undo "
                 "stopped halfway. Velocity is at 3 and Tank is still 0."),
]
NEXT_ID = 12


def _checkpoint_id(epoch: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(epoch))


def _files(character: str, profile: bool) -> list[dict]:
    files = [{"name": character.rsplit("/", 1)[-1], "size": 12649, "sha256": "3f" * 32, "kind": "character"}]
    if profile:
        files.append({"name": "profile.bin", "size": 2048, "sha256": "9a" * 32, "kind": "profile"})
    return files


def _cp(epoch: float, reason: str, note: str, profile: bool, character: str = CHARACTER_FILE,
        pinned: str | None = None) -> dict:
    return {"id": _checkpoint_id(epoch), "time": epoch, "reason": reason, "note": note,
            "character_file": character, "files": _files(character, profile), "pending": False, "pinned": pinned}


CHECKPOINTS = [  # oldest first; replies list them newest first
    _cp(NOW - 1500 * 60, "first write", "", True),
    _cp(NOW - 600 * 60, "manual", "before the skill test", False, character=OTHER_CHARACTER_FILE),
    _cp(NOW - 40 * 60, "before restore", "", True,
        pinned="Safety copy from the last restore: kept until you unpin it, so it survives pruning."),
]
RETENTION = 20
PENDING = None  # the staged restore: {"id", "include_profile", "staged_at", "files", "status", ...} or None
LAST_RESTORE = {  # the restore before this one went through cleanly
    "ok": True, "status": "ok", "id": CHECKPOINTS[0]["id"], "time": NOW - 40 * 60, "files": ["Save000A.sav"],
    "error": None, "safety_checkpoint": CHECKPOINTS[2]["id"],
}
# Blocking: the first-write backup failed for this session, so game writes are refused until back up or skip.
AUTO = {
    "done": False,
    "error": "Couldn't back up your save before the first change: the save was guessed (the newest save in the "
             "default folder), so it wasn't backed up automatically.",
    "checkpoint": None,
    "skipped": False,
    "blocking": True,
}


# ---------- history ----------

def _history() -> dict:
    return {"entries": list(reversed(ENTRIES)), "limit": LIMIT, "dropped": DROPPED, "log_file": LOG_FILE, "log_error": None}


def _plain(value) -> str:
    if isinstance(value, dict) and isinstance(value.get("name"), str):
        return value["name"]
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


PREFIX = {"ammo.value": "Ammo", "gear.part": "Gear part", "gear.stat": "Gear stat", "skills.level": "Skill",
          "world.mission": "Mission"}


def _title(record: dict) -> str:
    """The page's own label for a record, for the refusal sentences."""
    if record["kind"] == "character.money":
        return "Cash"
    if record["kind"] == "sdu.backpack":
        return "Backpack SDU"
    return f"{PREFIX.get(record['kind'], record['kind'])}: {record['target']}"


def _find_entry(entry_id: int) -> dict:
    for entry in ENTRIES:
        if entry["id"] == entry_id:
            return entry
    raise LookupError(f"no history entry #{entry_id}")


def _undo(params: dict) -> dict:
    global NEXT_ID
    try:
        entry_id = int(params["id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("id must be the number of a history entry") from None
    entry = _find_entry(entry_id)
    if entry["undone_by"] is not None:
        raise ValueError(f"Already undone (by #{entry['undone_by']}).")
    if not entry["undo"]["possible"]:
        raise ValueError(entry["undo"]["reason"])
    # all or nothing: check every record before any of them is written back
    for record in entry["records"]:
        key = f"{record['kind']}:{record['target']}"
        now = CURRENT.get(key)
        if not _same(now, record["after"]):
            raise ValueError(
                f"{_title(record)} is now {_plain(now)}, but this edit left {_plain(record['after'])}; "
                "undo refused so a newer change isn't overwritten"
            )
    steps, undo_records = [], []
    for record in entry["records"]:
        key = f"{record['kind']}:{record['target']}"
        now = CURRENT[key]
        CURRENT[key] = record["before"]
        steps.append(f"{_title(record)}: {_plain(now)} -> {_plain(record['before'])}")
        undo_records.append({**_rec(record["kind"], record["target"], record["before"], now, record["before"],
                                    record["route"]), "params": record.get("params")})
    # an undo can itself be undone (a redo), as in rtse/history.py
    undo_entry = _entry(NEXT_ID, 0, "/api/history/undo", {"id": entry_id}, undo_records, undo_of=entry_id)
    undo_entry["time"] = time.time()
    NEXT_ID += 1
    ENTRIES.append(undo_entry)
    entry["undone_by"] = undo_entry["id"]
    return {"applied": {"undid": entry_id, "entry": undo_entry, "steps": steps}, **_history()}


# ---------- checkpoints ----------

def _public(cp: dict) -> dict:
    """A backup as the page sees it: matches_current compares its character file with the save located now."""
    return {**cp, "files": list(cp["files"]), "matches_current": cp["character_file"] == CHARACTER_FILE}


def _view() -> dict:
    return {
        "save": {
            "dir": SAVE_DIR,
            "character_file": CHARACTER_FILE,
            "profile_file": PROFILE_FILE,
            "source": "guessed",
            "verified": False,
            "notes": [
                "The game didn't report the path of the save it loaded, so this is the newest Save*.sav in the default Documents folder.",
                "The folder name is the mock's, not a real one.",
            ],
        },
        "backups_dir": BACKUPS_DIR,
        "retention": RETENTION,
        "retention_range": RETENTION_RANGE,
        "auto": dict(AUTO),
        "checkpoints": [_public(cp) for cp in reversed(CHECKPOINTS)],
        "pending_restore": PENDING,
        "last_restore": LAST_RESTORE,
        "restore_gate": {
            "at_main_menu": AT_MAIN_MENU,
            "character_loaded": CHARACTER_LOADED,
            "stable_seconds": 5,
            "verify_seconds": 5,
            "rule": RESTORE_RULE,
        },
        "at_main_menu": AT_MAIN_MENU,
    }


def _checkpoint(cp_id) -> dict:
    for cp in CHECKPOINTS:
        if cp["id"] == cp_id:
            return cp
    raise LookupError(f"no backup called {cp_id}")


def _create(params: dict) -> dict:
    note = str(params.get("note") or "").strip()[:200]
    epoch = time.time()
    while any(cp["id"] == _checkpoint_id(epoch) for cp in CHECKPOINTS):
        epoch += 1  # two backups in the same second get distinct ids
    cp = _cp(epoch, "manual", note, True)
    CHECKPOINTS.append(cp)
    if AUTO["blocking"]:
        # a backup made by hand satisfies the gate; the mock records it as the automatic one
        AUTO.update(done=True, error=None, checkpoint=cp["id"], blocking=False)
    _prune()
    return {"applied": {"checkpoint": _public(cp)}, **_view()}


def _prune() -> list[str]:
    """Drops the oldest backups over the limit, but never a pinned one."""
    pruned = []
    while len(CHECKPOINTS) > RETENTION:
        victim = next((cp for cp in CHECKPOINTS if not cp.get("pinned")), None)
        if victim is None:
            break
        CHECKPOINTS.remove(victim)
        pruned.append(victim["id"])
    return pruned


def _restore(params: dict) -> dict:
    global PENDING
    if params.get("confirm") is not True:
        raise ValueError("confirm must be true: a restore replaces your save files")
    cp = _checkpoint(params.get("id"))
    include = bool(params.get("include_profile"))
    folder = cp["character_file"].rsplit("/", 1)[0]
    files = [
        {"name": f["name"], "to": cp["character_file"] if f["kind"] == "character" else f"{folder}/profile.bin",
         "sha256": f["sha256"]}
        for f in cp["files"] if f["kind"] == "character" or include
    ]
    for other in CHECKPOINTS:
        other["pending"] = False
    cp["pending"] = True
    PENDING = {
        "id": cp["id"], "include_profile": include, "staged_at": time.time(), "files": files,
        "status": "staged",
        "detail": "Waiting for the game to reach the main menu: it is in a game right now.",
        "safety_checkpoint": None, "error": None,
    }
    return {"applied": {"staged": PENDING}, **_view()}


def _cancel_restore() -> dict:
    global PENDING
    cancelled = PENDING["id"] if PENDING else None
    PENDING = None
    for cp in CHECKPOINTS:
        cp["pending"] = False
    return {"applied": {"cancelled": cancelled}, **_view()}


def _settings(params: dict) -> dict:
    global RETENTION
    try:
        value = int(params["retention"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("retention must be a whole number") from None
    low, high = RETENTION_RANGE
    if not low <= value <= high:
        raise ValueError(f"retention must be {low}-{high}")
    RETENTION = value
    return {"applied": {"retention": RETENTION, "pruned": _prune()}, **_view()}


def _skip_backup(params: dict) -> dict:
    if params.get("confirm") is not True:
        raise ValueError("confirm must be true: skipping the backup means this session's changes have no backup")
    AUTO.update(skipped=True, blocking=False)
    return {"applied": {"skipped": True}, **_view()}


def _unpin(params: dict) -> dict:
    cp = _checkpoint(params.get("id"))
    cp["pinned"] = None
    return {"applied": {"unpinned": cp["id"]}, **_view()}


def handle(path: str, params: dict, shared: dict) -> dict | None:
    if path == "/api/history":
        return _history()
    if path == "/api/history/undo":
        return _undo(params)
    if path == "/api/debug/history":
        return {"saved_to": "(mock) rtse/debug_history.json", "entries": len(ENTRIES), "limit": LIMIT}
    if path == "/api/checkpoints":
        return _view()
    if path == "/api/checkpoints/create":
        return _create(params)
    if path == "/api/checkpoints/restore":
        return _restore(params)
    if path == "/api/checkpoints/cancel_restore":
        return _cancel_restore()
    if path == "/api/checkpoints/settings":
        return _settings(params)
    if path == "/api/checkpoints/skip_backup":
        return _skip_backup(params)
    if path == "/api/checkpoints/unpin":
        return _unpin(params)
    return None
