"""Ammo pools: how much of each ammo type the player is carrying.

The player controller keeps its ammo in resource pools: Controller.ResourcePoolManager.ResourcePools holds
ResourcePool objects (AmmoResourcePool for ammo) with GetCurrentValue/SetCurrentValue/GetMaxValue, and each pool's
Definition names its ammo.

Which pool is which ammo type is settled from the game's own data, not from names: POOL_DEFINITIONS maps the seven
ammo pool definitions (Startup.upk, package D_Resourcepools.AmmoPools, class Engine.ResourcePoolDefinition; read with
dev/upk.py) to their kind. A live pool whose Definition path is in that table is "matched": "definition" (settled).
Only a pool whose definition is NOT in the table falls back to matching words in its names; that match is marked
"word" and reported in `unverified`. None of this has run in a live game.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import logging, unreal
from . import journal
from .routing import Route

DEBUG_AMMO_FILE = Path(__file__).parent / "debug_ammo.json"
MAX_AMMO = 100_000

# Game ammo pool definition (object path, read from Startup.upk) -> (kind key, label). Each definition's Resource is
# the matching D_Resources.AmmoResources.Ammo_* object. Verified present in the game data by dev/audit_names.py.
POOL_DEFINITIONS: dict[str, tuple[str, str]] = {
    "D_Resourcepools.AmmoPools.Ammo_Repeater_Pistol_Pool": ("pistol", "Pistol"),
    "D_Resourcepools.AmmoPools.Ammo_Patrol_SMG_Pool": ("smg", "SMG"),
    "D_Resourcepools.AmmoPools.Ammo_Combat_Rifle_Pool": ("rifle", "Assault Rifle"),
    "D_Resourcepools.AmmoPools.Ammo_Combat_Shotgun_Pool": ("shotgun", "Shotgun"),
    "D_Resourcepools.AmmoPools.Ammo_Sniper_Rifle_Pool": ("sniper", "Sniper Rifle"),
    "D_Resourcepools.AmmoPools.Ammo_Rocket_Launcher_Pool": ("launcher", "Launcher"),
    "D_Resourcepools.AmmoPools.Ammo_Grenade_Protean_Pool": ("grenade", "Grenades"),
}

# FALLBACK ONLY (unverified): (key, label, words in the pool's name that identify it); the first match wins.
# Used only for a pool whose definition is not in POOL_DEFINITIONS.
KINDS = (
    ("sniper", "Sniper Rifle", ("sniper",)),
    ("pistol", "Pistol", ("pistol", "revolver")),
    ("smg", "SMG", ("smg", "patrol", "submachine")),
    ("shotgun", "Shotgun", ("shotgun",)),
    ("launcher", "Launcher", ("launcher", "rocket")),
    ("grenade", "Grenades", ("grenade",)),
    ("rifle", "Assault Rifle", ("combat", "repeater", "assault", "rifle")),
)
ORDER = ("pistol", "smg", "rifle", "shotgun", "sniper", "launcher", "grenade")


class AmmoError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _pools() -> list[Any]:
    pc = get_pc(possibly_loading=True)
    if pc is None:
        raise AmmoError(409, "not in a save")
    manager = getattr(pc, "ResourcePoolManager", None)  # real: declared on Controller, not on the pawn
    pools = getattr(manager, "ResourcePools", None) if manager is not None else None
    if pools is None:
        raise AmmoError(501, "this game build doesn't expose the player's resource pools - run the ammo dump")
    return [pool for pool in pools if pool is not None]


def _definition_path(pool: Any) -> str | None:
    """The pool's Definition object path (real field: the ResourcePoolDefinition), or None."""
    definition = getattr(pool, "Definition", None)
    if definition is None:
        return None
    try:
        return definition._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None


def _names(pool: Any) -> str:
    """Everything name-like about a pool, lower-cased (used only by the unverified word fallback)."""
    found: list[str] = []
    targets = [pool]
    definition = getattr(pool, "Definition", None)
    if definition is not None:
        targets.append(definition)
    for target in targets:
        if isinstance(target, unreal.UObject):
            found.append(target._path_name())  # noqa: SLF001
        try:
            fields = list(target.Class._fields() if isinstance(target, unreal.UObject) else target._type._fields())  # noqa: SLF001
        except Exception:  # noqa: BLE001
            continue
        for prop in fields:
            try:
                value = getattr(target, prop.Name)
            except Exception:  # noqa: BLE001, S112
                continue
            if isinstance(value, str):
                found.append(value)
            elif isinstance(value, unreal.UObject):
                found.append(value._path_name())  # noqa: SLF001
    return " ".join(found).lower()


def _number(pool: Any, getter: str, *fields: str, args: tuple[Any, ...] = ()) -> float | None:
    try:
        return float(getattr(pool, getter)(*args))
    except Exception:  # noqa: BLE001, S110
        pass
    for field in fields:
        try:
            return float(getattr(pool, field))
        except Exception:  # noqa: BLE001, S112
            continue
    return None


def _describe(index: int, pool: Any) -> dict[str, Any] | None:
    path = _definition_path(pool)
    if path in POOL_DEFINITIONS:
        key, label = POOL_DEFINITIONS[path]
        matched = "definition"
    else:
        text = _names(pool)
        if "ammo" not in text:
            return None
        key, label, matched = None, None, "none"
        for kind, name, words in KINDS:
            if any(word in text for word in words):
                key, label, matched = kind, name, "word"
                break
        if label is None:
            raw = re.search(r"ammo[_ ]?([a-z_ ]+)", text)
            key = f"other{index}"
            label = raw.group(1).strip("_ ").replace("_", " ").title() if raw else f"Ammo {index}"
    value = _number(pool, "GetCurrentValue", "CurrentValue")
    # GetMaxValue(bWantBaseValue, ReturnValue) in the game: False = the maximum after upgrades and modifiers
    maximum = _number(pool, "GetMaxValue", "MaxValue", args=(False,))
    return {
        "id": index,
        "key": key,
        "label": label,
        "value": None if value is None else round(value),
        "max": None if maximum is None else round(maximum),
        "matched": matched,
        "definition": path,
    }


def _unverified(pools: list[dict[str, Any]]) -> dict[str, str]:
    out = {}
    for pool in pools:
        if pool["matched"] == "word":
            out[pool["key"]] = "matched by words in the pool's name, not by its pool definition"
        elif pool["matched"] == "none":
            out[pool["key"]] = "no pool definition or name match; the key is the pool's position, not an ammo type"
    return out


def read() -> dict[str, Any]:
    pools = [d for i, pool in enumerate(_pools()) if (d := _describe(i, pool))]
    pools.sort(key=lambda p: ORDER.index(p["key"]) if p["key"] in ORDER else len(ORDER))
    return {"pools": pools, "limit": MAX_AMMO, "unverified": _unverified(pools)}


def set_value(params: dict[str, Any]) -> dict[str, Any]:
    pools = _pools()
    try:
        index = int(params["id"])
        pool = pools[index]
    except (KeyError, TypeError, ValueError, IndexError):
        raise AmmoError(400, "unknown ammo pool") from None
    before = _describe(index, pool)
    if before is None:
        raise AmmoError(400, "that pool is not an ammo pool")

    raw = params.get("value")
    if raw == "max":
        if before["max"] is None:
            raise AmmoError(501, "can't read this pool's maximum")
        value = before["max"]
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise AmmoError(400, "amount must be a whole number") from None
    if not 0 <= value <= MAX_AMMO:
        raise AmmoError(400, f"amount must be 0-{MAX_AMMO}")

    try:
        pool.SetCurrentValue(float(value))  # real: ResourcePool.SetCurrentValue(Float Value)
        via = "SetCurrentValue"
    except Exception:  # noqa: BLE001
        try:
            pool.CurrentValue = float(value)  # real field (Float); the call above is the preferred route
            via = "CurrentValue"
        except Exception:  # noqa: BLE001
            raise AmmoError(501, "this game build can't write ammo - run the ammo dump") from None

    after = _describe(index, pool)
    applied = {"label": before["label"], "requested": value, "via": via, "before": before["value"], "after": after and after["value"]}
    logging.info(f"RTSE: ammo {applied}")
    journal.record("ammo.value", target=before["label"], before=before["value"], after=applied["after"])
    return {**read(), "applied": applied}


def debug_dump() -> dict[str, Any]:
    """Saves what every resource pool on the player's pawn exposes, to find the real field names, and how each
    pool was matched to its ammo type (definition path, exact table or word fallback)."""
    out: dict[str, Any] = {"pool_definitions": POOL_DEFINITIONS, "pools": []}
    for index, pool in enumerate(_pools()):
        described = _describe(index, pool)
        entry: dict[str, Any] = {
            "index": index,
            "type": type(pool).__name__,
            "definition_path": _definition_path(pool),
            "matched": described and described["matched"],
            "key": described and described["key"],
            "names": _names(pool)[:400],
        }
        for label, owner in (("pool", pool), ("definition", getattr(pool, "Definition", None))):
            if owner is None:
                continue
            try:
                fields = list(owner.Class._fields() if isinstance(owner, unreal.UObject) else owner._type._fields())  # noqa: SLF001
            except Exception as ex:  # noqa: BLE001
                entry[label] = f"<error {ex!r}>"
                continue
            values: dict[str, Any] = {}
            for prop in fields:
                try:
                    value = getattr(owner, prop.Name)
                except Exception as ex:  # noqa: BLE001
                    values[prop.Name] = f"<error {ex!r}>"
                    continue
                if value is None or isinstance(value, (bool, int, float, str)):
                    values[prop.Name] = value
                elif isinstance(value, unreal.UObject):
                    values[prop.Name] = value._path_name()  # noqa: SLF001
                else:
                    values[prop.Name] = f"<{type(value).__name__}>"
            entry[label] = values
        entry["current"] = _number(pool, "GetCurrentValue", "CurrentValue")
        entry["max"] = _number(pool, "GetMaxValue", "MaxValue", args=(False,))
        out["pools"].append(entry)
    DEBUG_AMMO_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_AMMO_FILE), "pools": len(out["pools"])}


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = AmmoError

GET_ROUTES: dict[str, Route] = {
    "/api/ammo": lambda _params: read(),
    "/api/debug/ammo": lambda _params: debug_dump(),
}
POST_ROUTES: dict[str, Route] = {"/api/ammo/set": set_value}
