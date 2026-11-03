"""BL2 item codes: read pasted codes into cards, import codes as new items, and export an item as a code.

Public API (other streams call these; the routes at the bottom are the HTTP side):

  decode(text: str) -> list[dict]
      One entry per BL2 code in `text`: {index, code, ok, error, kind ("weapon" | "item" | None), unique_id, card,
      uid_clash, warning}. More than MAX_CODES (200) codes raises ApiError 400.
      ok is False when the code is malformed or its checksum fails. That check is offline and verified on real codes.
      The card comes from the GAME's UnpackSerialNumber (UNVERIFIED). When the game cannot decode a code that is
      otherwise fine, ok stays True, card is None and warning says why.
      NO time budget: it decodes every code in one go. On the game thread prefer decode_chunk.

  decode_chunk(text: str, start: int = 0) -> dict
      The preview route's reply: {entries (this chunk only; `index` stays absolute), found (codes in the whole text),
      next (the `start` for the next call, or None when done)}. One call does at most CHUNK_STEPS (20) codes and stops
      starting new ones after CHUNK_SECONDS (2.0 s), but always does at least one. A `start` outside 0..found: 400.

  import_items(codes: list[str], destination: str = "backpack") -> dict
  import_pairs(pairs: list[tuple[str, str]]) -> dict      (one destination per code)
      {results: [{code, ok, error, id, name, unique_id}], processed, remaining (always 0), imported, failed,
      path: "unpack" | "create" | "mixed"}. Imports EVERY code (no time budget; on the game thread prefer
      import_chunk). Every imported item gets a FRESH UniqueId: not held by any backpack, equipped or bank item, nor
      by an earlier code in the same batch. Each add is verified (add_verified): exactly one new entry carrying that
      UniqueId must appear, or every new entry is removed again and that code fails. A bad code fails on its own and
      the rest still import. More than MAX_CODES codes, or a destination other than "backpack" / "bank": ApiError 400.

  import_chunk(codes: list[str], destination: str = "backpack") -> dict
      The import route's reply: the same shape for the first codes only, within the same budget as decode_chunk.
      `processed` codes were handled (results has one row each, in order); the caller re-sends codes[processed:]
      until `remaining` is 0.

  export_code(source) -> (code, header | None, warnings)
      source is an item (its CreateSerialNumber()) or a bank ChestData entry with no Inventory (its
      InventorySerialNumber). header is item_serial.read_header's dict, or None when its version is not 7.

  verified_backup(source, definition, is_weapon, *, name, action) -> code
      The item's code, but only when RTSE could restore the item from it: the header reads (version 7), the checksum
      matches, and the game decodes the bytes back to a definition whose every field (UniqueId, GameStage,
      ManufacturerGradeIndex, BalanceDefinition, every part slot, ...) equals `definition`. Otherwise ApiError 409
      "could not make a restorable code for <name>, so it was not <action> (...)". A compared field that cannot be
      read on EITHER side (the item or the decoded code) is also a 409: "could not read <field> of <name> (...), so
      it was not <action>". Delete and move call it first.

  add_verified(definition, is_weapon, destination, unique_id, mark) -> {id, name}
      Adds a definition and checks the destination gained exactly one entry with `unique_id` (and lost nothing).
      Otherwise every new entry is removed again (exactly) and ApiError 409 says what happened.

Other modules use the helpers below (gear_inventory.py, bank.py): serial_struct, serial_bytes,
definition_of_code_bytes, definition_from_serial, copy_definition, card_for_bytes, card_from_definition,
fresh_unique_id, place_definition, undo_add, restore_code, Budget, ImportBatch.

Verified offline: the code layer (rtse/item_serial.py, tested on real published codes in dev/tests/test_item_serial.py).
NOT verified in a live game: every game call in this module and in bank.py / gear_inventory.py. Each is marked
UNVERIFIED where it is called. A failed call becomes a message (an ApiError or a per-code error), never a crash.
LAST_PATHS records which alternative last worked (serial struct build, decode path). /api/debug/codes dumps it.

Game calls (all UNVERIFIED): unrealsdk.find_class(...).ClassDefaultObject, unrealsdk.make_struct(...),
WillowInventory.CreateSerialNumber / GetSerialNumberString, WillowWeapon|WillowItem.UnpackSerialNumber,
Create(Weapon|Item)FromSerialNumber, WillowInventoryManager.GetEmptyBackpackSlots / ClientAdd(Weapon|Item)ToBackpack.
"""

from __future__ import annotations

import contextlib
import json
import random
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import unrealsdk
from unrealsdk import logging

from . import bank, gear_inventory, item_serial, journal, stats
from .gear_parts import ITEM_SLOTS, WEAPON_SLOTS, _balance_label, _humanize, _title_name
from .routing import ApiError, Params, Route

MAX_CODES = 200
CHUNK_SECONDS = 2.0  # a preview/import request stops starting new codes after this long (game thread budget)
CHUNK_STEPS = 20  # ... or after this many codes, whichever comes first; it always does at least one
MARK_STANDARD = 1
DESTINATIONS = ("backpack", "bank")
DEFAULT_BUFFER = 40  # serial buffer size when there is no live serial to copy it from (UNVERIFIED)
CHECKSUM_ERROR = "checksum does not match: the code was copied wrong, or it is not a Gibbed BL2 code"
CHECKSUM_WARNING = "the game's serial does not match the Gibbed format; this code may not paste into other tools"
UNVERIFIED = "Reading codes, importing them and exporting items use game calls nobody has tested in a live game yet"
DEBUG_CODES_FILE = Path(__file__).parent / "debug_codes.json"
DEBUG_ITEMS = 3
DEBUG_BANK_ENTRIES = 3

# Which alternative worked last: "serial" is make_struct or elementwise, "decode" is unpack or create (UNVERIFIED).
LAST_PATHS: dict[str, str] = {}

_RNG = random.SystemRandom()
_now = time.monotonic  # the chunk clock; tests patch it

WEAPON_DEFINITION_FIELDS = (
    "UniqueId", "GameStage", "TitlePartDefinition", "PrefixPartDefinition",
    *(f"{s}PartDefinition" for s in WEAPON_SLOTS),
    "ManufacturerGradeIndex", "ManufacturerDefinition", "BalanceDefinition", "WeaponTypeDefinition",
)  # fmt: skip
ITEM_DEFINITION_FIELDS = (
    "UniqueId", "GameStage", "TitleItemNamePartDefinition", "PrefixItemNamePartDefinition",
    *(f"{s}ItemPartDefinition" for s in ITEM_SLOTS),
    "ManufacturerGradeIndex", "ManufacturerDefinition", "BalanceDefinition", "ItemDefinition",
)  # fmt: skip
CLASS_BY_DEFINITION = {  # an item's ItemDefinition class -> the class the card shows
    "ShieldDefinition": "WillowShield",
    "GrenadeModDefinition": "WillowGrenadeMod",
    "ArtifactDefinition": "WillowArtifact",
    "ClassModDefinition": "WillowClassMod",
}


class DecodeError(Exception):
    """The game could not turn a serial into a definition, by any path."""


# --- Game access (UNVERIFIED; tests replace these) --------------------------------------------------


def _cdo(class_name: str) -> Any:
    return unrealsdk.find_class(class_name).ClassDefaultObject  # UNVERIFIED


def _make_struct(struct_name: str, **fields: Any) -> Any:
    return unrealsdk.make_struct(struct_name, **fields)  # UNVERIFIED


def _safe(read: Callable[[], Any], default: Any = None) -> Any:
    try:
        return read()
    except Exception:  # noqa: BLE001
        return default


def _path(obj: Any) -> str | None:
    return None if obj is None else obj._path_name()  # noqa: SLF001


def _template() -> Any | None:
    """A live serial to copy the State, RunningCounter and buffer size from. None when there is no item."""
    try:
        found, _warnings = gear_inventory._inventory(gear_inventory._manager())  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None
    for item, _location in found:
        serial = _safe(item.CreateSerialNumber)  # UNVERIFIED
        if serial is not None:
            return serial
    return None


def serial_bytes(serial: Any) -> bytes:
    """The bytes of a game serial: its Buffer, cut to EncryptedLength. Accepts any iterable of ints (UNVERIFIED shape)."""
    length = int(serial.EncryptedLength)  # UNVERIFIED field
    values = [int(value) & 0xFF for value in serial.Buffer]  # UNVERIFIED field (static array)
    if not 0 < length <= len(values):
        raise item_serial.SerialError(f"the game's serial claims {length} bytes, its buffer holds {len(values)}")
    return bytes(values[:length])


def _buffer_size(template: Any) -> int:
    try:
        return max(len(list(template.Buffer)), 1)
    except Exception:  # noqa: BLE001
        return DEFAULT_BUFFER


def serial_struct(data: bytes, template: Any | None = None) -> Any:
    """An InventorySerialNumber holding `data`. Tries make_struct with the Buffer first, then writes the buffer element by element."""
    length = len(data)
    state, counter, size = 0, 0, DEFAULT_BUFFER
    if template is not None:
        state = _safe(lambda: int(template.State), 0)
        counter = _safe(lambda: int(template.RunningCounter), 0)
        size = _buffer_size(template)
    size = max(size, length)
    padded = [*data, *([0] * (size - length))]
    try:
        serial = _make_struct(  # UNVERIFIED: a static array field given as a list
            "InventorySerialNumber", EncryptedLength=length, RunningCounter=counter, State=state, Buffer=padded,
        )
        if serial_bytes(serial) == bytes(data):
            LAST_PATHS["serial"] = "make_struct"
            return serial
        problem = "make_struct kept a different Buffer"
    except Exception as ex:  # noqa: BLE001
        problem = f"make_struct raised {ex!r}"
    serial = _make_struct("InventorySerialNumber", EncryptedLength=length, RunningCounter=counter, State=state)
    for index, value in enumerate(padded):  # UNVERIFIED: element assignment into a struct's static array
        serial.Buffer[index] = value
    if serial_bytes(serial) != bytes(data):
        raise item_serial.SerialError(f"could not write the serial buffer ({problem}; element-wise write failed too)")
    LAST_PATHS["serial"] = "elementwise"
    return serial


def definition_from_serial(serial: Any, is_weapon: bool) -> tuple[Any, str]:
    """The game's definition for a serial, and which path gave it: "unpack" (UnpackSerialNumber on the class default
    object) or "create" (a temporary actor from Create*FromSerialNumber, whose copy of its definition is kept and whose
    actor is destroyed). Raises DecodeError when both fail. All UNVERIFIED."""
    cls = "WillowWeapon" if is_weapon else "WillowItem"
    try:
        found, definition = _cdo(cls).UnpackSerialNumber(serial)  # UNVERIFIED: out param comes back as a tuple
        if found and definition is not None:
            LAST_PATHS["decode"] = "unpack"
            return definition, "unpack"
        unpack_problem = "UnpackSerialNumber returned False"
    except Exception as ex:  # noqa: BLE001
        unpack_problem = f"UnpackSerialNumber raised {ex!r}"
    create = "CreateWeaponFromSerialNumber" if is_weapon else "CreateItemFromSerialNumber"
    actor = None
    try:
        actor = getattr(_cdo(cls), create)(serial, None)  # UNVERIFIED
        if actor is None:
            raise DecodeError("no actor was created")
        definition = copy_definition(actor.DefinitionData, is_weapon)
    except Exception as ex:  # noqa: BLE001
        raise DecodeError(f"{unpack_problem}; {create} also failed ({ex!r})") from None
    finally:
        if actor is not None:
            with contextlib.suppress(Exception):
                actor.Destroy()  # UNVERIFIED: the temporary actor must not stay in the level
    LAST_PATHS["decode"] = "create"
    return definition, "create"


def definition_of_code_bytes(data: bytes, template: Any | None = None) -> tuple[Any, bool, str]:
    """(definition, is_weapon, path) for code bytes. Raises SerialError for a bad header, DecodeError when the game cannot decode it."""
    header = item_serial.read_header(data)
    # Before ANY game call: checksum, version and every asset reference bounds-checked against the asset tables
    # (rtse/item_assets.py). The game decoder is still trusted for what valid references mean together.
    item_serial.validate_assets(data)
    if template is None:
        template = _template()
    definition, path = definition_from_serial(serial_struct(data, template), header["is_weapon"])
    return definition, header["is_weapon"], path


def copy_definition(source: Any, is_weapon: bool) -> Any:
    """A new definition struct with the same fields as `source` (UNVERIFIED: make_struct with every field).
    Fields this game build does not have are left out."""
    name, fields = ("WeaponDefinitionData", WEAPON_DEFINITION_FIELDS) if is_weapon else ("ItemDefinitionData", ITEM_DEFINITION_FIELDS)
    values: dict[str, Any] = {}
    for field in fields:
        try:
            values[field] = getattr(source, field)
        except AttributeError:
            continue
    return _make_struct(name, **values)


# --- Cards (the gear card shape from gear_inventory.card_of, built from a definition) ---------------------


def _name_of(definition: Any, is_weapon: bool) -> str:
    """Best-effort name (the game's text is UNVERIFIED): a unique gun's title, else "<make> <balance>"."""
    title = _safe(lambda: definition.TitlePartDefinition if is_weapon else definition.TitleItemNamePartDefinition)
    if title is not None:
        return _title_name(title)
    maker = _safe(lambda: definition.ManufacturerDefinition)
    balance = _safe(lambda: definition.BalanceDefinition)
    words = [
        _humanize(maker.Name) if maker is not None else "",  # UNVERIFIED: object names
        _balance_label(balance, weapon=is_weapon) if balance is not None else "",
    ]
    return " ".join(word for word in words if word) or "Unknown item"


def card_from_definition(definition: Any, is_weapon: bool, *, card_id: str, location: str, unique_id: int) -> dict[str, Any]:
    """The gear card for a definition: name, class, level (GameStage), type, parts and hidden bones. Not rarity (None)."""
    weapon_type = _safe(lambda: definition.WeaponTypeDefinition) if is_weapon else None
    item_definition = None if is_weapon else _safe(lambda: definition.ItemDefinition)
    if is_weapon:
        cls = "WillowWeapon"
    else:
        cls = CLASS_BY_DEFINITION.get(_safe(lambda: item_definition.Class.Name, ""), "WillowItem")
    return {
        "id": card_id,
        "class": cls,
        "name": _name_of(definition, is_weapon),
        "location": location,
        "level": _safe(lambda: int(definition.GameStage)),
        "rarity": None,
        "type_path": _path(weapon_type),
        "build": gear_inventory.build_of(definition, is_weapon),
        "hide_bones": gear_inventory.hidden_bones_of(weapon_type),
        "unique_id": unique_id,
    }


def card_for_bytes(
    data: bytes, *, card_id: str, location: str, unique_id: int, template: Any | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """(card, warning). The card is None and the warning says why when the game cannot decode the code."""
    try:
        definition, is_weapon, _path_used = definition_of_code_bytes(data, template)
        return card_from_definition(definition, is_weapon, card_id=card_id, location=location, unique_id=unique_id), None
    except Exception as ex:  # noqa: BLE001
        logging.warning(f"RTSE: could not decode {card_id}: {ex!r}")
        return None, f"the game could not decode this code ({ex})"


# --- Export ----------------------------------------------------------------------------------------------


def export_code(source: Any) -> tuple[str, dict[str, Any] | None, list[str]]:
    """The item code for an item (its CreateSerialNumber) or a chest entry (its InventorySerialNumber).

    Raises SerialError when the game's serial cannot be read as bytes. A serial whose header cannot be read, or whose
    checksum fails, is still exported, with a warning. UNVERIFIED: CreateSerialNumber / InventorySerialNumber.
    """
    create = getattr(source, "CreateSerialNumber", None)
    serial = create() if callable(create) else source.InventorySerialNumber  # UNVERIFIED
    data = serial_bytes(serial)
    warnings: list[str] = []
    try:
        header: dict[str, Any] | None = item_serial.read_header(data)
    except item_serial.SerialError as ex:
        header = None
        warnings.append(f"the header could not be read ({ex}); the code is exported anyway")
    else:
        if not header["checksum_ok"]:
            warnings.append(CHECKSUM_WARNING)
    return item_serial.bytes_to_code(data), header, warnings


class FieldUnreadable(Exception):
    """A definition field could not be read. Never compared: verified_backup refuses instead."""


def _field_value(definition: Any, name: str) -> Any:
    """A definition field as a comparable value (objects by path name). Raises FieldUnreadable when the read fails, so
    two unreadable fields can never compare equal. A field this game build lacks fails too (UNVERIFIED field list)."""
    try:
        return _plain(getattr(definition, name))
    except Exception as ex:  # noqa: BLE001
        raise FieldUnreadable(f"{name}: {ex!r}") from None


def definition_signature(definition: Any, is_weapon: bool) -> tuple[Any, ...]:
    """What makes an item THAT item: every WEAPON_/ITEM_DEFINITION_FIELDS value (objects by path name, UniqueId
    included), plus the kind. The same fields verified_backup compares. Raises FieldUnreadable when any read fails, so
    an unreadable item never matches anything."""
    fields = WEAPON_DEFINITION_FIELDS if is_weapon else ITEM_DEFINITION_FIELDS
    return (bool(is_weapon), *(_field_value(definition, field) for field in fields))


def signature_or_none(definition: Any, is_weapon: bool) -> tuple[Any, ...] | None:
    """definition_signature, or None when a field cannot be read (None never matches: compare with `matches`)."""
    try:
        return definition_signature(definition, is_weapon)
    except FieldUnreadable:
        return None


def matches(found: tuple[Any, ...] | None, expected: tuple[Any, ...] | None) -> bool:
    """True only when both signatures were read and are equal."""
    return found is not None and expected is not None and found == expected


def verified_backup(source: Any, definition: Any, is_weapon: bool, *, name: str = "the item", action: str = "deleted") -> str:
    """The code of `source`, checked to restore the same item (see the module docstring). ApiError 409 otherwise.

    `definition` is the item's own definition (its DefinitionData, or a bank entry's). Every field in
    WEAPON_DEFINITION_FIELDS / ITEM_DEFINITION_FIELDS is compared, objects by path name. UNVERIFIED game calls:
    CreateSerialNumber / InventorySerialNumber and the game decode (UnpackSerialNumber or Create*FromSerialNumber).
    """

    def refuse(why: str) -> ApiError:
        return ApiError(409, f"could not make a restorable code for {name}, so it was not {action} ({why})")

    try:
        code, header, _warnings = export_code(source)
    except Exception as ex:  # noqa: BLE001
        raise refuse(f"its serial could not be read: {ex}") from None
    if header is None:
        raise refuse("its header could not be read, or its serial version is not 7")
    if not header["checksum_ok"]:
        raise refuse("its checksum does not match")
    if header["is_weapon"] != is_weapon:
        raise refuse("its code says weapon/item the other way round")
    try:
        decoded, decoded_weapon, _path_used = definition_of_code_bytes(item_serial.code_to_bytes(code))
    except Exception as ex:  # noqa: BLE001
        raise refuse(f"the game could not decode it: {ex}") from None
    if decoded_weapon != is_weapon:
        raise refuse("the game decoded it as the wrong kind")
    fields = WEAPON_DEFINITION_FIELDS if is_weapon else ITEM_DEFINITION_FIELDS
    different = []
    for field in fields:
        values = []
        for side, source_definition in (("the item", definition), ("its decoded code", decoded)):
            try:
                values.append(_field_value(source_definition, field))
            except FieldUnreadable as ex:
                raise ApiError(
                    409, f"could not read {field} of {name} ({side}: {ex}), so it was not {action}",
                ) from None
        if values[0] != values[1]:
            different.append(field)
    if different:
        raise refuse(f"decoding it gives a different item: {', '.join(different)} differ")
    return code


# --- Chunk budget --------------------------------------------------------------------------------------


class Budget:
    """One request's work limit: at most CHUNK_STEPS steps, no new step after CHUNK_SECONDS, and never fewer than one
    step. `unlimited=True` never stops (the full Python APIs decode / import_items)."""

    def __init__(self, *, unlimited: bool = False) -> None:
        self.unlimited = unlimited
        self.started = _now()
        self.steps = 0

    def spent(self) -> bool:
        if self.unlimited or self.steps == 0:
            return False
        return self.steps >= CHUNK_STEPS or _now() - self.started >= CHUNK_SECONDS

    def step(self) -> None:
        self.steps += 1


# --- Decode (preview) ------------------------------------------------------------------------------------


def _kind(header: dict[str, Any]) -> str:
    return "weapon" if header["is_weapon"] else "item"


def _entry(index: int, code: str) -> dict[str, Any]:
    return {
        "index": index, "code": code, "ok": False, "error": None, "kind": None, "unique_id": None,
        "card": None, "uid_clash": False, "warning": None,
    }  # fmt: skip


def _extract(text: str) -> list[str]:
    found = item_serial.extract_codes(text or "")
    if len(found) > MAX_CODES:
        raise ApiError(400, f"at most {MAX_CODES} codes at a time ({len(found)} found)")
    return found


def _decode_one(index: int, code: str, used: set[int], template: Any | None) -> dict[str, Any]:
    entry = _entry(index, code)
    try:
        data = item_serial.code_to_bytes(code)
        header = item_serial.read_header(data)
    except item_serial.SerialError as ex:
        entry["error"] = str(ex)
        return entry
    entry.update(kind=_kind(header), unique_id=header["unique_id"], uid_clash=header["unique_id"] in used)
    if not header["checksum_ok"]:
        entry["error"] = CHECKSUM_ERROR
        return entry
    try:
        item_serial.validate_assets(data)  # offline: a code with impossible asset references is an error, not a card
    except item_serial.SerialError as ex:
        entry["error"] = f"not a valid item: {ex}"
    else:
        entry["ok"] = True
        entry["card"], entry["warning"] = card_for_bytes(
            data, card_id=f"preview:{index}", location="Preview", unique_id=header["unique_id"], template=template,
        )
    return entry


def _decode_range(found: list[str], start: int, budget: Budget) -> list[dict[str, Any]]:
    if start >= len(found):
        return []
    used = gear_inventory.used_unique_ids()
    template = _template()
    entries: list[dict[str, Any]] = []
    for index in range(start, len(found)):
        if budget.spent():
            break
        budget.step()
        entries.append(_decode_one(index, found[index], used, template))
    return entries


def decode(text: str) -> list[dict[str, Any]]:
    """The preview entries for EVERY code in `text`, with no time budget (see the module docstring). More than
    MAX_CODES codes: ApiError 400."""
    return _decode_range(_extract(text), 0, Budget(unlimited=True))


def decode_chunk(text: str, start: int = 0) -> dict[str, Any]:
    """One budgeted slice of the preview, from code `start` on: {entries, found, next} (see the module docstring)."""
    found = _extract(text)
    if isinstance(start, bool) or not isinstance(start, int) or not 0 <= start <= len(found):
        raise ApiError(400, f"'start' must be a whole number from 0 to {len(found)}")
    entries = _decode_range(found, start, Budget())
    end = start + len(entries)
    return {"entries": entries, "found": len(found), "next": end if end < len(found) else None}


# --- Import ----------------------------------------------------------------------------------------------


def fresh_unique_id(used: set[int]) -> int:
    """A random nonzero signed 32-bit UniqueId that is not in `used`."""
    while True:
        candidate = _RNG.randint(1, (1 << 31) - 1)
        if candidate not in used:
            return candidate


def place_definition(definition: Any, is_weapon: bool, destination: str, mark: int = MARK_STANDARD) -> str | None:
    """Adds a definition to the backpack (ClientAdd...ToBackpack) or the bank. None on success, else why not.
    UNVERIFIED calls. Success is confirmed by add_verified's snapshots, never by this return value alone."""
    try:
        if destination == "bank":
            return None if bank.add_def_to_bank(definition, is_weapon) else "the bank refused it (it may be full)"
        manager = gear_inventory._manager()  # noqa: SLF001
        if manager.GetEmptyBackpackSlots() <= 0:  # UNVERIFIED call
            return "the backpack is full"
        if is_weapon:
            manager.ClientAddWeaponToBackpack(definition, mark, False)  # UNVERIFIED call
        else:
            manager.ClientAddItemToBackpack(definition, 1, mark, False)  # UNVERIFIED call
        return None
    except ApiError as ex:
        return str(ex)
    except Exception as ex:  # noqa: BLE001
        return f"the game refused to add it ({ex})"


def _backpack_rows() -> list[tuple[str, int, Any]]:
    """(id, UniqueId, item) for every backpack item, in order."""
    manager = gear_inventory._manager()  # noqa: SLF001
    return [(gear_inventory._item_id(i), stats.unique_id(i), i) for i in manager.Backpack if i is not None]  # noqa: SLF001


def _unmatched(side: list[Any], other: list[Any], key: Callable[[Any], Any]) -> list[Any]:
    left = Counter(key(row) for row in other)
    out = []
    for row in side:
        if left[key(row)] > 0:
            left[key(row)] -= 1
        else:
            out.append(row)
    return out


def _add_problem(problem: str | None, destination: str, unique_id: int, new: int) -> str:
    if problem and new == 0:
        return problem
    added = f"the game added {new} entries" + (f" (and said: {problem})" if problem else "")
    return f"adding to the {destination} did not give exactly one new entry with UniqueId {unique_id}: {added}"


def _cleanup_note(failures: list[str], vanished: list[str]) -> str:
    if failures:
        note = f"; removing the new entries failed: {'; '.join(failures)}"
    else:
        note = "; every new entry was removed again"
    if vanished:
        note += f"; these vanished while adding, re-import them: {', '.join(vanished)}"
    return note


def _take(rows: list[Any], counts: Counter[int], uid_of: Callable[[Any], int]) -> list[Any]:
    """The rows (last first) that account for `counts`: as many per UniqueId as counted."""
    wanted = Counter(counts)
    out = []
    for row in reversed(rows):
        if wanted[uid_of(row)] > 0:
            wanted[uid_of(row)] -= 1
            out.append(row)
    return out


def _vanished_code(item: Any) -> str:
    # A vanished object may still answer CreateSerialNumber (UNVERIFIED); else only its name is known.
    return _safe(lambda: export_code(item)[0], f"{_safe(item.GetHumanReadableName, 'an item')} (no code)")


def _add_verified_backpack(definition: Any, is_weapon: bool, unique_id: int, mark: int) -> dict[str, Any]:
    before = _backpack_rows()
    problem = place_definition(definition, is_weapon, "backpack", mark)
    after = _backpack_rows()
    lost = Counter(row[1] for row in before) - Counter(row[1] for row in after)
    gained = Counter(row[1] for row in after) - Counter(row[1] for row in before)
    new = _unmatched(after, before, key=lambda row: row[0])
    expected = signature_or_none(definition, is_weapon)
    wrong_item = False
    if problem is None and gained == Counter({unique_id: 1}) and not lost:
        row = next((r for r in reversed(new) if r[1] == unique_id), None) or next(
            r for r in reversed(after) if r[1] == unique_id
        )
        # The UniqueId alone proves nothing: the new item must BE the item we added (every definition field).
        found = _safe(lambda: signature_or_none(row[2].DefinitionData, gear_inventory._is_weapon(row[2])))  # noqa: SLF001
        if matches(found, expected):
            return {"id": row[0], "name": row[2].GetHumanReadableName()}
        wrong_item = True
    # Remove only what is really new (as many per UniqueId as the backpack gained), never an existing item whose
    # object the game may have re-created.
    extra = _take(new, gained, lambda row: row[1])
    failures: list[str] = []
    for _item_id, _uid, item in extra:
        try:
            gear_inventory.remove_from_backpack(gear_inventory._manager(), item)  # noqa: SLF001
        except ApiError as ex:
            failures.append(str(ex))
    gone = _unmatched(before, after, key=lambda row: row[0])
    vanished = [_vanished_code(row[2]) for row in _take(gone, lost, lambda row: row[1])]
    message = _add_problem(problem, "backpack", unique_id, len(extra))
    if wrong_item:
        message += "; the item that appeared was not the item that was added (its parts or balance differ), so it was removed"
    if extra or lost:
        message += _cleanup_note(failures, vanished)
    raise ApiError(409, message)


def _add_verified_bank(definition: Any, is_weapon: bool, unique_id: int) -> dict[str, Any]:
    before = bank.snapshot()
    problem = place_definition(definition, is_weapon, "bank")
    after = bank.snapshot()
    lost, gained = bank.uid_changes(before, after)
    gone, new = bank.diff(before, after)
    expected = signature_or_none(definition, is_weapon)
    wrong_item = False
    if problem is None and gained == Counter({unique_id: 1}) and not lost:
        snap = next((s for s in reversed(new) if s.unique_id == unique_id), None)
        if snap is None:
            snap = next(s for s in reversed(after) if s.unique_id == unique_id)
        # The UniqueId alone proves nothing: the new entry must BE the item we added (every definition field).
        if matches(bank.identity_of(snap), expected):
            return {"id": bank.bank_id(snap.index, unique_id), "name": bank.entry_card(snap.index, snap.entry)[0]["name"]}
        wrong_item = True
        new = [snap]
        gained = Counter({unique_id: 1})
    # Remove only the entries that are really new: as many per UniqueId as the bank gained (a merely re-serialized
    # entry keeps its UniqueId count and is left alone). remove_gained lets one removal take the other new twins too.
    extra = _take(new, gained, lambda snap: snap.unique_id)
    failures = bank.remove_gained(before) if extra else []
    put =bank.put_back(gone, lost) if lost else bank.PutBack()  # each put-back verified by its own snapshots
    vanished = put.failed
    message = _add_problem(problem, "bank", unique_id, len(extra))
    if wrong_item:
        message += "; the entry that appeared was not the item that was added (its parts or balance differ), so it was removed"
    if extra or lost:
        message += _cleanup_note(failures, vanished)
        if lost and not vanished:
            message += f"; entries that vanished while adding were put back ({', '.join(put.restored)})"
        if put.notes:
            message += f"; {'; '.join(put.notes)}"
    raise ApiError(409, message)


def add_verified(
    definition: Any, is_weapon: bool, destination: str, unique_id: int, mark: int = MARK_STANDARD,
) -> dict[str, Any]:
    """Adds `definition` to the destination and returns {id, name} of the new entry, verified by snapshots: the
    destination must gain exactly one entry, carrying `unique_id`, and lose nothing. Otherwise every new entry is
    removed (exactly) and ApiError 409 says what happened (a refused add keeps its own message, e.g. "the backpack is
    full"). UNVERIFIED game calls (see place_definition, bank.snapshot, bank.remove_exact)."""
    if destination == "bank":
        return _add_verified_bank(definition, is_weapon, unique_id)
    return _add_verified_backpack(definition, is_weapon, unique_id, mark)


def undo_add(destination: str, unique_id: int) -> None:
    """Removes the item with this UniqueId from the destination (undo of an add)."""
    if destination == "bank":
        found = bank.find_by_unique_id(unique_id)
        if found is not None:
            bank.remove_exact(*found)
        return
    item = gear_inventory.backpack_item(unique_id)
    if item is not None:
        gear_inventory.remove_from_backpack(gear_inventory._manager(), item)  # noqa: SLF001


def restore_code(code: str, destination: str) -> dict[str, Any]:
    """Imports a code again (undo of a delete). A failure is logged, not raised: undo must not break the game."""
    result = import_items([code], destination)
    if not result["imported"]:
        logging.error(f"RTSE: could not restore a deleted item: {result['results'][0]['error']}")
    return result


def _result(code: str) -> dict[str, Any]:
    return {"code": code, "ok": False, "error": None, "id": None, "name": None, "unique_id": None}


def _import_one(code: str, destination: str, used: set[int], template: Any | None) -> tuple[dict[str, Any], str | None]:
    """Imports one code. Returns (result, decode path or None when the code never reached the game)."""
    result = _result(code)
    try:
        data = item_serial.code_to_bytes(code)
        header = item_serial.read_header(data)
    except item_serial.SerialError as ex:
        result["error"] = str(ex)
        return result, None
    if not header["checksum_ok"]:
        result["error"] = CHECKSUM_ERROR
        return result, None
    unique_id = fresh_unique_id(used)
    used.add(unique_id)
    try:
        fresh = item_serial.with_unique_id(data, unique_id)
    except item_serial.SerialError as ex:
        result["error"] = str(ex)
        return result, None
    try:
        definition, is_weapon, path = definition_of_code_bytes(fresh, template)
    except Exception as ex:  # noqa: BLE001
        result["error"] = f"the game could not decode this code ({ex})"
        return result, None
    definition.UniqueId = unique_id  # belt and braces: the serial already carries it (UNVERIFIED struct write)
    try:
        added = add_verified(definition, is_weapon, destination, unique_id, MARK_STANDARD)
    except ApiError as ex:
        result["error"] = str(ex)
        return result, path
    journal.record(
        "inventory.add", target=added["id"], before=None, after={"code": code, "unique_id": unique_id},
        undo=lambda: undo_add(destination, unique_id),
    )  # fmt: skip
    result.update(ok=True, id=added["id"], name=added["name"], unique_id=unique_id)
    return result, path


class ImportBatch:
    """The codes of one request: the UniqueIds already used (so each import gets a fresh one), the serial template,
    and the result rows in order. gear_inventory adds its own "skipped" rows with skip()."""

    def __init__(self) -> None:
        self.used = gear_inventory.used_unique_ids()
        self.template = _template()
        self.results: list[dict[str, Any]] = []
        self.paths: set[str] = set()
        self.skipped = 0

    def run(self, code: str, destination: str) -> dict[str, Any]:
        result, path = _import_one(code, destination, self.used, self.template)
        self.results.append(result)
        if path is not None:
            self.paths.add(path)
        return result

    def skip(self, result: dict[str, Any]) -> None:
        self.results.append(result)
        self.skipped += 1

    def summary(self, total: int) -> dict[str, Any]:
        """The reply: results, processed, remaining (of `total`), imported, failed (skipped rows not counted), path."""
        imported = sum(1 for result in self.results if result["ok"])
        path_name = "mixed" if len(self.paths) > 1 else (next(iter(self.paths)) if self.paths else "unpack")
        processed = len(self.results)
        return {
            "results": self.results, "processed": processed, "remaining": total - processed, "imported": imported,
            "failed": processed - imported - self.skipped, "path": path_name,
        }  # fmt: skip


def check_pairs(pairs: list[tuple[str, str]]) -> None:
    """ApiError 400 for more than MAX_CODES pairs or an unknown destination (checked before anything is imported)."""
    if len(pairs) > MAX_CODES:
        raise ApiError(400, f"at most {MAX_CODES} codes at a time")
    if any(destination not in DESTINATIONS for _code, destination in pairs):
        raise ApiError(400, "destination must be backpack or bank")


def _import(pairs: list[tuple[str, str]], budget: Budget) -> dict[str, Any]:
    check_pairs(pairs)
    batch = ImportBatch()
    for code, destination in pairs:
        if budget.spent():
            break
        budget.step()
        batch.run(code, destination)
    return batch.summary(len(pairs))


def import_pairs(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Imports EVERY (code, destination), with no time budget. See the module docstring for the result and errors."""
    return _import(pairs, Budget(unlimited=True))


def import_items(codes: list[str], destination: str = "backpack") -> dict[str, Any]:
    """Imports every code into one destination, with no time budget. See import_pairs."""
    return import_pairs([(code, destination) for code in codes])


def import_chunk(codes: list[str], destination: str = "backpack") -> dict[str, Any]:
    """Imports the first codes that fit in one request's budget (see the module docstring)."""
    return _import([(code, destination) for code in codes], Budget())


# --- Routes ----------------------------------------------------------------------------------------------


def export_route(params: Params) -> dict[str, Any]:
    item_id = params.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise ApiError(400, "'id' is required")
    if item_id.startswith("bank:"):
        index, entry = bank.find_bank_entry(item_id)
        name = bank.entry_card(index, entry)[0]["name"]
        source, unique_id = bank.export_source(entry), bank.entry_unique_id(entry)
    else:
        item, _location = gear_inventory._find_item(params)  # noqa: SLF001
        name, source, unique_id = item.GetHumanReadableName(), item, stats.unique_id(item)
    try:
        code, header, warnings = export_code(source)
    except item_serial.SerialError as ex:
        raise ApiError(409, f"the game's serial for {name} could not be read ({ex})") from None
    if header is None:
        view = {"kind": None, "unique_id": unique_id, "checksum_ok": False}
    else:
        view = {"kind": _kind(header), "unique_id": header["unique_id"], "checksum_ok": header["checksum_ok"]}
    return {"id": item_id, "name": name, "code": code, "header": view, "warnings": warnings}


def preview_route(params: Params) -> dict[str, Any]:
    text = params.get("text", "")
    if not isinstance(text, str):
        raise ApiError(400, "'text' must be text")
    start = params.get("start", 0)
    if isinstance(start, str):  # a query string value
        if not start.strip().isdigit():
            raise ApiError(400, "'start' must be a whole number")
        start = int(start)
    return decode_chunk(text, start)


def import_route(body: Params) -> dict[str, Any]:
    code_list = body.get("codes")
    destination = body.get("destination", "backpack")
    if not isinstance(code_list, list) or not all(isinstance(code, str) for code in code_list):
        raise ApiError(400, "'codes' must be a list of text codes")
    return import_chunk(code_list, destination)


def _record(report: dict[str, Any], key: str, read: Callable[[], Any]) -> None:
    try:
        report[key] = read()
    except Exception as ex:  # noqa: BLE001
        report[key] = f"<error {ex!r}>"


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "_path_name"):
        return value._path_name()  # noqa: SLF001
    return f"<{type(value).__name__}>"


def _serial_fields(serial: Any) -> dict[str, Any]:
    return {
        "EncryptedLength": int(serial.EncryptedLength), "RunningCounter": int(serial.RunningCounter),
        "State": int(serial.State), "Buffer": [int(v) & 0xFF for v in serial.Buffer],
    }  # fmt: skip


def _round_trip(item: Any, template: Any | None) -> dict[str, Any]:
    """Exports the item, decodes the bytes again, and compares every definition field with the live item."""
    weapon = gear_inventory._is_weapon(item)  # noqa: SLF001
    data = serial_bytes(item.CreateSerialNumber())
    definition, _is_weapon, path = definition_of_code_bytes(data, template)
    fields = WEAPON_DEFINITION_FIELDS if weapon else ITEM_DEFINITION_FIELDS
    mismatches = []
    for field in fields:
        original, decoded = _plain(getattr(item.DefinitionData, field, None)), _plain(getattr(definition, field, None))
        if original != decoded:
            mismatches.append({"field": field, "item": original, "decoded": decoded})
    return {"path": path, "fields_checked": len(fields), "mismatches": mismatches}


def _debug_item(item: Any, template: Any | None) -> dict[str, Any]:
    row: dict[str, Any] = {"name": _safe(item.GetHumanReadableName), "unique_id": stats.unique_id(item)}
    _record(row, "serial", lambda: _serial_fields(item.CreateSerialNumber()))  # UNVERIFIED
    _record(row, "serial_string", item.GetSerialNumberString)  # UNVERIFIED
    _record(row, "export", lambda: _export_summary(item))
    _record(row, "round_trip", lambda: _round_trip(item, template))
    return row


def _export_summary(item: Any) -> dict[str, Any]:
    code, header, warnings = export_code(item)
    return {"code": code, "header": header, "warnings": warnings}


def _debug_bank(manager: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    bank_obj = _safe(lambda: manager.TheBank)
    out["path"] = _safe(lambda: _path(bank_obj)) if bank_obj is not None else None
    if bank_obj is None:
        out["note"] = "TheBank is None"
        return out
    for name in ("ChestSlots", "MaxSlots"):
        _record(out, name, lambda n=name: _plain(getattr(bank_obj, n)))  # UNVERIFIED fields
    for name in ("GetSize", "GetMaxSize"):
        _record(out, name, lambda n=name: getattr(bank_obj, n)())  # UNVERIFIED calls
    _record(out, "chest_length", lambda: len(bank_obj.TheChest))
    entries = []
    for index, entry in list(enumerate(bank_obj.TheChest))[:DEBUG_BANK_ENTRIES]:
        row: dict[str, Any] = {"index": index}
        _record(row, "inventory", lambda e=entry: _plain(e.Inventory))
        _record(row, "inventory_class", lambda e=entry: e.InventoryClass.Name)
        _record(row, "header", lambda e=entry: item_serial.read_header(serial_bytes(e.InventorySerialNumber)))
        entries.append(row)
    out["first_entries"] = entries
    out["content_entries"] = len(bank.bank_entries()) if out.get("path") else 0
    return out


def debug_codes(_params: Params) -> dict[str, Any]:
    """Writes rtse/debug_codes.json: a few backpack items' serials, codes and round trips, and the bank's shape."""
    report: dict[str, Any] = {"unverified": UNVERIFIED, "last_paths": LAST_PATHS, "items": [], "bank": {}}
    counts = {"items": 0, "bank": 0}
    try:
        manager = gear_inventory._manager()  # noqa: SLF001
    except ApiError as ex:
        report["error"] = str(ex)
        manager = None
    if manager is not None:
        try:
            found, report["inventory_warnings"] = gear_inventory._inventory(manager)  # noqa: SLF001
        except Exception as ex:  # noqa: BLE001
            found, report["inventory_warnings"] = [], [repr(ex)]
        counts["items"] = len(found)
        template = _template()
        for item, location in [pair for pair in found if pair[1] == gear_inventory.BACKPACK][:DEBUG_ITEMS]:
            report["items"].append(_debug_item(item, template))
        report["bank"] = _debug_bank(manager)
        counts["bank"] = report["bank"].get("content_entries", 0) if isinstance(report["bank"], dict) else 0
    DEBUG_CODES_FILE.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_CODES_FILE), "items": counts["items"], "bank": counts["bank"]}


ROUTE_ERROR = ApiError

GET_ROUTES: dict[str, Route] = {
    "/api/codes/export": export_route,
    "/api/codes/preview": preview_route,
    "/api/debug/codes": debug_codes,
}
POST_ROUTES: dict[str, Route] = {"/api/codes/import": import_route}
