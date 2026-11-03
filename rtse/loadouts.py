"""Builds ("loadouts"): saves the current gear, skills and character as a named preset, previews what applying it would
change, and applies it through the editor's own write functions (gear_edit, character, skills), one step at a time.

Checked with fake game objects (dev/tests/test_loadouts.py): preset validation, the compact link codec (including a zip
bomb), the storage file, matching live gear to a preset, the plan (blocks, unchanged, missing), apply (readback, rollback
in reverse, journal outcome, item ids that change after a rebuild).

NOT verified in a live game: nothing here has been run against Borderlands 2. Every write goes through the same functions
as the editor pages, so each step carries the same unverified risks those pages have (README, "What is not verified in a
live game"). Skill writes ignore the tier lock, as rtse/skills.py does. Items that are missing from the save can't be
created yet: IMPORT_MISSING_HOOK is wired by the item-codes feature.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import time
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from unrealsdk import logging

from . import character, gear_edit, gear_inventory, gear_parts, journal, skills
from .gear_parts import ITEM_SLOTS, WEAPON_SLOTS
from .routing import ApiError, Params, Route

FORMAT = "rtse.loadout"
SCHEMA_VERSION = 1
STORE_FORMAT = "rtse.loadouts"
STORE_VERSION = 1
LINK_PREFIX = "RTSE-L1:"
MAX_PRESETS = 100
MAX_SKILLS = 200
MAX_GEAR = 120
MAX_STEPS = 300  # a whole run; it is applied in chunks (CHUNK_MAX_STEPS, CHUNK_SECONDS) so the game never stalls
CHUNK_MAX_STEPS = 20  # writes per apply request; a failed chunk's rollback is bounded by the same number
CHUNK_SECONDS = 2.0  # a chunk stops starting new steps after this long (measured inside the loop)
RUN_TTL_SECONDS = 15 * 60  # an unfinished run is forgotten after this
MAX_RUNS = 8
MAX_PRESET_BYTES = 256 * 1024
# The server's POST body limit is 64 KB (rtse/server.py MAX_BODY_BYTES), and an import arrives as a link inside a JSON
# body. A stored build must fit LINK_LIMIT as RTSE's own (level 9) link, so it can always be shared and imported again;
# an import may be up to IMPORT_LINK_LIMIT, the slack covering the browser's weaker compression and the JSON wrapper.
LINK_LIMIT = 56_000
IMPORT_LINK_LIMIT = 62_000
MAX_LEFT_OUT = 20
LEFT_OUT_LIMIT = 200
MAX_LEVEL = 100
NAME_LIMIT = 64
GEAR_NAME_LIMIT = 120
CREATED_LIMIT = 40

PRESETS_FILE = Path(__file__).parent / "loadouts.json"
DEBUG_FILE = Path(__file__).parent / "debug_loadouts.json"

PATH_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:]{0,255}")
CLASS_PATTERN = re.compile(r"Willow[A-Za-z]{1,40}")
CLASS_KEYS = ("assassin", "siren", "soldier", "mercenary", "mechromancer", "psycho")
SECTIONS = ("character", "skills", "gear")
SECTION_CHARACTER, SECTION_SKILLS, SECTION_GEAR = SECTIONS
GEAR_KINDS = ("weapon", "item")
LOCATIONS = (gear_inventory.EQUIPPED, gear_inventory.BACKPACK)
PRESET_KEYS = ("format", "version", "kind", "name", "created", "class_key", "left_out", "character", "skills", "gear")
GEAR_KEYS = ("kind", "class", "name", "location", "balance", "definition", "manufacturer", "level", "parts")
CHARACTER_KEYS = ("level", "xp", "skill_points", "money", "eridium", "seraph", "torgue")
CHARACTER_BOUNDS: dict[str, tuple[int, int]] = {
    "level": (character.MIN_LEVEL, MAX_LEVEL),
    "xp": (0, character.MAX_XP),
    "skill_points": (0, character.MAX_SKILL_POINTS),
    **{key: (0, character.MAX_CURRENCY) for key, _label, _index in character.CURRENCIES},
}
CHARACTER_LABELS: dict[str, str] = {
    "level": "Character level",
    "xp": "Experience",
    "skill_points": "Skill points",
    **{key: label for key, label, _index in character.CURRENCIES},
}
INCLUDE_KEYS = ("equipped", "backpack", "skills", "character", "currencies")
ON_FAILURE_MODES = ("rollback", "keep")
UNVERIFIED_NOTE = (
    "Applying presets has not been tested in a live game. Every step uses the same write functions as the editor pages."
)
GAME_ERRORS = (ApiError, character.CharacterError, skills.SkillsError)


class LoadoutError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@contextmanager
def _game_errors() -> Iterator[None]:
    """The game-side errors of the read and write functions, as the LoadoutError status the route answers with."""
    try:
        yield
    except GAME_ERRORS as ex:
        raise LoadoutError(ex.status, str(ex)) from None


# --- Validation of untrusted JSON --------------------------------------------------------------


def _dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LoadoutError(400, f"{what} must be an object")
    return value


def _unknown_keys(data: dict[str, Any], allowed: tuple[str, ...], what: str) -> None:
    for key in data:
        if key not in allowed:
            raise LoadoutError(400, f"unknown key {key!r} in {what}")


def _whole(value: Any, low: int, high: int, what: str) -> int:
    if type(value) is not int:  # bool is an int subclass, so `type is` is the check
        raise LoadoutError(400, f"{what} must be a whole number")
    if not low <= value <= high:
        raise LoadoutError(400, f"{what} must be {low}-{high}")
    return value


def _check_path(value: Any, what: str, *, nullable: bool = True) -> str | None:
    if value is None:
        if nullable:
            return None
        raise LoadoutError(400, f"{what} is required")
    if not isinstance(value, str) or not PATH_PATTERN.fullmatch(value):
        raise LoadoutError(400, f"{what} isn't a valid object path")
    return value


def _text(value: Any, what: str, limit: int, *, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise LoadoutError(400, f"{what} must be text")
    text = value.strip()
    if not minimum <= len(text) <= limit:
        raise LoadoutError(400, f"{what} must be {minimum}-{limit} characters")
    if not all(ch.isprintable() for ch in text):
        raise LoadoutError(400, f"{what} has control characters")
    return text


def _character(raw: Any) -> dict[str, int]:
    section = _dict(raw, "character")
    _unknown_keys(section, CHARACTER_KEYS, "character")
    return {key: _whole(value, *CHARACTER_BOUNDS[key], key) for key, value in section.items()}


def _skills(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise LoadoutError(400, "skills must be a list")
    if len(raw) > MAX_SKILLS:
        raise LoadoutError(400, f"too many skills ({len(raw)}; the limit is {MAX_SKILLS})")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        what = f"skill #{index + 1}"
        section = _dict(entry, what)
        _unknown_keys(section, ("path", "level"), what)
        path = _check_path(section.get("path"), f"{what} path", nullable=False)
        if path in seen:
            raise LoadoutError(400, f"skill {path} is listed twice")
        seen.add(path)
        level = _whole(section.get("level"), 0, skills.OVER_MAX_LIMIT, f"level of {path}")
        out.append({"path": path, "level": level})
    return out


def _parts(raw: Any, slots: tuple[str, ...], what: str) -> dict[str, str | None]:
    section = _dict(raw, f"{what} parts")
    _unknown_keys(section, slots, f"{what} parts")
    return {slot: _check_path(value, f"{what} part {slot}") for slot, value in section.items()}


def _gear_entry(raw: Any, index: int) -> dict[str, Any]:
    what = f"gear #{index + 1}"
    section = _dict(raw, what)
    _unknown_keys(section, GEAR_KEYS, what)
    kind = section.get("kind")
    if kind not in GEAR_KINDS:
        raise LoadoutError(400, f'{what}: kind must be "weapon" or "item"')
    gear_class = section.get("class")
    if not isinstance(gear_class, str) or not CLASS_PATTERN.fullmatch(gear_class):
        raise LoadoutError(400, f"{what}: class must be a Willow class name")
    location = section.get("location")
    if location not in LOCATIONS:
        raise LoadoutError(400, f'{what}: location must be "Equipped" or "Backpack"')
    entry: dict[str, Any] = {"kind": kind, "class": gear_class, "location": location}
    if "name" in section:
        entry["name"] = _text(section["name"], f"{what} name", GEAR_NAME_LIMIT, minimum=0)
    for key in ("balance", "definition", "manufacturer"):
        entry[key] = _check_path(section.get(key), f"{what} {key}")
    entry["level"] = _whole(section.get("level"), 1, MAX_LEVEL, f"{what} level")
    slots = WEAPON_SLOTS if kind == "weapon" else ITEM_SLOTS
    entry["parts"] = _parts(section.get("parts"), slots, what)
    return entry


def _gear(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise LoadoutError(400, "gear must be a list")
    if len(raw) > MAX_GEAR:
        raise LoadoutError(400, f"too many gear items ({len(raw)}; the limit is {MAX_GEAR})")
    return [_gear_entry(entry, index) for index, entry in enumerate(raw)]


def validate(data: Any) -> dict[str, Any]:
    """Checks a preset from outside (a file, a link, a request) and returns a normalized copy.

    Raises LoadoutError(400, why) for anything that isn't exactly the schema in the module's contract. The size check
    runs first, before anything is walked.
    """
    try:
        encoded = json.dumps(data, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        raise LoadoutError(400, "the preset isn't plain JSON data") from None
    size = len(encoded.encode("utf-8"))
    if size > MAX_PRESET_BYTES:
        raise LoadoutError(400, f"the preset is {size} bytes; the limit is {MAX_PRESET_BYTES}")
    data = _dict(data, "the preset")
    _unknown_keys(data, PRESET_KEYS, "the preset")
    if data.get("format") != FORMAT:
        raise LoadoutError(400, f"not a build preset (format must be {FORMAT!r})")
    version = data.get("version")
    if type(version) is not int:
        raise LoadoutError(400, "version must be a whole number")
    if version != SCHEMA_VERSION:
        raise LoadoutError(400, f"unsupported preset version {version}")
    kind = data.get("kind")
    if kind not in ("loadout", "character"):
        raise LoadoutError(400, 'kind must be "loadout" or "character"')
    if "class_key" not in data:
        raise LoadoutError(400, "class_key is required (null when unknown)")
    class_key = data["class_key"]
    if class_key is not None and class_key not in CLASS_KEYS:
        raise LoadoutError(400, f"class_key must be null or one of {', '.join(CLASS_KEYS)}")

    out: dict[str, Any] = {
        "format": FORMAT,
        "version": SCHEMA_VERSION,
        "kind": kind,
        "name": _text(data.get("name"), "name", NAME_LIMIT),
    }
    if "created" in data:
        out["created"] = _text(data["created"], "created", CREATED_LIMIT, minimum=0)
    out["class_key"] = class_key
    if "left_out" in data:
        out["left_out"] = _left_out(data["left_out"])
    if "character" in data:
        out["character"] = _character(data["character"])
    if "skills" in data:
        out["skills"] = _skills(data["skills"])
    if "gear" in data:
        out["gear"] = _gear(data["gear"])
    length = len(to_link(out))
    if length > LINK_LIMIT:
        raise LoadoutError(400, f"this build is too big to share: its link would be {length} characters and the limit is "
                                f"{LINK_LIMIT} - save fewer items")
    return out


def _left_out(raw: Any) -> list[str]:
    """What a capture couldn't read (a partial build). Shown to the user, never acted on."""
    if not isinstance(raw, list) or len(raw) > MAX_LEFT_OUT:
        raise LoadoutError(400, f"left_out must be a list of at most {MAX_LEFT_OUT} notes")
    return [_text(note, "a left_out note", LEFT_OUT_LIMIT) for note in raw]


# --- Compact link ------------------------------------------------------------------------------


def to_link(preset: dict[str, Any]) -> str:
    raw = json.dumps(preset, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return LINK_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")


def from_link(text: Any) -> dict[str, Any]:
    """Decodes a pasted link. The browser's CompressionStream output is zlib format, so any valid zlib stream is
    accepted, not only level 9. Decompression stops at the size limit, so a zip bomb can't use up memory."""
    if not isinstance(text, str):
        raise LoadoutError(400, "link must be text")
    compact = "".join(text.split())
    if len(compact) > IMPORT_LINK_LIMIT:
        raise LoadoutError(400, f"that build link is {len(compact)} characters; the limit is {IMPORT_LINK_LIMIT}")
    if not compact.startswith(LINK_PREFIX):
        raise LoadoutError(400, f"not a build link (it starts with {LINK_PREFIX})")
    body = compact[len(LINK_PREFIX) :]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", body) or len(body) % 4 == 1:
        raise LoadoutError(400, "the build link is damaged (bad characters)")
    try:
        packed = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError):
        raise LoadoutError(400, "the build link is damaged (bad base64)") from None
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(packed, MAX_PRESET_BYTES + 1)
    except zlib.error:
        raise LoadoutError(400, "the build link is damaged (not compressed data)") from None
    if len(raw) > MAX_PRESET_BYTES or inflater.unconsumed_tail:
        raise LoadoutError(400, f"the build inside the link is larger than {MAX_PRESET_BYTES} bytes")
    if not inflater.eof or inflater.unused_data:
        raise LoadoutError(400, "the build link is incomplete or has extra data")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise LoadoutError(400, "the build inside the link isn't valid JSON") from None
    return validate(data)


# --- Storage -----------------------------------------------------------------------------------


def _quarantine() -> str:
    """Copies an unreadable builds file aside, never over an earlier copy. Returns the copy's name."""
    target = PRESETS_FILE.with_name("loadouts.bad.json")
    number = 1
    while target.exists():
        target = PRESETS_FILE.with_name(f"loadouts.bad.{number}.json")
        number += 1
    shutil.copyfile(PRESETS_FILE, target)
    return target.name


def _load() -> tuple[list[dict[str, Any]], list[str]]:
    """Every stored build that still validates, and a warning for each one skipped. An unreadable file is copied aside
    first and treated as empty."""
    if not PRESETS_FILE.exists():
        return [], []
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("format") != STORE_FORMAT
            or type(data.get("version")) is not int
            or data["version"] != STORE_VERSION
            or not isinstance(data.get("presets"), list)
        ):
            raise ValueError("it isn't a builds file this version reads")
    except (OSError, ValueError, RecursionError) as ex:
        copy = _quarantine()
        return [], [f"{PRESETS_FILE.name} couldn't be read ({ex}); the original was copied to {copy}, the list is empty"]

    presets: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(data["presets"]):
        try:
            preset = validate(raw)
        except LoadoutError as ex:
            warnings.append(f"skipped build #{index + 1}: {ex}")
            continue
        key = preset["name"].casefold()
        if key in seen:
            warnings.append(f"skipped build {preset['name']!r}: its name is already used")
            continue
        seen.add(key)
        presets.append(preset)
    return presets, warnings


def _write_store(presets: list[dict[str, Any]]) -> None:
    """Writes the whole file to a temporary name, flushes it to disk, then swaps it in (atomic on the same volume)."""
    text = json.dumps({"format": STORE_FORMAT, "version": STORE_VERSION, "presets": presets}, indent=2, ensure_ascii=False)
    temporary = PRESETS_FILE.with_name(PRESETS_FILE.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, PRESETS_FILE)
    except OSError as ex:
        raise LoadoutError(500, f"couldn't write the builds file: {ex}") from None


def _index_of(presets: list[dict[str, Any]], name: str) -> int | None:
    key = name.casefold()
    for index, preset in enumerate(presets):
        if preset["name"].casefold() == key:
            return index
    return None


def _name_argument(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise LoadoutError(400, "name is required")
    return raw.strip()


def _require_index(presets: list[dict[str, Any]], raw_name: Any) -> int:
    index = _index_of(presets, _name_argument(raw_name))
    if index is None:
        raise LoadoutError(404, f"no build named {raw_name!r}")
    return index


def _find_stored(raw_name: Any) -> dict[str, Any]:
    presets, _warnings = _load()
    return presets[_require_index(presets, raw_name)]


def _summary(preset: dict[str, Any]) -> dict[str, Any]:
    gear = preset.get("gear", [])
    return {
        "name": preset["name"],
        "kind": preset["kind"],
        "class_key": preset["class_key"],
        "created": preset.get("created"),
        "partial": bool(preset.get("left_out")),
        "left_out": list(preset.get("left_out", [])),
        "counts": {
            "gear": len(gear),
            "equipped": sum(1 for g in gear if g["location"] == gear_inventory.EQUIPPED),
            "backpack": sum(1 for g in gear if g["location"] == gear_inventory.BACKPACK),
            "skills": len(preset.get("skills", [])),
            "character": len(preset.get("character", {})),
        },
    }


def _store(preset: dict[str, Any], *, overwrite: bool, warnings: list[str] | None = None) -> dict[str, Any]:
    presets, load_warnings = _load()
    index = _index_of(presets, preset["name"])
    if index is not None and not overwrite:
        raise LoadoutError(409, f'a build named "{preset["name"]}" already exists - overwrite it or pick another name')
    if index is None:
        if len(presets) >= MAX_PRESETS:
            raise LoadoutError(400, f"you already have {MAX_PRESETS} builds - delete one first")
        presets.append(preset)
    else:
        presets[index] = preset
    _write_store(presets)
    logging.info(f"RTSE: saved build {preset['name']!r}")
    return {
        "saved": True,
        "preset": _summary(preset),
        "presets": [_summary(p) for p in presets],
        "warnings": [*load_warnings, *(warnings or [])],
    }


# --- Reading the current build -----------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _field(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)
    except Exception:  # noqa: BLE001 - a field this game build lacks reads as None
        return None


def _path_of(obj: Any) -> str | None:
    if obj is None:
        return None
    try:
        return obj._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None


def _item_name(item: Any) -> str:
    try:
        text = str(item.GetHumanReadableName() or "")
    except Exception:  # noqa: BLE001
        text = ""
    return "".join(ch for ch in text if ch.isprintable()).strip()[:GEAR_NAME_LIMIT]


def _live_item(item: Any, location: str) -> dict[str, Any]:
    """One inventory item as plain data (its game object is kept under "raw" for the parts lookups)."""
    data = _field(item, "DefinitionData")
    weapon = gear_parts._is_weapon(item)  # noqa: SLF001
    partless = gear_parts._is_partless(item)  # noqa: SLF001
    suffix = "PartDefinition" if weapon else "ItemPartDefinition"
    slots = WEAPON_SLOTS if weapon else ITEM_SLOTS
    return {
        "id": gear_inventory._item_id(item),  # noqa: SLF001
        "raw": item,
        "kind": "weapon" if weapon else "item",
        "class": item.Class.Name,
        "name": _item_name(item),
        "location": location,
        "balance": _path_of(_field(data, "BalanceDefinition")),
        "definition": _path_of(_field(data, "WeaponTypeDefinition" if weapon else "ItemDefinition")),
        "manufacturer": _path_of(_field(data, "ManufacturerDefinition")),
        "level": _field(item, "ExpLevel"),
        "parts": {} if partless else {slot: _path_of(_field(data, f"{slot}{suffix}")) for slot in slots},
    }


def _live_gear() -> list[dict[str, Any]]:
    found, _notes = gear_inventory._inventory(gear_inventory._manager())  # noqa: SLF001
    return [_live_item(item, location) for item, location in found]


def _gear_entry_of(live: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": live["kind"],
        "class": live["class"],
        "location": live["location"],
        "balance": live["balance"],
        "definition": live["definition"],
        "manufacturer": live["manufacturer"],
        "level": live["level"],
        "parts": dict(live["parts"]),
    }
    if live["name"]:
        entry["name"] = live["name"]
    return entry


def _capture_gear(*, equipped: bool, backpack: bool, warnings: list[str]) -> list[dict[str, Any]]:
    found, notes = gear_inventory._inventory(gear_inventory._manager())  # noqa: SLF001
    warnings.extend(notes)
    gear = []
    for item, location in found:
        if (location == gear_inventory.EQUIPPED and not equipped) or (location == gear_inventory.BACKPACK and not backpack):
            continue
        gear.append(_gear_entry_of(_live_item(item, location)))
    return gear


def _capture_skills() -> list[dict[str, Any]]:
    state = skills.read()
    if not state.get("available"):
        raise LoadoutError(409, f"the skill tree can't be read: {state.get('reason') or 'unknown reason'}")
    out = []
    for skill in state["skills"]:
        if skill.get("action") or skill.get("hidden") or skill.get("path") is None or skill.get("level") is None:
            continue
        out.append({"path": skill["path"], "level": skill["level"]})
    return out


def _capture_character(state: dict[str, Any], keys: list[str], warnings: list[str]) -> dict[str, int]:
    values: dict[str, Any] = {"level": state.get("level"), "xp": state.get("xp"), "skill_points": state.get("skill_points")}
    values.update({c["key"]: c["value"] for c in state.get("currencies", [])})
    out: dict[str, int] = {}
    for key in keys:
        if values.get(key) is None:
            warnings.append(f"{CHARACTER_LABELS[key]} couldn't be read, so it's left out")
        else:
            out[key] = values[key]
    return out


def _default_name(class_key: str | None, level: Any) -> str:
    label = (class_key or "character").capitalize()
    day = _now()[:10]
    return f"{label} L{level} {day}" if level is not None else f"{label} {day}"


def capture(name: str | None, include: dict[str, bool], *, whole: bool = False) -> tuple[dict[str, Any], list[str]]:
    """The current build as a validated preset, not saved. `whole` exports the whole character: every section, every
    character key (xp included) and all gear. `name` None gives a name from the class, level and date."""
    flags = {key: True for key in INCLUDE_KEYS} if whole else include
    warnings: list[str] = []
    with _game_errors():
        char_state = character.read()
        class_key = char_state.get("class_key")
        sections: dict[str, Any] = {}
        if whole:
            keys = list(CHARACTER_KEYS)
        else:
            keys = (["level", "skill_points"] if flags.get("character") else []) + (
                ["money", "eridium", "seraph", "torgue"] if flags.get("currencies") else []
            )
        if keys:
            sections["character"] = _capture_character(char_state, keys, warnings)
        if flags.get("skills") or whole:
            try:
                sections["skills"] = _capture_skills()
            except LoadoutError as ex:
                if not whole:
                    raise
                warnings.append(f"skills left out: {ex}")
        if flags.get("equipped") or flags.get("backpack") or whole:
            sections["gear"] = _capture_gear(
                equipped=bool(flags.get("equipped") or whole),
                backpack=bool(flags.get("backpack") or whole),
                warnings=warnings,
            )
    if not sections:
        raise LoadoutError(400, "tick something to save (gear, skills or character)")
    preset = {
        "format": FORMAT,
        "version": SCHEMA_VERSION,
        "kind": "character" if whole else "loadout",
        "name": name or _default_name(class_key, char_state.get("level")),
        "created": _now(),
        "class_key": class_key,
        **sections,
    }
    if warnings:  # a partial capture says so inside the preset itself, so the file and the list show it
        preset["left_out"] = [note[:LEFT_OUT_LIMIT] for note in warnings[:MAX_LEFT_OUT]]
    return validate(preset), warnings


# --- Matching and planning ---------------------------------------------------------------------


def _is_candidate(entry: dict[str, Any], item: dict[str, Any]) -> bool:
    if item["class"] != entry["class"]:
        return False
    if entry["balance"] is not None:
        return item["balance"] == entry["balance"]
    return item["definition"] == entry["definition"]


def _is_exact(entry: dict[str, Any], item: dict[str, Any]) -> bool:
    return item["level"] == entry["level"] and all(item["parts"].get(s) == p for s, p in entry["parts"].items())


def _score(entry: dict[str, Any], item: dict[str, Any]) -> int:
    parts = sum(1 for s, p in entry["parts"].items() if item["parts"].get(s) == p)
    return parts + (1 if item["level"] == entry["level"] else 0)


def _match(gear: list[dict[str, Any]], live: list[dict[str, Any]], skip: set[int]) -> dict[int, int]:
    """Which live item each preset entry takes (entry index -> live index). Each live item is used once."""
    assigned: dict[int, int] = {}
    used: set[int] = set()
    for index, entry in enumerate(gear):  # pass 1: exact matches are unchanged, same location first
        if index in skip:
            continue
        exact = [j for j, item in enumerate(live) if j not in used and _is_candidate(entry, item) and _is_exact(entry, item)]
        if exact:
            best = min(exact, key=lambda j: (live[j]["location"] != entry["location"], j))
            assigned[index] = best
            used.add(best)
    for index, entry in enumerate(gear):  # pass 2: the best remaining candidate, tie -> same location, then inventory order
        if index in skip or index in assigned:
            continue
        options = [j for j, item in enumerate(live) if j not in used and _is_candidate(entry, item)]
        if options:
            best = max(options, key=lambda j: (_score(entry, live[j]), live[j]["location"] == entry["location"], -j))
            assigned[index] = best
            used.add(best)
    return assigned


def _offered(item: dict[str, Any], mode: str, cache: dict[tuple[str, str], dict[str, set[str]]]) -> dict[str, set[str]]:
    """The part ids each slot offers in one mode (cached per item and mode)."""
    key = (item["id"], mode)
    if key not in cache:
        cache[key] = {slot.name: {ref.id for ref in slot.options} for slot in gear_parts._slots(item["raw"], mode)}  # noqa: SLF001
    return cache[key]


def _mode_for(item: dict[str, Any], slot: str, want: str | None, cache: dict[tuple[str, str], dict[str, set[str]]]) -> str | None:
    for mode in gear_parts.MODES:
        offered = _offered(item, mode, cache)
        if slot in offered and (want is None or want in offered[slot]):  # clearing needs only the slot
            return mode
    return None


def _entry_label(entry: dict[str, Any]) -> str:
    return entry.get("name") or entry["class"]


class _Draft:
    """The plan being built: steps to run, entries that already match, blocked notes and missing gear."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.unchanged: list[dict[str, Any]] = []
        self.blocked: list[dict[str, Any]] = []
        self.missing: list[dict[str, Any]] = []

    def step(self, section: str, kind: str, label: str, target: Any, before: Any, after: Any, params: dict[str, Any],
             entry: int | None = None, side_effect: str | None = None) -> None:
        self.steps.append({"section": section, "kind": kind, "label": label, "target": target, "before": before,
                           "after": after, "params": params, "entry": entry, "side_effect": side_effect})

    def keep(self, section: str, label: str, reason: str) -> None:
        self.unchanged.append({"section": section, "label": label, "reason": reason})

    def block(self, section: str, label: str, reason: str, kind: str) -> None:
        self.blocked.append({"section": section, "label": label, "reason": reason, "kind": kind})


def _plan_character(draft: _Draft, wanted: dict[str, int], state: dict[str, Any]) -> None:
    cap = state["limits"]["level"][1]
    now_values: dict[str, Any] = {"level": state.get("level"), "xp": state.get("xp"),
                                  "skill_points": state.get("skill_points")}
    now_values.update({c["key"]: c["value"] for c in state.get("currencies", [])})
    level_changes = False
    for key in CHARACTER_KEYS:
        if key not in wanted:
            continue
        value, now, label = wanted[key], now_values.get(key), CHARACTER_LABELS[key]
        if key == "level" and value > cap:
            draft.block(SECTION_CHARACTER, label, f"level {value} is above this game's cap of {cap}", "limit")
            continue
        if now is None:
            draft.block(SECTION_CHARACTER, label, "the current value can't be read, so RTSE won't guess", "unreadable")
            continue
        # Writing a level rewrites XP, so a planned level change always carries the captured XP after it.
        if now == value and not (key == "xp" and level_changes):
            draft.keep(SECTION_CHARACTER, label, f"already {value}")
            continue
        side_effect = _level_side_effect(value, now_values.get("xp"), "xp" in wanted) if key == "level" else None
        draft.step(SECTION_CHARACTER, f"character.{key}", label, key, now, value, {"field": key, "value": value},
                   side_effect=side_effect)
        level_changes = level_changes or key == "level"


def _xp_threshold(level: int) -> int | None:
    """The XP the level write will set (character.set_field moves level and XP together, by design)."""
    try:
        return character._xp_required(character._owners(), level)[0]  # noqa: SLF001
    except Exception:  # noqa: BLE001 - only the preview's wording depends on it
        return None


def _level_side_effect(level: int, xp_now: Any, build_sets_xp: bool) -> str:
    xp = _xp_threshold(level)
    amount = f"{xp:,}" if isinstance(xp, int) else "the game's amount"
    now = f" (now {xp_now:,})" if isinstance(xp_now, int) else ""
    text = f"Experience also changes{now}: it becomes {amount}, the minimum for level {level}"
    return f"{text}; the next step then sets the build's XP" if build_sets_xp else text


def _plan_skills(draft: _Draft, wanted: list[dict[str, Any]], over_max: bool, preset_class: str | None,
                 current_class: str | None) -> None:
    state = skills.read()
    if not state.get("available"):
        draft.block(SECTION_SKILLS, "Skills", state.get("reason") or "the skill tree can't be read", "unavailable")
        return
    if preset_class and current_class and preset_class != current_class:
        draft.block(SECTION_SKILLS, "Skills", f"this build is for {preset_class}, and you are {current_class}", "class")
        return
    tree = {s["path"]: s for s in state["skills"] if s.get("path")}
    for skill in wanted:
        path, level = skill["path"], skill["level"]
        label = path.rpartition(".")[2] or path
        now_skill = tree.get(path)
        if now_skill is None:
            draft.block(SECTION_SKILLS, label, "this skill isn't in your current tree", "missing")
            continue
        now = now_skill["level"]
        if now is None:
            draft.block(SECTION_SKILLS, label, "its points can't be read", "unreadable")
            continue
        top = now_skill["max"] if now_skill["max"] is not None else skills.MAX_GRADE_FALLBACK
        if level > top and not over_max:
            draft.block(SECTION_SKILLS, label, f"{level} is above its maximum {top}; tick Allow above max", "max")
            continue
        if level == now:
            draft.keep(SECTION_SKILLS, label, f"already {level}")
            continue
        draft.step(SECTION_SKILLS, "skills.level", label, path, now, level,
                   {"id": path, "level": level, "over_max": over_max})


def _missing(index: int, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry": index,
        "name": _entry_label(entry),
        "kind": entry["kind"],
        "class": entry["class"],
        "location": entry["location"],
        "balance": entry["balance"],
        "level": entry["level"],
        "can_import": IMPORT_MISSING_HOOK is not None,
    }


def _plan_assigned(draft: _Draft, index: int, entry: dict[str, Any], item: dict[str, Any], limits: dict[str, Any],
                   cache: dict[tuple[str, str], dict[str, set[str]]]) -> None:
    label = f"{item['name'] or item['class']} ({item['location']})"
    steps_before, blocked_before = len(draft.steps), len(draft.blocked)
    if item["location"] != entry["location"]:
        draft.block(SECTION_GEAR, label,
                    f"it's in your {item['location']} but the build wants it {entry['location']}; RTSE can't move "
                    "items, so equip or unequip it yourself (its parts and level are still planned)", "location")
    slots = WEAPON_SLOTS if item["kind"] == "weapon" else ITEM_SLOTS
    for slot in slots:
        if slot not in entry["parts"]:
            continue
        want, now = entry["parts"][slot], item["parts"].get(slot)
        if now == want:
            continue
        mode = _mode_for(item, slot, want, cache)
        if mode is None:
            draft.block(SECTION_GEAR, f"{label}: {slot}", "that part isn't loaded in the game right now", "part")
            continue
        draft.step(SECTION_GEAR, "gear.part", f"{label}: {slot}", item["id"], now, want,
                   {"id": item["id"], "slot": slot, "part": want, "mode": mode}, entry=index)
    if item["level"] != entry["level"]:
        low, high = limits["min"], limits["max"]
        if low <= entry["level"] <= high:
            draft.step(SECTION_GEAR, "gear.level", f"{label}: level", item["id"], item["level"], entry["level"],
                       {"id": item["id"], "level": entry["level"]}, entry=index)
        else:
            draft.block(SECTION_GEAR, f"{label}: level",
                        f"level {entry['level']} is outside this game's gear range {low}-{high}", "level")
    new_blocks = [b for b in draft.blocked[blocked_before:] if b["kind"] != "location"]
    if len(draft.steps) == steps_before and not new_blocks:
        draft.keep(SECTION_GEAR, label, "already matches")


def _plan_gear(draft: _Draft, gear: list[dict[str, Any]], skipped: list[int]) -> None:
    live = _live_gear()
    limits = gear_edit._gear_limits()  # noqa: SLF001
    skip = set(skipped)
    assigned = _match(gear, live, skip)
    cache: dict[tuple[str, str], dict[str, set[str]]] = {}
    for index, entry in enumerate(gear):
        if index in skip:
            draft.keep(SECTION_GEAR, _entry_label(entry), "skipped by you")
        elif index not in assigned:
            draft.missing.append(_missing(index, entry))
        else:
            _plan_assigned(draft, index, entry, live[assigned[index]], limits, cache)


def _chosen_sections(preset: dict[str, Any], sections: list[str] | None) -> list[str]:
    present = [s for s in SECTIONS if s in preset]
    if sections is None:
        return present
    return [s for s in present if s in sections]


def _class_match(preset_class: str | None, current_class: str | None) -> bool | None:
    if preset_class is None or current_class is None:
        return None
    return preset_class == current_class


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _basis(sections: list[str]) -> str:
    """A fingerprint of everything an apply can touch, read from the game now: every character value (XP included,
    which a level write moves), every skill's level, every item's id, place, level and parts. A plan id includes it,
    and each apply chunk compares it with the one recorded when the previous chunk ended."""
    state: dict[str, Any] = {}
    if SECTION_CHARACTER in sections:
        c = character.read()
        state["character"] = [c.get("level"), c.get("xp"), c.get("skill_points"),
                              [[x["key"], x["value"]] for x in c.get("currencies", [])]]
    if SECTION_SKILLS in sections:
        tree = skills.read()
        if tree.get("available"):
            state["skills"] = sorted([s.get("path") or "", s.get("level")] for s in tree.get("skills", []))
        else:
            state["skills"] = tree.get("reason")
    if SECTION_GEAR in sections:
        state["gear"] = sorted([g["id"], g["class"], g["balance"], g["location"], g["level"], sorted(g["parts"].items())]
                               for g in _live_gear())
    return _digest(state)


def _plan_id(steps: list[dict[str, Any]], blocked: list[dict[str, Any]], missing: list[dict[str, Any]],
             sections: list[str], over_max: bool, skip: list[int], name: str, basis: str) -> str:
    return _digest([steps, blocked, missing, sections, over_max, skip, name, basis])


def plan(preset: dict[str, Any], *, sections: list[str] | None = None, over_max: bool = False,
         skip: list[int] | tuple[int, ...] = ()) -> dict[str, Any]:
    """What applying the preset would do to the save right now. Reads only; nothing is written.

    `sections` None means every section the preset has. `skip` lists gear entry indexes the user unticked.
    """
    chosen = _chosen_sections(preset, sections)
    skipped = sorted(set(skip))
    gear = preset.get("gear", [])
    if any(index >= len(gear) for index in skipped):
        raise LoadoutError(400, "skip names a gear entry the build doesn't have")
    draft = _Draft()
    with _game_errors():
        state = character.read()
        current_class = state.get("class_key")
        preset_class = preset.get("class_key")
        if SECTION_CHARACTER in chosen:
            _plan_character(draft, preset["character"], state)
        if SECTION_SKILLS in chosen:
            _plan_skills(draft, preset["skills"], over_max, preset_class, current_class)
        if SECTION_GEAR in chosen:
            _plan_gear(draft, gear, skipped)
        basis = _basis(chosen)
    if len(draft.steps) > MAX_STEPS:
        raise LoadoutError(400, f"this build would take {len(draft.steps)} steps; the limit is {MAX_STEPS} - split it up")
    steps = [{"n": n, **step} for n, step in enumerate(draft.steps, start=1)]
    return {
        "plan_id": _plan_id(steps, draft.blocked, draft.missing, chosen, over_max, skipped, preset["name"], basis),
        "preset": {"name": preset["name"], "kind": preset["kind"], "class_key": preset_class},
        "class": {"preset": preset_class, "current": current_class, "match": _class_match(preset_class, current_class)},
        "sections": chosen,
        "steps": steps,
        "unchanged": draft.unchanged,
        "missing": draft.missing,
        "blocked": draft.blocked,
        "summary": {"changes": len(steps), "unchanged": len(draft.unchanged), "missing": len(draft.missing),
                    "blocked": len(draft.blocked)},
        "unverified": UNVERIFIED_NOTE,
    }


# --- Apply -------------------------------------------------------------------------------------

_last_report: dict[str, Any] | None = None


class _StepFailed(Exception):
    """A step whose readback doesn't match what was asked, or an unknown step."""


class _Undone(Exception):
    """Raised inside the journal transaction after a rollback, so the journal drops the whole user action."""


def _expect(got: Any, want: Any) -> Any:
    if got != want:
        raise _StepFailed(f"the game reads {got!r}, not {want!r}")
    return got


def _current_id(ids: dict[str, str], plan_id: str) -> str:
    return ids.get(plan_id, plan_id)


def _slot_part(detail: dict[str, Any], slot: str) -> str | None:
    for entry in detail.get("slots", []):
        if entry.get("slot") == slot:
            current = entry.get("current")
            return None if current is None else current.get("id")
    raise _StepFailed(f"the {slot} slot is missing from the item after the change")


def _current_xp() -> int | None:
    try:
        return character.read()["xp"]
    except Exception:  # noqa: BLE001 - without a reading, rollback just can't restore XP separately
        return None


def _skill_is_over(path: str, before: int) -> bool:
    """Whether restoring `before` needs the over-max opt-in: true when the skill's maximum is below it (or unknown)."""
    for skill in skills.read()["skills"]:
        if skill.get("path") == path:
            top = skill["max"] if skill["max"] is not None else skills.MAX_GRADE_FALLBACK
            return before > top
    return True


def _forward(step: dict[str, Any], ids: dict[str, str], context: dict[int, int | None]) -> Any:
    """Runs one step through its write function. Returns the value read back; raises when it failed."""
    kind, params = step["kind"], step["params"]
    if kind.startswith("character."):
        if kind == "character.level":
            context[step["n"]] = _current_xp()  # the level write rewrites XP: remember it for the rollback
        applied = character.set_field(dict(params))["applied"]
        return _expect(applied["after"], params["value"])
    if kind == "skills.level":
        applied = skills.set_level(dict(params))["applied"]
        return _expect(applied["after"], params["level"])
    if kind == "gear.part":
        detail = gear_edit.set_part({"id": _current_id(ids, params["id"]), "slot": params["slot"],
                                     "part": params["part"], "mode": params["mode"]})
        ids[params["id"]] = detail["id"]
        return _expect(_slot_part(detail, params["slot"]), params["part"])
    if kind == "gear.level":
        detail = gear_edit.set_level({"id": _current_id(ids, params["id"]), "level": params["level"]})
        ids[params["id"]] = detail["id"]
        return _expect(detail["level"], params["level"])
    raise _StepFailed(f"unknown step kind {kind!r}")


def _undo(step: dict[str, Any], ids: dict[str, str], context: dict[int, int | None]) -> None:
    """Puts a step's `before` back through the same write function. Raises when that doesn't stick."""
    kind, params, before = step["kind"], step["params"], step["before"]
    if kind.startswith("character."):
        _expect(character.set_field({"field": params["field"], "value": before})["applied"]["after"], before)
        xp = context.get(step["n"])
        if kind == "character.level" and xp is not None:
            _expect(character.set_field({"field": "xp", "value": xp})["applied"]["after"], xp)
        return
    if kind == "skills.level":
        over = _skill_is_over(params["id"], before)
        _expect(skills.set_level({"id": params["id"], "level": before, "over_max": over})["applied"]["after"], before)
        return
    if kind == "gear.part":
        item_id = _current_id(ids, params["id"])
        raw, location = gear_inventory._find_item({"id": item_id})  # noqa: SLF001
        mode = _mode_for(_live_item(raw, location), params["slot"], before, {})
        if mode is None:
            raise _StepFailed("the old part isn't offered in any mode now")
        detail = gear_edit.set_part({"id": item_id, "slot": params["slot"], "part": before, "mode": mode})
        ids[params["id"]] = detail["id"]
        _expect(_slot_part(detail, params["slot"]), before)
        return
    if kind == "gear.level":
        detail = gear_edit.set_level({"id": _current_id(ids, params["id"]), "level": before})
        ids[params["id"]] = detail["id"]
        _expect(detail["level"], before)
        return
    raise _StepFailed(f"unknown step kind {kind!r}")


def _result(step: dict[str, Any]) -> dict[str, Any]:
    return {"n": step["n"], "label": step["label"], "status": "pending", "before": step["before"], "after": None,
            "error": None}


def _read_target(step: dict[str, Any], ids: dict[str, str]) -> Any:
    """What the game holds now for a step's target (the same reads the plan used). Raises when it can't be read."""
    kind, params = step["kind"], step["params"]
    if kind.startswith("character."):
        state = character.read()
        field = params["field"]
        if field in state:
            return state[field]
        return next((c["value"] for c in state.get("currencies", []) if c["key"] == field), None)
    if kind == "skills.level":
        return next((s["level"] for s in skills.read()["skills"] if s.get("path") == params["id"]), None)
    raw, location = gear_inventory._find_item({"id": _current_id(ids, params["id"])})  # noqa: SLF001
    live = _live_item(raw, location)
    return live["parts"].get(params["slot"]) if kind == "gear.part" else live["level"]


def _left_behind(step: dict[str, Any], ids: dict[str, str], context: dict[int, int | None]) -> str | None:
    """None when the step's target verifiably still holds its `before` value, else what is different (or why it
    couldn't be checked). A level write also moves XP, so for a level step XP must be back too."""
    try:
        now = _read_target(step, ids)
    except Exception as ex:  # noqa: BLE001 - unreadable means "can't tell", never "unchanged"
        return f"it can't be read back ({str(ex) or ex.__class__.__name__})"
    if now != step["before"]:
        return f"the game reads {now!r}, not the old {step['before']!r}"
    if step["kind"] == "character.level":
        xp = context.get(step["n"])
        if xp is not None and _current_xp() != xp:
            return f"experience is no longer {xp}"
    return None


def _settle_failed(step: dict[str, Any], result: dict[str, Any], ids: dict[str, str],
                   context: dict[int, int | None]) -> None:
    """A write step that raised or read back wrong may still have changed the game (a setter can write and then
    raise). Whatever on_failure says, the failed step itself must leave nothing behind: read its target back and, if
    it differs from before, put `before` back through the same write function and check again. If that can't be
    verified, the step is reported rollback_failed."""
    changed = _left_behind(step, ids, context)
    if changed is None:
        return
    try:
        _undo(step, ids, context)
        still = _left_behind(step, ids, context)
        if still is not None:
            raise _StepFailed(still)
    except Exception as ex:  # noqa: BLE001 - reported below
        logging.error(f"RTSE: build step {step['n']} left a change behind and it couldn't be put back: {ex}")
        result.update(status="rollback_failed",
                      error=f"{result['error']}; it left a change behind ({changed}) and putting it back failed "
                            f"({str(ex) or ex.__class__.__name__}) - reload the save before playing")
    else:
        result["error"] = f"{result['error']} (it had changed the game - {changed} - and that was put back)"


def _undo_chunk(results: list[dict[str, Any]], steps: list[dict[str, Any]], indexes: range, ids: dict[str, str],
                context: dict[int, int | None]) -> None:
    """Undoes, last first, every step of one chunk that went ok. Each undo uses the same write function as the step
    and reads back; a failed undo is reported and the rest still run. (The failed step was settled already.)"""
    for index in reversed(indexes):
        result = results[index]
        if result["status"] != "ok":
            continue
        try:
            _undo(steps[index], ids, context)
        except Exception as ex:  # noqa: BLE001 - every failed undo is reported, the rest still run
            message = str(ex) or ex.__class__.__name__
            logging.error(f"RTSE: could not undo build step {steps[index]['n']}: {message}")
            result.update(status="rollback_failed", error=f"{message} - reload the save before playing")
        else:
            result["status"] = "rolled_back"


def _counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(("ok", "failed", "rolled_back", "rollback_failed", "not_run", "pending"), 0)
    for result in results:
        counts[result["status"]] += 1
    return counts


class _Run:
    """One apply of one plan, run a chunk per request. Lives in memory on the game thread (routes run there)."""

    def __init__(self, preset: dict[str, Any], fresh: dict[str, Any], on_failure: str) -> None:
        self.run_id = os.urandom(12).hex()
        self.plan_id: str = fresh["plan_id"]
        self.name = preset["name"]
        self.on_failure = on_failure
        self.sections: list[str] = fresh["sections"]
        self.steps: list[dict[str, Any]] = fresh["steps"]
        self.results = [_result(step) for step in self.steps]
        self.missing = fresh["missing"]
        self.blocked = fresh["blocked"]
        self.cursor = 0
        self.chunk = 0
        self.chunks: list[dict[str, Any]] = []
        self.ids: dict[str, str] = {}
        self.context: dict[int, int | None] = {}
        self.state = "running"
        self.basis: str | None = None  # the game state when the previous chunk ended (always a real reading)
        self.message = f"Ready to apply {len(self.steps)} steps."
        self.touched = _clock()


_runs: dict[str, _Run] = {}
_clock = time.monotonic  # tests replace it to simulate slow steps


def _forget_expired_runs() -> None:
    """Drops runs nobody has touched for RUN_TTL_SECONDS (finished, or abandoned mid-way by a closed page)."""
    now = _clock()
    for run_id in [r for r, run in _runs.items() if now - run.touched > RUN_TTL_SECONDS]:
        del _runs[run_id]


def _make_room() -> None:
    """Frees a slot for a new run. Only finished runs are ever dropped early; a run still in progress is never evicted,
    so with MAX_RUNS of those the new run is refused."""
    _forget_expired_runs()
    while len(_runs) >= MAX_RUNS:
        finished = [run for run in _runs.values() if run.state != "running"]
        if not finished:
            raise LoadoutError(409, f"{len(_runs)} builds are still being applied - let one finish, or wait "
                                    f"{RUN_TTL_SECONDS // 60} minutes for an abandoned one to expire")
        del _runs[min(finished, key=lambda run: run.touched).run_id]


def _find_run(raw: Any) -> _Run:
    _forget_expired_runs()  # expiry is enforced on every lookup, not only when a run starts
    run = _runs.get(raw) if isinstance(raw, str) else None
    if run is None:
        raise LoadoutError(404, "that apply run is unknown or expired - preview the build again")
    run.touched = _clock()
    return run


def _steps_text(first: int, last: int) -> str:
    return f"step {first}" if first == last else f"steps {first}-{last}"


def _in_history(run: _Run, chunk: dict[str, Any]) -> bool:
    """Whether the journal's committed entries show this chunk. Checked at runtime, never assumed: the journal may be
    a stub that keeps nothing (journal.entries() empty), and its entry format belongs to the journal, so this looks for
    the chunk's label, or the request that ran it (chunk 1: the plan id; later chunks: the run id and cursor)."""
    try:
        texts = [json.dumps(entry, default=str) for entry in journal.entries()]
    except Exception:  # noqa: BLE001 - a journal that can't be read doesn't count as History
        return False
    for text in texts:
        if chunk["label"] in text:
            return True
        if chunk["chunk"] == 1 and run.plan_id in text:
            return True
        if chunk["chunk"] > 1 and run.run_id in text and f'"cursor": {chunk["from"] - 1}' in text:
            return True
    return False


def _earlier_chunks(run: _Run, current: dict[str, Any] | None) -> tuple[str, bool | None]:
    """The chunks that stay applied (all but `current`, whose request is still open), as the end of a sentence, and
    whether History holds every one of them (None when there are none)."""
    applied = [c for c in run.chunks if c["status"] == "applied" and c is not current]
    if not applied:
        return "", None
    for chunk in applied:
        chunk["in_history"] = _in_history(run, chunk)
    found = all(c["in_history"] for c in applied)
    which = f"chunk {applied[0]['chunk']}" if len(applied) == 1 else f"chunks {applied[0]['chunk']}-{applied[-1]['chunk']}"
    span = _steps_text(applied[0]["from"], applied[-1]["to"])
    if found:
        return f" Earlier {which} ({span}) stay applied; History lists them, so you can undo them there.", True
    return (f" Earlier {which} ({span}) stay applied. RTSE can't find them in History, so the result lists each "
            "value before and after: put back by hand whatever you don't want."), False


def _stop_rest(run: _Run, state: str) -> None:
    run.state = state
    for result in run.results:
        if result["status"] == "pending":
            result["status"] = "not_run"


def _label(tx: Any, label: str) -> None:
    """Names the journal transaction of this request after the build and chunk (it was opened by the route registry
    with the route path; nested transaction() calls join it, so the label is set on the shared object)."""
    try:
        tx.label = label
    except AttributeError:
        pass


def _run_chunk(run: _Run) -> dict[str, Any]:
    """Runs the next chunk: at most CHUNK_MAX_STEPS steps, and no new step once CHUNK_SECONDS have passed. The deadline
    is soft: it is checked before each step, so one slow step can overrun it, and the game-state fingerprints read
    before and after the chunk are outside it; the step cap is what bounds the work. Each chunk is one journal
    transaction. A failure stops the run; with on_failure "rollback" only THIS chunk is undone (its transaction is
    then discarded); earlier chunks stay applied and committed."""
    if run.state != "running":
        raise LoadoutError(409, f"this apply run has already {'finished' if run.state == 'done' else 'stopped'}")
    if run.chunk:  # a continuation: did the game change since the previous chunk ended?
        try:
            now: str | None = _basis(run.sections)
            problem = None if now == run.basis else "the game changed"
        except Exception as ex:  # noqa: BLE001 - a state that can't be read is never treated as unchanged
            problem = f"the game state couldn't be read ({str(ex) or ex.__class__.__name__})"
        if problem is not None:
            _stop_rest(run, "stopped")
            earlier, _found = _earlier_chunks(run, None)
            run.message = (f"{problem.capitalize()} since chunk {run.chunk} ended, so the build stopped before step "
                           f"{run.cursor + 1}; nothing more was written.{earlier}")
            logging.error(f"RTSE: build {run.name!r} stopped: {problem}")
            return _reply(run, ran=None)

    run.chunk += 1
    first = run.cursor
    deadline = _clock() + CHUNK_SECONDS
    # M is the least number of chunks the run can take; a chunk that hits CHUNK_SECONDS early makes it grow.
    total_chunks = run.chunk - 1 + -(-(len(run.steps) - first) // CHUNK_MAX_STEPS)
    label = f"Build {run.name!r}: chunk {run.chunk}/{total_chunks}"
    failed_at: int | None = None
    index = first
    try:
        with journal.transaction(label, {"run_id": run.run_id, "chunk": run.chunk}) as tx:
            _label(tx, label)
            while index < len(run.steps) and index - first < CHUNK_MAX_STEPS:
                if index > first and _clock() >= deadline:
                    break
                step, result = run.steps[index], run.results[index]
                result["status"] = "ok"
                try:
                    result["after"] = _forward(step, run.ids, run.context)
                except Exception as ex:  # noqa: BLE001 - any refusal or failed readback is this step's failure
                    result.update(status="failed", error=str(ex) or ex.__class__.__name__)
                    logging.error(f"RTSE: build step {step['n']} failed: {result['error']}")
                    _settle_failed(step, result, run.ids, run.context)
                    failed_at = index
                    index += 1
                    break
                index += 1
            if failed_at is not None and run.on_failure == "rollback":
                _undo_chunk(run.results, run.steps, range(first, failed_at), run.ids, run.context)
                raise _Undone  # aborts this chunk's journal transaction: History keeps none of it
    except _Undone:
        pass

    run.cursor = index
    chunk = {"chunk": run.chunk, "from": first + 1, "to": index, "label": label, "in_history": None}
    span = _steps_text(first + 1, index)
    if failed_at is None:
        chunk["status"] = "applied"
        run.chunks.append(chunk)
        if run.cursor >= len(run.steps):
            run.state = "done"
            run.message = f"All {len(run.steps)} steps applied."
        else:
            run.message = f"Chunk {run.chunk} applied ({span} of {len(run.steps)})."
            try:
                run.basis = _basis(run.sections)
            except Exception as ex:  # noqa: BLE001 - without a reading the next chunk can't be checked: stop now
                _stop_rest(run, "stopped")
                earlier, _found = _earlier_chunks(run, chunk)
                run.message = (f"Chunk {run.chunk} applied ({span}), but the game state couldn't be read afterwards "
                               f"({str(ex) or ex.__class__.__name__}), so the build stopped rather than guess.{earlier}")
    else:
        n = run.steps[failed_at]["n"]
        chunk["status"] = "rolled_back" if run.on_failure == "rollback" else "kept_partial"
        earlier, _found = _earlier_chunks(run, None)
        run.chunks.append(chunk)
        _stop_rest(run, "failed")
        if run.on_failure == "rollback":
            run.message = f"Step {n} failed, so chunk {run.chunk} ({span}) was rolled back.{earlier}"
        else:
            run.message = (f"Step {n} failed. The steps before it stay applied (nothing was rolled back); the result "
                           f"lists each value before and after.{earlier}")
    logging.info(f"RTSE: build {run.name!r} chunk {run.chunk}: {run.message}")
    return _reply(run, ran=chunk)


def _reply(run: _Run, *, ran: dict[str, Any] | None) -> dict[str, Any]:
    """The run as JSON. Every step carries `before` (the plan's reading) and `after` (the game's readback), so the
    page can show exactly what stayed applied even when the journal keeps no History."""
    global _last_report  # noqa: PLW0603
    last = ran or (run.chunks[-1] if run.chunks else None)
    settled = [c for c in run.chunks if c["status"] == "applied" and c is not ran and c["in_history"] is not None]
    report = {
        "run_id": run.run_id,
        "name": run.name,
        "on_failure": run.on_failure,
        "state": run.state,
        "ok": run.state in ("running", "done"),
        "done": run.state != "running",
        "total": len(run.steps),
        "cursor": run.cursor,
        "chunk": last["chunk"] if last else 0,
        "chunk_from": last["from"] if last else None,
        "chunk_to": last["to"] if last else None,
        "chunks": [{k: v for k, v in c.items() if k != "label"} for c in run.chunks],
        "history": all(c["in_history"] for c in settled) if settled else None,
        "rolled_back": bool(ran and ran.get("status") == "rolled_back"),
        "steps": [dict(r) for r in run.results],
        "summary": _counts(run.results),
        "message": run.message,
        "missing": run.missing,
        "blocked": run.blocked,
    }
    _last_report = report
    return report


def apply(params: Params) -> dict[str, Any]:
    """Applies a stored build, one chunk per request.

    Start: {name, plan_id, sections?, over_max?, skip?, on_failure?}. `plan_id` must come from a fresh preview, so the
    run is exactly what the user saw. Continue: {run_id, cursor}, where cursor is the previous reply's. The page loops
    while the reply's state is "running"; GET /api/loadouts/run answers where a run stands.
    """
    if "run_id" in params:
        run = _find_run(params.get("run_id"))
        cursor = params.get("cursor")
        if type(cursor) is not int or cursor != run.cursor:
            raise LoadoutError(409, f"this run is at step {run.cursor + 1}, not {cursor} - ask /api/loadouts/run where it stands")
        return _run_chunk(run)

    preset = _find_stored(params.get("name"))
    plan_id = params.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id:
        raise LoadoutError(400, "plan_id is required (preview the build first)")
    on_failure = params.get("on_failure", "rollback")
    if on_failure not in ON_FAILURE_MODES:
        raise LoadoutError(400, 'on_failure must be "rollback" or "keep"')
    sections, over_max, skip = _options(params)
    fresh = plan(preset, sections=sections, over_max=over_max, skip=skip)
    if fresh["plan_id"] != plan_id:
        raise LoadoutError(409, "the game changed since the preview - preview again")
    _make_room()
    run = _Run(preset, fresh, on_failure)
    _runs[run.run_id] = run
    if not run.steps:
        run.state = "done"
        run.message = "Nothing to change."
        return _reply(run, ran=None)
    return _run_chunk(run)


# --- Options from query strings or JSON bodies -------------------------------------------------


def _as_list(raw: Any, what: str) -> list[Any] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return raw
    raise LoadoutError(400, f"{what} must be a list")


def _flag(raw: Any, what: str) -> bool:
    if raw is None:
        return False
    if type(raw) is bool:
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("", "0", "false", "no", "off"):
            return False
    raise LoadoutError(400, f"{what} must be true or false")


def _skip_list(raw: Any) -> list[int]:
    out: list[int] = []
    for item in _as_list(raw, "skip") or []:
        if isinstance(item, str):
            try:
                item = int(item)
            except ValueError:
                raise LoadoutError(400, "skip must be gear numbers") from None
        if type(item) is not int or item < 0:
            raise LoadoutError(400, "skip must be gear numbers")
        out.append(item)
    return sorted(set(out))


def _options(params: Params) -> tuple[list[str] | None, bool, list[int]]:
    sections = _as_list(params.get("sections"), "sections")
    if sections is not None:
        for section in sections:
            if section not in SECTIONS:
                raise LoadoutError(400, f"unknown section {section!r}")
    return sections, _flag(params.get("over_max"), "over_max"), _skip_list(params.get("skip"))


def _include(raw: Any) -> dict[str, bool]:
    section = _dict(raw if raw is not None else {}, "include")
    _unknown_keys(section, INCLUDE_KEYS, "include")
    flags = dict.fromkeys(INCLUDE_KEYS, False)
    for key, value in section.items():
        if type(value) is not bool:
            raise LoadoutError(400, f"include.{key} must be true or false")
        flags[key] = value
    return flags


# --- Routes: save, import, list, get, link, plan, export, delete, apply, import_missing, debug ------------------


def list_presets(_params: Params) -> dict[str, Any]:
    presets, warnings = _load()
    try:
        current_class = character.read().get("class_key")
    except Exception:  # noqa: BLE001 - listing must work outside a save
        current_class = None
    return {
        "presets": [_summary(p) for p in presets],
        "warnings": warnings,
        "class_key": current_class,
        "schema_version": SCHEMA_VERSION,
        "can_import_missing": IMPORT_MISSING_HOOK is not None,
    }


def get_preset(params: Params) -> dict[str, Any]:
    return {"preset": _find_stored(params.get("name"))}


def link(params: Params) -> dict[str, Any]:
    return {"link": to_link(_find_stored(params.get("name")))}


def run_route(params: Params) -> dict[str, Any]:
    """Where an apply run stands, for a page that lost a chunk's answer (a timeout or a dropped connection)."""
    return _reply(_find_run(params.get("run_id")), ran=None)


def plan_route(params: Params) -> dict[str, Any]:
    sections, over_max, skip = _options(params)
    return plan(_find_stored(params.get("name")), sections=sections, over_max=over_max, skip=skip)


def export_character(_params: Params) -> dict[str, Any]:
    preset, warnings = capture(None, {}, whole=True)
    return {"preset": preset, "link": to_link(preset), "warnings": warnings}


def save(params: Params) -> dict[str, Any]:
    name = _text(params.get("name"), "name", NAME_LIMIT)
    preset, warnings = capture(name, _include(params.get("include")))
    if warnings and not _flag(params.get("allow_partial"), "allow_partial"):
        # Nothing is stored: the page shows what couldn't be read and asks before saving a partial build.
        return {"saved": False, "partial": True, "warnings": warnings}
    return _store(preset, overwrite=_flag(params.get("overwrite"), "overwrite"), warnings=warnings)


def import_preset(params: Params) -> dict[str, Any]:
    has_preset, has_link = "preset" in params, "link" in params
    if has_preset == has_link:
        raise LoadoutError(400, "send exactly one of preset or link")
    preset = validate(params["preset"]) if has_preset else from_link(params["link"])
    if params.get("rename") is not None:
        preset = {**preset, "name": _text(params["rename"], "rename", NAME_LIMIT)}
    return _store(preset, overwrite=_flag(params.get("overwrite"), "overwrite"))


def delete(params: Params) -> dict[str, Any]:
    presets, warnings = _load()
    removed = presets.pop(_require_index(presets, params.get("name")))
    _write_store(presets)
    return {"presets": [_summary(p) for p in presets], "warnings": warnings, "deleted": removed["name"]}


def import_missing(params: Params) -> dict[str, Any]:
    """Hands the preset's missing gear to the item-code importer (IMPORT_MISSING_HOOK) and returns its results."""
    if IMPORT_MISSING_HOOK is None:
        raise LoadoutError(501, "importing items from codes isn't available yet (needs the item-codes feature)")
    preset = _find_stored(params.get("name"))
    missing = plan(preset)["missing"]
    entries = [preset["gear"][m["entry"]] for m in missing]
    if not entries:
        return {"results": []}
    try:
        returned = IMPORT_MISSING_HOOK(entries)
    except Exception as ex:  # noqa: BLE001
        raise LoadoutError(500, f"the item-code importer failed: {ex}") from None
    return {"results": _hook_results(returned, len(entries))}


def _hook_results(returned: Any, expected: int) -> list[dict[str, Any]]:
    if not isinstance(returned, list) or len(returned) != expected:
        raise LoadoutError(500, f"the item-code importer returned the wrong number of results (wanted {expected})")
    out = []
    for result in returned:
        if (
            not isinstance(result, dict)
            or not {"ok", "id", "error"} <= result.keys()
            or type(result["ok"]) is not bool
            or not (result["id"] is None or isinstance(result["id"], str))
            or not (result["error"] is None or isinstance(result["error"], str))
        ):
            raise LoadoutError(500, "the item-code importer returned a result of the wrong shape")
        out.append({"ok": result["ok"], "id": result["id"], "error": result["error"]})
    return out


def debug_dump(_params: Params) -> dict[str, Any]:
    """Saves what RTSE sees for a build (a full capture, the raw identity of each item, the stored names, the last
    apply report and the hook state) to DEBUG_FILE. Works outside a save too: the capture then carries the error."""
    try:
        capture_state: dict[str, Any] = {"ok": True, "preset": capture(None, {}, whole=True)[0]}
    except Exception as ex:  # noqa: BLE001 - the dump is for diagnosing exactly this
        capture_state = {"ok": False, "error": str(ex)}
    try:
        items = [{k: v for k, v in _live_item(i, loc).items() if k != "raw"} for i, loc in
                 gear_inventory._inventory(gear_inventory._manager())[0]]  # noqa: SLF001
        items_error = None
    except Exception as ex:  # noqa: BLE001
        items, items_error = [], str(ex)
    presets, warnings = _load()
    out = {
        "capture": capture_state,
        "items": items,
        "items_error": items_error,
        "stored": [_summary(p) for p in presets],
        "warnings": warnings,
        "last_apply": _last_report,
        "hook": {
            "installed": IMPORT_MISSING_HOOK is not None,
            "name": getattr(IMPORT_MISSING_HOOK, "__qualname__", None),
        },
    }
    DEBUG_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_FILE), "items": len(items)}


# ---- HOOK: stream B (item codes) -------------------------------------------------------------------------------
# Wired by the hub after stream B merges. Set IMPORT_MISSING_HOOK to a callable
#     (entries: list[dict]) -> list[dict]
# taking the preset's gear entries (schema above) and returning one {"ok": bool, "id": str | None, "error": str | None}
# per entry, in order. Until then it is None, the plan says can_import=false and the route answers 501.
IMPORT_MISSING_HOOK: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = LoadoutError

GET_ROUTES: dict[str, Route] = {
    "/api/loadouts": list_presets,
    "/api/loadouts/get": get_preset,
    "/api/loadouts/link": link,
    "/api/loadouts/plan": plan_route,
    "/api/loadouts/run": run_route,
    "/api/loadouts/export_character": export_character,
    "/api/debug/loadouts": debug_dump,
}
POST_ROUTES: dict[str, Route] = {
    "/api/loadouts/save": save,
    "/api/loadouts/import": import_preset,
    "/api/loadouts/delete": delete,
    "/api/loadouts/apply": apply,
    "/api/loadouts/import_missing": import_missing,
}
