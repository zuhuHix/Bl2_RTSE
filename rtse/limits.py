"""Level limits shared by the character page and gear editing.

Sources (checked against the game's own class definitions with dev/class_index.py; existence only, the return
values have NOT been observed in a live game):
  - WillowPlayerController.GetMaximumPossiblePlayerLevelCap() -> int: the highest character level this install
    allows (base game 50, +11 per Ultimate Vault Hunter Upgrade Pack, 80 with the Commander Lilith DLC).
  - WillowPlayerController.GetMaxOverpowerLevel() -> int: the highest Overpower (OP) level available.
  - WillowPlayerReplicationInfo.NumOverpowerLevelsUnlocked: how many OP levels this character has unlocked.

Character level and gear level are different things. A character tops out at level 80. Overpower gear is
"level 80 + OP n": the game builds it at game stage / grade 80 + n, so OP8 gear is stored as gear level 88 and OP10
as 90. So the gear cap is the character cap plus the maximum OP level.

When the game can't be asked (not in a save, or the call fails) the fallbacks below are used and the result says
so (`source`). FALLBACK_MAX_OVERPOWER = 10 comes from the 2019 Commander Lilith update (OP8 before it); unverified.
"""

from __future__ import annotations

from typing import Any

MIN_LEVEL = 1
FALLBACK_CHARACTER_MAX = 80
FALLBACK_MAX_OVERPOWER = 10
# Sanity bounds for what the game reports; anything outside them is treated as a failed read.
_CHARACTER_SANE = range(1, 101)
_OVERPOWER_SANE = range(0, 21)


def _ask(pc: Any, function: str, sane: range) -> int | None:
    if pc is None:
        return None
    try:
        value = getattr(pc, function)()
        if isinstance(value, tuple):  # functions with out parameters return (result, *outs)
            value = value[0] if value else None
        value = int(value)
    except Exception:  # noqa: BLE001
        return None
    return value if value in sane else None


def character_level_cap(pc: Any) -> tuple[int, str]:
    """(highest character level, where that number came from)."""
    value = _ask(pc, "GetMaximumPossiblePlayerLevelCap", _CHARACTER_SANE)
    if value is not None:
        return value, "pc.GetMaximumPossiblePlayerLevelCap()"
    return FALLBACK_CHARACTER_MAX, "fallback (unverified)"


def overpower_cap(pc: Any) -> tuple[int, str]:
    """(highest Overpower level, where that number came from)."""
    value = _ask(pc, "GetMaxOverpowerLevel", _OVERPOWER_SANE)
    if value is not None:
        return value, "pc.GetMaxOverpowerLevel()"
    return FALLBACK_MAX_OVERPOWER, "fallback (unverified)"


def gear_level_cap(pc: Any) -> dict[str, Any]:
    """The gear level range: character cap + Overpower levels. JSON-ready, for the UI's level box.

    {"min": 1, "max": 90, "character_max": 80, "overpower_max": 10, "sources": {...}}
    """
    character, character_source = character_level_cap(pc)
    overpower, overpower_source = overpower_cap(pc)
    return {
        "min": MIN_LEVEL,
        "max": character + overpower,
        "character_max": character,
        "overpower_max": overpower,
        "sources": {"character_max": character_source, "overpower_max": overpower_source},
    }


def overpower_of(level: int, character_max: int) -> int:
    """How many OP levels a gear level stands for (0 for ordinary gear): 88 with an 80 cap is OP8."""
    return max(0, level - character_max)
