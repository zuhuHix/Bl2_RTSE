"""Weapon lab: the damage the player deals with the held weapon, measured by game hooks, and an A/B comparison of two
snapshots of a gun's stats.

MEASURED vs ESTIMATED
  - Measured, per hit (primary hook): the game's own DamageSummary for each hit the player deals: FinalDamage,
    DamageDealtToShields, InitialDamage, bWasCrit, bWasOneShotKill.
  - Measured, per shot: ProjectilesPerShot (pellets) and ShotCost, read from the weapon when it consumes ammo.
  - Estimated, fallback hits: TakeDamage fires for every pawn, so Damage is the incoming amount BEFORE mitigation. The
    shield value is unknown (recorded as 0.0), kill is not measured (always False) and crit comes from
    bWasLastDamageACriticalHit read after the call.
  - Estimated, derived numbers: per-pellet, hits-per-shot, crit multiplier, DPS, and the two derived compare rows.
    They are arithmetic over the readings above, not game readings.

HOOKS (HOOK_ID "rtse_damage_lab"). ALL ARE UNVERIFIED IN GAME. The names exist in the game's class data
(dev/class_index.py, checked by dev/audit_names.py), but nothing here has run in Borderlands 2, so whether each one
fires, how often, in which order and with which values is not known.
  1. POST WillowPlayerController.NotifyDamageDealt: the primary; player-dealt damage only, with the game's summary.
  2. PRE + POST WillowPawn.TakeDamage, fires for every pawn. PRE remembers the DamageCauser of a hit the player
     instigated for the length of that call. POST is the fallback recorder.
  3. POST WillowWeapon.ConsumeAmmo: one shot each time the local pawn's weapon uses ammo.

ONE CANONICAL STREAM: a hit is recorded once, published once. Once NotifyDamageDealt has recorded a hit this session
("primary_seen"), the fallback records nothing. Before that, a fallback hit waits DEDUPE_WINDOW (0.25 s) in "pending";
if the primary reports anything in that time, the pending hits are discarded as duplicates, otherwise they are
committed as estimated hits. A primary report made inside the TakeDamage call it belongs to marks that call
"notified", so its fallback is skipped outright.

ATTRIBUTION (the hit's "source" weapon and "attribution"):
  - "weapon":     TakeDamage's DamageCauser was a WillowWeapon (an instant hit). Exact.
  - "projectile": DamageCauser's Owner is a WillowWeapon (a projectile, splash). Exact if the game sets Owner so.
  - "held":       no causer was available (NotifyDamageDealt outside a TakeDamage call, or an unreadable causer): the
                  weapon held when the hit landed. APPROXIMATE: a projectile or DoT landing after a weapon swap is
                  credited to the new gun. The "only this weapon" filter never drops a "held" hit, since it may belong
                  to the filtered gun; the UI says how many hits are approximate.
  Shots are always exact: ConsumeAmmo's object is the weapon itself.

KILLS are estimated: a hit counts as a killing blow when bWasOneShotKill is set or FinalDamage >= PreviousHealth > 0.

RING BUFFER: one deque(maxlen=cap) of hits (both hooks) and one of shots. When full the oldest entry is dropped and
counted in "dropped". The first 20 raw DamageSummary reads of a session are kept for the debug dump only (a full field
read on every hit would cost too much).

TARGET FILTER: the summary for one target uses that target's hits only, its duration (and DPS) spans those hits only,
and the shot-based fields are None and listed in "session_wide" (shots cannot be tied to a target).

SESSION LIFECYCLE: start (cap 10..20000, optional weapon id) clears the buffers and begins a new session id. stop
keeps the buffers for reading. reset clears the buffers and keeps the active flag, cap and weapon, with a new session
id. remove_hooks() also ends the session. While no session is active every hook returns on its first statement,
touching nothing. One DAMAGE event (events.DAMAGE) is published per committed hit, only while a session runs.
The session "shots" counter counts every shot seen; the summary's "shots" counts the shots still kept.

A/B: snapshot A and B are taken from rtse.gear_edit.item_detail (read-only). compare() lists every stat and two
derived rows that are marked estimated.

DEBUG: GET /api/debug/lab writes rtse/debug_lab.json.
"""

from __future__ import annotations

import collections
import json
import time
from pathlib import Path
from typing import Any

from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.hooks import Type, add_hook, remove_hook

from . import events, gear_inventory
from .gear_edit import item_detail
from .routing import Route

DEBUG_LAB_FILE = Path(__file__).parent / "debug_lab.json"
HOOK_ID = "rtse_damage_lab"
PRIMARY = "WillowGame.WillowPlayerController:NotifyDamageDealt"
FALLBACK = "WillowGame.WillowPawn:TakeDamage"
SHOTS = "WillowGame.WillowWeapon:ConsumeAmmo"

VIA_NOTIFY = "notify"
VIA_TAKE = "take_damage"
VIAS = (VIA_NOTIFY, VIA_TAKE)
# How a hit's source weapon was found
ATTR_WEAPON = "weapon"  # TakeDamage's DamageCauser was the weapon (instant hit)
ATTR_PROJECTILE = "projectile"  # DamageCauser was a projectile whose Owner is the weapon
ATTR_HELD = "held"  # no causer: the weapon held when the hit landed (approximate)
DEDUPE_WINDOW = 0.25  # seconds a fallback hit waits for the primary hook to report the same hit
MAX_PENDING = 256
MAX_OPEN_CALLS = 16
# Summary fields that need shots, which cannot be tied to one target
SESSION_WIDE = ("shots", "pellets_per_shot", "pellets_fired", "hits_per_shot", "per_pellet")

DEFAULT_CAP = 2000
MIN_CAP = 10
MAX_CAP = 20000
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000
RAW_KEEP = 20
TOP_TARGETS = 10
MIN_DPS_DURATION = 0.5  # seconds; a shorter window gives no DPS
MAX_LOGGED = 20  # distinct hook error messages that are logged; later ones only count
MAX_RAW_FIELDS = 80
CHANGE_TOLERANCE = 1e-6

RAW_FIELDS = ("FinalDamage", "DamageDealtToShields", "InitialDamage", "bWasCrit", "bWasOneShotKill", "PreviousHealth")
# Summary fields that are estimates when the hits come from the fallback hook (see the module docstring).
TAKE_ESTIMATED = ("crits", "crit_rate", "kills", "total", "shield", "max_hit", "min_hit", "avg_hit", "avg_crit", "avg_noncrit", "dps")
ALWAYS_ESTIMATED = ("crit_multiplier", "per_pellet", "hits_per_shot", "kills")

UNVERIFIED = {
    "hooks": "None of the three hooks (NotifyDamageDealt, TakeDamage, ConsumeAmmo) has been seen firing in game.",
    "shield": "DamageDealtToShields is read from the game's damage summary; whether it is the shield absorbed by this hit is not checked.",  # noqa: E501
    "dps_clock": "DPS is total damage over the time from the first to the last recorded event, not the game's own combat clock.",  # noqa: E501
    "pellets": "Pellets per shot come from ProjectilesPerShot at each shot; whether the game fires that many and all of them hit is unchecked.",  # noqa: E501
}


class LabError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


ROUTE_ERROR = LabError


# --- Session state ----------------------------------------------------------------------------------------------------

_session_active = False  # checked FIRST in every hook
_s: dict[str, Any]  # the current session's buffer and counters; built by _new_session()
_raw: collections.deque = collections.deque(maxlen=RAW_KEEP)
_snapshots: dict[str, dict[str, Any] | None] = {"a": None, "b": None}
# Open player-instigated TakeDamage calls, innermost last: {"victim": UObject, "weapon": path | None, "how": str,
# "notified": bool}. Lives only for the length of one game call (PRE pushes, POST pops); never leaves this module.
_calls: list[dict[str, Any]] = []

_installed: list[str] = []
_failed: dict[str, str] = {}
_fired: dict[str, int] = {}
_errors = 0
_last_error: str | None = None
_logged: set[str] = set()


def _new_session(session_id: int, cap: int, weapon: str | None, started: float | None) -> dict[str, Any]:
    return {
        "id": session_id,
        "cap": cap,
        "weapon": weapon,
        "started": started,
        "ended": None,
        "seq": 0,
        "hits": collections.deque(maxlen=cap),  # ONE buffer, ONE cap, both hooks
        "recorded": 0,
        "dropped": 0,
        "primary_seen": False,  # NotifyDamageDealt has recorded a hit this session: the fallback stands down
        "pending": collections.deque(maxlen=MAX_PENDING),  # (time, hit): fallback hits waiting out DEDUPE_WINDOW
        "shots": collections.deque(maxlen=cap),
        "shots_seen": 0,
        "shots_dropped": 0,
    }


_s = _new_session(0, DEFAULT_CAP, None, None)


def _now() -> float:
    return time.monotonic()


def _elapsed_since_start() -> float:
    started = _s["started"]
    return 0.0 if started is None else _now() - started


def _session_via() -> str | None:
    if _s["primary_seen"]:
        return VIA_NOTIFY
    return VIA_TAKE if _s["hits"] else None


def _active_hits(target: str | None = None) -> list[dict[str, Any]]:
    """Every kept hit (the one canonical stream), optionally for one target."""
    _flush_pending()
    return [dict(hit) for hit in _s["hits"] if target is None or hit["target_id"] == target]


def _start(cap: int, weapon: str | None) -> None:
    global _session_active, _s  # noqa: PLW0603
    _s = _new_session(_s["id"] + 1, cap, weapon, _now())
    _raw.clear()
    _calls.clear()
    _session_active = True


def _stop() -> None:
    global _session_active  # noqa: PLW0603
    if _session_active:
        _flush_pending(force=True)
        _s["ended"] = _now()
    _session_active = False
    _calls.clear()


def _reset() -> None:
    global _s  # noqa: PLW0603
    _s = _new_session(_s["id"] + 1, _s["cap"], _s["weapon"], _now() if _session_active else None)
    _raw.clear()
    _calls.clear()


def _clear_all() -> None:
    """Test helper: back to a fresh module (no session, no snapshots, no hook bookkeeping)."""
    global _session_active, _s, _errors, _last_error  # noqa: PLW0603
    _session_active = False
    _s = _new_session(0, DEFAULT_CAP, None, None)
    _raw.clear()
    _calls.clear()
    _snapshots["a"] = None
    _snapshots["b"] = None
    _installed.clear()
    _failed.clear()
    _fired.clear()
    _fired.update(dict.fromkeys(HOOK_PATHS, 0))
    _errors = 0
    _last_error = None
    _logged.clear()


# --- Hook helpers -----------------------------------------------------------------------------------------------------


def _path_of(obj: Any) -> str | None:
    try:
        return obj._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None


def _held_path(pc: Any) -> str | None:
    pawn = getattr(pc, "Pawn", None)
    weapon = getattr(pawn, "Weapon", None) if pawn is not None else None
    return None if weapon is None else _path_of(weapon)


def _is_weapon(obj: Any) -> bool:
    try:
        return obj.Class.Name == "WillowWeapon"
    except Exception:  # noqa: BLE001
        return False


def _causing_weapon(causer: Any) -> tuple[str | None, str]:
    """(weapon path, how) from TakeDamage's DamageCauser: the weapon itself (instant hit), or the weapon that owns the
    projectile. (None, ATTR_HELD) when neither is readable; the caller then falls back to the held weapon."""
    if causer is None:
        return None, ATTR_HELD
    if _is_weapon(causer):
        return _path_of(causer), ATTR_WEAPON
    owner = getattr(causer, "Owner", None)
    if owner is not None and _is_weapon(owner):
        return _path_of(owner), ATTR_PROJECTILE
    return None, ATTR_HELD


def _attribute(pc: Any, weapon: str | None, how: str) -> tuple[str | None, str]:
    """The hit's source and how it was found. Without a causer it is the held weapon, which is only approximate."""
    if how != ATTR_HELD and weapon is not None:
        return weapon, how
    return _held_path(pc), ATTR_HELD


def _weapon_ok(source: str | None, how: str) -> bool:
    """The "only this weapon" filter. A hit attributed only by the held weapon is never dropped on that basis: it may
    be a projectile, splash or DoT from the filtered gun landing after a swap."""
    return _s["weapon"] is None or how == ATTR_HELD or source == _s["weapon"]


def _target_names(pawn: Any) -> tuple[str, str]:
    """(human-readable or class name, path name). Both empty when there is no target."""
    if pawn is None:
        return "", ""
    path = _path_of(pawn) or ""
    name: Any = None
    try:
        name = pawn.GetHumanReadableName()
    except Exception:  # noqa: BLE001 - most pawns have no such call
        name = None
    if not name:
        try:
            name = pawn.Class.Name
        except Exception:  # noqa: BLE001
            name = None
    return str(name or path), path


def _killing_blow(summary: Any) -> bool:
    """Estimated: the hit took at least the health the target had before it (shield hits never count)."""
    before = float(summary.PreviousHealth)
    return bool(summary.bWasOneShotKill) or (before > 0 and float(summary.FinalDamage) >= before)


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _remember_raw(summary: Any) -> None:
    """Keeps the six known DamageSummary fields plus every other readable plain field, for the debug dump."""
    names = list(RAW_FIELDS)
    try:
        names += [n for n in dir(summary) if not n.startswith("_") and n not in RAW_FIELDS]
    except Exception:  # noqa: BLE001 - dir() may not work on game structs
        pass
    out: dict[str, Any] = {}
    for name in names[:MAX_RAW_FIELDS]:
        try:
            value = getattr(summary, name)
        except Exception as ex:  # noqa: BLE001
            out[name] = f"<unreadable: {ex!r}>"
            continue
        if not callable(value):
            out[name] = _plain(value)
    _raw.append(out)


def _hit(via: str, *, amount: float, shield: float, initial: float | None, critical: bool, kill: bool, target: str,
         target_id: str, source: str | None, attribution: str, estimated: bool) -> dict[str, Any]:  # fmt: skip
    return {
        "seq": 0,  # set when committed
        "t": _elapsed_since_start(),
        "amount": amount,
        "shield": shield,
        "initial": initial,
        "critical": critical,
        "kill": kill,
        "target": target,
        "target_id": target_id,
        "source": source,
        "attribution": attribution,
        "via": via,
        "estimated": estimated,
    }


def _commit(hit: dict[str, Any]) -> None:
    """Puts one hit in the session's single buffer and publishes it: exactly one DAMAGE event per hit."""
    buf = _s["hits"]
    _s["recorded"] += 1
    if len(buf) == buf.maxlen:
        _s["dropped"] += 1
    _s["seq"] += 1
    hit["seq"] = _s["seq"]
    buf.append(hit)
    events.publish(events.DAMAGE, dict(hit))


def _flush_pending(*, force: bool = False) -> None:
    """Settles fallback hits older than DEDUPE_WINDOW (all of them when force): dropped if the primary hook has worked
    this session (it reports the same hits), otherwise committed as estimated hits."""
    pending = _s["pending"]
    if not pending:
        return
    now = _now()
    while pending and (force or now - pending[0][0] >= DEDUPE_WINDOW):
        _at, hit = pending.popleft()
        if not _s["primary_seen"]:
            _commit(hit)


def _record_shot(*, weapon: str | None, pellets: int | None, cost: float | None, fire_mode: int | None) -> None:
    buf = _s["shots"]
    _s["shots_seen"] += 1
    if len(buf) == buf.maxlen:
        _s["shots_dropped"] += 1
    _s["seq"] += 1
    buf.append(
        {
            "seq": _s["seq"],
            "t": _elapsed_since_start(),
            "weapon": weapon,
            "pellets": pellets,
            "cost": cost,
            "fire_mode": fire_mode,
        },
    )


def _bump(path: str) -> None:
    _fired[path] = _fired.get(path, 0) + 1


def _fail(path: str, ex: BaseException) -> None:
    global _errors, _last_error  # noqa: PLW0603
    _errors += 1
    message = f"{type(ex).__name__}: {ex}"
    _last_error = f"{path}: {message}"
    if _last_error not in _logged and len(_logged) < MAX_LOGGED:
        _logged.add(_last_error)
        logging.error(f"RTSE: damage lab hook failed at {path}: {message}")


# --- Hooks (the first statement of each is the cheap session check) ---------------------------------------------------


def _on_notify(obj: Any, args: Any, _ret: Any, _func: Any) -> None:
    """NotifyDamageDealt on the player controller: one call per hit the player deals."""
    if not _session_active:
        return
    _bump(PRIMARY)
    try:
        pc = get_pc(possibly_loading=True)
        if pc is None or obj != pc:
            return
        summary = args.DamageSummary
        if len(_raw) < RAW_KEEP:  # the full field read is only for the debug dump: the first hits of a session do
            _remember_raw(summary)
        victim = args.DamagedPawn
        # Inside the player's TakeDamage call on this victim (if the game nests them), the causer is known exactly
        call = _calls[-1] if _calls and _calls[-1]["victim"] == victim else None
        if call is not None:
            call["notified"] = True
        source, how = _attribute(pc, call["weapon"] if call else None, call["how"] if call else ATTR_HELD)
        _flush_pending()  # fallback hits that already waited out the window are kept (the primary missed them)
        if not _s["primary_seen"]:
            _s["primary_seen"] = True
            _s["pending"].clear()  # the primary works: hits still waiting are the same hits, reported again
        if not _weapon_ok(source, how):
            return
        target, target_id = _target_names(victim)
        _commit(
            _hit(
                VIA_NOTIFY,
                amount=float(summary.FinalDamage),
                shield=float(summary.DamageDealtToShields),
                initial=_opt_float(summary.InitialDamage),
                critical=bool(summary.bWasCrit),
                kill=_killing_blow(summary),
                target=target,
                target_id=target_id,
                source=source,
                attribution=how,
                estimated=False,
            ),
        )
    except Exception as ex:  # noqa: BLE001 - a hook must never raise into the game
        _fail(PRIMARY, ex)


def _on_take_damage_pre(obj: Any, args: Any, _ret: Any, _func: Any) -> None:
    """Before TakeDamage on any pawn: remembers the causing weapon of a player-instigated hit for the call's length."""
    if not _session_active:
        return
    try:
        pc = get_pc(possibly_loading=True)
        if pc is None or args.InstigatedBy != pc or obj == pc.Pawn:
            return
        if len(_calls) >= MAX_OPEN_CALLS:  # a POST that never came: never let this grow
            _calls.clear()
        weapon, how = _causing_weapon(getattr(args, "DamageCauser", None))
        _calls.append({"victim": obj, "weapon": weapon, "how": how, "notified": False})
    except Exception as ex:  # noqa: BLE001
        _fail(FALLBACK, ex)


def _on_take_damage(obj: Any, args: Any, _ret: Any, _func: Any) -> None:
    """After TakeDamage on any pawn: the fallback. Records only when the primary hook did not report this hit and has
    not worked this session; such hits wait DEDUPE_WINDOW in case the primary reports them a little later."""
    if not _session_active:
        return
    _bump(FALLBACK)
    try:
        _flush_pending()
        call = _calls.pop() if _calls and _calls[-1]["victim"] == obj else None
        if call is None or call["notified"] or _s["primary_seen"]:
            return  # not the player's hit, or the primary hook already has it
        pc = get_pc(possibly_loading=True)
        if pc is None:
            return
        source, how = _attribute(pc, call["weapon"], call["how"])
        if not _weapon_ok(source, how):
            return
        target, target_id = _target_names(obj)
        hit = _hit(
            VIA_TAKE,
            amount=float(args.Damage),
            shield=0.0,  # not measured by this hook
            initial=None,
            critical=bool(getattr(obj, "bWasLastDamageACriticalHit", False)),
            kill=False,  # not measured by this hook
            target=target,
            target_id=target_id,
            source=source,
            attribution=how,
            estimated=True,
        )
        _s["pending"].append((_now(), hit))
    except Exception as ex:  # noqa: BLE001
        _fail(FALLBACK, ex)


def _on_consume_ammo(obj: Any, args: Any, _ret: Any, _func: Any) -> None:
    """ConsumeAmmo on a weapon: one call per shot. Counted when the weapon belongs to the local pawn. The weapon is the
    hooked object itself, so the "only this weapon" filter is exact here."""
    if not _session_active:
        return
    _bump(SHOTS)
    try:
        pc = get_pc(possibly_loading=True)
        pawn = getattr(pc, "Pawn", None) if pc is not None else None
        if pawn is None or obj.Owner != pawn:
            return
        weapon = _path_of(obj)
        if _s["weapon"] is not None and weapon != _s["weapon"]:
            return
        _record_shot(
            weapon=weapon,
            pellets=_opt_int(obj.ProjectilesPerShot),
            cost=_opt_float(obj.ShotCost),
            fire_mode=_opt_int(getattr(args, "FireModeNum", None)),
        )
    except Exception as ex:  # noqa: BLE001
        _fail(SHOTS, ex)


HOOKS = (
    (PRIMARY, Type.POST, _on_notify),
    (FALLBACK, Type.PRE, _on_take_damage_pre),
    (FALLBACK, Type.POST, _on_take_damage),
    (SHOTS, Type.POST, _on_consume_ammo),
)
HOOK_PATHS = tuple(dict.fromkeys(path for path, _kind, _fn in HOOKS))
_fired.update(dict.fromkeys(HOOK_PATHS, 0))


def install_hooks() -> None:
    """Idempotent: each hook is first cleared of our old one (an F8 restart or a module reload), then added."""
    global _installed  # noqa: PLW0603
    _installed = []
    _failed.clear()
    for path, kind, fn in HOOKS:
        try:
            remove_hook(path, kind, HOOK_ID)
        except Exception:  # noqa: BLE001, S110 - nothing to remove yet
            pass
        try:
            add_hook(path, kind, HOOK_ID, fn)
        except Exception as ex:  # noqa: BLE001
            _failed[path] = repr(ex)
            logging.error(f"RTSE: could not hook {path}: {ex!r}")
        else:
            if path not in _installed:
                _installed.append(path)
    logging.info(f"RTSE: damage lab hooks installed: {_installed}")


def remove_hooks() -> None:
    """Removes our hooks from every path in HOOKS (whether or not this run installed them), and ends any session."""
    global _installed  # noqa: PLW0603
    _stop()
    for path, kind, _fn in HOOKS:
        try:
            remove_hook(path, kind, HOOK_ID)
        except Exception:  # noqa: BLE001, S110
            pass
    _installed = []


# --- Summary (pure) ---------------------------------------------------------------------------------------------------


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(hits: list[dict[str, Any]], shots: list[dict[str, Any]], *, target: str | None = None) -> dict[str, Any]:
    """The SUMMARY of the route docs, from hit and shot records. Pure: no game objects, no clock.

    With `target`, only that target's hits count, the duration spans those hits only, and the shot-based fields
    (which cannot be tied to a target) are None and listed in "session_wide"."""
    if target is not None:
        hits = [h for h in hits if h["target_id"] == target]
        shots = []
    amounts = [h["amount"] for h in hits]
    crit_amounts = [h["amount"] for h in hits if h["critical"]]
    normal_amounts = [h["amount"] for h in hits if not h["critical"]]
    total = sum(amounts)
    avg_crit = _mean(crit_amounts)
    avg_noncrit = _mean(normal_amounts)
    crit_multiplier = avg_crit / avg_noncrit if avg_crit is not None and avg_noncrit else None

    times = [h["t"] for h in hits] + [s["t"] for s in shots]
    duration = max(times) - min(times) if times else None
    dps = total / duration if duration is not None and duration >= MIN_DPS_DURATION else None

    pellet_counts = [s["pellets"] for s in shots if s["pellets"] is not None]
    pellets_fired = sum(pellet_counts)

    by_target: dict[str, dict[str, Any]] = {}
    for hit in hits:
        row = by_target.setdefault(hit["target_id"], {"target_id": hit["target_id"], "target": hit["target"], "hits": 0, "total": 0.0})
        row["hits"] += 1
        row["total"] += hit["amount"]
    top_targets = sorted(by_target.values(), key=lambda row: row["total"], reverse=True)[:TOP_TARGETS]

    estimated = list(ALWAYS_ESTIMATED)
    if any(h["estimated"] for h in hits):
        estimated += list(TAKE_ESTIMATED)

    out = {
        "scope": "session" if target is None else "target",
        "hits": len(hits),
        "crits": len(crit_amounts),
        "crit_rate": len(crit_amounts) / len(hits) if hits else None,
        "total": total,
        "shield": sum(h["shield"] or 0.0 for h in hits),
        "kills": sum(1 for h in hits if h["kill"]),
        "max_hit": max(amounts) if amounts else None,
        "min_hit": min(amounts) if amounts else None,
        "avg_hit": _mean(amounts),
        "avg_crit": avg_crit,
        "avg_noncrit": avg_noncrit,
        "crit_multiplier": crit_multiplier,
        "duration": duration,
        "dps": dps,
        "shots": len(shots),
        "pellets_per_shot": shots[-1]["pellets"] if shots else None,
        "pellets_fired": pellets_fired,
        "hits_per_shot": len(hits) / len(shots) if shots else None,
        "per_pellet": total / pellets_fired if pellets_fired else None,
        "approximate": sum(1 for h in hits if h.get("attribution") == ATTR_HELD),
        "targets": top_targets,
        "estimated": list(dict.fromkeys(estimated)),
        "session_wide": [],
    }
    if target is not None:
        for name in SESSION_WIDE:
            out[name] = None
        out["session_wide"] = list(SESSION_WIDE)
    return out


# --- State ------------------------------------------------------------------------------------------------------------


def _session_state() -> dict[str, Any]:
    _flush_pending()
    started, ended = _s["started"], _s["ended"]
    if started is None:
        elapsed = 0.0
    else:
        end = ended if ended is not None else (_now() if _session_active else started)
        elapsed = end - started
    return {
        "active": _session_active,
        "id": _s["id"],
        "elapsed": elapsed,
        "cap": _s["cap"],
        "weapon": _s["weapon"],
        "recorded": _s["recorded"],
        "kept": len(_s["hits"]),
        "dropped": _s["dropped"],
        "pending": len(_s["pending"]),
        "approximate": sum(1 for h in _s["hits"] if h["attribution"] == ATTR_HELD),
        "shots": _s["shots_seen"],
        "via": _session_via(),
    }


def _hooks_state() -> dict[str, Any]:
    return {
        "installed": list(_installed),
        "failed": dict(_failed),
        "fired": dict(_fired),
        "errors": _errors,
        "last_error": _last_error,
    }


def _summary(target: str | None = None) -> dict[str, Any]:
    return summarize(_active_hits(), list(_s["shots"]), target=target)


def state(target: str | None = None) -> dict[str, Any]:
    return {
        "session": _session_state(),
        "summary": _summary(target),
        "hooks": _hooks_state(),
        "snapshots": {"a": _snapshots["a"], "b": _snapshots["b"]},
        "unverified": dict(UNVERIFIED),
    }



# --- Parameters -------------------------------------------------------------------------------------------------------


def _int_param(params: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = params.get(name)
    if value is None or value == "":
        return default
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise LabError(400, f"'{name}' must be a whole number") from None
    if isinstance(value, bool) or not isinstance(value, int):
        raise LabError(400, f"'{name}' must be a whole number")
    if not low <= value <= high:
        raise LabError(400, f"'{name}' must be between {low} and {high}")
    return value


def _str_param(params: dict[str, Any], name: str) -> str | None:
    value = params.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise LabError(400, f"'{name}' must be a string")
    return value


def _slot_param(value: Any, *, required: bool) -> str | None:
    if value is None or value == "":
        if required:
            raise LabError(400, "'slot' must be \"a\" or \"b\"")
        return None
    if value not in ("a", "b"):
        raise LabError(400, "'slot' must be \"a\" or \"b\"")
    return value


def _flag(value: Any) -> bool:
    if value in (None, "", "0", 0, False):
        return False
    if value in ("1", 1, True):
        return True
    raise LabError(400, "'live' must be 1 or 0")


# --- Routes -----------------------------------------------------------------------------------------------------------


def _start_route(params: dict[str, Any]) -> dict[str, Any]:
    cap = _int_param(params, "cap", DEFAULT_CAP, MIN_CAP, MAX_CAP)
    weapon = _str_param(params, "weapon")
    _start(cap, None if weapon is None else gear_inventory._resolve_alias(weapon))  # noqa: SLF001
    return state()


def _stop_route(_params: dict[str, Any]) -> dict[str, Any]:
    _stop()
    return state()


def _reset_route(_params: dict[str, Any]) -> dict[str, Any]:
    _reset()
    return state()


def _state_route(params: dict[str, Any]) -> dict[str, Any]:
    return state(_str_param(params, "target"))


def _hits_route(params: dict[str, Any]) -> dict[str, Any]:
    """Hits with seq > after, oldest first, up to limit. With tail=N: the newest N hits instead (after is ignored), so a
    client opening a long session gets the latest hits in one read."""
    after = _int_param(params, "after", -1, -1, 2**62)
    limit = _int_param(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    tail = _int_param(params, "tail", 0, 0, MAX_LIMIT)
    target = _str_param(params, "target")
    _flush_pending()
    kept = _s["hits"]
    matching = [hit for hit in kept if target is None or hit["target_id"] == target]
    if tail:
        out = [dict(hit) for hit in matching[-tail:]]
    else:
        out = [dict(hit) for hit in matching if hit["seq"] > after][:limit]
    return {"session": _s["id"], "hits": out, "last": kept[-1]["seq"] if kept else -1}



def _take_snapshot(item_id: str, slot: str) -> dict[str, Any]:
    detail = item_detail({"id": item_id})
    return _snapshot_from_detail(slot, detail, item_id)


def _snapshot_from_detail(slot: str, detail: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    stats = [
        {"name": st["name"], "label": st["label"], "value": _num(st.get("value")), "base": _num(st.get("base"))}
        for st in detail.get("stats", [])
    ]
    parts = [{"slot": sl["slot"], "name": (sl.get("current") or {}).get("name")} for sl in detail.get("slots", [])]
    return {
        "slot": slot,
        "id": detail.get("id", fallback_id),
        "name": detail.get("name"),
        "taken_at": time.time(),
        "stats": stats,
        "parts": parts,
    }


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _snapshot_route(params: dict[str, Any]) -> dict[str, Any]:
    item_id = params.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise LabError(400, "'id' is required")
    slot = _slot_param(params.get("slot"), required=True)
    _snapshots[slot] = _take_snapshot(item_id, slot)
    return state()


def _snapshot_clear_route(params: dict[str, Any]) -> dict[str, Any]:
    slot = _slot_param(params.get("slot"), required=False)
    for key in (slot,) if slot else ("a", "b"):
        _snapshots[key] = None
    return state()


def _differs(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return abs(a - b) > CHANGE_TOLERANCE


def _row(name: str, label: str, a: float | None, b: float | None, *, estimated: bool) -> dict[str, Any]:
    delta = b - a if a is not None and b is not None else None
    pct = delta / abs(a) * 100 if delta is not None and a else None
    return {
        "name": name,
        "label": label,
        "a": a,
        "b": b,
        "delta": delta,
        "pct": pct,
        "changed": _differs(a, b),
        "estimated": estimated,
    }


def _derived(values: dict[str, float | None]) -> tuple[float | None, float | None]:
    """(damage per shot, card DPS) from one snapshot's stat values. Estimates: pellets are assumed to all hit."""
    damage = values.get("InstantHitDamage")
    pellets = values.get("ProjectilesPerShot")
    interval = values.get("FireInterval")
    per_shot = damage * pellets if damage is not None and pellets is not None else None
    card_dps = per_shot / interval if per_shot is not None and interval else None
    return per_shot, card_dps


def compare_snapshots(a: dict[str, Any], b: dict[str, Any], b_live: bool) -> dict[str, Any]:
    """COMPARE from two snapshots. Pure."""
    a_stats = {st["name"]: st for st in a["stats"]}
    b_stats = {st["name"]: st for st in b["stats"]}
    rows = []
    for name in list(a_stats) + [n for n in b_stats if n not in a_stats]:
        sa, sb = a_stats.get(name), b_stats.get(name)
        label = (sa or sb)["label"]
        rows.append(
            _row(
                name,
                label,
                sa["value"] if sa else None,
                sb["value"] if sb else None,
                estimated=False,
            ),
        )
    a_values = {name: st["value"] for name, st in a_stats.items()}
    b_values = {name: st["value"] for name, st in b_stats.items()}
    a_shot, a_card = _derived(a_values)
    b_shot, b_card = _derived(b_values)
    rows.append(
        _row("est_shot_damage", "Damage per shot (estimated: damage x pellets)", a_shot, b_shot, estimated=True),
    )
    rows.append(_row("est_card_dps", "Card DPS (estimated: per shot / fire interval)", a_card, b_card, estimated=True))

    a_parts = {p["slot"]: p["name"] for p in a["parts"]}
    b_parts = {p["slot"]: p["name"] for p in b["parts"]}
    parts = [
        {"slot": slot, "a": a_parts.get(slot), "b": b_parts.get(slot), "changed": a_parts.get(slot) != b_parts.get(slot)}
        for slot in list(a_parts) + [s for s in b_parts if s not in a_parts]
    ]
    return {"a": a, "b": b, "b_live": b_live, "rows": rows, "parts": parts}


def _compare_route(params: dict[str, Any]) -> dict[str, Any]:
    live = _flag(params.get("live"))
    a = _snapshots["a"]
    if a is None:
        raise LabError(409, "take snapshot A first (POST /api/lab/snapshot with slot \"a\")")
    b_live = live or _snapshots["b"] is None
    b = _take_snapshot(a["id"], "b") if b_live else _snapshots["b"]
    return compare_snapshots(a, b, b_live)


def debug_dump() -> dict[str, Any]:
    """Writes rtse/debug_lab.json: session, hook bookkeeping, the last raw DamageSummary reads and the local paths."""
    pc = get_pc(possibly_loading=True)
    pawn = getattr(pc, "Pawn", None) if pc is not None else None
    out = {
        "session": _session_state(),
        "hooks": _hooks_state(),
        "raw_summaries": list(_raw),
        "pc": _path_of(pc) if pc is not None else None,
        "pawn": _path_of(pawn) if pawn is not None else None,
        "held_weapon": _held_path(pc) if pc is not None else None,
        "summary": _summary(),
    }
    DEBUG_LAB_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_LAB_FILE)}


# --- Routes (paths are the API; api.py collects these) ---

GET_ROUTES: dict[str, Route] = {
    "/api/lab": _state_route,
    "/api/lab/hits": _hits_route,
    "/api/lab/compare": _compare_route,
    "/api/debug/lab": lambda _params: debug_dump(),
}
POST_ROUTES: dict[str, Route] = {
    "/api/lab/session/start": _start_route,
    "/api/lab/session/stop": _stop_route,
    "/api/lab/session/reset": _reset_route,
    "/api/lab/snapshot": _snapshot_route,
    "/api/lab/snapshot/clear": _snapshot_clear_route,
}
