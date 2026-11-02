"""Persistent weapon stat overrides.

The game saves a weapon's parts, not its computed stats, and rebuilds the stats from the parts
whenever it initialises the weapon. So overrides live in our own file and are re-applied whenever the
game recomputes.

Two keys, two kinds of entry (file format v2, see _save):

  - "weapons": keyed by identity(item) = "<UniqueId>#<fingerprint>". One entry per individual gun.
    Edits made through this module always land here.
  - "legacy": keyed by fingerprint(item) (definition data only). This is what version 1 of the file
    stored. Migrated entries live here, unchanged.

Lookup is the gun's own entry first, else the legacy entry for its fingerprint.

Ambiguity rule: version 1 could not tell identical guns apart, so a legacy entry applies to every gun
with that fingerprint, exactly as before (nothing is lost). The first time a stat is changed or cleared
on a gun that is using a legacy entry, that gun gets its own copy (the legacy stats plus the change) under
its identity. The legacy entry stays for the other identical guns.

UniqueId caveat: identity() depends on the UniqueId being the same after a save is reloaded. That is
UNVERIFIED. If the game regenerates UniqueIds on load, own entries stop matching and those guns fall back
to the legacy entry (or to no overrides). Check with the debug_item route (it reports the UniqueId) across
a save and reload before relying on this.

A UniqueId of 0 means "unknown" (unreadable). Identical guns with uid 0 therefore share one own entry, the
same as the old behaviour. This is documented, not fixed.

Migration (load): a file without "version" is version 1. It is copied byte-for-byte to
overrides.v1.bak.json (or overrides.v1.bak.<n>.json if that exists), then every entry moves into "legacy"
and the file is saved as version 2. A file that cannot be read, or has an unknown version, is left
untouched and is copied aside to overrides.corrupt.bak.json before the first save.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

from unrealsdk import logging, unreal
from unrealsdk.hooks import Type, add_hook, remove_hook

OVERRIDES_FILE = Path(__file__).parent / "overrides.json"
FILE_VERSION = 2

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

# identity -> {"label": str, "stats": {stat name: value}}: one entry per individual gun (version 2)
_weapons: dict[str, dict[str, Any]] = {}
# fingerprint -> {"label": str, "stats": {stat name: value}}: version 1 entries, applying to all identical guns
_legacy: dict[str, dict[str, Any]] = {}
_backup_before_save = False  # The file on disk is unreadable or unknown: copy it aside before the first write
_save_blocked = False  # Migration could not back the old file up: never overwrite it
_applying = False  # Our own writes must not re-trigger the hooks
_installed: list[str] = []


# --- Persistence ---------------------------------------------------------------------------------


def load() -> None:
    global _backup_before_save, _save_blocked  # noqa: PLW0603
    _weapons.clear()
    _legacy.clear()
    _backup_before_save = False
    _save_blocked = False
    if not OVERRIDES_FILE.exists():
        return
    try:
        raw = OVERRIDES_FILE.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as ex:
        logging.error(f"RTSE: could not read {OVERRIDES_FILE.name}, leaving it untouched: {ex!r}")
        _backup_before_save = True
        return
    if not isinstance(data, dict):
        logging.error(f"RTSE: {OVERRIDES_FILE.name} is not an overrides file, leaving it untouched")
        _backup_before_save = True
        return
    if "version" not in data:
        _migrate_v1(raw, data)
        return
    if data.get("version") != FILE_VERSION:
        logging.error(
            f"RTSE: {OVERRIDES_FILE.name} has version {data.get('version')!r}, expected {FILE_VERSION}; "
            "leaving it untouched",
        )
        _backup_before_save = True
        return
    _weapons.update(_entries(data.get("weapons")))
    _legacy.update(_entries(data.get("legacy")))
    logging.info(f"RTSE: loaded {len(_weapons)} per-gun override(s) and {len(_legacy)} legacy override(s)")


def _entries(mapping: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(mapping, dict):
        return {}
    return {key: value for key, value in mapping.items() if isinstance(value, dict)}


def _migrate_v1(raw: bytes, data: dict[str, Any]) -> None:
    global _save_blocked  # noqa: PLW0603
    _legacy.update(_entries(data))  # Every old entry, unchanged
    try:
        backup = _write_backup(raw, "v1")
    except OSError as ex:
        # Keep applying the old entries, but never write over a file we could not copy aside
        logging.error(f"RTSE: could not back up {OVERRIDES_FILE.name} before migrating, so it is not changed: {ex!r}")
        _save_blocked = True
        return
    logging.info(
        f"RTSE: migrated {len(_legacy)} override(s) from the version 1 {OVERRIDES_FILE.name}; "
        f"backup at {backup.name}",
    )
    try:
        _save()
    except OSError as ex:  # The backup exists, so the next edit simply saves again
        logging.error(f"RTSE: migrated overrides could not be saved yet: {ex!r}")


def _write_backup(raw: bytes, tag: str) -> Path:
    """Copies the file's bytes to overrides.<tag>.bak.json, or .bak.<n>.json if that name is taken."""
    number = 0
    while True:
        name = f"overrides.{tag}.bak.json" if number == 0 else f"overrides.{tag}.bak.{number}.json"
        path = OVERRIDES_FILE.with_name(name)
        try:
            with path.open("xb") as handle:  # Never overwrites an existing backup
                handle.write(raw)
        except FileExistsError:
            number += 1
            continue
        if path.read_bytes() != raw:
            raise OSError(f"{path.name} does not match the original")
        return path


def _prepare_write() -> None:
    """Runs before any change is made in memory, so a failure here leaves everything as it was."""
    global _backup_before_save  # noqa: PLW0603
    if _save_blocked:
        raise OSError(f"{OVERRIDES_FILE.name} could not be backed up, so it will not be overwritten")
    if _backup_before_save:
        _write_backup(OVERRIDES_FILE.read_bytes(), "corrupt")
        _backup_before_save = False


def _save() -> None:
    """Writes the file atomically: a temp file next to it, flushed to disk, then renamed over the real one. A failure
    midway leaves the old file intact, and the temp file is removed."""
    text = json.dumps({"version": FILE_VERSION, "weapons": _weapons, "legacy": _legacy}, indent=2)
    tmp = OVERRIDES_FILE.with_name(OVERRIDES_FILE.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, OVERRIDES_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# --- Identity ------------------------------------------------------------------------------------


def fingerprint(item: unreal.UObject) -> str | None:
    """Identifies a kind of weapon by its definition data. None for anything that is not a weapon."""
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


def unique_id(item: unreal.UObject) -> int:
    """The game's UniqueId for this weapon. 0 when it cannot be read (see the module docstring)."""
    try:
        return int(item.DefinitionData.UniqueId)
    except Exception:  # noqa: BLE001
        return 0


def identity(item: unreal.UObject) -> str | None:
    """Identifies one individual weapon: "<UniqueId>#<fingerprint>". None for anything that is not a weapon."""
    key = fingerprint(item)
    if key is None:
        return None
    return f"{unique_id(item)}#{key}"


def _legacy_stats(key: str | None) -> dict[str, Any] | None:
    """The stats of a legacy entry, or None when there is none (or it has no stats)."""
    entry = _legacy.get(key) if key is not None else None
    if not entry or not entry.get("stats"):
        return None
    return dict(entry["stats"])


def override_scope(item: unreal.UObject) -> str | None:
    """"own" when this gun has its own entry, "legacy" when it uses the shared legacy entry, else None."""
    ident = identity(item)
    if ident is not None and ident in _weapons:
        return "own"
    if _legacy_stats(fingerprint(item)) is not None:
        return "legacy"
    return None


def move_overrides(old_identity: str | None, new_identity: str | None) -> str | None:
    """Carries a gun's overrides to its new identity after the game rebuilt it (new UniqueId and/or parts).

    Moves its own entry. If it had none but a legacy entry matched its old fingerprint, and the fingerprint
    changed, the legacy stats are copied under the new identity (the legacy entry itself stays for the
    other identical guns). Nothing is saved if nothing moved. The game has already been changed, so a save
    failure is not raised: the entry is kept under both the old and the new key in memory (a restart then
    still finds it under whichever identity the file has), and a warning is returned. None on success.
    """
    if old_identity is None or new_identity is None or old_identity == new_identity:
        return None
    kept_old = None
    if old_identity in _weapons:
        kept_old = _weapons[old_identity]
        _weapons[new_identity] = _weapons.pop(old_identity)
    else:
        old_key = old_identity.partition("#")[2]
        new_key = new_identity.partition("#")[2]
        legacy = _legacy_stats(old_key)
        if legacy is None or old_key == new_key or new_identity in _weapons:
            return None
        _weapons[new_identity] = {"label": _legacy[old_key].get("label", ""), "stats": legacy}
    try:
        _prepare_write()
        _save()
    except OSError as ex:
        if kept_old is not None:
            _weapons[old_identity] = kept_old  # Both keys stay in memory until a save succeeds
        logging.error(f"RTSE: could not save moved overrides: {ex!r}")
        return f"the moved overrides could not be saved ({ex}); they are kept in memory and may be lost after a restart"
    return None


# --- Reading and writing -------------------------------------------------------------------------


def overrides_for(item: unreal.UObject) -> dict[str, float]:
    ident = identity(item)
    if ident is not None and ident in _weapons:
        return dict(_weapons[ident].get("stats", {}))
    return _legacy_stats(fingerprint(item)) or {}


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
    """Stores (or with None, removes) an override on this gun and applies it. Returns what the game reports.

    The first change on a gun that uses a legacy entry gives that gun its own copy of the legacy stats first.
    If the file can't be saved, the change is kept in memory and the result's "warning" says so (None otherwise).
    """
    if stat not in STAT_NAMES:
        raise KeyError(stat)
    ident = identity(item)
    key = fingerprint(item)
    if ident is None or key is None:
        raise TypeError("only weapons have editable stats")
    _prepare_write()

    before = {"value": getattr(item, stat), "base": getattr(item, f"{stat}BaseValue")}
    label = item.GetHumanReadableName()
    if value is not None:
        value = _coerce(item, stat, value)

    own = _weapons.get(ident)
    if own is None and _legacy_stats(key) is not None:
        own = _weapons[ident] = {"label": label, "stats": _legacy_stats(key)}  # Claim the legacy overrides
    if value is None:
        if own is not None:
            own["stats"].pop(stat, None)
            # An empty own entry still hides the legacy one, so only drop it when no legacy entry applies
            if not own["stats"] and _legacy_stats(key) is None:
                del _weapons[ident]
    else:
        if own is None:
            own = _weapons[ident] = {"label": label, "stats": {}}
        own["stats"][stat] = value
        own["label"] = label
        _write(item, stat, value)
    warning = None
    try:
        _save()
    except OSError as ex:
        logging.error(f"RTSE: could not save stat override {stat}: {ex!r}")
        warning = f"the {stat} override could not be saved to {OVERRIDES_FILE.name} ({ex}); it is kept in memory and may be lost after a restart"

    after = {"value": getattr(item, stat), "base": getattr(item, f"{stat}BaseValue")}
    return {"stat": stat, "requested": value, "before": before, "after": after, "warning": warning}


# --- Re-applying ---------------------------------------------------------------------------------


def reapply(item: unreal.UObject, *, reason: str) -> int:
    """Re-applies every stored override for this weapon. Returns how many values were rewritten."""
    if _applying or not has_overrides():
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
    return any(entry.get("stats") for entry in (*_weapons.values(), *_legacy.values()))


def reapply_all(items: Iterable[unreal.UObject], *, reason: str) -> None:
    if not has_overrides():
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
