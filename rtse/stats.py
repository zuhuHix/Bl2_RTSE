"""Persistent weapon stat overrides.

The game saves a weapon's parts, not its computed stats, and rebuilds the stats from the parts
whenever it initialises the weapon. So overrides live in our own file, keyed by a fingerprint of the
weapon's definition data, and are re-applied whenever the game recomputes.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

from unrealsdk import logging, unreal
from unrealsdk.hooks import Type, add_hook, remove_hook

OVERRIDES_FILE = Path(__file__).parent / "overrides.json"

MAX_FLOAT = 1e9
MAX_INT = 1_000_000


class StatSpec(NamedTuple):
    name: str  # Attribute name on the weapon, e.g. "ClipSize"
    label: str


WEAPON_STATS = (
    StatSpec("InstantHitDamage", "Damage per shot / pellet"),
    StatSpec("ProjectilesPerShot", "Pellets per shot"),
    StatSpec("ShotCost", "Ammo used per shot"),
    StatSpec("ClipSize", "Magazine size"),
    StatSpec("FireInterval", "Time between shots (s) - lower is faster"),
    StatSpec("ReloadTime", "Reload time (s)"),
    StatSpec("Spread", "Spread"),
    StatSpec("AimError", "Aim error"),
    StatSpec("WeaponRange", "Range"),
    StatSpec("ProjectileSpeedMultiplier", "Projectile speed multiplier"),
    StatSpec("StatusEffectChanceModifier", "Status effect chance multiplier"),
    StatSpec("StatusEffectDamage", "Status effect damage"),
    StatSpec("AdditionalRicochets", "Extra ricochets"),
    StatSpec("ExtraShotChance", "Extra shot chance"),
    StatSpec("AutomaticBurstCount", "Burst count"),
    StatSpec("MeleeDamage", "Melee damage"),
    StatSpec("EquipTime", "Equip time (s)"),
    StatSpec("PutDownTime", "Put-down time (s)"),
)
STAT_NAMES = frozenset(spec.name for spec in WEAPON_STATS)

# Everything which makes one weapon different from another. The game's own tooltip data is derived
# from these, and they are what gets saved, so identical fingerprints are identical weapons.
FINGERPRINT_FIELDS = (
    "WeaponTypeDefinition", "BalanceDefinition", "ManufacturerDefinition", "ManufacturerGradeIndex",
    "BodyPartDefinition", "GripPartDefinition", "BarrelPartDefinition", "SightPartDefinition",
    "StockPartDefinition", "ElementalPartDefinition", "Accessory1PartDefinition",
    "Accessory2PartDefinition", "MaterialPartDefinition", "PrefixPartDefinition",
    "TitlePartDefinition", "GameStage",
)  # fmt: skip

HOOK_ID = "rtse_stat_overrides"
# Functions after which the game has just recomputed (or is about to use) a weapon's stats.
HOOK_FUNCTIONS = (
    "WillowGame.WillowWeapon:CalculateWeaponBaseValues",
    "WillowGame.WillowWeapon:InitializeFromDefinitionData",
    "WillowGame.WillowWeapon:InitializeInternal",
)

# fingerprint -> {"label": str, "stats": {stat name: value}}
_store: dict[str, dict[str, Any]] = {}
_applying = False  # Our own writes must not re-trigger the hooks
_installed: list[str] = []


# --- Persistence ---------------------------------------------------------------------------------


def load() -> None:
    _store.clear()
    if not OVERRIDES_FILE.exists():
        return
    try:
        data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
        _store.update({k: v for k, v in data.items() if isinstance(v, dict) and v.get("stats")})
    except (OSError, ValueError) as ex:
        logging.error(f"RTSE: could not read {OVERRIDES_FILE.name}: {ex!r}")


def _save() -> None:
    OVERRIDES_FILE.write_text(json.dumps(_store, indent=2), encoding="utf-8")


# --- Identity ------------------------------------------------------------------------------------


def fingerprint(item: unreal.UObject) -> str | None:
    """Identifies a weapon by its definition data. None for anything that is not a weapon."""
    if item.Class.Name != "WillowWeapon":
        return None
    try:
        data = item.DefinitionData
        parts = []
        for field in FINGERPRINT_FIELDS:
            value = getattr(data, field)
            parts.append(value._path_name() if isinstance(value, unreal.UObject) else str(value))  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None
    return "|".join(parts)


def move_overrides(old: str | None, new: str | None) -> None:
    """Carries overrides across when the weapon's parts or level are edited."""
    if old is None or new is None or old == new or old not in _store:
        return
    _store[new] = _store.pop(old)
    _save()


# --- Reading and writing -------------------------------------------------------------------------


def overrides_for(item: unreal.UObject) -> dict[str, float]:
    key = fingerprint(item)
    return dict(_store[key]["stats"]) if key in _store else {}


def read_stats(item: unreal.UObject) -> list[dict[str, Any]]:
    if item.Class.Name != "WillowWeapon":
        return []
    overrides = overrides_for(item)
    stats = []
    for spec in WEAPON_STATS:
        try:
            stats.append(
                {
                    "name": spec.name,
                    "label": spec.label,
                    "value": getattr(item, spec.name),
                    "base": getattr(item, f"{spec.name}BaseValue"),
                    "override": overrides.get(spec.name),
                },
            )
        except AttributeError:
            continue
    return stats


def _coerce(item: unreal.UObject, stat: str, value: float) -> int | float:
    if not math.isfinite(value):
        raise ValueError("value must be a finite number")
    if isinstance(getattr(item, stat), int):
        return max(-MAX_INT, min(MAX_INT, round(value)))
    return max(-MAX_FLOAT, min(MAX_FLOAT, float(value)))


def _write(item: unreal.UObject, stat: str, value: float) -> None:
    """Sets both the live value and the base value, so a recompute from the base keeps it."""
    global _applying  # noqa: PLW0603
    value = _coerce(item, stat, value)
    _applying = True
    try:
        setattr(item, stat, value)
        setattr(item, f"{stat}BaseValue", value)
    finally:
        _applying = False


def set_override(item: unreal.UObject, stat: str, value: float | None) -> dict[str, Any]:
    """Stores (or with None, removes) an override and applies it. Returns what the game reports."""
    if stat not in STAT_NAMES:
        raise KeyError(stat)
    key = fingerprint(item)
    if key is None:
        raise TypeError("only weapons have editable stats")

    before = {"value": getattr(item, stat), "base": getattr(item, f"{stat}BaseValue")}
    entry = _store.setdefault(key, {"label": item.GetHumanReadableName(), "stats": {}})
    if value is None:
        entry["stats"].pop(stat, None)
        if not entry["stats"]:
            del _store[key]
    else:
        value = _coerce(item, stat, value)
        entry["stats"][stat] = value
        entry["label"] = item.GetHumanReadableName()
        _write(item, stat, value)
    _save()

    after = {"value": getattr(item, stat), "base": getattr(item, f"{stat}BaseValue")}
    return {"stat": stat, "requested": value, "before": before, "after": after}


# --- Re-applying ---------------------------------------------------------------------------------


def reapply(item: unreal.UObject, *, reason: str) -> int:
    """Re-applies every stored override for this weapon. Returns how many values were rewritten."""
    if _applying or not _store:
        return 0
    overrides = overrides_for(item)
    changed = 0
    for stat, value in overrides.items():
        try:
            if getattr(item, stat) != _coerce(item, stat, value):
                _write(item, stat, value)
                changed += 1
        except Exception as ex:  # noqa: BLE001
            logging.error(f"RTSE: could not apply {stat} override: {ex!r}")
    if changed:
        logging.info(f"RTSE: re-applied {changed} stat override(s) after {reason}")
    return changed


def has_overrides() -> bool:
    return bool(_store)


def reapply_all(items: Iterable[unreal.UObject], *, reason: str) -> None:
    if not _store:
        return
    for item in items:
        reapply(item, reason=reason)


def _on_game_recompute(obj: unreal.UObject, _args: unreal.WrappedStruct, _ret: Any, _func: Any) -> None:
    reapply(obj, reason="the game recomputing stats")


def install_hooks() -> None:
    """Hooks each recompute point. Each is attempted alone so one bad name cannot stop the rest."""
    remove_hooks()
    for path in HOOK_FUNCTIONS:
        try:
            add_hook(path, Type.POST, HOOK_ID, _on_game_recompute)
        except Exception as ex:  # noqa: BLE001
            logging.error(f"RTSE: could not hook {path}: {ex!r}")
        else:
            _installed.append(path)
    logging.info(f"RTSE: stat hooks installed: {_installed}")


def remove_hooks() -> None:
    for path in _installed:
        try:
            remove_hook(path, Type.POST, HOOK_ID)
        except Exception:  # noqa: BLE001, S110
            pass
    _installed.clear()
