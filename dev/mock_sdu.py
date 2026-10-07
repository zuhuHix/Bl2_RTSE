"""Mock of the SDU endpoints (rtse/sdu.py), for front-end work. Loaded by dev/mock_server.py.

The capacity tables are FROM MEMORY of the base game and not checked against anything, same as in
rtse/sdu.py. Set RTSE_MOCK_SDU=partial to simulate a game build that doesn't expose some values.
"""

from __future__ import annotations

import os

BACKPACK_TABLE = (12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 39)  # levels 0-13
BANK_TABLE = (12, 14, 16, 18, 20, 22, 24)  # levels 0-6


def _linear(base: int, step: int, levels: int) -> tuple[int, ...]:
    return tuple(base + step * level for level in range(levels + 1))


# kind -> (label, group, unit, table, ammo index in the shared AMMO dict)
KINDS = {
    "backpack": ("Backpack", "storage", "slots", BACKPACK_TABLE, None),
    "bank": ("Bank", "storage", "slots", BANK_TABLE, None),
    "ammo_pistol": ("Pistol", "ammo", "rounds", _linear(200, 100, 7), 0),
    "ammo_smg": ("SMG", "ammo", "rounds", _linear(400, 200, 7), 1),
    "ammo_rifle": ("Assault Rifle", "ammo", "rounds", _linear(400, 100, 7), 2),
    "ammo_shotgun": ("Shotgun", "ammo", "rounds", _linear(50, 25, 7), 3),
    "ammo_sniper": ("Sniper Rifle", "ammo", "rounds", _linear(20, 10, 7), 4),
    "ammo_launcher": ("Launcher", "ammo", "rounds", _linear(8, 4, 7), 5),
    "ammo_grenade": ("Grenades", "ammo", "rounds", _linear(3, 1, 3), 6),
}
LEVELS = {"backpack": 6, "bank": 3, "ammo_pistol": 4, "ammo_smg": 4, "ammo_rifle": 4, "ammo_shotgun": 4, "ammo_sniper": 4, "ammo_launcher": 4, "ammo_grenade": 2}
STORAGE = {"backpack": 24, "bank": 18}  # current slot counts
MAX_AMMO_CAPACITY = 100_000
PARTIAL = os.environ.get("RTSE_MOCK_SDU") == "partial"
HIDDEN = {"ammo_grenade", "bank"} if PARTIAL else set()  # kinds the pretend game doesn't expose


def _sync_storage(kind: str) -> None:
    STORAGE[kind] = KINDS[kind][3][LEVELS[kind]]


def _capacity(kind: str, shared: dict) -> int | None:
    if kind in HIDDEN:
        return None
    index = KINDS[kind][4]
    return STORAGE[kind] if index is None else shared["AMMO"][index][2]


def _entry(kind: str, shared: dict) -> dict:
    label, group, unit, table, index = KINDS[kind]
    capacity = _capacity(kind, shared)
    hidden = kind in HIDDEN
    inferred = kind == "ammo_launcher"  # pretend one game field has no stored level, to show the inferred state
    level = None if hidden else LEVELS[kind]
    if inferred and capacity is not None:
        level = min(range(len(table)), key=lambda i: abs(table[i] - capacity))
    sources = {"level": None, "capacity": None}
    if not hidden:
        sources = {
            "level": "inferred from capacity table" if inferred else ("pri.BackpackSDULevel" if kind == "backpack" else "data.Level"),
            "capacity": "invmgr.InventorySlotMax" if kind == "backpack" else "pc.BankSize" if kind == "bank" else "pool.GetMaxValue()",
        }
    return {
        "key": kind, "group": group, "label": label, "unit": unit, "level": level, "level_inferred": inferred and not hidden,
        "max_level": len(table) - 1, "capacity": capacity,
        "expected": None if level is None else table[level], "table": list(table), "pool_id": index, "sources": sources,
    }


def _state(shared: dict) -> dict:
    kinds = [_entry(kind, shared) for kind in KINDS]
    warnings = []
    missing = [k["key"] for k in kinds if k["level"] is None and k["capacity"] is None]
    if missing:
        warnings.append(f"this game build didn't expose: {', '.join(missing)}")
    return {
        "kinds": kinds, "limits": {"ammo_capacity": MAX_AMMO_CAPACITY, "slots": 255},
        "sources": {k["key"]: k["sources"] for k in kinds}, "warnings": warnings,
    }


def _brief(kind: str, shared: dict) -> dict:
    entry = _entry(kind, shared)
    return {"level": entry["level"], "capacity": entry["capacity"]}


def _apply_ammo_capacity(kind: str, capacity: int, shared: dict) -> None:
    pool = shared["AMMO"][KINDS[kind][4]]
    pool[2] = capacity
    pool[1] = min(pool[1], capacity)  # the game clamps what you carry to the new maximum


def handle(path: str, params: dict, shared: dict) -> dict | None:
    if path == "/api/sdu":
        return _state(shared)

    if path == "/api/debug/sdu":
        return {"saved_to": "(mock) rtse/debug_sdu.json", "sources": _state(shared)["sources"], "warnings": []}

    if path == "/api/sdu/set":
        kind = params.get("kind")
        if kind not in KINDS:
            raise ValueError("unknown upgrade kind")
        if kind in HIDDEN:
            raise ValueError("this game build exposes nothing writable for that upgrade - run the SDU dump")
        table = KINDS[kind][3]
        try:
            level = int(params.get("level"))
        except (TypeError, ValueError):
            raise ValueError("level must be a whole number") from None
        if not 0 <= level < len(table):
            raise ValueError(f"level must be 0-{len(table) - 1}")
        before = _brief(kind, shared)
        LEVELS[kind] = level
        if KINDS[kind][4] is None:
            _sync_storage(kind)
        else:
            _apply_ammo_capacity(kind, table[level], shared)
        applied = {
            "kind": kind, "label": KINDS[kind][0], "requested": level, "expected_capacity": table[level],
            "via": {"level": "data.Level", "capacity": None, "refresh": []}, "before": before, "after": _brief(kind, shared),
        }
        return {**_state(shared), "applied": applied}

    if path == "/api/sdu/ammo_max":
        if "id" in params:
            kind = next((k for k, v in KINDS.items() if v[4] is not None and str(v[4]) == str(params["id"])), None)
        else:
            kind = params.get("kind")
        if kind not in KINDS or KINDS[kind][4] is None:
            raise ValueError("unknown ammo pool")
        if kind in HIDDEN:
            raise ValueError("this game build can't write ammo capacity - run the SDU dump")
        try:
            value = int(params.get("max"))
        except (TypeError, ValueError):
            raise ValueError("capacity must be a whole number") from None
        if not 1 <= value <= MAX_AMMO_CAPACITY:
            raise ValueError(f"capacity must be 1-{MAX_AMMO_CAPACITY}")
        before = _brief(kind, shared)
        _apply_ammo_capacity(kind, value, shared)
        applied = {
            "kind": kind, "label": KINDS[kind][0], "requested": value, "via": {"capacity": "data.MaxValue", "refresh": []},
            "before": before, "after": _brief(kind, shared),
        }
        return {**_state(shared), "applied": applied}

    return None
