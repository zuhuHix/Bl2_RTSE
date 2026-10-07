"""Stand-in for the in-game RTSE server, with fake items but REAL part/mesh names, for front-end work.

    python -I dev/mock_server.py [port]

Then open http://127.0.0.1:<port>/?t=mock . Serves the real files from rtse/web, and the same JSON
API shapes as rtse/api.py. Parts come from rtse/web/models/parts.json, so the 3D view draws real meshes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

WEB_DIR = Path(__file__).resolve().parent.parent / "rtse" / "web"
TOKEN = "mock"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8766

MODELS = json.loads((WEB_DIR / "models" / "parts.json").read_text(encoding="utf-8"))
WEAPON_SLOTS = ["Body", "Grip", "Barrel", "Sight", "Stock", "Elemental", "Accessory1", "Accessory2", "Material"]
SHIELD_SLOTS = {"Alpha": "Body", "Beta": "Battery", "Gamma": "Capacitor", "Delta": "Accessory"}
GUN_TYPES = {  # model key -> (group word, weapon type path)
    "Pistol": ("Pistol", "GD_Weap_Pistol.A_Weapons.WeaponType_Pistol"),
    "SMG": ("SMG", "GD_Weap_SMG.A_Weapons.WeaponType_SMG"),
    "Assault Rifle": ("AssaultRifle", "GD_Weap_AssaultRifle.A_Weapons.WeaponType_AssaultRifle"),
    "Shotgun": ("Shotgun", "GD_Weap_Shotgun.A_Weapons.WeaponType_Shotgun"),
    "Sniper Rifle": ("SniperRifles", "GD_Weap_SniperRifles.A_Weapons.WeaponType_Sniper"),
    "Launcher": ("Launchers", "GD_Weap_Launchers.A_Weapons.WeaponType_Launcher"),
}
STATS = [
    ("InstantHitDamage", "Damage per shot / pellet", 76.9), ("ProjectilesPerShot", "Pellets per shot", 1),
    ("ShotCost", "Ammo used per shot", 2), ("ClipSize", "Magazine size", 20),
    ("FireInterval", "Time between shots (s) - lower is faster", 0.5), ("ReloadTime", "Reload time (s)", 1.95),
    ("Spread", "Spread", 0.175), ("AimError", "Aim error", 3.0), ("WeaponRange", "Range", 16384.0),
    ("ProjectileSpeedMultiplier", "Projectile speed multiplier", 1.92),
    ("StatusEffectChanceModifier", "Status effect chance multiplier", 1.35), ("StatusEffectDamage", "Status effect damage", 64.65),
    ("AdditionalRicochets", "Extra ricochets", 0), ("ExtraShotChance", "Extra shot chance", 0.0),
    ("AutomaticBurstCount", "Burst count", 0), ("MeleeDamage", "Melee damage", 2655.6),
    ("EquipTime", "Equip time (s)", 0.33), ("PutDownTime", "Put-down time (s)", 0.45),
]


def gun_slot(mesh: str) -> str | None:
    """Which weapon slot a gestalt mesh name belongs to."""
    if re.search(r"_Var[0-9A-Z]", mesh):
        return None  # variant add-ons ride along with a body part; they are not parts of their own
    if "Elemental" in mesh:
        return "Elemental"
    if re.search(r"_Body_", mesh) and not mesh.startswith("Acc_"):
        return "Body"
    if "_Grip_" in mesh and not mesh.startswith("Acc_"):
        return "Grip"
    if "_Barrel_" in mesh and not mesh.startswith("Acc_"):
        return "Barrel"
    if re.search(r"_(Scope|Sight)_", mesh) and not mesh.startswith("Acc_"):
        return "Sight"
    if "_Stock_" in mesh and not mesh.startswith("Acc_"):
        return "Stock"
    if mesh.startswith("Acc_Barrel"):
        return "Accessory1"
    if mesh.startswith("Acc_") or "_Exhaust_" in mesh:
        return "Accessory2"
    return None


def make_part(group: str, slot: str, mesh: str, cls: str = "WeaponPartDefinition") -> dict:
    return {"id": f"GD_{group}.{slot}.{mesh}", "name": mesh, "group": f"GD_{group}.{slot}", "class": cls, "meshes": [mesh]}


# every part, by model key then slot
PARTS: dict[str, dict[str, list[dict]]] = {}
BY_ID: dict[str, dict] = {}
for key, (group, _path) in GUN_TYPES.items():
    PARTS[key] = {slot: [] for slot in WEAPON_SLOTS}
    for mesh in MODELS[key]["parts"]:
        slot = gun_slot(mesh)
        if slot:
            part = make_part(f"Weap_{group}", slot, mesh)
            PARTS[key][slot].append(part)
            BY_ID[part["id"]] = part
PARTS["Shield"] = {slot: [] for slot in SHIELD_SLOTS}
for mesh in MODELS["Shield"]["parts"]:
    for slot, word in SHIELD_SLOTS.items():
        if f"Shield_{word}" in mesh:
            part = make_part("Item_Shields", slot, mesh, "ItemPartDefinition")
            PARTS["Shield"][slot].append(part)
            BY_ID[part["id"]] = part


def find_part(key: str, slot: str, *needles: str) -> dict | None:
    for part in PARTS[key][slot]:
        if all(n in part["name"] for n in needles):
            return part
    return None


class Item:
    def __init__(self, id_, cls, name, location, level, rarity, kind, key=None, parts=None, hide=()):
        self.id, self.cls, self.name, self.location = id_, cls, name, location
        self.level, self.rarity, self.kind, self.key = level, rarity, kind, key
        self.parts: dict[str, dict | None] = parts or {}
        self.hide = list(hide)
        self.stats = {s[0]: s[2] for s in STATS}
        self.overrides: dict[str, float] = {}

    @property
    def type_path(self):
        return GUN_TYPES[self.key][1] if self.kind == "weapon" else None


def gun(key, name, level, rarity, location, wants, hide=()):
    """wants: slot -> (needles...) picks the first matching real part."""
    parts = {slot: find_part(key, slot, *needles) for slot, needles in wants.items()}
    return Item(f"Transient.WillowWeapon_{len(ITEMS) + 1}", "WillowWeapon", name, location, level, rarity, "weapon", key, parts, hide)


ITEMS: list[Item] = []
ITEMS += [
    gun("Pistol", "Hyperion INVALID 34 Earnest Logan's Gun", 34, 9, "Equipped",
        {"Body": ("Body_Hyperion",), "Grip": ("Grip_Hyperion",), "Barrel": ("Barrel_Hyperion",), "Sight": ("Scope_Hyperion",), "Accessory1": ("Laser",)}),
    gun("Pistol", "Jakobs 14 Ornery Revolver", 14, 2, "Equipped",
        {"Body": ("Body_Jakobs",), "Grip": ("Grip_Jakobs",), "Barrel": ("Barrel_Dahl",), "Sight": ("Scope_Tediore",)}, hide=("MoonClip", "Bullet")),
    gun("Launcher", "Torgue 40 Nukem Launcher", 40, 7, "Equipped",
        {"Body": ("Body_Torgue",), "Grip": ("Grip_Torgue",), "Barrel": ("Barrel_Torgue",), "Sight": ("Scope_Torgue",), "Accessory2": ("Exhaust_Torgue",)}),
    Item("Transient.WillowShield_4", "WillowShield", "Tediore Rapid Shield", "Equipped", 31, 3, "shield", "Shield", {
        "Alpha": find_part("Shield", "Alpha", "Tediore"), "Beta": find_part("Shield", "Beta", "Tediore"),
        "Gamma": find_part("Shield", "Gamma", "Tediore"), "Delta": None}),
    Item("Transient.WillowClassMod_5", "WillowClassMod", "Gunzerker Mod", "Equipped", 33, 5, "none"),
    gun("SMG", "Maliwan 27 Slag Storm", 27, 2, "Backpack",
        {"Body": ("Body_Maliwan",), "Grip": ("Grip_Maliwan",), "Barrel": ("Barrel_Maliwan",), "Sight": ("Scope_Maliwan",), "Stock": ("Stock_Maliwan",)}),
    gun("Assault Rifle", "Vladof 35 Bunny Gun", 35, 6, "Backpack",
        {"Body": ("Body_Vladof",), "Grip": ("Grip_Vladof",), "Barrel": ("Barrel_Vladof",), "Sight": ("Scope_Vladof",), "Stock": ("Stock_Vladof",)}),
    gun("Sniper Rifle", "Dahl 22 Scoped Sniper", 22, 1, "Backpack",
        {"Body": ("Body_Dahl",), "Grip": ("Grip_Dahl",), "Barrel": ("Barrel_Dahl",), "Sight": ("Scope_Dahl",), "Stock": ("Stock_Dahl",)}),
    gun("Shotgun", "Hyperion 30 Sweeper", 30, 4, "Backpack",
        {"Body": ("Body_Hyperion",), "Grip": ("Grip_Hyperion",), "Barrel": ("Barrel_Hyperion",), "Stock": ("Stock_Hyperion",)}),
    Item("Transient.WillowUsableCustomizationItem_9", "WillowUsableCustomizationItem", "Greenblood", "Backpack", 1, 0, "none"),
]
for index, item in enumerate(ITEMS, start=1):
    item.id = f"Transient.{item.cls}_{index}"


def fake_effects(pid: str) -> list[dict]:
    digest = hashlib.sha256(pid.encode()).digest()
    effects = []
    for i in range(1 + digest[0] % 3):
        attr = STATS[digest[1 + i] % len(STATS)][0]
        kind = ["MT_Scale", "MT_PreAdd", "MT_PostAdd"][digest[5 + i] % 3]
        value = round(0.5 + (digest[9 + i] % 25) / 10, 2) if kind == "MT_Scale" else (digest[9 + i] % 40) - 10
        effects.append({"source": "WeaponAttributeEffects", "attribute": attr, "type": kind, "value": value, "raw": {}})
    return effects


def slots_for(item: Item, mode: str) -> list[dict]:
    if item.kind == "weapon":
        names = WEAPON_SLOTS
        everything = [p for slots in PARTS.values() for lst in slots.values() for p in lst if p["class"] == "WeaponPartDefinition"]
    elif item.kind == "shield":
        names = list(SHIELD_SLOTS)
        everything = [p for lst in PARTS["Shield"].values() for p in lst]
    else:
        return []
    out = []
    for slot in names:
        current = item.parts.get(slot)
        if mode == "legal":
            options = list(PARTS[item.key][slot])
        elif mode == "same_slot":
            options = [p for k, slots in PARTS.items() for p in slots.get(slot, []) if (k in GUN_TYPES) == (item.kind == "weapon")]
        else:
            options = list(everything)
        by_id = {p["id"]: p for p in options}
        if current:
            by_id.setdefault(current["id"], current)
        ordered = sorted(by_id.values(), key=lambda p: (p["name"].lower(), p["id"]))
        out.append({"slot": slot, "current": current, "options": ordered})
    return out


UNIQUES = {  # invented for the mock: which guns "own" a part
    "Hyperion": ["Logan's Gun", "Bitch", "Mongol"], "Jakobs": ["Unkempt Harold", "Lady Fist", "Ogre"],
    "Dahl": ["Hornet", "Pitchfork", "Veruc"], "Maliwan": ["Hellfire", "Fibber", "Thunderball Fists"],
    "Vladof": ["Bunny", "Lead Storm", "Hail"], "Torgue": ["Nukem", "Bearcat", "Roaster"],
    "Tediore": ["Deliverance", "Sherifs Badge", "Shooting Star"], "Bandit": ["Gub", "Cobra", "Rapid Hawk"],
}


def owners_for(kind: str) -> dict:
    out = {}
    for part in BY_ID.values():
        if (part["class"] == "WeaponPartDefinition") != (kind == "weapon"):
            continue
        digest = hashlib.sha256(part["id"].encode()).digest()
        maker = next((m for m in UNIQUES if m in part["name"]), None)
        if maker and digest[0] % 3 == 0:
            names = [UNIQUES[maker][digest[1] % 3], UNIQUES[maker][(digest[1] + 1) % 3]][: 1 + digest[2] % 2]
            out[part["id"]] = [len(names), *names]
        else:
            out[part["id"]] = [20 + digest[1] % 80, "Generic A", "Generic B", "Generic C", "Generic D"]
    return out


CHAR = {"name": "Zer0 Cool", "level": 34, "xp": 1_450_000, "skill_points": 3, "money": 48_213, "eridium": 212, "seraph": 17, "torgue": 4}
CURRENCY_DEFS = [("money", "Cash", 0), ("eridium", "Eridium", 1), ("seraph", "Seraph Crystals", 2), ("torgue", "Torgue Tokens", 3)]


def xp_for_level(level: int) -> int:
    return 0 if level <= 1 else math.ceil(60 * (level ** 2.8 - 1))


def character() -> dict:
    return {
        "class_key": "assassin", "class_source": "pawn.PlayerClass",
        "name": CHAR["name"], "level": CHAR["level"], "xp": CHAR["xp"],
        "xp_this_level": xp_for_level(CHAR["level"]), "xp_next_level": xp_for_level(CHAR["level"] + 1),
        "skill_points": CHAR["skill_points"],
        "currencies": [{"key": k, "label": label, "index": i, "value": CHAR[k]} for k, label, i in CURRENCY_DEFS],
        "limits": {"level": [1, 80], "currency": 2_000_000_000, "skill_points": 999},
        "sources": {"level": "pri.ExpLevel", "xp": "pri.ExpPoints", "skill_points": "pri.GeneralSkillPoints", "name": "pri.PlayerName"},
    }


AMMO = {0: ["Pistol", 312, 600], 1: ["SMG", 880, 1200], 2: ["Assault Rifle", 410, 800], 3: ["Shotgun", 64, 150], 4: ["Sniper Rifle", 28, 60], 5: ["Launcher", 9, 24], 6: ["Grenades", 3, 5]}


def ammo() -> dict:
    return {"pools": [{"id": i, "key": v[0].lower(), "label": v[0], "value": v[1], "max": v[2]} for i, (k, v) in enumerate(AMMO.items())], "limit": 100000}


# Feature mocks: dev/mock_<feature>.py, each exporting handle(path, params, shared) -> dict | None
# (None = not my route). Raise LookupError for 404, ValueError for 400. Loaded by path because -I drops dev/ from sys.path.
FEATURE_MOCKS = []
for _name in ("sdu", "skills", "world"):
    _file = Path(__file__).resolve().parent / f"mock_{_name}.py"
    if _file.is_file():
        _spec = importlib.util.spec_from_file_location(f"mock_{_name}", _file)
        _module = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_module)
        FEATURE_MOCKS.append(_module)


def detail(item: Item, mode: str) -> dict:
    stats = []
    if item.kind == "weapon":
        for name, label, base in STATS:
            value = item.overrides.get(name, item.stats[name])
            stats.append({"name": name, "label": label, "value": value, "base": base, "override": item.overrides.get(name)})
    return {
        "id": item.id, "location": item.location, "mode": mode, "class": item.cls, "name": item.name,
        "level": item.level, "grade": item.level, "game_stage": item.level, "stats": stats,
        "slots": slots_for(item, mode), "hide_bones": item.hide,
    }


def find(item_id: str) -> Item:
    for item in ITEMS:
        if item.id == item_id:
            return item
    raise LookupError("that item is no longer in your inventory")


STATIC = re.compile(
    r"^/(?:[a-z0-9_]+\.(?:js|css)|icons/[A-Za-z0-9_.-]+\.(?:png|svg|webp)|fonts/[a-z0-9-]+\.woff2"
    r"|models/[a-z_]+\.(?:glb|json)|portraits/[a-z]+\.webp|vendor/three/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.js)$"
)
TYPES = {
    ".js": "text/javascript; charset=utf-8", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml",
    ".webp": "image/webp", ".woff2": "font/woff2", ".glb": "model/gltf-binary", ".json": "application/json",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def send_json(self, status: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def route(self, path: str, params: dict) -> None:
        try:
            for feature in FEATURE_MOCKS:
                if (reply := feature.handle(path, params, {"CHAR": CHAR, "AMMO": AMMO})) is not None:
                    return self.send_json(200, reply)
            if path == "/api/ping":
                return self.send_json(200, {"ok": True, "in_game": True})
            if path == "/api/items":
                items = [
                    {"id": i.id, "class": i.cls, "name": i.name, "location": i.location, "level": i.level, "rarity": i.rarity, "type_path": i.type_path,
                     "build": [{"slot": sl, "name": pt["name"], "group": pt["group"], "meshes": pt["meshes"]} for sl, pt in i.parts.items() if pt],
                     "hide_bones": i.hide}
                    for i in sorted(ITEMS, key=lambda i: i.location != "Equipped")
                ]
                return self.send_json(200, {"items": items, "warnings": []})
            if path == "/api/item":
                return self.send_json(200, detail(find(params["id"]), params.get("mode", "legal")))
            if path == "/api/part":
                return self.send_json(200, {"id": params["id"], "effects": fake_effects(params["id"])})
            if path == "/api/ammo":
                return self.send_json(200, ammo())
            if path == "/api/ammo/set":
                pool = AMMO[int(params["id"])]
                before = pool[1]
                pool[1] = pool[2] if params["value"] == "max" else int(params["value"])
                return self.send_json(200, {**ammo(), "applied": {"label": pool[0], "requested": pool[1], "before": before, "after": pool[1]}})
            if path == "/api/character":
                return self.send_json(200, character())
            if path == "/api/character/set":
                before = CHAR.get(params["field"])
                CHAR[params["field"]] = int(params["value"])
                if params["field"] == "level":
                    CHAR["xp"] = xp_for_level(int(params["value"]))
                return self.send_json(200, {**character(), "applied": {"field": params["field"], "requested": params["value"], "before": before, "after": CHAR[params["field"]]}})
            if path == "/api/part_owners":
                return self.send_json(200, {"owners": owners_for(params.get("kind", "weapon"))})
            if path == "/api/parts/rescan":
                return self.send_json(200, {"ok": True})
            if path.startswith("/api/debug/"):
                return self.send_json(200, {"saved_to": "(mock)", "fields": 0, "chains": {}, "effects": 0, "gestalts": []})

            item = find(params["id"])
            mode = params.get("mode", "legal")
            if path == "/api/item/set_part":
                item.parts[params["slot"]] = BY_ID.get(params["part"]) if params.get("part") else None
                applied = {"what": f"{params['slot']} ({item.location})", "requested": params.get("part"), "before": "-", "after": {}}
            elif path == "/api/item/set_level":
                item.level = int(params["level"])
                applied = {"what": f"level ({item.location})", "requested": item.level, "before": {}, "after": {"exp_level": item.level}}
            elif path == "/api/item/set_stat":
                if params.get("value") is None:
                    item.overrides.pop(params["stat"], None)
                else:
                    item.overrides[params["stat"]] = float(params["value"])
                applied = {"what": f"{params['stat']} ({item.location})", "requested": params.get("value"), "before": {}, "after": {}}
            else:
                return self.send_json(404, {"error": "not found"})
            return self.send_json(200, {**detail(item, mode), "applied": applied})
        except LookupError as ex:
            return self.send_json(404, {"error": str(ex)})
        except KeyError as ex:
            return self.send_json(400, {"error": f"missing {ex}"})
        except ValueError as ex:
            return self.send_json(400, {"error": str(ex)})

    def do_GET(self) -> None:  # noqa: N802
        split = urlsplit(self.path)
        if STATIC.match(split.path):
            file = (WEB_DIR / split.path.lstrip("/")).resolve()
            if file.is_file() and WEB_DIR in file.parents:
                return self.send_file(file, TYPES[file.suffix])
            return self.send_json(404, {"error": "not found"})
        token = self.headers.get("X-RTSE-Token") or parse_qs(split.query).get("t", [""])[0]
        if token != TOKEN:
            return self.send_json(403, {"error": "forbidden"})
        if split.path == "/":
            return self.send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        params = {k: v[0] for k, v in parse_qs(split.query).items() if k != "t"}
        return self.route(split.path, params)

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("X-RTSE-Token") != TOKEN:
            return self.send_json(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length", "0"))
        params = json.loads(self.rfile.read(length) or b"{}")
        return self.route(urlsplit(self.path).path, params)


if __name__ == "__main__":
    print(f"mock RTSE on http://127.0.0.1:{PORT}/?t={TOKEN}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
