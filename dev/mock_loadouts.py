"""Stand-in for rtse/loadouts.py (build presets) so the Builds page can be worked on without the game.

Answers every /api/loadouts... route and /api/debug/loadouts in the response shapes of the spec. The inventory, the
skill tree and the character are fake and live in memory; nothing is written to disk. Loaded by mock_server.py like
every mock_<feature>.py. mock_server maps only ValueError (400) and LookupError (404), so the real routes' 409 (name
taken, stale plan) come back from this mock as 400.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
import time
import zlib
from datetime import datetime, timezone

LINK_PREFIX = "RTSE-L1:"
SCHEMA_VERSION = 2  # 2 adds each gear entry's item code; version 1 builds have none and still load
MAX_PRESETS = 100
MAX_STEPS = 600
MAX_NAME = 64
LINK_LIMIT_STORED = 56000  # a stored build's own link must fit this (the server refuses a bigger one)
LINK_LIMIT_IMPORT = 62000  # an import link may be this long
CHUNK = 4  # steps per apply request
CHUNK_DELAY = 0.9  # seconds a continuation waits, so the progress shows in the screenshots
MIN_XP = {70: 1100000, 72: 1234567}  # the game's minimum XP for a level: a level step sets it (side_effect)
UNREADABLE = ("torgue", "Torgue Tokens couldn't be read, so it's left out")  # the currency the mock cannot read
CLASSES = ("assassin", "siren", "soldier", "mercenary", "mechromancer", "psycho")
SECTIONS = ("character", "skills", "gear")
CHAR_FIELDS = ("level", "xp", "skill_points", "money", "eridium", "seraph", "torgue")
CURRENCY_FIELDS = ("money", "eridium", "seraph", "torgue")
CHAR_LABEL = {
    "level": "Character level", "xp": "Character XP", "skill_points": "Skill points", "money": "Money",
    "eridium": "Eridium", "seraph": "Seraph crystals", "torgue": "Torgue tokens",
}
CLASS_KEY = "siren"  # the class of the open (fake) save
LEVEL_CAP = 80
GEAR_LEVEL = (1, 90)
UNVERIFIED = "Applying presets has not been tested in a live game. Every step uses the same write functions as the editor pages."

# ---- the open save (fake) ----
CHARACTER = {"level": 70, "xp": 1100000, "skill_points": 0, "money": 180000, "eridium": 12, "seraph": 0, "torgue": 0}
TREE = {
    "GD_Siren_Skills.Phasewalk.Duration": {"name": "Phasewalk Duration", "level": 3, "max": 5},
    "GD_Siren_Skills.Phasewalk.Recharge": {"name": "Phasewalk Recharge", "level": 5, "max": 5},
    "GD_Siren_Skills.Phasewalk.Cloak": {"name": "Cloak", "level": 1, "max": 1},
}
INVENTORY: list[dict] = []

SNIPER = ("GD_Weap_SniperRifles.Balance.SR_Jakobs_Legendary", "GD_Weap_SniperRifles.A_Weapons.WeaponType_Sniper", "GD_Manufacturer.ManufacturerDefinition.Jakobs")
PISTOL = ("GD_Weap_Pistol.Balance.PS_Hyperion_Legendary", "GD_Weap_Pistol.A_Weapons.WeaponType_Pistol", "GD_Manufacturer.ManufacturerDefinition.Hyperion")
RIFLE = ("GD_Weap_AssaultRifle.Balance.AR_Vladof_Rare", "GD_Weap_AssaultRifle.A_Weapons.WeaponType_AssaultRifle", "GD_Manufacturer.ManufacturerDefinition.Vladof")
SHIELD = ("GD_Shields.Balance.Shield_Alpha", "GD_Shields.A_Item.Shield_Alpha", "GD_Manufacturer.ManufacturerDefinition.Tediore")
MOD = ("GD_Siren.ClassMod.Balance_Mod_Bolt", "GD_Siren.ClassMod.Mod_Bolt", None)
SHOTGUN = ("GD_Weap_Shotgun.Balance.SG_Torgue_Epic", "GD_Weap_Shotgun.A_Weapons.WeaponType_Shotgun", "GD_Manufacturer.ManufacturerDefinition.Torgue")
LAUNCHER = ("GD_Weap_Launchers.Balance.RL_Maliwan_Epic", "GD_Weap_Launchers.A_Weapons.WeaponType_Launcher", "GD_Manufacturer.ManufacturerDefinition.Maliwan")


def _add(kind, cls, name, location, level, model, parts):
    balance, definition, manufacturer = model
    item = {
        "id": f"Transient.{cls}_{len(INVENTORY) + 1}", "kind": kind, "class": cls, "name": name, "location": location,
        "level": level, "balance": balance, "definition": definition, "manufacturer": manufacturer, "parts": dict(parts),
    }
    INVENTORY.append(item)
    return item


_add("weapon", "WillowWeapon", "Sniper of Hellfire", "Equipped", 34, SNIPER, {
    "Body": "GD_Weap_SniperRifles.Body.Body_01", "Grip": "GD_Weap_SniperRifles.Grip.Grip_02",
    "Barrel": "GD_Weap_SniperRifles.Barrel.Barrel_03", "Stock": None, "Sight": None, "Material": None,
})
_add("weapon", "WillowWeapon", "Lucky Pistol", "Equipped", 30, PISTOL, {
    "Body": "GD_Weap_Pistol.Body.Body_03", "Grip": "GD_Weap_Pistol.Grip.Grip_01", "Barrel": "GD_Weap_Pistol.Barrel.Barrel_01",
})
_add("weapon", "WillowWeapon", "Assault Rifle", "Backpack", 28, RIFLE, {
    "Body": "GD_Weap_AssaultRifle.Body.Body_02", "Barrel": "GD_Weap_AssaultRifle.Barrel.Barrel_01",
})
_add("item", "WillowShield", "Shield Alpha", "Backpack", 40, SHIELD, {
    "Body": "GD_Shields.Body.Shield_Body_01", "Battery": "GD_Shields.Battery.Battery_02", "Capacitor": None, "Accessory": None,
})
_add("item", "WillowClassMod", "Siren Mod", "Backpack", 50, MOD, {})
_add("weapon", "WillowWeapon", "Torgue Shotgun", "Equipped", 26, SHOTGUN, {"Body": "GD_Weap_Shotgun.Body.Body_04", "Barrel": None})

# part id -> the first list the game finds it in: "legal" (the item's own parts), "same_slot" or "everything".
# A part not listed here is not loaded in the game right now.
LOADED = {part: "legal" for item in INVENTORY for part in item["parts"].values() if part}
LOADED.update({
    "GD_Weap_SniperRifles.Barrel.Barrel_05": "legal",
    "GD_Weap_AssaultRifle.Barrel.Barrel_04": "legal",
    "GD_Weap_SniperRifles.Stock.Stock_03": "same_slot",
    "GD_Weap_SniperRifles.Sight.Sight_Scope_04": "everything",
    "GD_Weap_Shotgun.Body.Body_01": "legal",
    "GD_Weap_Shotgun.Barrel.Barrel_02": "legal",
    "GD_Weap_Pistol.Body.Body_01": "legal",
    "GD_Weap_Pistol.Grip.Grip_02": "legal",
    "GD_Weap_Pistol.Barrel.Barrel_02": "legal",
})


class _Refused(Exception):
    """One game write refused (mock)."""


LAST: dict = {"report": None}
RUNS: dict[str, dict] = {}  # run_id -> the apply run in progress or finished (the last 20 are kept)


# ---- helpers ----

def _cap(text: str | None) -> str:
    return text[:1].upper() + text[1:] if text else "No class"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_name(value) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be text")
    name = value.strip()
    if not 1 <= len(name) <= MAX_NAME:
        raise ValueError(f"name must be 1-{MAX_NAME} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("name has control characters")
    return name


def _index(name: str) -> int | None:
    for i, preset in enumerate(PRESETS):
        if preset["name"].lower() == name.lower():
            return i
    return None


def _find(name: str) -> dict:
    i = _index(name)
    if i is None:
        raise LookupError(f'no build named "{name}"')
    return PRESETS[i]


def _live(item_id: str) -> dict:
    for item in INVENTORY:
        if item["id"] == item_id:
            return item
    raise LookupError("that item is no longer in your inventory")


def _summary(preset: dict) -> dict:
    gear = preset.get("gear") or []
    left_out = list(preset.get("left_out") or [])
    return {
        "name": preset["name"], "kind": preset["kind"], "class_key": preset.get("class_key"), "created": preset.get("created"),
        "partial": bool(left_out), "left_out": left_out, "schema_version": preset.get("version", 1),
        "counts": {
            "gear": len(gear),
            "equipped": sum(1 for g in gear if g["location"] == "Equipped"),
            "backpack": sum(1 for g in gear if g["location"] == "Backpack"),
            "skills": len(preset.get("skills") or []),
            "character": len(preset.get("character") or {}),
            "codes": sum(1 for g in gear if g.get("code")),
        },
    }


def _summaries() -> list[dict]:
    return [_summary(p) for p in PRESETS]


def _entry(item: dict) -> dict:
    return {
        "kind": item["kind"], "class": item["class"], "name": item["name"], "location": item["location"],
        "balance": item["balance"], "definition": item["definition"], "manufacturer": item["manufacturer"],
        "level": item["level"], "parts": dict(item["parts"]),
        "code": make_code(item) if item["name"] not in NO_CODE else None,
    }


def _list_param(value):
    if value is None:
        return None
    if isinstance(value, str):
        return [part for part in value.split(",") if part]
    return list(value)


def _int_list(value) -> list[int]:
    return [int(x) for x in (_list_param(value) or [])]


def _truthy(value) -> bool:
    return value in (True, 1, "1", "true")


# ---- item codes (fake): "BL2(" + base64 of the item's JSON + "-" + a checksum byte in hex + ")" ----

CODE_ERROR = "checksum does not match: the code was copied wrong, or it is not a Gibbed BL2 code"
CODE_KEYS = ("kind", "class", "name", "level", "balance", "definition", "manufacturer", "parts")
NO_CODE = {"Assault Rifle", "Torgue Shotgun"}  # inventory items with no code the game can give (saved without one)


def make_code(item: dict, bad: bool = False) -> str:
    """The code for an item. `bad` gives a code whose checksum is off by one (a copied-wrong code)."""
    raw = json.dumps({k: item.get(k) for k in CODE_KEYS}, sort_keys=True, separators=(",", ":")).encode()
    checksum = (sum(raw) + (1 if bad else 0)) % 256
    return f"BL2({base64.b64encode(raw).decode()}-{checksum:02X})"


def read_code(code: str) -> tuple[dict | None, str | None]:
    """(the item's JSON, None) for a good code, or (None, the error text) when the code is damaged or not BL2."""
    try:
        if not (code.startswith("BL2(") and code.endswith(")")):
            raise ValueError("not BL2")
        text, checksum = code[4:-1].rsplit("-", 1)
        raw = base64.b64decode(text, validate=True)
        if sum(raw) % 256 != int(checksum, 16):
            raise ValueError("checksum")
        return json.loads(raw), None
    except (ValueError, TypeError):
        return None, CODE_ERROR


def _coded(entry: dict, card: dict | None = None, bad: bool = False) -> dict:
    """A gear entry as a build saves it: with the code of `card` (the entry itself when no card is given)."""
    return {**entry, "code": make_code(card or entry, bad)}


def _notes(preset: dict) -> list[str]:
    """Informational notes on a save or export (they don't make the build partial)."""
    bare = sum(1 for g in preset.get("gear") or [] if not g.get("code"))
    if not bare:
        return []
    one = bare == 1
    return [f"{bare} item{'' if one else 's'} {'was' if one else 'were'} saved without a code, so {'it' if one else 'they'} "
            f"can't be imported if you lose {'it' if one else 'them'}"]


# ---- links ----

def to_link(preset: dict) -> str:
    raw = json.dumps(preset, separators=(",", ":"), sort_keys=True).encode()
    return LINK_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


def from_link(text: str) -> dict:
    text = text.strip()
    if not text.startswith(LINK_PREFIX):
        raise ValueError("a link starts with RTSE-L1:")
    body = text[len(LINK_PREFIX):]
    try:
        data = zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return json.loads(data)
    except (ValueError, zlib.error) as ex:
        raise ValueError(f"that link is damaged ({ex})") from None


def _check(data) -> dict:
    """Light stand-in for loadouts.validate(): the shape the page relies on, not the full rule set."""
    if not isinstance(data, dict) or data.get("format") != "rtse.loadout":
        raise ValueError('not a build preset (format "rtse.loadout")')
    if data.get("version") not in (1, SCHEMA_VERSION):
        raise ValueError(f"unsupported preset version {data.get('version')}")
    if data.get("kind") not in ("loadout", "character"):
        raise ValueError("kind must be loadout or character")
    if data.get("class_key") not in (None, *CLASSES):
        raise ValueError(f"unknown class {data.get('class_key')!r}")
    left_out = data.get("left_out", [])
    if not isinstance(left_out, list) or len(left_out) > 20 or any(not isinstance(s, str) or len(s) > 200 for s in left_out):
        raise ValueError("left_out is up to 20 lines of up to 200 characters")
    checked = dict(data)
    checked["name"] = _clean_name(data.get("name"))
    return checked


def _store(preset: dict, overwrite: bool) -> dict:
    index = _index(preset["name"])
    if index is not None and not overwrite:
        raise ValueError(f'a build named "{preset["name"]}" already exists')
    if index is None and len(PRESETS) >= MAX_PRESETS:
        raise ValueError(f"at most {MAX_PRESETS} builds can be saved")
    size = len(to_link(preset))
    if size > LINK_LIMIT_STORED:
        raise ValueError(f"this build's link is {size:,} characters; a saved build's link is limited to {LINK_LIMIT_STORED:,}")
    if index is None:
        PRESETS.append(preset)
    else:
        PRESETS[index] = preset
    return {"preset": _summary(preset), "presets": _summaries()}


def _capture(name: str, include: dict, kind: str = "loadout", unreadable: bool = False) -> dict:
    """The open save as a preset. `unreadable` leaves out the one currency the mock cannot read, and says so in left_out."""
    everything = kind == "character"
    preset = {"format": "rtse.loadout", "version": SCHEMA_VERSION, "kind": kind, "name": name, "created": _now(), "class_key": CLASS_KEY}
    if everything:
        wanted = set(CHAR_FIELDS)
    else:
        wanted = set()
        if include.get("character"):
            wanted |= {"level", "skill_points"}
        if include.get("currencies"):
            wanted |= set(CURRENCY_FIELDS)
    if wanted:
        skip = UNREADABLE[0] if unreadable else None
        preset["character"] = {f: CHARACTER[f] for f in CHAR_FIELDS if f in wanted and f != skip}
        if skip in wanted:
            preset["left_out"] = [UNREADABLE[1]]
    if everything or include.get("skills"):
        preset["skills"] = [{"path": path, "level": info["level"]} for path, info in TREE.items()]
    if everything or include.get("equipped") or include.get("backpack"):
        preset["gear"] = [_entry(item) for item in INVENTORY if everything or include.get(item["location"].lower())]
    return preset


# ---- plan ----

def _is_candidate(entry: dict, item: dict) -> bool:
    if item["class"] != entry["class"]:
        return False
    if entry.get("balance"):
        return item["balance"] == entry["balance"]
    return item["definition"] == entry.get("definition")


def _same(entry: dict, item: dict) -> bool:
    return entry["level"] == item["level"] and all(item["parts"].get(slot) == want for slot, want in entry["parts"].items())


def _match(entries: list[dict], skip: set[int]) -> dict[int, dict]:
    """Gear entry index -> live item. Exact matches first, then the best score; each live item is used once."""
    assigned: dict[int, dict] = {}
    used: set[str] = set()
    for index, entry in enumerate(entries):
        if index in skip:
            continue
        for item in INVENTORY:
            if item["id"] not in used and _is_candidate(entry, item) and _same(entry, item):
                assigned[index] = item
                used.add(item["id"])
                break
    for index, entry in enumerate(entries):
        if index in skip or index in assigned:
            continue
        best = None
        for position, item in enumerate(INVENTORY):
            if item["id"] in used or not _is_candidate(entry, item):
                continue
            score = sum(1 for slot, want in entry["parts"].items() if item["parts"].get(slot) == want) + (item["level"] == entry["level"])
            key = (score, item["location"] == entry["location"], -position)
            if best is None or key > best[0]:
                best = (key, item)
        if best is not None:
            assigned[index] = best[1]
            used.add(best[1]["id"])
    return assigned


def _plan(name: str, sections=None, over_max: bool = False, skip=()) -> dict:
    preset = _find(name)
    skip = sorted(set(skip))
    present = [s for s in SECTIONS if s in preset]
    chosen = present if sections is None else [s for s in SECTIONS if s in sections and s in present]
    steps: list[dict] = []
    unchanged: list[dict] = []
    missing: list[dict] = []
    blocked: list[dict] = []

    def add(section, kind, label, target, before, after, params, entry=None, side_effect=None):
        steps.append({"n": len(steps) + 1, "section": section, "kind": kind, "label": label, "target": target,
                      "before": before, "after": after, "params": params, "entry": entry, "side_effect": side_effect})

    def block(section, label, reason, kind):
        blocked.append({"section": section, "label": label, "reason": reason, "kind": kind})

    cls = preset.get("class_key")
    match = None if cls is None else cls == CLASS_KEY

    if "character" in chosen:
        for field in CHAR_FIELDS:
            if field not in preset["character"]:
                continue
            want, have = preset["character"][field], CHARACTER[field]
            if want == have:
                unchanged.append({"section": "character", "label": CHAR_LABEL[field], "reason": f"already {have:,}"})
            elif field == "level" and want > LEVEL_CAP:
                block("character", CHAR_LABEL[field], f"level {want} is above the cap of {LEVEL_CAP}", "level")
            else:
                side = None
                if field == "level" and want in MIN_XP:
                    side = f"Experience becomes {MIN_XP[want]:,} (the game's minimum for level {want})"
                add("character", f"character.{field}", CHAR_LABEL[field], field, have, want, {"field": field, "value": want},
                    side_effect=side)

    if "skills" in chosen and match is False:
        block("skills", "Skills", f"this build is for {_cap(cls)}, you are {_cap(CLASS_KEY)}", "class")
    elif "skills" in chosen:
        for skill in preset["skills"]:
            path, want = skill["path"], skill["level"]
            info = TREE.get(path)
            if info is None:
                block("skills", path.rsplit(".", 1)[-1], f"not in the {_cap(CLASS_KEY)} skill tree", "skill")
                continue
            have, top = info["level"], info["max"]
            if want == have:
                unchanged.append({"section": "skills", "label": info["name"], "reason": f"already {have}"})
            elif want > top and not over_max:
                block("skills", info["name"], f"above its maximum {top}; tick Allow above max", "max")
            else:
                add("skills", "skills.level", info["name"], path, have, want, {"id": path, "level": want, "over_max": want > top})

    if "gear" in chosen:
        assigned = _match(preset["gear"], set(skip))
        for index, entry in enumerate(preset["gear"]):
            label = f"{entry.get('name') or entry['class']} ({entry['location']})"
            if index in skip:
                unchanged.append({"section": "gear", "label": label, "reason": "skipped by you"})
                continue
            item = assigned.get(index)
            if item is None:
                missing.append({"entry": index, "name": entry.get("name") or entry["class"], "kind": entry["kind"], "class": entry["class"],
                                "location": entry["location"], "balance": entry.get("balance"), "level": entry["level"],
                                "has_code": bool(entry.get("code")), "can_import": bool(entry.get("code"))})
                continue
            if _same(entry, item):
                unchanged.append({"section": "gear", "label": label, "reason": "already matches"})
                continue
            if item["location"] != entry["location"]:
                block("gear", label, f"it is in your {item['location']}; RTSE can't move items. Equip or unequip it yourself. "
                      "Its parts and level are still planned.", "location")
            for slot, want in entry["parts"].items():
                have = item["parts"].get(slot)
                if want == have:
                    continue
                if want is None:
                    add("gear", "gear.part", f"{label}: {slot}", item["id"], have, None,
                        {"id": item["id"], "slot": slot, "part": None, "mode": "legal"}, index)
                elif want not in LOADED:
                    block("gear", f"{label}: {slot}", "that part isn't loaded in the game right now", "part")
                else:
                    add("gear", "gear.part", f"{label}: {slot}", item["id"], have, want,
                        {"id": item["id"], "slot": slot, "part": want, "mode": LOADED[want]}, index)
            if entry["level"] != item["level"]:
                low, high = GEAR_LEVEL
                if low <= entry["level"] <= high:
                    add("gear", "gear.level", f"{label}: level", item["id"], item["level"], entry["level"],
                        {"id": item["id"], "level": entry["level"]}, index)
                else:
                    block("gear", f"{label}: level", f"level {entry['level']} is outside the game's {low}-{high}", "level")

    if len(steps) > MAX_STEPS:
        raise ValueError(f"a build with more than {MAX_STEPS} steps can't be applied at once")
    key = [steps, blocked, missing, chosen, bool(over_max), skip, preset["name"]]
    plan_id = hashlib.sha256(json.dumps(key, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "plan_id": plan_id,
        "preset": {"name": preset["name"], "kind": preset["kind"], "class_key": cls},
        "class": {"preset": cls, "current": CLASS_KEY, "match": match},
        "sections": chosen,
        "steps": steps,
        "unchanged": unchanged,
        "missing": missing,
        "blocked": blocked,
        "summary": {"changes": len(steps), "unchanged": len(unchanged), "missing": len(missing), "blocked": len(blocked),
                    "importable": sum(1 for m in missing if m["can_import"])},
        "unverified": UNVERIFIED,
    }


# ---- apply ----

def _read(step: dict):
    kind, p = step["kind"], step["params"]
    if kind.startswith("character."):
        return CHARACTER[p["field"]]
    if kind == "skills.level":
        return TREE[p["id"]]["level"]
    if kind == "gear.part":
        return _live(p["id"])["parts"].get(p["slot"])
    return _live(p["id"])["level"]


def _write(step: dict, value) -> None:
    kind, p = step["kind"], step["params"]
    if kind.startswith("character."):
        CHARACTER[p["field"]] = value
        if p["field"] == "level" and value in MIN_XP:  # the game sets the minimum XP with the level (side_effect)
            CHARACTER["xp"] = MIN_XP[value]
    elif kind == "skills.level":
        TREE[p["id"]]["level"] = value
    elif kind == "gear.part":
        _live(p["id"])["parts"][p["slot"]] = value
    else:
        _live(p["id"])["level"] = value


def _drifted(step: dict) -> bool:
    """True when the game no longer holds what the plan saw before this step (or the thing is gone)."""
    try:
        return _read(step) != step["before"]
    except LookupError:
        return True


def _reply(run: dict) -> dict:
    """The reply shape the page reads: the whole run, every step, the chunk this request ran."""
    rows = run["rows"]
    keys = ("ok", "failed", "rolled_back", "rollback_failed", "not_run", "pending")
    return {
        "run_id": run["id"], "name": run["name"], "on_failure": run["on_failure"], "state": run["state"],
        "ok": run["ok"], "done": run["state"] != "running", "total": len(rows), "cursor": run["cursor"],
        "chunk": run["chunk"], "chunk_from": run["chunk_from"], "chunk_to": run["chunk_to"], "chunks": run["chunks"],
        "rolled_back": run["rolled_back"], "message": run["message"], "steps": rows,
        # the mock has no journal, like the real one before stream A: earlier chunks are never found in History
        "history": False if run["state"] in ("failed", "stopped") and run["chunk"] > 1 else None,
        "summary": {k: sum(1 for r in rows if r["status"] == k) for k in keys},
        "missing": run["missing"], "blocked": run["blocked"],
    }


def _finish(run: dict) -> None:
    """The run is over: steps that never ran are not_run, and the run is the last report."""
    for row in run["rows"]:
        if row["status"] == "pending":
            row["status"] = "not_run"
    LAST["report"] = {"name": run["name"], **_reply(run)}


def _earlier(number: int) -> str:
    if number <= 1:
        return ""
    which = "chunk 1" if number == 2 else f"chunks 1-{number - 1}"
    return (f" Earlier {which} stay applied. RTSE can't find them in History, so the result lists each value before and "
            "after: put back by hand whatever you don't want.")


def _start(params: dict) -> dict:
    """POST /api/loadouts/apply with a name: plans the run, then runs its first chunk."""
    name = params["name"]
    plan = _plan(name, _list_param(params.get("sections")), _truthy(params.get("over_max")), _int_list(params.get("skip")))
    if params.get("plan_id") != plan["plan_id"]:
        raise ValueError("the game changed since the preview - preview again")
    on_failure = params.get("on_failure", "rollback")
    if on_failure not in ("rollback", "keep"):
        raise ValueError("on_failure is rollback or keep")
    steps = plan["steps"]
    run = {
        "id": secrets.token_hex(16), "name": name, "on_failure": on_failure, "steps": steps,
        # mock rule: a build whose name contains "fail" is refused at its sixth step (its last one, if it has fewer)
        "refuse_at": min(5, len(steps) - 1) if steps and "fail" in name.lower() else None,
        "rows": [{"n": s["n"], "label": s["label"], "status": "pending", "before": s["before"], "after": s["after"], "error": None}
                 for s in steps],
        "done": [], "state": "running" if steps else "done", "ok": True, "cursor": 0, "chunk": 0, "chunk_from": 0,
        "chunk_to": 0, "chunks": [], "rolled_back": False, "missing": plan["missing"], "blocked": plan["blocked"],
        "message": "Nothing to apply: the save already matches this build." if not steps else "",
    }
    RUNS[run["id"]] = run
    while len(RUNS) > 20:
        RUNS.pop(next(iter(RUNS)))
    if not steps:
        _finish(run)
        return _reply(run)
    return _chunk(run)


def _continue(params: dict) -> dict:
    """POST /api/loadouts/apply with run_id and cursor: runs the next chunk of that run."""
    run = RUNS.get(str(params.get("run_id", "")))
    if run is None:
        raise LookupError("that run is unknown or has expired")
    time.sleep(CHUNK_DELAY)
    return _chunk(run, int(params.get("cursor", -1)))


def _chunk(run: dict, cursor: int | None = None) -> dict:
    """Runs one chunk of CHUNK steps from the run's cursor. A game that changed since the plan stops the run first."""
    if run["state"] != "running":
        return _reply(run)
    if cursor is not None and cursor != run["cursor"]:
        raise ValueError(f"the run is at step {run['cursor']}, not {cursor}; reload the run")
    total = len(run["rows"])
    first = run["cursor"]
    last = min(first + CHUNK, total)
    number = run["chunk"] + 1
    run["chunk"], run["chunk_from"], run["chunk_to"] = number, first + 1, last
    if any(_drifted(run["steps"][i]) for i in range(first, last)):
        run["state"], run["ok"] = "stopped", False
        run["message"] = (f"The game changed between chunks, so chunk {number} was not run and nothing more was applied."
                          + _earlier(number))
        _finish(run)
        return _reply(run)
    failed = None
    for i in range(first, last):
        step, row = run["steps"][i], run["rows"][i]
        try:
            if i == run["refuse_at"]:
                raise _Refused(f"the game refused the write (mock: a build named *fail* is refused at step {i + 1})")
            _write(step, step["after"])
            if _read(step) != step["after"]:
                raise _Refused(f"the game reports {_read(step)!r} instead")
        except _Refused as ex:
            row["status"], row["error"] = "failed", str(ex)
            failed = i
            break
        row["status"] = "ok"
        run["done"].append(i)
    run["cursor"] = last
    status = "applied"
    if failed is not None:
        run["ok"] = False
        n = run["steps"][failed]["n"]
        if run["on_failure"] == "rollback":
            # only this chunk is undone; the chunks before it stay applied (History can undo them)
            for i in reversed([j for j in run["done"] if j >= first]):
                _write(run["steps"][i], run["steps"][i]["before"])
                run["rows"][i]["status"] = "rolled_back"
            status = "rolled_back"
            run["rolled_back"] = True
            run["message"] = (f"Step {n} failed. The steps that worked were rolled back, and the steps after it did not run."
                              if number == 1 else
                              f"Step {n} failed. Chunk {number} was rolled back.{_earlier(number)}")
        else:
            status = "kept_partial"
            run["message"] = (f"Step {n} failed. The steps that worked were kept, and the steps after it did not run."
                              if number == 1 else
                              f"Step {n} failed. Chunk {number} kept the steps that worked, and the steps after it did not run;"
                              f"{_earlier(number)}")
        run["state"] = "failed"
    elif last >= total:
        run["state"] = "done"
        run["message"] = f"Applied all {total} steps in {number} chunk{'s' if number != 1 else ''}. Not tested in a live game: check the save in game."
    run["chunks"].append({"chunk": number, "from": first + 1, "to": last, "status": status})
    if run["state"] != "running":
        _finish(run)
    return _reply(run)


# ---- seeds: fake saved builds, made from the state above before anything is applied ----

_SNIPER_ENTRY = {
    "kind": "weapon", "class": "WillowWeapon", "name": "Sniper of Hellfire", "location": "Equipped",
    "balance": SNIPER[0], "definition": SNIPER[1], "manufacturer": SNIPER[2],
}
_PISTOL_ENTRY = {
    "kind": "weapon", "class": "WillowWeapon", "name": "Lucky Pistol", "location": "Equipped",
    "balance": PISTOL[0], "definition": PISTOL[1], "manufacturer": PISTOL[2],
}
_SHOTGUN_ENTRY = {
    "kind": "weapon", "class": "WillowWeapon", "name": "Torgue Shotgun", "location": "Equipped",
    "balance": SHOTGUN[0], "definition": SHOTGUN[1], "manufacturer": SHOTGUN[2],
}
# the missing gear of the crit build, each saved with its item code (the import screens read these)
ROCKET = _coded({"kind": "weapon", "class": "WillowWeapon", "name": "Rocket Launcher", "location": "Backpack", "balance": LAUNCHER[0],
                 "definition": LAUNCHER[1], "manufacturer": LAUNCHER[2], "level": 40, "parts": {"Body": "GD_Weap_Launchers.Body.Body_01"}})
HSHIELD = _coded({"kind": "item", "class": "WillowShield", "name": "Hyperion Shield", "location": "Backpack",
                  "balance": "GD_Shields.Balance.Shield_Hyperion", "definition": "GD_Shields.A_Item.Shield_Hyperion",
                  "manufacturer": "GD_Manufacturer.ManufacturerDefinition.Hyperion", "level": 42,
                  "parts": {"Body": "GD_Shields.Body.Shield_Body_02"}}, bad=True)  # a copied-wrong code: the checksum fails
_JAKOBS = {"kind": "weapon", "class": "WillowWeapon", "name": "Jakobs Pistol", "location": "Backpack",
           "balance": "GD_Weap_Pistol.Balance.PS_Jakobs_Rare", "definition": "GD_Weap_Pistol.A_Weapons.WeaponType_Pistol",
           "manufacturer": "GD_Manufacturer.ManufacturerDefinition.Jakobs", "level": 24, "parts": {"Body": "GD_Weap_Pistol.Body.Body_02"}}
# its code is for a shield: the import reads it, but it is not a weapon, so the game's new item is not the build's
JAKOBS = _coded(_JAKOBS, card={**_JAKOBS, "kind": "item", "class": "WillowShield", "name": "Shield Beta"})
GRENADE = {"kind": "item", "class": "WillowGrenadeMod", "name": "Grenade Mod", "location": "Backpack",
           "balance": "GD_Grenades.Balance.GM_Hyperion", "definition": "GD_Grenades.A_Item.GM_Hyperion", "manufacturer": None,
           "level": 35, "parts": {}}  # saved without a code
PRESETS: list[dict] = [
    {
        "format": "rtse.loadout", "version": 2, "kind": "loadout", "name": "Siren crit build", "created": "2026-10-30T19:12:00Z",
        "class_key": "siren",
        "character": {"level": 72, "xp": 1234567, "skill_points": 3, "money": 250000, "eridium": 12},
        "skills": [
            {"path": "GD_Siren_Skills.Phasewalk.Duration", "level": 5},
            {"path": "GD_Siren_Skills.Phasewalk.Recharge", "level": 7},
            {"path": "GD_Siren_Skills.Phasewalk.Cloak", "level": 1},
            {"path": "GD_Siren_Skills.Hyperion.Overdrive", "level": 2},
        ],
        "gear": [
            {**_SNIPER_ENTRY, "level": 36, "parts": {
                "Body": "GD_Weap_SniperRifles.Body.Body_01", "Grip": "GD_Weap_SniperRifles.Grip.Grip_02",
                "Barrel": "GD_Weap_SniperRifles.Barrel.Barrel_05", "Stock": "GD_Weap_SniperRifles.Stock.Stock_03",
                "Sight": "GD_Weap_SniperRifles.Sight.Sight_Scope_04", "Material": "GD_Weap_SniperRifles.Material.Mat_Gold"}},
            {"kind": "weapon", "class": "WillowWeapon", "name": "Lucky Pistol", "location": "Equipped", "balance": PISTOL[0],
             "definition": PISTOL[1], "manufacturer": PISTOL[2], "level": 30, "parts": {
                 "Body": "GD_Weap_Pistol.Body.Body_03", "Grip": "GD_Weap_Pistol.Grip.Grip_01", "Barrel": "GD_Weap_Pistol.Barrel.Barrel_01"}},
            {"kind": "weapon", "class": "WillowWeapon", "name": "Assault Rifle", "location": "Equipped", "balance": RIFLE[0],
             "definition": RIFLE[1], "manufacturer": RIFLE[2], "level": 30, "parts": {
                 "Body": "GD_Weap_AssaultRifle.Body.Body_02", "Barrel": "GD_Weap_AssaultRifle.Barrel.Barrel_04"}},
            {"kind": "item", "class": "WillowShield", "name": "Shield Alpha", "location": "Backpack", "balance": SHIELD[0],
             "definition": SHIELD[1], "manufacturer": SHIELD[2], "level": 45, "parts": {
                 "Body": "GD_Shields.Body.Shield_Body_01", "Battery": "GD_Shields.Battery.Battery_02"}},
            {"kind": "item", "class": "WillowClassMod", "name": "Siren Mod", "location": "Backpack", "balance": MOD[0],
             "definition": MOD[1], "manufacturer": None, "level": 50, "parts": {}},
            ROCKET,
            HSHIELD,
            JAKOBS,
            GRENADE,
        ],
    },
    {
        # six steps, all on gear the Siren crit build does not touch, so the drill works after that apply too:
        # step 6 (the pistol's barrel) is the one refused, so chunk 1 stays applied and chunk 2 is rolled back
        "format": "rtse.loadout", "version": 1, "kind": "loadout", "name": "Siren rig - fail drill", "created": "2026-10-31T08:00:00Z",
        "class_key": "siren",
        "gear": [
            {**_SHOTGUN_ENTRY, "level": 28, "parts": {"Body": "GD_Weap_Shotgun.Body.Body_01", "Barrel": "GD_Weap_Shotgun.Barrel.Barrel_02"}},
            {**_PISTOL_ENTRY, "level": 30, "parts": {
                "Body": "GD_Weap_Pistol.Body.Body_01", "Grip": "GD_Weap_Pistol.Grip.Grip_02", "Barrel": "GD_Weap_Pistol.Barrel.Barrel_02"}},
        ],
    },
    {
        # stored with the currencies it could not read left out: the list tags it partial
        "format": "rtse.loadout", "version": 1, "kind": "loadout", "name": "Siren cash run", "created": "2026-10-29T14:20:00Z",
        "class_key": "siren", "left_out": [UNREADABLE[1]],
        "character": {"level": 70, "skill_points": 0, "money": 180000, "eridium": 12},
        "skills": [{"path": "GD_Siren_Skills.Phasewalk.Duration", "level": 3}],
    },
    {
        "format": "rtse.loadout", "version": 1, "kind": "loadout", "name": "Soldier tank build", "created": "2026-10-12T10:00:00Z",
        "class_key": "soldier",
        "character": {"level": 65},
        "skills": [{"path": "GD_Soldier_Skills.Tank.Armor", "level": 5}],
    },
]
PRESETS.append(_capture("Siren L70 2026-10-28", {}, kind="character"))


# ---- import missing items from their codes: preview (read-only, chunked), then import (chunked) ----

IMPORT_CHUNK = 2  # import preview: codes read per request
IMPORT_DELAY = 0.9  # seconds a continuation waits, so the progress shows in the screenshots
IMPORT_UNVERIFIED = "Reading and importing codes use game calls nobody has tested in a live game yet"
STALE = "the game changed since the preview - preview again"
# the last import preview: its plan_id (the import preview's own, not the build preview's), its rows, skipped, codes
IMPORT: dict = {"plan_id": None, "name": None, "rows": [], "skipped": [], "codes": {}}
IMPORT_RUNS: dict[str, dict] = {}  # run_id -> an import in progress or finished (the last 20 are kept)


def _card(body: dict) -> dict:
    return {
        "name": body["name"], "class": body["class"], "level": body["level"],
        "build": [{"slot": slot, "name": part.rsplit(".", 1)[-1].replace("_", " "), "group": part.split(".")[1], "meshes": []}
                  for slot, part in body["parts"].items() if part],
        "type_path": body["definition"],
    }


def _preview_row(index: int, entry_index: int, entry: dict, code: str) -> dict:
    row = {"index": index, "entry": entry_index, "name": entry.get("name") or entry["class"], "location": entry["location"],
           "level": entry["level"], "warning": None, "uid_clash": False}
    body, error = read_code(code)
    if error:
        return {**row, "ok": False, "error": error, "card": None, "matches": None}
    return {**row, "ok": True, "error": None, "card": _card(body), "matches": body["class"] == entry["class"]}


def _import_preview(params: dict) -> dict:
    """GET import_preview: start=0 reads the build's missing codes afresh; later chunks need the plan_id it returned."""
    name = params["name"]
    start = int(params.get("start") or 0)
    if start == 0:
        preset = _find(name)
        rows, skipped, codes = [], [], {}
        for missing in _plan(name)["missing"]:
            entry = preset["gear"][missing["entry"]]
            if entry.get("code"):
                codes[missing["entry"]] = entry["code"]
                rows.append(_preview_row(len(rows), missing["entry"], entry, entry["code"]))
            else:
                skipped.append({"entry": missing["entry"], "name": missing["name"], "reason": "no code was saved for this item"})
        IMPORT.update(plan_id=secrets.token_hex(8), name=name, rows=rows, skipped=skipped, codes=codes)
    elif params.get("plan_id") != IMPORT["plan_id"] or name != IMPORT["name"]:
        raise ValueError(STALE)
    else:
        time.sleep(IMPORT_DELAY)
    rows = IMPORT["rows"]
    nxt = start + IMPORT_CHUNK if start + IMPORT_CHUNK < len(rows) else None
    reply = {"plan_id": IMPORT["plan_id"], "found": len(rows), "next": nxt,
             "entries": rows[start:start + IMPORT_CHUNK], "unverified": IMPORT_UNVERIFIED}
    if start == 0:
        reply["skipped"] = IMPORT["skipped"]
    return reply


def _new_item(body: dict) -> dict:
    """The item a code gives once it is in the backpack: a fresh id, never equipped."""
    return {"id": f"Transient.{body['class']}_{secrets.randbelow(900) + 100}", "kind": body["kind"], "class": body["class"],
            "name": body["name"], "location": "Backpack", "level": body["level"], "balance": body["balance"],
            "definition": body["definition"], "manufacturer": body["manufacturer"], "parts": dict(body["parts"])}


def _import_reply(run: dict) -> dict:
    results = run["results"]
    keys = ("imported", "failed", "removed", "remove_failed", "pending")
    return {
        "run_id": run["run_id"], "name": run["name"], "state": run["state"], "total": len(results), "cursor": run["cursor"],
        "chunk": run["chunk"], "results": copy.deepcopy(results),
        "summary": {k: sum(1 for r in results if r["status"] == k) for k in keys},
        "message": run.get("message") or f"Importing {run['cursor']} of {len(results)}...",
    }


def _import_chunk(run: dict) -> dict:
    """Imports one code (chunk of 1). A code that reads but is not the build's item is imported and removed again."""
    if run["state"] != "running":
        return _import_reply(run)
    i = run["cursor"]
    row, result = run["rows"][i], run["results"][i]
    run["chunk"] += 1
    body, error = read_code(row["code"])
    if error:
        result.update(status="failed", error=error)
    elif row["matches"] is False:
        result.update(status="removed", error=f"the new item is a {body['class']}, not the build's item, so RTSE removed it again")
    else:
        item = _new_item(body)
        INVENTORY.append(item)
        result.update(status="imported", id=item["id"], item_name=item["name"])
    run["cursor"] += 1
    if run["cursor"] >= len(run["results"]):
        counts = {k: sum(1 for r in run["results"] if r["status"] == k) for k in ("imported", "removed", "failed")}
        bits = [f"{counts['imported']} imported into the backpack"]
        if counts["removed"]:
            bits.append(f"{counts['removed']} removed again because it is not the build's item")
        if counts["failed"]:
            bits.append(f"{counts['failed']} failed")
        run["state"] = "done"
        run["message"] = "; ".join(bits) + ". RTSE can't equip them."
    return _import_reply(run)


def _import_start(params: dict) -> dict:
    """POST import_missing {name, plan_id, entries}: checks the plan_id, then runs the first code."""
    name = params["name"]
    if params.get("plan_id") != IMPORT["plan_id"] or name != IMPORT["name"]:
        raise ValueError(STALE)
    by_entry = {row["entry"]: row for row in IMPORT["rows"]}
    picked = []
    for entry in sorted(set(_int_list(params.get("entries")))):
        row = by_entry.get(entry)
        if row is None or not row["ok"]:
            raise ValueError(f"gear entry {entry} has no code that can be read, so it can't be imported")
        picked.append({**row, "code": IMPORT["codes"][entry]})
    if not picked:
        raise ValueError("tick at least one item to import")
    run = {"run_id": secrets.token_hex(16), "name": name, "state": "running", "rows": picked, "cursor": 0, "chunk": 0,
           "results": [{"entry": r["entry"], "name": r["name"], "status": "pending", "id": None, "item_name": None, "error": None}
                       for r in picked]}
    IMPORT_RUNS[run["run_id"]] = run
    while len(IMPORT_RUNS) > 20:
        IMPORT_RUNS.pop(next(iter(IMPORT_RUNS)))
    return _import_chunk(run)


def _import_continue(params: dict) -> dict:
    """POST import_missing {run_id, cursor}: runs the next code of that import."""
    run = IMPORT_RUNS.get(str(params.get("run_id", "")))
    if run is None:
        raise LookupError("that import is unknown or has expired")
    if int(params.get("cursor", -1)) != run["cursor"]:
        raise ValueError(f"the import is at code {run['cursor']}, not {params.get('cursor')}; reload the import")
    if run["state"] == "running":
        time.sleep(IMPORT_DELAY)
    return _import_chunk(run)


# ---- routes ----

def handle(path: str, params: dict, shared: dict) -> dict | None:
    if path == "/api/debug/loadouts":
        return {"saved_to": "(mock) rtse/debug_loadouts.json", "items": len(INVENTORY), "presets": len(PRESETS),
                "last_report": LAST["report"] is not None}
    if path == "/api/loadouts":
        return {"presets": _summaries(), "warnings": [], "class_key": CLASS_KEY, "schema_version": SCHEMA_VERSION,
                "can_import_missing": True}
    if path == "/api/loadouts/get":
        return {"preset": copy.deepcopy(_find(params["name"]))}
    if path == "/api/loadouts/link":
        return {"link": to_link(_find(params["name"]))}
    if path == "/api/loadouts/plan":
        return _plan(params["name"], _list_param(params.get("sections")), _truthy(params.get("over_max")), _int_list(params.get("skip")))
    if path == "/api/loadouts/export_character":
        name = f"{CLASS_KEY.capitalize()} L{CHARACTER['level']} {datetime.now(timezone.utc):%Y-%m-%d}"
        preset = _capture(name, {}, kind="character", unreadable=True)
        left_out = list(preset.get("left_out") or [])
        return {"preset": preset, "link": to_link(preset), "warnings": left_out, "notes": _notes(preset)}
    if path == "/api/loadouts/save":
        include = params.get("include") or {}
        preset = _capture(_clean_name(params["name"]), include, unreadable=_truthy(include.get("currencies")))
        left_out = list(preset.get("left_out") or [])
        if left_out and not _truthy(params.get("allow_partial")):
            return {"saved": False, "partial": True, "warnings": left_out}  # nothing is stored until the user says so
        return {"saved": True, **_store(preset, _truthy(params.get("overwrite"))), "warnings": left_out, "notes": _notes(preset)}
    if path == "/api/loadouts/import":
        if ("preset" in params) == ("link" in params):
            raise ValueError("send exactly one of preset or link")
        if "link" in params and len(str(params["link"])) > LINK_LIMIT_IMPORT:
            raise ValueError(f"that link is {len(params['link']):,} characters; the limit is {LINK_LIMIT_IMPORT:,}")
        preset = _check(from_link(params["link"]) if "link" in params else params["preset"])
        if params.get("rename"):
            preset["name"] = _clean_name(params["rename"])
        return {**_store(preset, _truthy(params.get("overwrite"))), "warnings": list(preset.get("left_out") or [])}
    if path == "/api/loadouts/delete":
        _find(params["name"])
        PRESETS.pop(_index(params["name"]))
        return {"presets": _summaries()}
    if path == "/api/loadouts/apply":
        # a request with run_id continues that run by one chunk; one with a name starts a run (and runs its first chunk)
        return _continue(params) if "run_id" in params else _start(params)
    if path == "/api/loadouts/run":
        run = RUNS.get(str(params.get("run_id", "")))
        if run is None:
            raise LookupError("that run is unknown or has expired")
        return _reply(run)
    if path == "/api/loadouts/import_preview":
        return _import_preview(params)
    if path == "/api/loadouts/import_missing":
        return _import_continue(params) if "run_id" in params else _import_start(params)
    if path == "/api/loadouts/import_run":
        run = IMPORT_RUNS.get(str(params.get("run_id", "")))
        if run is None:
            raise LookupError("that import is unknown or has expired")
        return _import_reply(run)
    return None
