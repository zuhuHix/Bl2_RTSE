"""Mock of the Weapon lab routes (/api/lab*, /api/debug/lab), for front-end work. Loaded by dev/mock_server.py.

MOCK DATA, NOT THE GAME: nothing here reads a live game. While a session runs, hits are synthesised from the time
since it started: about 8 shots a second, 90% of them hit, 20% of hits crit for 2x, two invented targets, one pellet
per shot. Each shot's random numbers are seeded by the session id and the shot index, so a replay gives the same
numbers. Item data comes from the running mock server (its ITEMS, find() and detail()) when there is one, else from
a built-in fake item (used by the tests). RTSE_MOCK_LAB_VIA=take_damage makes the mock pretend only the fallback
hook fires, which shows the pre-mitigation banner.

The contract is the lab section of the feature spec: rtse/lab.py will answer the same paths and shapes.
"""

from __future__ import annotations

import copy
import os
import random
import sys
import time

RATE = 8.0  # shots per second while a session runs
HIT_CHANCE = 0.9  # share of shots that hit something
CRIT_CHANCE = 0.2  # share of hits that crit
CRIT_MULTIPLIER = 2.0
KILL_CHANCE = 0.02
TARGETS = [("GD_Mock.Bandit.Bandit_Grunt", "Bandit Grunt"), ("GD_Mock.Psycho.Psycho_Badass", "Badass Psycho")]
HOOKS = [
    "WillowGame.WillowPlayerController:NotifyDamageDealt",
    "WillowGame.WillowPawn:TakeDamage",
    "WillowGame.WillowWeapon:ConsumeAmmo",
]
VIA = "take_damage" if os.environ.get("RTSE_MOCK_LAB_VIA") == "take_damage" else "notify"
DAMAGE_FIELDS = ["total", "shield", "max_hit", "min_hit", "avg_hit", "avg_crit", "avg_noncrit", "dps", "per_pellet", "crit_multiplier"]
UNVERIFIED = {
    "hooks": "The three damage hooks have never been seen firing in a live game; this mock invents every hit.",
    "shield": "Shield damage is taken from DamageDealtToShields, which no live game has confirmed.",
    "dps_clock": "DPS divides by the time from the first to the last recorded event, not by the game's own clock.",
    "pellets": "ProjectilesPerShot is read from the weapon; whether the game fires that many pellets per shot is unconfirmed.",
}
# Used when the mock server's item data is not there (tests). Same shape as the mock server's detail().
FALLBACK = {
    "id": "Transient.WillowWeapon_lab_fallback",
    "name": "Fallback Pistol (no mock server)",
    "location": "Equipped",
    "stats": [
        {"name": "InstantHitDamage", "label": "Damage per shot / pellet", "value": 76.9, "base": 76.9},
        {"name": "ProjectilesPerShot", "label": "Pellets per shot", "value": 1, "base": 1},
        {"name": "FireInterval", "label": "Time between shots (s) - lower is faster", "value": 0.5, "base": 0.5},
        {"name": "ClipSize", "label": "Magazine size", "value": 12, "base": 12},
    ],
    "slots": [
        {"slot": "Body", "current": {"id": "GD_Fallback.Body.Body_A", "name": "Body_A"}, "options": []},
        {"slot": "Grip", "current": None, "options": []},
    ],
}

_S = {
    "id": 0, "active": False, "cap": 2000, "weapon": None, "base": 76.9,
    "started": None, "stopped": None, "hits": [], "shots": 0,
    "snapshots": {"a": None, "b": None},
}


def _clock() -> float:
    return time.monotonic()


def _wall() -> float:
    return time.time()


# ---------- item data: the mock server's, or the fallback ----------

def _game():
    """The running mock server (dev/mock_server.py, as __main__), when it carries the item data."""
    main = sys.modules.get("__main__")
    return main if all(hasattr(main, name) for name in ("ITEMS", "find", "detail")) else None


def _detail(item_id: str) -> dict:
    game = _game()
    if game is not None:
        return game.detail(game.find(item_id), "legal")  # find() raises LookupError for an unknown id
    if item_id != FALLBACK["id"]:
        raise LookupError("that item is no longer in your inventory")
    return copy.deepcopy(FALLBACK)


def _value(snap: dict, name: str):
    for stat in snap["stats"]:
        if stat["name"] == name:
            return stat["value"]
    return None


def _snapshot(item_id: str, slot: str) -> dict:
    d = _detail(item_id)
    return {
        "slot": slot, "id": d["id"], "name": d["name"], "taken_at": _wall(),
        "stats": [{"name": s["name"], "label": s["label"], "value": s["value"], "base": s["base"]} for s in d["stats"]],
        "parts": [{"slot": s["slot"], "name": (s["current"] or {}).get("name")} for s in d["slots"]],
    }


def _base_damage(item_id: str) -> float:
    damage = _value({"stats": _detail(item_id)["stats"]}, "InstantHitDamage")
    return float(damage) if damage is not None else 76.9


# ---------- the synthesised session ----------

def _elapsed() -> float:
    s = _S
    if s["started"] is None:
        return 0.0
    end = s["stopped"] if s["stopped"] is not None else _clock()
    return max(0.0, end - s["started"])


def _shot(index: int) -> None:
    """One shot of the session. Misses leave no hit; every hit is appended with the next seq."""
    s = _S
    rng = random.Random(s["id"] * 1_000_003 + index)
    if rng.random() >= HIT_CHANCE:
        return
    crit = rng.random() < CRIT_CHANCE
    target_id, target = TARGETS[rng.randrange(len(TARGETS))]
    amount = s["base"] * (0.9 + 0.2 * rng.random()) * (CRIT_MULTIPLIER if crit else 1.0)
    shield = round(amount * 0.4, 2) if rng.random() < 0.3 else 0.0
    s["hits"].append({
        "seq": len(s["hits"]), "t": round(index / RATE, 3), "amount": round(amount, 2), "shield": shield,
        "initial": round(s["base"] * (CRIT_MULTIPLIER if crit else 1.0), 2),
        "critical": crit, "kill": rng.random() < KILL_CHANCE,
        "target": target, "target_id": target_id, "source": s["weapon"],
        # demo: one hit in five has no causer, so it is credited to the held weapon (approximate)
        "attribution": "held" if index % 5 == 4 else "weapon",
        "via": VIA, "estimated": VIA == "take_damage",
    })


def _advance() -> None:
    """Synthesises every shot due by now (one shot per 1/RATE s since the session started)."""
    s = _S
    if s["started"] is None:
        return
    due = int(_elapsed() * RATE) + 1
    while s["shots"] < due:
        _shot(s["shots"])
        s["shots"] += 1


def _kept(target: str | None = None) -> list[dict]:
    """The newest `cap` hits (older ones are dropped), optionally for one target."""
    hits = _S["hits"][-_S["cap"]:]
    return [h for h in hits if target is None or h["target_id"] == target]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _session() -> dict:
    s = _S
    recorded = len(s["hits"])
    kept = min(recorded, s["cap"])
    return {
        "active": s["active"], "id": s["id"], "elapsed": round(_elapsed(), 3), "cap": s["cap"], "weapon": s["weapon"],
        "recorded": recorded, "kept": kept, "dropped": recorded - kept, "shots": s["shots"], "pending": 0,
        "approximate": sum(1 for h in _kept() if h["attribution"] == "held"),
        "via": VIA if recorded else None,
    }


def _summary(target: str | None) -> dict:
    s = _S
    every = _kept()
    kept = [h for h in every if target is None or h["target_id"] == target]
    amounts = [h["amount"] for h in kept]
    crits = [h["amount"] for h in kept if h["critical"]]
    plain = [h["amount"] for h in kept if not h["critical"]]
    total = sum(amounts)
    avg_crit, avg_noncrit = _mean(crits), _mean(plain)
    times = [h["t"] for h in kept]
    if s["shots"] and target is None:
        times += [0.0, (s["shots"] - 1) / RATE]  # the first and the last shot count as events too
    duration = round(max(times) - min(times), 3) if times else None
    dps = total / duration if duration is not None and duration >= 0.5 else None
    shots = s["shots"]
    pellets_fired = shots  # one pellet per shot in this mock
    per_target: dict[str, dict] = {}
    for h in every:
        row = per_target.setdefault(h["target_id"], {"target_id": h["target_id"], "target": h["target"], "hits": 0, "total": 0.0})
        row["hits"] += 1
        row["total"] = round(row["total"] + h["amount"], 2)
    out = {
        "hits": len(kept), "crits": len(crits), "crit_rate": len(crits) / len(kept) if kept else None,
        "total": round(total, 2), "shield": round(sum(h["shield"] for h in kept), 2),
        "kills": sum(1 for h in kept if h["kill"]),
        "max_hit": max(amounts) if amounts else None, "min_hit": min(amounts) if amounts else None,
        "avg_hit": _mean(amounts), "avg_crit": avg_crit, "avg_noncrit": avg_noncrit,
        "crit_multiplier": avg_crit / avg_noncrit if avg_crit is not None and avg_noncrit else None,
        "duration": duration, "dps": dps,
        "shots": shots, "pellets_per_shot": 1 if shots else None, "pellets_fired": pellets_fired,
        "hits_per_shot": len(kept) / shots if shots and target is None else None,
        "per_pellet": total / pellets_fired if pellets_fired and target is None else None,
        "targets": sorted(per_target.values(), key=lambda row: -row["total"])[:10],
        "estimated": sorted(set(["crit_multiplier", "per_pellet", "hits_per_shot", "kills"] + (DAMAGE_FIELDS if VIA == "take_damage" else []))),
        "approximate": sum(1 for h in kept if h["attribution"] == "held"),
        "scope": "session" if target is None else "target",
        "session_wide": list(SESSION_WIDE) if target is not None else [],
    }
    if target is not None:  # shots cannot be tied to a target (same as rtse/damage.py)
        out.update(dict.fromkeys(SESSION_WIDE))
    return out


SESSION_WIDE = ("shots", "pellets_per_shot", "pellets_fired", "hits_per_shot", "per_pellet")


def _hooks() -> dict:
    seen = len(_S["hits"])
    return {
        "installed": list(HOOKS), "failed": {},
        "fired": {HOOKS[0]: seen if VIA == "notify" else 0, HOOKS[1]: seen if VIA == "take_damage" else 0, HOOKS[2]: _S["shots"]},
        "errors": 0, "last_error": None,
    }


def _state(target: str | None = None) -> dict:
    _advance()
    return {
        "session": _session(), "summary": _summary(target), "hooks": _hooks(),
        "snapshots": {"a": _S["snapshots"]["a"], "b": _S["snapshots"]["b"]}, "unverified": UNVERIFIED,
    }


def _opt_int(value, default: int) -> int:
    return default if value in (None, "") else int(value)


def _start(body: dict) -> None:
    cap = _opt_int(body.get("cap"), 2000)
    if not 10 <= cap <= 20000:
        raise ValueError("cap must be 10-20000")
    weapon = body.get("weapon") or None
    base = _base_damage(weapon) if weapon else 76.9  # resolve the weapon before anything changes
    _S.update(id=_S["id"] + 1, active=True, cap=cap, weapon=weapon, base=base, started=_clock(), stopped=None, hits=[], shots=0)


def _stop() -> None:
    if _S["active"]:
        _S["stopped"] = _clock()
        _S["active"] = False
    _advance()  # the shots up to the stop are now fixed


def _reset() -> None:
    s = _S
    s.update(id=s["id"] + 1, hits=[], shots=0)
    if s["active"]:
        s["started"], s["stopped"] = _clock(), None
    else:
        s["started"], s["stopped"] = None, None


def _snapshot_route(body: dict) -> None:
    slot = body.get("slot")
    if slot not in ("a", "b"):
        raise ValueError("slot must be a or b")
    _S["snapshots"][slot] = _snapshot(body["id"], slot)


def _clear_route(body: dict) -> None:
    slot = body.get("slot")
    if slot is None:
        _S["snapshots"] = {"a": None, "b": None}
    elif slot in ("a", "b"):
        _S["snapshots"][slot] = None
    else:
        raise ValueError("slot must be a or b")


# ---------- compare ----------

def _derived(snap: dict) -> tuple[float | None, float | None]:
    damage = _value(snap, "InstantHitDamage")
    pellets = _value(snap, "ProjectilesPerShot")
    interval = _value(snap, "FireInterval")
    per_shot = damage * pellets if damage is not None and pellets is not None else None
    card = per_shot / interval if per_shot is not None and interval else None
    return per_shot, card


def _diff(name: str, label: str, x: float | None, y: float | None, estimated: bool = False) -> dict:
    delta = None if x is None or y is None else y - x
    pct = delta / abs(x) * 100 if delta is not None and x else None
    changed = abs(x - y) > 1e-6 if x is not None and y is not None else x != y
    return {"name": name, "label": label, "a": x, "b": y, "delta": delta, "pct": pct, "changed": changed, "estimated": estimated}


def _rows(a: dict, b: dict) -> list[dict]:
    labels: dict[str, str] = {}
    for snap in (a, b):
        for stat in snap["stats"]:
            labels.setdefault(stat["name"], stat["label"])  # A's order first, then B-only stats
    rows = [_diff(name, label, _value(a, name), _value(b, name)) for name, label in labels.items()]
    shot_a, card_a = _derived(a)
    shot_b, card_b = _derived(b)
    rows.append(_diff("est_shot_damage", "Damage per shot (estimated: damage x pellets)", shot_a, shot_b, True))
    rows.append(_diff("est_card_dps", "Card DPS (estimated: per shot / fire interval)", card_a, card_b, True))
    return rows


def _parts(a: dict, b: dict) -> list[dict]:
    pa = {p["slot"]: p["name"] for p in a["parts"]}
    pb = {p["slot"]: p["name"] for p in b["parts"]}
    slots = list(pa) + [slot for slot in pb if slot not in pa]
    return [{"slot": slot, "a": pa.get(slot), "b": pb.get(slot), "changed": pa.get(slot) != pb.get(slot)} for slot in slots]


def _compare(params: dict) -> dict:
    a = _S["snapshots"]["a"]
    if a is None:
        raise LookupError("take Snapshot A first")
    b = _S["snapshots"]["b"]
    b_live = params.get("live") == "1" or b is None
    if b_live:
        b = _snapshot(a["id"], "b")  # A's item as it is now
    return {"a": a, "b": b, "b_live": b_live, "rows": _rows(a, b), "parts": _parts(a, b)}


def _hits(params: dict) -> dict:
    after = _opt_int(params.get("after"), -1)
    limit = min(2000, max(1, _opt_int(params.get("limit"), 500)))
    tail = min(2000, max(0, _opt_int(params.get("tail"), 0)))
    target = params.get("target") or None
    matching = _kept(target)
    rows = matching[-tail:] if tail else [h for h in matching if h["seq"] > after][:limit]
    every = _kept()
    return {"session": _S["id"], "hits": rows, "last": every[-1]["seq"] if every else -1}


# ---------- the route table ----------

def handle(path: str, params: dict, shared: dict) -> dict | None:
    if path == "/api/debug/lab":
        return {"saved_to": "(mock) rtse/debug_lab.json"}
    if not path.startswith("/api/lab"):
        return None  # not ours: the mock server answers it
    _advance()
    if path == "/api/lab":
        return _state(params.get("target") or None)
    if path == "/api/lab/hits":
        return _hits(params)
    if path == "/api/lab/compare":
        return _compare(params)
    if path == "/api/lab/session/start":
        _start(params)
    elif path == "/api/lab/session/stop":
        _stop()
    elif path == "/api/lab/session/reset":
        _reset()
    elif path == "/api/lab/snapshot":
        _snapshot_route(params)
    elif path == "/api/lab/snapshot/clear":
        _clear_route(params)
    else:
        raise LookupError(f"no lab route {path}")
    return _state()
