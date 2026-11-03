"""The bank (the player's storage chest): reads its entries as gear cards, and adds, finds and removes entries.

Bank ids are "bank:<index in TheChest>:<unique_id>". The UniqueId is checked on every lookup, so an id whose entry
moved gets a 404 ("reload") rather than the wrong item.

Verified offline: nothing here (this module is game-only). Every game call is UNVERIFIED: TheBank, TheChest and its
ChestData fields (Inventory, InventorySerialNumber, InventoryClass), GetMaxSize / MaxSlots, AddWeaponFromDef /
AddItemFromDef, `del TheChest[index]` and RemoveWeaponsByDef / RemoveItemsByDef. A failed call becomes an ApiError
or a warning, never a crash.

Every removal goes through remove_exact: a snapshot before and after must differ by exactly the target entry, and
anything else is put back from the removed entries' own serial bytes. codes.add_verified does the same for adds.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from unrealsdk import logging

from . import codes, gear_inventory, item_serial, stats
from .routing import ApiError, Params, Route

UNVERIFIED = (
    "Bank access and bank items use game calls nobody has tested in a live game yet "
    "(TheBank, TheChest, GetMaxSize, AddWeaponFromDef, RemoveWeaponsByDef)"
)


def bank_id(index: int, unique_id: int) -> str:
    return f"bank:{index}:{unique_id}"


def _the_bank() -> Any:
    bank = gear_inventory._manager().TheBank  # UNVERIFIED property
    if bank is None:
        raise ApiError(409, "the bank is not loaded; open it in the game first")
    return bank


def _has_content(entry: Any) -> bool:
    """A chest slot holds something: an Inventory object, or a serial with a length."""
    if entry.Inventory is not None:
        return True
    try:
        return int(entry.InventorySerialNumber.EncryptedLength) > 0  # UNVERIFIED field
    except Exception:  # noqa: BLE001
        return False


def _content_entries(bank: Any) -> list[tuple[int, Any]]:
    """(index in TheChest, entry) for every slot that holds something. The index is the id's index."""
    chest = bank.TheChest  # UNVERIFIED property
    if chest is None:
        return []
    return [(index, entry) for index, entry in enumerate(chest) if _has_content(entry)]


def bank_entries() -> list[tuple[int, Any]]:
    """The bank's occupied entries as (index in TheChest, ChestData). ApiError 409 when the bank is not loaded."""
    return _content_entries(_the_bank())


def max_slots(bank: Any) -> int | None:
    """The bank's total size. UNVERIFIED: GetMaxSize, then MaxSlots. None when neither can be read."""
    for read in (lambda: bank.GetMaxSize(), lambda: bank.MaxSlots):
        try:
            return int(read())
        except Exception:  # noqa: BLE001, S112
            continue
    return None


def capacity() -> int | None:
    """The bank's total size (None when unknown). ApiError 409 when the bank is not loaded."""
    return max_slots(_the_bank())


def entry_unique_id(entry: Any) -> int:
    """The UniqueId of a bank entry: its Inventory's, else the one in its serial. 0 when unreadable."""
    if entry.Inventory is not None:
        return stats.unique_id(entry.Inventory)
    try:
        return int(item_serial.read_header(codes.serial_bytes(entry.InventorySerialNumber))["unique_id"])
    except Exception:  # noqa: BLE001
        return 0


def export_source(entry: Any) -> Any:
    """What codes.export_code should read: the live item when there is one, else the chest entry (its serial)."""
    return entry.Inventory if entry.Inventory is not None else entry


def entry_mark(entry: Any) -> int:
    return gear_inventory.mark_of(entry.Inventory) if entry.Inventory is not None else codes.MARK_STANDARD


def entry_card(index: int, entry: Any) -> tuple[dict[str, Any], str | None]:
    """The gear card for a bank entry, and a warning when it had to be built from the serial (or could not be)."""
    unique_id = entry_unique_id(entry)
    card_id = bank_id(index, unique_id)
    if entry.Inventory is not None:
        return gear_inventory.card_of(entry.Inventory, gear_inventory.BANK, card_id), None
    try:
        data = codes.serial_bytes(entry.InventorySerialNumber)
    except Exception as ex:  # noqa: BLE001
        return _unreadable_card(card_id, unique_id, entry), f"its serial could not be read ({ex})"
    card, warning = codes.card_for_bytes(data, card_id=card_id, location=gear_inventory.BANK, unique_id=unique_id)
    return (card if card is not None else _unreadable_card(card_id, unique_id, entry)), warning


def _unreadable_card(card_id: str, unique_id: int, entry: Any) -> dict[str, Any]:
    try:
        cls = entry.InventoryClass.Name  # UNVERIFIED field
    except Exception:  # noqa: BLE001
        cls = "WillowItem"
    return {
        "id": card_id, "class": cls, "name": "Unreadable item", "location": gear_inventory.BANK, "level": None,
        "rarity": None, "type_path": None, "build": [], "hide_bones": [], "unique_id": unique_id,
    }


def entry_definition(entry: Any) -> tuple[Any, bool]:
    """(definition, is_weapon) for a bank entry: from its live item, or decoded from its serial. ApiError 409 if unreadable."""
    if entry.Inventory is not None:
        item = entry.Inventory
        return item.DefinitionData, gear_inventory._is_weapon(item)
    try:
        definition, is_weapon, _path = codes.definition_of_code_bytes(codes.serial_bytes(entry.InventorySerialNumber))
    except Exception as ex:  # noqa: BLE001
        raise ApiError(409, f"could not read that bank item's definition ({ex})") from None
    return definition, is_weapon


def find_bank_entry(item_id: str) -> tuple[int, Any]:
    """The (index, entry) for a bank id. 400 for a malformed id, 404 when the entry moved or is gone."""
    parts = item_id.split(":")
    try:
        if len(parts) != 3 or parts[0] != "bank":
            raise ValueError(item_id)
        index, unique_id = int(parts[1]), int(parts[2])
    except ValueError:
        raise ApiError(400, f"not a bank id: {item_id}") from None
    for entry_index, entry in bank_entries():
        if entry_index == index and entry_unique_id(entry) == unique_id:
            return entry_index, entry
    raise ApiError(404, "that bank item moved; reload")


def find_by_unique_id(unique_id: int) -> tuple[int, Any] | None:
    for index, entry in bank_entries():
        if entry_unique_id(entry) == unique_id:
            return index, entry
    return None


def bank_unique_ids() -> set[int]:
    """UniqueIds of every bank entry. Empty when the bank is not loaded (or cannot be read)."""
    try:
        entries = bank_entries()
    except ApiError:
        return set()
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: reading the bank UniqueIds failed: {ex!r}")
        return set()
    return {entry_unique_id(entry) for _index, entry in entries}


def add_def_to_bank(definition: Any, is_weapon: bool) -> bool:
    """Adds a definition to the bank as a new entry. False when the game refuses (for example, the bank is full)."""
    bank = _the_bank()
    if is_weapon:
        return bool(bank.AddWeaponFromDef(definition, False, False))  # UNVERIFIED call
    return bool(bank.AddItemFromDef(definition, False, False))  # UNVERIFIED call


def _balance_path(definition: Any) -> str | None:
    try:
        balance = definition.BalanceDefinition
        return None if balance is None else balance._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None


# --- Snapshots and the verified removal ---------------------------------------------------------------


@dataclass(frozen=True)
class Snap:
    """One occupied bank slot as it was when the snapshot was taken.

    `data` is the entry's serial bytes (its live item's CreateSerialNumber, else its InventorySerialNumber; UNVERIFIED)
    or None when they cannot be read. `balance` is read only from a live item (cheap); a serial-only entry's balance is
    inside `data`, which identifies it. `entry` is the ChestData as read (not compared).
    """

    index: int
    unique_id: int
    balance: str | None
    data: bytes | None
    entry: Any = field(compare=False, repr=False)

    @property
    def key(self) -> tuple[int, str | None, bytes | None]:
        """What two snapshots compare: the same UniqueId, balance and bytes is the same item."""
        return self.unique_id, self.balance, self.data


def _entry_bytes(entry: Any) -> bytes | None:
    try:
        if entry.Inventory is not None:
            return codes.serial_bytes(entry.Inventory.CreateSerialNumber())  # UNVERIFIED call
        return codes.serial_bytes(entry.InventorySerialNumber)  # UNVERIFIED field
    except Exception:  # noqa: BLE001
        return None


def _snap(index: int, entry: Any) -> Snap:
    balance = None
    if entry.Inventory is not None:
        balance = _balance_path(getattr(entry.Inventory, "DefinitionData", None))
    return Snap(index, entry_unique_id(entry), balance, _entry_bytes(entry), entry)


def snapshot() -> list[Snap]:
    """Every occupied bank slot, in TheChest order. ApiError 409 when the bank is not loaded."""
    return [_snap(index, entry) for index, entry in bank_entries()]


def diff(before: list[Snap], after: list[Snap]) -> tuple[list[Snap], list[Snap]]:
    """(gone, new) as multisets by `key`: the snaps of `before` with no match left in `after`, and the snaps of `after`
    with no match left in `before`. Two identical twins count twice."""

    def unmatched(side: list[Snap], other: list[Snap]) -> list[Snap]:
        left = Counter(snap.key for snap in other)
        out: list[Snap] = []
        for snap in side:
            if left[snap.key] > 0:
                left[snap.key] -= 1
            else:
                out.append(snap)
        return out

    return unmatched(before, after), unmatched(after, before)


def uid_changes(before: list[Snap], after: list[Snap]) -> tuple[Counter[int], Counter[int]]:
    """(lost, gained) UniqueId counts between two snapshots. An entry the game only re-serialized changes neither."""
    old = Counter(snap.unique_id for snap in before)
    now = Counter(snap.unique_id for snap in after)
    return old - now, now - old


def describe(snap: Snap) -> str:
    """The entry's code (to re-import it), or a note that it has none."""
    if snap.data is not None:
        try:
            return item_serial.bytes_to_code(snap.data)
        except item_serial.SerialError:
            pass
    return f"(an entry with UniqueId {snap.unique_id} whose code could not be read)"


def put_back(gone: list[Snap], lost: Counter[int]) -> list[str]:
    """Re-adds removed entries from their own serial bytes (game decode + Add*FromDef, so the UniqueId is kept).

    Only as many entries per UniqueId as the bank really lost are re-added, so an entry the game merely re-serialized
    is not doubled. Each re-add is checked (the bank gains exactly that UniqueId once). Returns the codes (or notes)
    of the entries that could not be put back. UNVERIFIED game calls.
    """
    failed: list[str] = []
    wanted = Counter(lost)
    for snap in gone:
        if wanted[snap.unique_id] <= 0:
            continue
        wanted[snap.unique_id] -= 1
        if snap.data is None:
            failed.append(describe(snap))
            continue
        try:
            definition, is_weapon, _path = codes.definition_of_code_bytes(snap.data)  # UNVERIFIED game decode
            before = snapshot()
            added = add_def_to_bank(definition, is_weapon)  # UNVERIFIED call
            vanished, gained = uid_changes(before, snapshot())
            restored = added and gained == Counter({snap.unique_id: 1}) and not vanished
        except Exception as ex:  # noqa: BLE001
            logging.error(f"RTSE: putting a bank entry back failed: {ex!r}")
            restored = False
        if not restored:
            failed.append(describe(snap))
    return failed


def restore_message(problem: str, failed: list[str]) -> str:
    """A 409 message after a rollback: everything was put back, or which codes the user must re-import."""
    if not failed:
        return f"{problem}; everything it removed was put back"
    return f"{problem}; these could NOT be put back, re-import them: {', '.join(failed)}"


def remove_exact(index: int, entry: Any) -> None:
    """Removes the bank entry at `index` and nothing else, or raises ApiError 409 with the bank as it was.

    First deletes that slot from TheChest (`del TheChest[index]`, UNVERIFIED). If that raises, falls back to
    Remove(Weapons|Items)ByDef (UNVERIFIED), which can remove every entry with the same definition. Either way the
    snapshots before and after must differ by exactly the target entry (same UniqueId and bytes; nothing else lost or
    gained). Otherwise every entry that went is put back from its own serial bytes and a 409 says so, naming by code
    any entry that could not be put back. 404 when the slot no longer holds that item.
    """
    bank = _the_bank()
    before = snapshot()
    target = next((snap for snap in before if snap.index == index), None)
    if target is None or target.unique_id != entry_unique_id(entry):
        raise ApiError(404, "that bank item moved; reload")
    problems: list[str] = []
    try:
        del bank.TheChest[index]  # UNVERIFIED: array delete through pyunrealsdk (WrappedArray.__delitem__)
    except Exception as ex:  # noqa: BLE001
        problems.append(f"deleting the slot failed ({ex})")
        try:
            definition, is_weapon = entry_definition(entry)
            if is_weapon:
                bank.RemoveWeaponsByDef(definition)  # UNVERIFIED call
            else:
                bank.RemoveItemsByDef(definition)  # UNVERIFIED call
        except Exception as ex2:  # noqa: BLE001
            problems.append(f"removing by definition failed ({ex2})")
    after = snapshot()
    gone, new = diff(before, after)
    if [snap.key for snap in gone] == [target.key] and not new:
        return
    if not gone and not new:
        detail = f" ({'; '.join(problems)})" if problems else ""
        raise ApiError(409, f"the game did not remove that bank item; it is still in the bank{detail}")
    lost, gained = uid_changes(before, after)
    problem = f"removing one bank item removed {len(gone)} entries and changed or added {len(new)}, so it was undone"
    if gained:
        problem += f" (entries that appeared were left in place: UniqueIds {sorted(gained)})"
    raise ApiError(409, restore_message(problem, put_back(gone, lost)))


# --- Routes ----------------------------------------------------------------------------------------


def _closed(reason: str) -> dict[str, Any]:
    return {
        "available": False, "reason": reason, "slots": {"used": 0, "max": None}, "items": [], "warnings": [],
        "unverified": UNVERIFIED,
    }


def bank_get(_params: Params) -> dict[str, Any]:
    try:
        bank = gear_inventory._manager().TheBank  # UNVERIFIED property
    except ApiError as ex:
        return _closed(str(ex))
    if bank is None:
        return _closed("the bank is not loaded: open it in the game first")
    entries = _content_entries(bank)
    cards: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, entry in entries:
        card, warning = entry_card(index, entry)
        cards.append(card)
        if warning:
            warnings.append(f"{card['name']}: {warning}")
    return {
        "available": True, "reason": None,
        "slots": {"used": len(entries), "max": max_slots(bank)},
        "items": cards, "warnings": warnings, "unverified": UNVERIFIED,
    }


ROUTE_ERROR = ApiError

GET_ROUTES: dict[str, Route] = {"/api/bank": bank_get}
POST_ROUTES: dict[str, Route] = {}
