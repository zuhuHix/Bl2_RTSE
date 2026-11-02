"""Save Deposit Upgrades (SDUs): backpack slots, bank slots and per-ammo-type capacity.

What the game's data settles (dev/upk.py on Startup.upk, WillowGame.upk; checked by dev/audit_names.py):
  - ammo: each ammo pool definition (D_Resourcepools.AmmoPools.*, Engine.ResourcePoolDefinition) has BaseMaxValue (the
    base capacity), TotalUpgradeCount = 6 (the upgrade levels), and MaxValueUpgrade whose initialization definition
    (D_Resourcepools.MiscData.AmmoUpgrade_*) scales by GD_BlackMarket.Misc.Att_<X>AmmoPerUpgrade (a ConstantValue).
    The stored upgrade level is the pool's (real) GetUpgradeLevel(). Capacity = base + per-upgrade x level.
  - backpack: WillowInventoryManager default InventorySlotMax_Misc = 12 (WillowGame.upk Default__WillowInventoryManager);
    GD_BlackMarket.Upgrades_Ammo.Upgrades_Backpack adds Att_BackPackSlotsPerUpgrade = 3 per purchase.
  - bank: GD_BlackMarket.Misc.Att_BankStartingSlots = 6, Att_BankSlotsPerUpgrade = 2 (Upgrades_Bank adds them).
Not settled: how many backpack and bank upgrades exist (9 each is assumed; the known maxima 39 and 24 are consistent
with it, but no game object states it), which controller getter reports the bank size, and whether writes through
ClientSetBankSlots take effect on the authority. Those are listed in the JSON's "unverified" dict.

Every value lists the places it might live (candidates), is read defensively, and reports which candidate worked
(`sources`); a value nobody could read is None ("unavailable"), never an exception. Every write is read back and
returns what the game now says.

Candidate syntax: (owner, name). "Name()" is a function called with no arguments. "Name(Param=literal)" is a function
called with fixed keyword arguments (read). "Name(Param, Other=True)" is write-only: the value goes in Param and the
others are fixed. Owners: pc, pri, pawn, invmgr (pawn.InvManager); for ammo, pool (a resource pool).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import logging, unreal

from . import ammo
from .routing import Route

DEBUG_SDU_FILE = Path(__file__).parent / "debug_sdu.json"
MAX_AMMO_CAPACITY = 100_000
MAX_SLOTS = 255  # a raw slot-count write is refused above this

# Upgrade levels per kind. Ammo: TotalUpgradeCount (game data). Backpack and bank: NOT in the game data (see docstring).
BACKPACK_UPGRADES = 9  # unverified
BANK_UPGRADES = 9  # unverified


def _linear(base: int, step: int, levels: int) -> tuple[int, ...]:
    return tuple(base + step * level for level in range(levels + 1))


BACKPACK_TABLE = _linear(12, 3, BACKPACK_UPGRADES)  # base 12 (game default), +3 per upgrade (data); 9 levels unverified
BANK_TABLE = _linear(6, 2, BANK_UPGRADES)  # base 6 (Att_BankStartingSlots), +2 per upgrade (Att_BankSlotsPerUpgrade); 9 unverified

# ammo key (ammo.KINDS) -> (label, table). Base = BaseMaxValue of the pool definition, step = Att_<X>AmmoPerUpgrade,
# levels = TotalUpgradeCount (6), all read from the game data (see the docstring).
AMMO_TABLES: dict[str, tuple[str, tuple[int, ...]]] = {
    "pistol": ("Pistol", _linear(200, 100, 6)),  # Ammo_Repeater_Pistol_Pool: 200; Att_PistolAmmoPerUpgrade 100
    "smg": ("SMG", _linear(360, 180, 6)),  # Ammo_Patrol_SMG_Pool: 360; Att_SMGAmmoPerUpgrade 180
    "rifle": ("Assault Rifle", _linear(280, 140, 6)),  # Ammo_Combat_Rifle_Pool: 280; Att_CombatRifleAmmoPerUpgrade 140
    "shotgun": ("Shotgun", _linear(80, 20, 6)),  # Ammo_Combat_Shotgun_Pool: 80; Att_ShotgunAmmoPerUpgrade 20
    "sniper": ("Sniper Rifle", _linear(48, 12, 6)),  # Ammo_Sniper_Rifle_Pool: 48; Att_SniperAmmoPerUpgrade 12
    "launcher": ("Launcher", _linear(12, 3, 6)),  # Ammo_Rocket_Launcher_Pool: 12; Att_RocketAmmoPerUpgrade 3
    "grenade": ("Grenades", _linear(3, 1, 6)),  # Ammo_Grenade_Protean_Pool: 3; Att_GrenadeAmmoPerUpgrade 1
}

# The game objects the tables above were read from (dev/audit_names.py checks each one exists).
DATA_OBJECTS = (
    "GD_BlackMarket.Misc.Att_BackPackSlotsPerUpgrade",
    "GD_BlackMarket.Misc.Att_BankStartingSlots",
    "GD_BlackMarket.Misc.Att_BankSlotsPerUpgrade",
    "GD_BlackMarket.Upgrades_Ammo.Upgrades_Backpack",
    "GD_BlackMarket.Upgrades_Ammo.Upgrades_Bank",
    "GD_BlackMarket.Misc.Att_CombatRifleAmmoPerUpgrade",
    "GD_BlackMarket.Misc.Att_GrenadeAmmoPerUpgrade",
    "GD_BlackMarket.Misc.Att_PistolAmmoPerUpgrade",
    "GD_BlackMarket.Misc.Att_RocketAmmoPerUpgrade",
    "GD_BlackMarket.Misc.Att_ShotgunAmmoPerUpgrade",
    "GD_BlackMarket.Misc.Att_SMGAmmoPerUpgrade",
    "GD_BlackMarket.Misc.Att_SniperAmmoPerUpgrade",
)

# kind -> {group, label, table, unit}; ammo kinds are "ammo_<key>"
KINDS: dict[str, dict[str, Any]] = {
    "backpack": {"group": "storage", "label": "Backpack", "unit": "slots", "table": BACKPACK_TABLE},
    "bank": {"group": "storage", "label": "Bank", "unit": "slots", "table": BANK_TABLE},
}
for _key in ammo.ORDER:
    _label, _table = AMMO_TABLES[_key]
    KINDS[f"ammo_{_key}"] = {"group": "ammo", "label": _label, "unit": "rounds", "table": _table, "ammo_key": _key}

# Where the stored SDU level might live, and where the resulting capacity might be read / written. The game stores
# no backpack/bank level (only the capacity), so "level" is empty and the level is worked out from the capacity table.
STORAGE: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
    "backpack": {
        "level": (),
        "capacity_read": (("invmgr", "InventorySlotMax_Misc"), ("invmgr", "GetUnreadiedInventoryMaxSize()")),  # real
        "capacity_write": (("invmgr", "SetInventoryMaxSize(NewSize, bOverrideDefaultMin=True)"), ("invmgr", "InventorySlotMax_Misc")),  # real
    },
    "bank": {
        "level": (),
        # Real names. Whether either returns the TOTAL bank size (what the table assumes) is UNVERIFIED: GetBankUpgradeSlots
        # may return only the upgrade part, and MaxBankSlotsLoadedFromSavegame is only the value loaded from the save.
        "capacity_read": (("pc", "GetBankUpgradeSlots()"), ("pc", "MaxBankSlotsLoadedFromSavegame")),
        # ClientSetBankSlots(Int NewSlotCount, Bool) is real, but it is a client-side call: whether it changes the
        # authoritative bank size is UNVERIFIED.
        "capacity_write": (("invmgr", "ClientSetBankSlots(NewSlotCount)"),),
    },
}
AMMO_LEVEL = (("pool", "GetUpgradeLevel()"),)  # real: ResourcePool.GetUpgradeLevel(Int ReturnValue)
AMMO_LEVEL_WRITE = (("pool", "SetUpgradeLevel(NewUpgradeLevel)"),)  # real: ResourcePool.SetUpgradeLevel(Int, Bool)
# GetMaxValue(bWantBaseValue, ReturnValue) is real; False asks for the maximum after upgrades (not the base value).
AMMO_CAPACITY_READ = (("pool", "GetMaxValue(bWantBaseValue=False)"), ("pool", "MaxValue"))
# ResourcePool.SetMaxValueFromProfile(Float Value) is real; that it changes the live maximum is UNVERIFIED.
AMMO_CAPACITY_WRITE = (("pool", "SetMaxValueFromProfile(Value)"),)
# Optional nudges after a write so the game recomputes things; failures are ignored.
REFRESH_CALLS = (("pool", "ApplyUpgrades()"),)  # real

DUMP_WORDS = ("backpack", "bank", "slot", "sdu", "upgrade", "chest", "capacity", "storage", "inventory", "ammo", "max")


class SduError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# ---------- reading and writing candidates ----------


def _owners() -> dict[str, Any]:
    pc = get_pc(possibly_loading=True)
    if pc is None:
        raise SduError(409, "not in a save")
    pawn = getattr(pc, "Pawn", None)
    return {
        "pc": pc,
        "pri": getattr(pc, "PlayerReplicationInfo", None),
        "pawn": pawn,
        "invmgr": getattr(pawn, "InvManager", None) if pawn is not None else None,
    }


def _number(value: Any) -> int | None:
    if isinstance(value, tuple):  # functions with out parameters return (result, *outs)
        value = value[0] if value else None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _literal(text: str) -> Any:
    return {"True": True, "False": False}.get(text.strip(), text.strip())


def _is_readable(name: str) -> bool:
    """A plain field, a "Name()" function, or a "Name(Param=literal, ...)" function with every argument fixed."""
    if "(" not in name or name.endswith("()"):
        return True
    rest = name[:-1].partition("(")[2]
    return all("=" in part for part in rest.split(","))


def _fetch(target: Any, name: str) -> Any:
    if name.endswith("()"):
        return getattr(target, name[:-2])()
    if "(" in name:
        function, _, rest = name[:-1].partition("(")
        kwargs = {}
        for part in rest.split(","):
            key, _, literal = part.partition("=")
            kwargs[key.strip()] = _literal(literal)
        return getattr(target, function)(**kwargs)
    return getattr(target, name)


def _read(owners: dict[str, Any], candidates: tuple[tuple[str, str], ...]) -> tuple[int | None, str | None]:
    """The first candidate that reads as a number: (value, "owner.name"), or (None, None)."""
    for owner, name in candidates:
        target = owners.get(owner)
        if target is None or not _is_readable(name):
            continue
        try:
            raw = _fetch(target, name)
        except Exception:  # noqa: BLE001, S112
            continue
        value = _number(raw)
        if value is not None:
            return value, f"{owner}.{name}"
    return None, None


def _write(owners: dict[str, Any], candidates: tuple[tuple[str, str], ...], value: int) -> str | None:
    """Writes to the first candidate that exists and accepts the value; None if none did."""
    for owner, name in candidates:
        target = owners.get(owner)
        if target is None:
            continue
        try:
            if "(" in name and not name.endswith("()"):
                function, _, rest = name[:-1].partition("(")
                params = [part.strip() for part in rest.split(",")]
                kwargs = {params[0]: value}
                for fixed in params[1:]:
                    key, _, literal = fixed.partition("=")
                    kwargs[key.strip()] = _literal(literal)
                getattr(target, function)(**kwargs)
            elif name.endswith("()"):
                getattr(target, name[:-2])(value)
            else:
                current = getattr(target, name)  # does it exist?
                setattr(target, name, float(value) if isinstance(current, float) else value)
        except Exception:  # noqa: BLE001, S112
            continue
        return f"{owner}.{name}"
    return None


def _refresh(owners: dict[str, Any]) -> list[str]:
    done = []
    for owner, name in REFRESH_CALLS:
        target = owners.get(owner)
        if target is None:
            continue
        try:
            getattr(target, name[:-2])()
            done.append(f"{owner}.{name}")
        except Exception:  # noqa: BLE001, S112
            continue
    return done


def _expected(kind: str, level: int | None) -> int | None:
    table = KINDS[kind]["table"]
    return table[level] if level is not None and 0 <= level < len(table) else None


def _nearest_level(kind: str, capacity: int | None) -> int | None:
    """Which table level a capacity most looks like; only used when the game has no level field we can read."""
    if capacity is None:
        return None
    table = KINDS[kind]["table"]
    return min(range(len(table)), key=lambda i: abs(table[i] - capacity))


def _pool_owners(pool: Any) -> dict[str, Any]:
    return {"pool": pool}


# Found ammo pools: ammo key -> (pool index, pool, how it was matched: "definition" | "word")
Found = dict[str, tuple[int, Any, str]]


def _ammo_pools() -> tuple[Found, str | None]:
    """ammo key -> (pool index, pool, match). The second value is why the pools couldn't be read, if so."""
    try:
        pools = ammo._pools()  # noqa: SLF001
    except ammo.AmmoError as ex:
        if ex.status == 409:
            raise SduError(409, str(ex)) from None
        return {}, str(ex)
    found: Found = {}
    for index, pool in enumerate(pools):
        try:
            described = ammo._describe(index, pool)  # noqa: SLF001
        except Exception:  # noqa: BLE001, S112
            continue
        if described and described["key"] in AMMO_TABLES:
            found.setdefault(described["key"], (index, pool, described["matched"]))
    return found, None


def _entry(kind: str, level: int | None, capacity: int | None, sources: dict[str, str | None], inferred: bool = False,
           pool_id: int | None = None, pool_match: str | None = None) -> dict[str, Any]:
    spec = KINDS[kind]
    table = spec["table"]
    return {
        "key": kind,
        "group": spec["group"],
        "label": spec["label"],
        "unit": spec["unit"],
        "level": level,
        "level_inferred": inferred,
        "max_level": len(table) - 1,
        "capacity": capacity,
        "expected": _expected(kind, level),
        "table": list(table),
        "pool_id": pool_id,
        "pool_match": pool_match,
        "sources": sources,
    }


def _storage_entry(owners: dict[str, Any], kind: str) -> dict[str, Any]:
    spec = STORAGE[kind]
    level, level_src = _read(owners, spec["level"])
    capacity, cap_src = _read(owners, spec["capacity_read"])
    inferred = False
    if level is None and capacity is not None:
        level, inferred = _nearest_level(kind, capacity), True
        level_src = "inferred from capacity table"
    return _entry(kind, level, capacity, {"level": level_src, "capacity": cap_src}, inferred)


def _ammo_entry(key: str, found: Found) -> dict[str, Any]:
    kind = f"ammo_{key}"
    if key not in found:
        return _entry(kind, None, None, {"level": None, "capacity": None})
    index, pool, match = found[key]
    owners = _pool_owners(pool)
    level, level_src = _read(owners, AMMO_LEVEL)
    capacity, cap_src = _read(owners, AMMO_CAPACITY_READ)
    inferred = False
    if level is None and capacity is not None:
        level, inferred = _nearest_level(kind, capacity), True
        level_src = "inferred from capacity table"
    return _entry(kind, level, capacity, {"level": level_src, "capacity": cap_src}, inferred, index, match)


def _unverified(kinds: list[dict[str, Any]]) -> dict[str, str]:
    """Why each kind's numbers can't be fully trusted yet, keyed by kind key. Empty when everything is settled."""
    out: dict[str, str] = {}
    for kind in kinds:
        reasons = []
        key = kind["key"]
        if key == "backpack":
            reasons.append(f"upgrade count ({BACKPACK_UPGRADES}) is not in the game data; the table assumes it")
            reasons.append("no stored backpack level in the game; level is inferred from the capacity")
        if key == "bank":
            reasons.append(f"upgrade count ({BANK_UPGRADES}) is not in the game data; the table assumes it")
            reasons.append("bank size getter/setter not confirmed to mean total slots (GetBankUpgradeSlots / ClientSetBankSlots)")
        if kind.get("pool_match") == "word":
            reasons.append("ammo pool matched by name words, not by its pool definition")
        if kind["level_inferred"] and key.startswith("ammo_"):
            reasons.append("stored upgrade level not readable; inferred from the capacity")
        if reasons:
            out[key] = "; ".join(reasons)
    return out


def read() -> dict[str, Any]:
    owners = _owners()
    found, ammo_problem = _ammo_pools()
    kinds = [_storage_entry(owners, "backpack"), _storage_entry(owners, "bank")] + [_ammo_entry(key, found) for key in ammo.ORDER]
    warnings = []
    if ammo_problem:
        warnings.append(f"ammo pools unavailable: {ammo_problem}")
    unreadable = [k["key"] for k in kinds if k["level"] is None and k["capacity"] is None]
    if unreadable:
        warnings.append(f"this game build didn't expose: {', '.join(unreadable)}")
    return {
        "kinds": kinds,
        "limits": {"ammo_capacity": MAX_AMMO_CAPACITY, "slots": MAX_SLOTS},
        "sources": {k["key"]: k["sources"] for k in kinds},
        "warnings": warnings,
        "unverified": _unverified(kinds),
    }


def _kind_state(kind: str) -> dict[str, Any]:
    return next(k for k in read()["kinds"] if k["key"] == kind)


def _whole(raw: Any, low: int, high: int, what: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise SduError(400, f"{what} must be a whole number") from None
    if not low <= value <= high:
        raise SduError(400, f"{what} must be {low}-{high}")
    return value


def _brief(state: dict[str, Any]) -> dict[str, Any]:
    return {"level": state["level"], "capacity": state["capacity"]}


# ---------- writes ----------


def set_level(params: dict[str, Any]) -> dict[str, Any]:
    """Sets one SDU level: backpack, bank or ammo_<key>. Writes the stored level if the game has one we
    can find, then the capacity from the table if the capacity didn't follow."""
    kind = params.get("kind")
    if kind not in KINDS:
        raise SduError(400, "unknown upgrade kind")
    level = _whole(params.get("level"), 0, len(KINDS[kind]["table"]) - 1, "level")
    owners = _owners()
    before = _kind_state(kind)
    wanted = KINDS[kind]["table"][level]
    via: dict[str, Any] = {"level": None, "capacity": None, "refresh": []}

    if kind in STORAGE:
        cap_write = STORAGE[kind]["capacity_write"]
        targets = owners
    else:
        found, problem = _ammo_pools()
        key = KINDS[kind]["ammo_key"]
        if key not in found:
            raise SduError(501, problem or "that ammo pool wasn't found - run the SDU dump")
        targets = _pool_owners(found[key][1])
        cap_write = AMMO_CAPACITY_WRITE

    # ammo pools have a real level setter; backpack and bank have no stored level (their capacity is written below)
    via["level"] = _write(targets, AMMO_LEVEL_WRITE, level) if kind not in STORAGE else None
    via["refresh"] = _refresh(targets)

    after = _kind_state(kind)
    if after["capacity"] != wanted:
        # the capacity didn't follow the level (or there is no level field): set it from the table
        via["capacity"] = _write(targets, cap_write, wanted)
        via["refresh"] += _refresh(targets)
        after = _kind_state(kind)
    if via["level"] is None and via["capacity"] is None:
        raise SduError(501, "this game build exposes nothing writable for that upgrade - run the SDU dump")

    applied = {
        "kind": kind, "label": KINDS[kind]["label"], "requested": level, "expected_capacity": wanted,
        "via": via, "before": _brief(before), "after": _brief(after),
    }
    logging.info(f"RTSE: sdu {applied}")
    return {**read(), "applied": applied}


def set_ammo_max(params: dict[str, Any]) -> dict[str, Any]:
    """Sets an ammo pool's maximum capacity directly. The pool is given by "id" (as on the ammo page) or "kind"."""
    found, problem = _ammo_pools()
    if "id" in params:
        key = next((k for k, (index, _pool, _match) in found.items() if str(index) == str(params["id"])), None)
    else:
        key = str(params.get("kind", "")).removeprefix("ammo_")
    if key not in AMMO_TABLES:
        raise SduError(400, "unknown ammo pool")
    if key not in found:
        raise SduError(501, problem or "that ammo pool wasn't found - run the SDU dump")
    value = _whole(params.get("max"), 1, MAX_AMMO_CAPACITY, "capacity")

    kind = f"ammo_{key}"
    owners = _pool_owners(found[key][1])
    before = _kind_state(kind)
    via = _write(owners, AMMO_CAPACITY_WRITE, value)
    if via is None:
        raise SduError(501, "this game build can't write ammo capacity - run the SDU dump")
    refreshed = _refresh(owners)
    after = _kind_state(kind)
    applied = {
        "kind": kind, "label": KINDS[kind]["label"], "requested": value, "via": {"capacity": via, "refresh": refreshed},
        "before": _brief(before), "after": _brief(after),
    }
    logging.info(f"RTSE: sdu {applied}")
    return {**read(), "applied": applied}


# ---------- dump ----------


def _scalars(obj: Any) -> dict[str, Any]:
    """Every readable scalar field of an object, plus the names of its members that look related."""
    out: dict[str, Any] = {}
    try:
        fields = list(obj.Class._fields() if isinstance(obj, unreal.UObject) else obj._type._fields())  # noqa: SLF001
    except Exception as ex:  # noqa: BLE001
        return {"<fields>": f"<error {ex!r}>"}
    for prop in fields:
        try:
            value = getattr(obj, prop.Name)
        except Exception as ex:  # noqa: BLE001
            out[prop.Name] = f"<error {ex!r}>"
            continue
        if value is None or isinstance(value, (bool, int, float, str)):
            out[prop.Name] = value
        elif isinstance(value, unreal.UObject):
            out[prop.Name] = value._path_name()  # noqa: SLF001
        else:
            out[prop.Name] = f"<{type(value).__name__}>"
    try:
        out["<related members>"] = sorted(n for n in dir(obj) if any(w in n.lower() for w in DUMP_WORDS))
    except Exception:  # noqa: BLE001, S110
        pass
    return out


def _probe(owners: dict[str, Any], candidates: tuple[tuple[str, str], ...]) -> list[dict[str, Any]]:
    """What each candidate does right now, so the dump shows which names exist and what they return."""
    rows = []
    for owner, name in candidates:
        target = owners.get(owner)
        row: dict[str, Any] = {"candidate": f"{owner}.{name}"}
        if target is None:
            row["result"] = "<no such owner>"
        elif not _is_readable(name):
            row["result"] = "<write-only: not probed>"
        else:
            try:
                row["result"] = _fetch(target, name)
            except Exception as ex:  # noqa: BLE001
                row["result"] = f"<error {ex!r}>"
        rows.append(row)
    return rows


def debug_dump() -> dict[str, Any]:
    """Saves the player's backpack/bank/ammo-related fields, what every candidate name returns, and how each ammo pool
    was matched to its kind (definition path or word fallback). Shows the raw bank candidates for the unverified part."""
    owners = _owners()
    out: dict[str, Any] = {"owners": {name: _scalars(obj) for name, obj in owners.items() if obj is not None}}
    out["candidates"] = {kind: {part: _probe(owners, STORAGE[kind][part]) for part in STORAGE[kind]} for kind in STORAGE}
    out["tables"] = {kind: list(KINDS[kind]["table"]) for kind in KINDS}
    out["data_objects"] = list(DATA_OBJECTS)
    found, problem = _ammo_pools()
    out["ammo_problem"] = problem
    out["ammo"] = {}
    for key, (index, pool, match) in found.items():
        pool_owners = _pool_owners(pool)
        out["ammo"][key] = {
            "index": index,
            "matched": match,
            "definition": ammo._definition_path(pool),  # noqa: SLF001
            "pool": _scalars(pool),
            "definition_fields": _scalars(pool.Definition) if getattr(pool, "Definition", None) is not None else None,
            "level_candidates": _probe(pool_owners, AMMO_LEVEL),
            "capacity_candidates": _probe(pool_owners, AMMO_CAPACITY_READ),
        }
    state = read()
    out["read"] = state
    DEBUG_SDU_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_SDU_FILE), "sources": state["sources"], "warnings": state["warnings"], "unverified": state["unverified"]}


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = SduError

GET_ROUTES: dict[str, Route] = {
    "/api/sdu": lambda _params: read(),
    "/api/debug/sdu": lambda _params: debug_dump(),
}
POST_ROUTES: dict[str, Route] = {"/api/sdu/set": set_level, "/api/sdu/ammo_max": set_ammo_max}
