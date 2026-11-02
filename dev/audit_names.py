"""Checks every game-side name RTSE's modules use against the game's real class definitions (dev/class_index.py).

    python -I dev/audit_names.py

Proves only that a name EXISTS on the class it is used on (and what type/parameters it has) - never that it behaves
as hoped at runtime. Candidate lists are read straight from the modules, so this stays in step with the code.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


import unreal_stub  # noqa: E402

unreal_stub.install()

import json  # noqa: E402

import class_index  # noqa: E402
import upk  # noqa: E402
from rtse import ammo, character, damage, limits, sdu, skills, world  # noqa: E402

ix = class_index.Index()

# ---- game data objects and enums (read from the packages once, cached in dev/out/objects.json) ----
OBJECT_PACKAGES = ("Startup", "Engine", "WillowGame")  # Startup holds the ammo pools, SDU attributes and XP pool
OBJECTS_CACHE = Path(__file__).resolve().parent / "out" / "objects.json"
WATCHED_PREFIXES = ("GD_BlackMarket.Misc.Att_", "D_Resourcepools.AmmoPools.", "D_Resourcepools.PlayerPools.ExperiencePool",
                    "Default__WillowInventoryManager")  # their property values are kept for the value checks below


def _enum_names(pkg: upk.Package, export: dict) -> list[str]:
    """An Enum's value names: its tagged properties end at "None", then a UField.Next reference, then the Names array."""
    import struct

    raw = pkg.raw
    pos, end = export["offset"] + 4, export["offset"] + export["size"]
    while pos + 8 <= end:
        ni, nn = struct.unpack_from("<ii", raw, pos)
        name = pkg.name(ni, nn)
        pos += 8
        if name == "None":
            break
        ti, tn, size, _index = struct.unpack_from("<iiii", raw, pos)
        kind = pkg.name(ti, tn)
        pos += 16
        if kind in ("StructProperty", "ByteProperty"):
            pos += 8
        if kind == "BoolProperty":
            pos += 1
            continue
        pos += size
    pos += 4  # UField.Next
    count, = struct.unpack_from("<i", raw, pos)
    pos += 4
    names = []
    for _ in range(count):
        i, n = struct.unpack_from("<ii", raw, pos)
        pos += 8
        names.append(pkg.name(i, n))
    return names


def build_objects() -> dict:
    objects: dict[str, str] = {}
    props: dict[str, Any] = {}
    enums: dict[str, list[str]] = {}
    for name in OBJECT_PACKAGES:
        pkg = upk.load(class_index.GAME / f"{name}.upk")
        for i, e in enumerate(pkg.exports, 1):
            path = pkg.object_name(i)
            cls = pkg.class_name(e)
            objects.setdefault(path, cls)
            if cls.endswith(".Enum"):
                try:
                    enums.setdefault(path, _enum_names(pkg, e))
                except Exception:  # noqa: BLE001, S112
                    continue
            if path.startswith(WATCHED_PREFIXES) or path.endswith(".ConstantAttributeValueResolver_0"):
                try:
                    props.setdefault(path, pkg.properties(e))
                except Exception:  # noqa: BLE001, S112
                    continue
    return {"objects": objects, "props": props, "enums": enums}


def load_objects() -> dict:
    if not OBJECTS_CACHE.is_file():
        OBJECTS_CACHE.parent.mkdir(exist_ok=True)
        OBJECTS_CACHE.write_text(json.dumps(build_objects()), encoding="utf-8")
    return json.loads(OBJECTS_CACHE.read_text(encoding="utf-8"))


GAME_DATA = load_objects()
CLASS_OF = {  # the owner names the modules use -> the real class they hold at runtime
    "pc": "WillowPlayerController", "pri": "WillowPlayerReplicationInfo", "pawn": "WillowPlayerPawn",
    "invmgr": "WillowInventoryManager", "pool": "AmmoResourcePool", "tree": "PlayerSkillTree",
    "missions": "MissionTracker", "game": "WillowGameInfo", "gri": "WillowGameReplicationInfo",
    "missiondef": "MissionDefinition", "chaldef": "ChallengeDefinition", "chalcat": "ChallengeCategoryDefinition",
    "station": "FastTravelStationDefinition", "xppool": "ExperienceResourcePool", "pooldef": "ResourcePoolDefinition",
}
rows: list[tuple[str, str, str, str]] = []  # (module, what, verdict, detail)


def check(module: str, owner: str, name: str, kind: str = "any") -> bool:
    """Is `name` (a field, or a function when it ends in "()") a real member of the owner's class?"""
    cls = CLASS_OF.get(owner)
    function = "(" in name or kind == "function"  # "Fn()" or "Fn(Param, Other=True)"
    bare = name.partition("(")[0]
    if cls is None:
        rows.append((module, f"{owner}.{name}", "SKIP", "owner is not a game class"))
        return False
    hit = ix.function(cls, bare) if function else ix.field(cls, bare)
    if hit is None and kind == "any":
        hit = ix.field(cls, bare) or ix.function(cls, bare)
    if hit:
        detail = f"{hit[0]} (on {hit[1]})" if not isinstance(hit[0], list) else f"({', '.join(hit[0])}) (on {hit[1]})"
        rows.append((module, f"{owner}.{name}", "REAL", detail))
        return True
    near = [m.split(".", 1)[1] for m in ix.search(cls, bare[:6])][:4]
    rows.append((module, f"{owner}.{name}", "MISSING", "similar: " + "; ".join(near) if near else "no similar name"))
    return False


def struct_check(module: str, struct: str, names: tuple[str, ...]) -> None:
    fields = ix.structs.get(struct, {})
    for name in names:
        rows.append((module, f"{struct}.{name}", "REAL" if name in fields else "MISSING", fields.get(name, f"fields: {', '.join(fields) or 'unknown struct'}")))


# ---- character ----
for key, candidates in character.FIELDS.items():
    for owner, attribute in candidates:
        check("character", owner, attribute)
for function in ("GetCurrencyOnHand", "AddCurrencyOnHand", "SetCurrencyOnHand"):
    check("character", "pri", function + "()")
check("character", "pc", "GetExpPoints()")
check("character", "pc", "ResourcePoolManager")  # where the experience pool lives
check("character", "pc", "GetExpPointsRequiredForLevel()")  # the game's XP table, per level
check("character", "xppool", "SetCurrentValue()")  # the experience pool write
check("character", "xppool", "GetCurrentValue()")
check("character", "xppool", "Definition")
check("character", "pc", "ExpLevelUp()")  # considered as a level alternative (adds one level only); exists, not used
check("limits", "pc", "GetMaximumPossiblePlayerLevelCap()")  # limits.character_level_cap

# ---- ammo ----
check("ammo", "pc", "ResourcePoolManager")
for field in ("ResourcePools",):
    rows.append(("ammo", "ResourcePoolManager.ResourcePools", "REAL" if ix.field("ResourcePoolManager", field) else "MISSING", str(ix.field("ResourcePoolManager", field))))
for name in ("GetCurrentValue()", "GetMaxValue(bWantBaseValue=False)", "SetCurrentValue()"):
    check("ammo", "pool", name)
for name in ("CurrentValue", "MaxValue", "Definition"):
    check("ammo", "pool", name)
check("ammo", "pool", "SetMaxValueFromProfile(Value)")  # the ammo capacity write
for name in ("BaseMaxValue", "TotalUpgradeCount", "MaxValueUpgrade", "UpgradeLevelAttribute", "Resource"):
    check("ammo.definition", "pooldef", name)
for name in ("GetUpgradeLevel()", "SetUpgradeLevel(NewUpgradeLevel)", "ApplyUpgrades()"):
    check("ammo", "pool", name)

# ---- sdu ----
for kind, spec in sdu.STORAGE.items():
    for what, candidates in spec.items():
        for owner, name in candidates:
            check(f"sdu.{kind}.{what}", owner, name)
for what, candidates in (("ammo_level", sdu.AMMO_LEVEL), ("ammo_level_write", sdu.AMMO_LEVEL_WRITE), ("ammo_capacity_read", sdu.AMMO_CAPACITY_READ), ("ammo_capacity_write", sdu.AMMO_CAPACITY_WRITE), ("refresh", sdu.REFRESH_CALLS)):
    for owner, name in candidates:
        check(f"sdu.{what}", owner, name)

# ---- world ----
for name in (world.PLAYTHROUGH_READERS, world.PLAYTHROUGH_SETTERS):
    for owner, function in name:
        check("world.playthrough", owner, function + "()")
for owner, attribute in world.PLAYTHROUGH_FIELDS:
    check("world.playthrough", owner, attribute)
for owner, attribute in (*world.MISSION_PLAYTHROUGH_LISTS, *world.MISSION_FLAT_LISTS):
    check("world.missions", owner, attribute)
for owner, function in world.MISSION_SETTERS:
    check("world.missions", owner, function + "()")
# owner paths are walked segment by segment: each segment must be a real field of the class reached so far
SEGMENT_CLASS = {"WorldInfo": "WorldInfo", "GRI": "WillowGameReplicationInfo", "MissionTracker": "MissionTracker", "Game": "WillowGameInfo"}
for owner_key, paths in world.OWNER_PATHS.items():
    for path in paths:
        cls = "WillowPlayerController"
        for segment in path.split("."):
            hit = ix.field(cls, segment)
            rows.append(("world.owners", f"{cls}.{segment}", "REAL" if hit else "MISSING", f"{hit[0]} (on {hit[1]})" if hit else f"owner '{owner_key}' path {path}"))
            cls = SEGMENT_CLASS.get(segment, cls)
struct_check("world.missions", "IMission.MissionData", (*world.MISSION_DEF, *world.MISSION_STATUS))
struct_check("world.missions", "WillowPlayerController.MissionPlaythroughData", (*world.MISSION_PT_NUMBER, *world.MISSION_GROUP_ENTRIES))
for name in (*world.MISSION_NAME, *world.MISSION_PLOT_FLAG):
    check("world.missions", "missiondef", name)
for owner, attribute in world.CHALLENGE_LISTS:
    check("world.challenges", owner, attribute)
struct_check("world.challenges", "ChallengeDefinition.ChallengeData", world.CHALLENGE_DEF)
for name in (*world.CHALLENGE_NAME, *world.CHALLENGE_DESC, *world.CHALLENGE_DEF_GOAL, *world.CHALLENGE_LEVELS, *world.CHALLENGE_CATEGORY):
    check("world.challenges", "chaldef", name)
for name in world.CHALLENGE_CATEGORY_NAME:
    check("world.challenges", "chalcat", name)
for function in ("IsChallengeComplete", "IsChallengeLevelComplete", "GetChallengeTotalProgress", "ServerCompleteChallenge", "GetCurrentChallengeLevel",
                 "GetHighestChallengeLevelComplete", "PlayerHasChallenge", "IsPlaythroughComplete"):
    check("world.challenges", "pc", function + "()")
for owner, attribute in (*world.STATION_LISTS, world.STATION_GETTER, *world.STATION_SETTERS):
    check("world.stations", owner, attribute + ("()" if (owner, attribute) in (world.STATION_GETTER, *world.STATION_SETTERS) else ""))
for name in (*world.STATION_NAME, *world.STATION_LEVEL):
    check("world.stations", "station", name)
for name in ("PlayerReplicationInfo", "LastVisitedTeleporter"):
    check("world.stations", "pc", name)
for function in ("NotifyPlaythroughChanged", "GetMissionStatus"):
    check("world.playthrough" if function.startswith("Notify") else "world.missions", "gri" if function.startswith("Notify") else "missions", function + "()")

# ---- skills ----
for owner, attribute in skills.TREE_SOURCES:
    check("skills", owner, attribute)

# ---- damage lab (rtse/damage.py): the hooked functions, and the fields read inside the hooks ----
for path, _fn in damage.HOOKS:
    cls, _, function = path.split(".", 1)[1].partition(":")
    hit = ix.function(cls, function)
    rows.append(("damage.hooks", f"{cls}.{function}()", "REAL" if hit else "MISSING", str(hit)))
struct_check("damage", "DamagePipeline.DamageEventSummary", damage.RAW_FIELDS)
for cls, names in (("WillowPawn", ("bWasLastDamageACriticalHit", "GetHumanReadableName")), ("WillowWeapon", ("Owner", "ProjectilesPerShot", "ShotCost")), ("Pawn", ("Weapon",))):
    for name in names:
        hit = ix.field(cls, name) or ix.function(cls, name)
        rows.append(("damage", f"{cls}.{name}", "REAL" if hit else "MISSING", str(hit)))


# ---- game data objects and values (what the ammo, SDU and XP tables were read from) ----
def data_check(module: str, what: str, ok: bool, detail: Any) -> None:
    rows.append((module, what, "REAL" if ok else "MISSING", str(detail)))


def prop(path: str) -> dict:
    return GAME_DATA["props"].get(path) or {}


def existing(path: str, cls: str | None = None) -> bool:
    found = GAME_DATA["objects"].get(path)
    return found is not None and (cls is None or found == cls)


for path, (key, _label) in ammo.POOL_DEFINITIONS.items():
    data_check("data.ammo", f"pool definition {path}", existing(path, "Engine.ResourcePoolDefinition"), GAME_DATA["objects"].get(path, "not in game data"))
    table = sdu.AMMO_TABLES[key][1]
    base = prop(path).get("BaseMaxValue", {}).get("BaseValueConstant")
    data_check("data.ammo", f"  {key}: BaseMaxValue = table base {table[0]}", base == table[0], f"BaseMaxValue {base}")
    total = prop(path).get("TotalUpgradeCount")
    data_check("data.ammo", f"  {key}: TotalUpgradeCount = {len(table) - 1}", total == len(table) - 1, f"TotalUpgradeCount {total}")
    attribute = {"pistol": "Pistol", "smg": "SMG", "rifle": "CombatRifle", "shotgun": "Shotgun", "sniper": "Sniper",
                 "launcher": "Rocket", "grenade": "Grenade"}[key]
    step_path = f"GD_BlackMarket.Misc.Att_{attribute}AmmoPerUpgrade.ConstantAttributeValueResolver_0"
    step = prop(step_path).get("ConstantValue")
    data_check("data.ammo", f"  {key}: Att_{attribute}AmmoPerUpgrade = table step {table[1] - table[0]}", step == table[1] - table[0], f"ConstantValue {step}")

data_check("data.sdu", "backpack base = Default__WillowInventoryManager.InventorySlotMax_Misc",
           prop("Default__WillowInventoryManager").get("InventorySlotMax_Misc") == sdu.BACKPACK_TABLE[0],
           prop("Default__WillowInventoryManager").get("InventorySlotMax_Misc"))
bp_step = prop("GD_BlackMarket.Misc.Att_BackPackSlotsPerUpgrade.ConstantAttributeValueResolver_0").get("ConstantValue")
data_check("data.sdu", f"backpack step = Att_BackPackSlotsPerUpgrade {sdu.BACKPACK_TABLE[1] - sdu.BACKPACK_TABLE[0]}",
           bp_step == sdu.BACKPACK_TABLE[1] - sdu.BACKPACK_TABLE[0], bp_step)
bank_start = prop("GD_BlackMarket.Misc.Att_BankStartingSlots.ConstantAttributeValueResolver_0").get("ConstantValue")
data_check("data.sdu", f"bank base = Att_BankStartingSlots {sdu.BANK_TABLE[0]}", bank_start == sdu.BANK_TABLE[0], bank_start)
bank_step = prop("GD_BlackMarket.Misc.Att_BankSlotsPerUpgrade.ConstantAttributeValueResolver_0").get("ConstantValue")
data_check("data.sdu", f"bank step = Att_BankSlotsPerUpgrade {sdu.BANK_TABLE[1] - sdu.BANK_TABLE[0]}", bank_step == sdu.BANK_TABLE[1] - sdu.BANK_TABLE[0], bank_step)
for path in sdu.DATA_OBJECTS:
    data_check("data.sdu", f"object {path}", existing(path), GAME_DATA["objects"].get(path, "not in game data"))
data_check("data.xp", f"experience pool {character.EXPERIENCE_POOL}", existing(character.EXPERIENCE_POOL, "Engine.ResourcePoolDefinition"),
           GAME_DATA["objects"].get(character.EXPERIENCE_POOL, "not in game data"))

currency_enum = GAME_DATA["enums"].get("WillowInventoryDefinition.ECurrencyType", [])
for index, expected in ((0, "CURRENCY_Credits"), (1, "CURRENCY_Eridium"), (2, "CURRENCY_SeraphCrystals"), (3, "CURRENCY_Reserved_A")):
    data_check("data.currency", f"ECurrencyType[{index}] = {expected}", index < len(currency_enum) and currency_enum[index] == expected,
               currency_enum[index] if index < len(currency_enum) else "missing")
for key, label, index in character.CURRENCIES:
    rows.append(("data.currency", f"currency {key} ({label}) -> index {index}", "REAL" if index < len(currency_enum) else "MISSING",
                 "UNVERIFIED as " + label if key in character.UNVERIFIED_CURRENCY else "settled"))

# ---- report ----
width = max(len(r[1]) for r in rows)
counts = {"REAL": 0, "MISSING": 0, "SKIP": 0}
current = None
for module, what, verdict, detail in rows:
    counts[verdict] += 1
    if module.split(".")[0] != current:
        current = module.split(".")[0]
        print(f"\n== {current}")
    print(f"  {verdict:7} {what.ljust(width)}  {detail}")
print(f"\n{counts['REAL']} real, {counts['MISSING']} missing, {counts['SKIP']} skipped")
