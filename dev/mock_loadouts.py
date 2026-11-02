"""Stand-in for rtse/loadouts.py (build presets) so the Builds page can be worked on without the game.

Answers every /api/loadouts... route and /api/debug/loadouts in the response shapes of the spec. The inventory, the
skill tree and the character are fake and live in memory; nothing is written to disk. Loaded by mock_server.py like
every mock_<feature>.py. mock_server maps only ValueError (400) and LookupError (404), so the real routes' 409 (name
taken, stale plan) and 501 (no item-code import yet) come back from this mock as 400.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import zlib
from datetime import datetime, timezone

LINK_PREFIX = "RTSE-L1:"
SCHEMA_VERSION = 1
MAX_PRESETS = 100
MAX_STEPS = 600
MAX_NAME = 64
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
})


class _Refused(Exception):
    """One game write refused (mock)."""


LAST: dict = {"report": None}


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
    return {
        "name": preset["name"], "kind": preset["kind"], "class_key": preset.get("class_key"), "created": preset.get("created"),
        "counts": {
            "gear": len(gear),
            "equipped": sum(1 for g in gear if g["location"] == "Equipped"),
            "backpack": sum(1 for g in gear if g["location"] == "Backpack"),
            "skills": len(preset.get("skills") or []),
            "character": len(preset.get("character") or {}),
        },
    }


def _summaries() -> list[dict]:
    return [_summary(p) for p in PRESETS]


def _entry(item: dict) -> dict:
    return {
        "kind": item["kind"], "class": item["class"], "name": item["name"], "location": item["location"],
        "balance": item["balance"], "definition": item["definition"], "manufacturer": item["manufacturer"],
        "level": item["level"], "parts": dict(item["parts"]),
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
    if data.get("version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported preset version {data.get('version')}")
    if data.get("kind") not in ("loadout", "character"):
        raise ValueError("kind must be loadout or character")
    if data.get("class_key") not in (None, *CLASSES):
        raise ValueError(f"unknown class {data.get('class_key')!r}")
    checked = dict(data)
    checked["name"] = _clean_name(data.get("name"))
    return checked


def _store(preset: dict, overwrite: bool) -> dict:
    index = _index(preset["name"])
    if index is not None and not overwrite:
        raise ValueError(f'a build named "{preset["name"]}" already exists')
    if index is None and len(PRESETS) >= MAX_PRESETS:
        raise ValueError(f"at most {MAX_PRESETS} builds can be saved")
    if index is None:
        PRESETS.append(preset)
    else:
        PRESETS[index] = preset
    return {"preset": _summary(preset), "presets": _summaries()}


def _capture(name: str, include: dict, kind: str = "loadout") -> dict:
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
        preset["character"] = {f: CHARACTER[f] for f in CHAR_FIELDS if f in wanted}
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

    def add(section, kind, label, target, before, after, params, entry=None):
        steps.append({"n": len(steps) + 1, "section": section, "kind": kind, "label": label, "target": target,
                      "before": before, "after": after, "params": params, "entry": entry})

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
                add("character", f"character.{field}", CHAR_LABEL[field], field, have, want, {"field": field, "value": want})

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
                                "location": entry["location"], "balance": entry.get("balance"), "level": entry["level"], "can_import": False})
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
        "summary": {"changes": len(steps), "unchanged": len(unchanged), "missing": len(missing), "blocked": len(blocked)},
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
    elif kind == "skills.level":
        TREE[p["id"]]["level"] = value
    elif kind == "gear.part":
        _live(p["id"])["parts"][p["slot"]] = value
    else:
        _live(p["id"])["level"] = value


def _apply(params: dict) -> dict:
    name = params["name"]
    plan = _plan(name, _list_param(params.get("sections")), _truthy(params.get("over_max")), _int_list(params.get("skip")))
    if params.get("plan_id") != plan["plan_id"]:
        raise ValueError("the game changed since the preview - preview again")
    on_failure = params.get("on_failure", "rollback")
    if on_failure not in ("rollback", "keep"):
        raise ValueError("on_failure is rollback or keep")
    steps = plan["steps"]
    # mock rule: a build whose name contains "fail" is refused at its third step (its last one, if it has fewer)
    refuse_at = min(2, len(steps) - 1) if steps and "fail" in name.lower() else None
    rows = [{"n": s["n"], "label": s["label"], "status": "not_run", "before": s["before"], "after": s["after"], "error": None} for s in steps]
    done: list[int] = []
    failed = None
    for index, step in enumerate(steps):
        if failed is not None:
            continue
        try:
            if index == refuse_at:
                raise _Refused("the game refused the write (mock: a build named *fail* is refused at its third step)")
            _write(step, step["after"])
            if _read(step) != step["after"]:
                raise _Refused(f"the game reports {_read(step)!r} instead")
        except _Refused as ex:
            rows[index]["status"] = "failed"
            rows[index]["error"] = str(ex)
            failed = index
            continue
        rows[index]["status"] = "ok"
        done.append(index)
    rolled = failed is not None and on_failure == "rollback"
    if rolled:
        for index in reversed(done):
            _write(steps[index], steps[index]["before"])
            rows[index]["status"] = "rolled_back"
    keys = ("ok", "failed", "rolled_back", "rollback_failed", "not_run")
    report = {
        "ok": failed is None, "rolled_back": rolled, "on_failure": on_failure, "steps": rows,
        "summary": {k: sum(1 for r in rows if r["status"] == k) for k in keys},
        "missing": plan["missing"], "blocked": plan["blocked"],
    }
    LAST["report"] = {"name": name, **report}
    return report


# ---- seeds: fake saved builds, made from the state above before anything is applied ----

_SNIPER_ENTRY = {
    "kind": "weapon", "class": "WillowWeapon", "name": "Sniper of Hellfire", "location": "Equipped",
    "balance": SNIPER[0], "definition": SNIPER[1], "manufacturer": SNIPER[2],
}
PRESETS: list[dict] = [
    {
        "format": "rtse.loadout", "version": 1, "kind": "loadout", "name": "Siren crit build", "created": "2026-10-30T19:12:00Z",
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
            {"kind": "weapon", "class": "WillowWeapon", "name": "Rocket Launcher", "location": "Backpack", "balance": LAUNCHER[0],
             "definition": LAUNCHER[1], "manufacturer": LAUNCHER[2], "level": 40, "parts": {"Body": "GD_Weap_Launchers.Body.Body_01"}},
        ],
    },
    {
        "format": "rtse.loadout", "version": 1, "kind": "loadout", "name": "Siren rig - fail drill", "created": "2026-10-31T08:00:00Z",
        "class_key": "siren",
        "character": {"skill_points": 1, "money": 300000},
        "skills": [{"path": "GD_Siren_Skills.Phasewalk.Duration", "level": 4}],
        "gear": [{**_SNIPER_ENTRY, "level": 33, "parts": {"Barrel": "GD_Weap_SniperRifles.Barrel.Barrel_03"}}],
    },
    {
        "format": "rtse.loadout", "version": 1, "kind": "loadout", "name": "Soldier tank build", "created": "2026-10-12T10:00:00Z",
        "class_key": "soldier",
        "character": {"level": 65},
        "skills": [{"path": "GD_Soldier_Skills.Tank.Armor", "level": 5}],
    },
]
PRESETS.append(_capture("Siren L70 2026-10-28", {}, kind="character"))


# ---- routes ----

def handle(path: str, params: dict, shared: dict) -> dict | None:
    if path == "/api/debug/loadouts":
        return {"saved_to": "(mock) rtse/debug_loadouts.json", "items": len(INVENTORY), "presets": len(PRESETS),
                "last_report": LAST["report"] is not None}
    if path == "/api/loadouts":
        return {"presets": _summaries(), "warnings": [], "class_key": CLASS_KEY, "schema_version": SCHEMA_VERSION,
                "can_import_missing": False}
    if path == "/api/loadouts/get":
        return {"preset": copy.deepcopy(_find(params["name"]))}
    if path == "/api/loadouts/link":
        return {"link": to_link(_find(params["name"]))}
    if path == "/api/loadouts/plan":
        return _plan(params["name"], _list_param(params.get("sections")), _truthy(params.get("over_max")), _int_list(params.get("skip")))
    if path == "/api/loadouts/export_character":
        name = f"{CLASS_KEY.capitalize()} L{CHARACTER['level']} {datetime.now(timezone.utc):%Y-%m-%d}"
        preset = _capture(name, {}, kind="character")
        return {"preset": preset, "link": to_link(preset)}
    if path == "/api/loadouts/save":
        preset = _capture(_clean_name(params["name"]), params.get("include") or {})
        return _store(preset, _truthy(params.get("overwrite")))
    if path == "/api/loadouts/import":
        if ("preset" in params) == ("link" in params):
            raise ValueError("send exactly one of preset or link")
        preset = _check(from_link(params["link"]) if "link" in params else params["preset"])
        if params.get("rename"):
            preset["name"] = _clean_name(params["rename"])
        return _store(preset, _truthy(params.get("overwrite")))
    if path == "/api/loadouts/delete":
        _find(params["name"])
        PRESETS.pop(_index(params["name"]))
        return {"presets": _summaries()}
    if path == "/api/loadouts/apply":
        return _apply(params)
    if path == "/api/loadouts/import_missing":
        _find(params["name"])
        raise ValueError("importing items from codes isn't available yet (needs the item-codes feature)")
    return None
