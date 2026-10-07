"""Ammo pools: how much of each ammo type the player is carrying.

The player's pawn keeps its ammo in resource pools. Their exact field names have not been confirmed
in a running game, so reading is defensive and the dump saves everything a pool exposes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import logging, unreal

DEBUG_AMMO_FILE = Path(__file__).parent / "debug_ammo.json"
MAX_AMMO = 100_000

# (key, label, words in the pool's name that identify it); the first match wins, so order matters
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
    pawn = getattr(pc, "Pawn", None) if pc is not None else None
    if pawn is None:
        raise AmmoError(409, "not in a save")
    manager = getattr(pawn, "ResourcePoolManager", None)
    pools = getattr(manager, "ResourcePools", None) if manager is not None else None
    if pools is None:
        raise AmmoError(501, "this game build doesn't expose the player's resource pools - run the ammo dump")
    return [pool for pool in pools if pool is not None]


def _names(pool: Any) -> str:
    """Everything name-like about a pool, lower-cased, so its ammo type can be recognised."""
    found: list[str] = []
    targets = [pool]
    data = getattr(pool, "Data", None)
    if data is not None:
        targets.append(data)
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


def _number(pool: Any, getter: str, *fields: str) -> float | None:
    try:
        return float(getattr(pool, getter)())
    except Exception:  # noqa: BLE001, S110
        pass
    for owner in (pool, getattr(pool, "Data", None)):
        if owner is None:
            continue
        for field in fields:
            try:
                return float(getattr(owner, field))
            except Exception:  # noqa: BLE001, S112
                continue
    return None


def _describe(index: int, pool: Any) -> dict[str, Any] | None:
    text = _names(pool)
    if "ammo" not in text:
        return None
    key, label = None, None
    for kind, name, words in KINDS:
        if any(word in text for word in words):
            key, label = kind, name
            break
    if label is None:
        raw = re.search(r"ammo[_ ]?([a-z_ ]+)", text)
        key, label = f"other{index}", (raw.group(1).strip("_ ").replace("_", " ").title() if raw else f"Ammo {index}")
    value = _number(pool, "GetCurrentValue", "CurrentValue")
    return {
        "id": index,
        "key": key,
        "label": label,
        "value": None if value is None else round(value),
        "max": None if (m := _number(pool, "GetMaxValue", "MaxValue")) is None else round(m),
    }


def read() -> dict[str, Any]:
    pools = [d for i, pool in enumerate(_pools()) if (d := _describe(i, pool))]
    pools.sort(key=lambda p: ORDER.index(p["key"]) if p["key"] in ORDER else len(ORDER))
    return {"pools": pools, "limit": MAX_AMMO}


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
        pool.SetCurrentValue(float(value))
        via = "SetCurrentValue"
    except Exception:  # noqa: BLE001
        data = getattr(pool, "Data", None)
        if data is None:
            raise AmmoError(501, "this game build can't write ammo - run the ammo dump") from None
        data.CurrentValue = float(value)
        via = "Data.CurrentValue"

    after = _describe(index, pool)
    applied = {"label": before["label"], "requested": value, "via": via, "before": before["value"], "after": after and after["value"]}
    logging.info(f"RTSE: ammo {applied}")
    return {**read(), "applied": applied}


def debug_dump() -> dict[str, Any]:
    """Saves what every resource pool on the player's pawn exposes, to find the real field names."""
    out: dict[str, Any] = {"pools": []}
    for index, pool in enumerate(_pools()):
        entry: dict[str, Any] = {"index": index, "type": type(pool).__name__, "names": _names(pool)[:400]}
        for label, owner in (("pool", pool), ("data", getattr(pool, "Data", None))):
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
        entry["max"] = _number(pool, "GetMaxValue", "MaxValue")
        out["pools"].append(entry)
    DEBUG_AMMO_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_AMMO_FILE), "pools": len(out["pools"])}
