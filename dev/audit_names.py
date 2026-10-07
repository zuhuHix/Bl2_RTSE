"""Checks every game-side name RTSE's modules use against the game's real class definitions (dev/class_index.py).

    python -I dev/audit_names.py

Proves only that a name EXISTS on the class it is used on (and what type/parameters it has) - never that it behaves
as hoped at runtime. Candidate lists are read straight from the modules, so this stays in step with the code.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _stub(name: str, **attrs) -> None:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module


class _Anything:
    pass


_stub("unrealsdk", logging=types.SimpleNamespace(info=lambda *_: None, error=print),
      unreal=types.SimpleNamespace(UObject=_Anything, WrappedStruct=_Anything, BoundFunction=_Anything), find_all=lambda *_: [], find_object=lambda *_: None)
_stub("unrealsdk.hooks", Type=types.SimpleNamespace(POST=1), add_hook=lambda *_: None, remove_hook=lambda *_: None)
_stub("mods_base", get_pc=lambda **_: None, build_mod=lambda **_: None, hook=lambda *_a, **_k: (lambda f: f), keybind=lambda *_a, **_k: (lambda f: f))

import class_index  # noqa: E402
from rtse import ammo, character, sdu, skills, world  # noqa: E402

ix = class_index.Index()
CLASS_OF = {  # the owner names the modules use -> the real class they hold at runtime
    "pc": "WillowPlayerController", "pri": "WillowPlayerReplicationInfo", "pawn": "WillowPlayerPawn",
    "invmgr": "WillowInventoryManager", "pool": "AmmoResourcePool", "tree": "PlayerSkillTree",
    "missions": "MissionTracker", "challenges": "ChallengeManager", "game": "WillowGameInfo", "gri": "WillowGameReplicationInfo",
}
rows: list[tuple[str, str, str, str]] = []  # (module, what, verdict, detail)


def check(module: str, owner: str, name: str, kind: str = "any") -> bool:
    """Is `name` (a field, or a function when it ends in "()") a real member of the owner's class?"""
    cls = CLASS_OF.get(owner)
    function = name.endswith("()") or kind == "function"
    bare = name[:-2] if name.endswith("()") else name
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
for function in ("GetCurrencyOnHand", "AddCurrencyOnHand"):
    check("character", "pc", function + "()")   # the code calls these on the controller
    check("character", "pri", function + "()")  # where they should be

# ---- ammo ----
check("ammo", "pawn", "ResourcePoolManager")
check("ammo", "pc", "ResourcePoolManager")
for field in ("ResourcePools",):
    rows.append(("ammo", "ResourcePoolManager.ResourcePools", "REAL" if ix.field("ResourcePoolManager", field) else "MISSING", str(ix.field("ResourcePoolManager", field))))
for name in ("GetCurrentValue()", "GetMaxValue()", "SetCurrentValue()"):
    check("ammo", "pool", name)
for name in ("Data", "CurrentValue", "MaxValue", "Definition"):
    check("ammo", "pool", name)

# ---- sdu ----
for kind, spec in sdu.STORAGE.items():
    for what, candidates in spec.items():
        for owner, name in candidates:
            check(f"sdu.{kind}.{what}", owner, name)
for what, candidates in (("ammo_level", sdu.AMMO_LEVEL), ("ammo_capacity_read", sdu.AMMO_CAPACITY_READ), ("ammo_capacity_write", sdu.AMMO_CAPACITY_WRITE), ("refresh", sdu.REFRESH_CALLS)):
    for owner, name in candidates:
        if owner == "data":
            rows.append((f"sdu.{what}", f"data.{name}", "MISSING", "ResourcePool has no Data field"))
        else:
            check(f"sdu.{what}", owner, name)

# ---- world ----
for name in (world.PLAYTHROUGH_READERS, world.PLAYTHROUGH_SETTERS):
    for owner, function in name:
        check("world.playthrough", "gri" if owner == "game" else owner, function + "()")
for owner, attribute in world.PLAYTHROUGH_FIELDS:
    check("world.playthrough", "gri" if owner == "game" else owner, attribute)
for owner, attribute in (*world.MISSION_PLAYTHROUGH_LISTS, *world.MISSION_FLAT_LISTS):
    check("world.missions", owner, attribute)
for path_name, attributes in world.OWNER_PATHS.items():
    for attribute in attributes:
        if attribute.startswith("WorldInfo"):
            rows.append(("world.owners", f"pc.{attribute}", "REAL" if ix.field("WillowPlayerController", "WorldInfo") else "MISSING", "Actor.WorldInfo; Game is server-side only"))
        else:
            check("world.owners", "pc", attribute)
struct_check("world.missions", "IMission.MissionData", (*world.MISSION_DEF[:1], *world.MISSION_STATUS[:1]))
struct_check("world.missions", "WillowPlayerController.MissionPlaythroughData", ("PlayThroughNumber", "MissionList"))
for owner, attribute in world.CHALLENGE_LISTS:
    check("world.challenges", owner, attribute)
struct_check("world.challenges", "ChallengeDefinition.ChallengeData", ("ChallengeDefinition",))
for owner, attribute in (*world.STATION_LISTS, world.STATION_GETTER, world.STATION_SETTER):
    check("world.stations", owner, attribute)

# ---- skills ----
for owner, attribute in skills.TREE_SOURCES:
    check("skills", owner, attribute)

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
