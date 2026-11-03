"""The bank (the player's storage chest): reads its entries as gear cards, and adds, finds and removes entries.

Bank ids are "bank:<index in TheChest>:<unique_id>". The UniqueId is checked on every lookup, so an id whose entry
moved gets a 404 ("reload") rather than the wrong item.

Verified offline: nothing here (this module is game-only). Every game call is UNVERIFIED: TheBank, TheChest and its
ChestData fields (Inventory, InventorySerialNumber, InventoryClass), GetMaxSize / MaxSlots, AddWeaponFromDef /
AddItemFromDef, RemoveWeaponsByDef / RemoveItemsByDef and `del TheChest[index]`. A failed call becomes an ApiError
or a warning, never a crash.

Every removal goes through remove_exact. The game's own Remove(Weapons|Items)ByDef runs first; `del TheChest[index]`
(a direct array edit nobody has seen the bank UI or the save follow) is only the fallback, and the reply says which
one ran ("method"). Snapshots before and after must differ by exactly the target entry; anything else the game took
is put back from the removed entries' own serial bytes, and every put-back is itself verified (put_back).
codes.add_verified does the same for adds.

Reading the bank (/api/bank) decodes serial-only entries through the game, so it is budgeted (codes.Budget) and
cached by serial bytes: entries not decoded yet come back as placeholder cards ("pending": true), the reply counts
them in "pending", and the page asks again until none are left.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any

from unrealsdk import logging

from . import codes, gear_inventory, item_serial, stats
from .routing import ApiError, Params, Route

UNVERIFIED = (
    "Bank access and bank items use game calls nobody has tested in a live game yet "
    "(TheBank, TheChest, GetMaxSize, AddWeaponFromDef, RemoveWeaponsByDef)"
)
METHOD_GAME = "game"  # remove_exact used the game's RemoveWeaponsByDef / RemoveItemsByDef
METHOD_DIRECT = "direct array edit (unverified)"  # remove_exact fell back to `del TheChest[index]`
CARD_CACHE_MAX = 512  # decoded bank cards kept, by serial bytes (least recently used dropped first)
PENDING_NAME = "Not decoded yet"


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


# Decoded cards by serial bytes: the same bytes always decode to the same item, so nothing is ever invalidated.
# Stored without the per-request id / location / UniqueId (re-applied on every read). Undecodable bytes are cached
# too (as the "Unreadable item" card with its warning), so one bad entry cannot use up every request's budget.
_CARD_CACHE: OrderedDict[bytes, tuple[dict[str, Any], str | None]] = OrderedDict()
_PER_READ = ("id", "location", "unique_id")


def clear_card_cache() -> None:
    _CARD_CACHE.clear()


def _cache_get(data: bytes) -> tuple[dict[str, Any], str | None] | None:
    hit = _CARD_CACHE.get(data)
    if hit is not None:
        _CARD_CACHE.move_to_end(data)
    return hit


def _cache_put(data: bytes, card: dict[str, Any], warning: str | None) -> None:
    _CARD_CACHE[data] = ({key: value for key, value in card.items() if key not in _PER_READ}, warning)
    _CARD_CACHE.move_to_end(data)
    while len(_CARD_CACHE) > CARD_CACHE_MAX:
        _CARD_CACHE.popitem(last=False)


def needs_decode(entry: Any) -> bool:
    """True when entry_card would have to ask the game to decode this entry (serial-only, readable, not cached)."""
    if entry.Inventory is not None:
        return False
    try:
        return codes.serial_bytes(entry.InventorySerialNumber) not in _CARD_CACHE  # UNVERIFIED field
    except Exception:  # noqa: BLE001
        return False


def entry_card(index: int, entry: Any, *, decode: bool = True) -> tuple[dict[str, Any], str | None]:
    """The gear card for a bank entry, and a warning when it had to be built from the serial (or could not be).

    A serial-only entry is decoded by the game once and then served from the cache. With decode=False an entry the
    cache does not hold is not decoded: it gets the placeholder card (pending_card).
    """
    unique_id = entry_unique_id(entry)
    card_id = bank_id(index, unique_id)
    if entry.Inventory is not None:
        return gear_inventory.card_of(entry.Inventory, gear_inventory.BANK, card_id), None
    try:
        data = codes.serial_bytes(entry.InventorySerialNumber)
    except Exception as ex:  # noqa: BLE001
        return _unreadable_card(card_id, unique_id, entry), f"its serial could not be read ({ex})"
    hit = _cache_get(data)
    if hit is not None:
        cached, warning = hit
        return {**cached, "id": card_id, "location": gear_inventory.BANK, "unique_id": unique_id}, warning
    if not decode:
        return pending_card(card_id, unique_id, entry), None
    card, warning = codes.card_for_bytes(data, card_id=card_id, location=gear_inventory.BANK, unique_id=unique_id)
    if card is None:
        card = _unreadable_card(card_id, unique_id, entry)
    _cache_put(data, card, warning)
    return card, warning


def _entry_class(entry: Any) -> str:
    try:
        return str(entry.InventoryClass.Name)  # UNVERIFIED field
    except Exception:  # noqa: BLE001
        return "WillowItem"


def _bare_card(card_id: str, unique_id: int, entry: Any, name: str) -> dict[str, Any]:
    return {
        "id": card_id, "class": _entry_class(entry), "name": name, "location": gear_inventory.BANK, "level": None,
        "rarity": None, "type_path": None, "build": [], "hide_bones": [], "unique_id": unique_id,
    }


def _unreadable_card(card_id: str, unique_id: int, entry: Any) -> dict[str, Any]:
    return _bare_card(card_id, unique_id, entry, "Unreadable item")


def pending_card(card_id: str, unique_id: int, entry: Any) -> dict[str, Any]:
    """The placeholder for an entry not decoded yet: its class (from InventoryClass when readable), its UniqueId,
    the name "Not decoded yet" and "pending": True."""
    return {**_bare_card(card_id, unique_id, entry, PENDING_NAME), "pending": True}


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


def identity_of(snap: Snap) -> tuple[Any, ...] | None:
    """The entry's identity signature (codes.definition_signature): from its live item's DefinitionData, else from the
    game decode of its serial bytes (UNVERIFIED). None when it cannot be read, which never matches anything."""
    try:
        if snap.entry is not None and snap.entry.Inventory is not None:
            item = snap.entry.Inventory
            return codes.signature_or_none(item.DefinitionData, gear_inventory._is_weapon(item))  # noqa: SLF001
        if snap.data is None:
            return None
        definition, is_weapon, _path = codes.definition_of_code_bytes(snap.data)  # UNVERIFIED game decode
        return codes.signature_or_none(definition, is_weapon)
    except Exception as ex:  # noqa: BLE001
        logging.warning(f"RTSE: could not read the identity of bank entry {snap.index}: {ex!r}")
        return None


def _gained_entry(before: list[Snap], unique_id: int) -> Snap | None:
    """The one entry with `unique_id` that is new since `before`, or None when there is not exactly one."""
    new = [snap for snap in diff(before, snapshot())[1] if snap.unique_id == unique_id]
    return new[0] if len(new) == 1 else None


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


@dataclass
class PutBack:
    """What put_back did. `restored`: entries verified back in the bank ("UniqueId N"). `failed`: the codes (or notes)
    of entries that are NOT back. `notes`: anything else the user must know (an extra entry left in the bank)."""

    restored: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def remove_gained(reference: list[Snap], keep: Counter[int] | None = None, *, depth: int | None = 0) -> list[str]:
    """Removes every entry the bank gained since the snapshot `reference` (as many per UniqueId as it gained, less
    `keep`), one remove_exact at a time. Each removal may also take the other entries still to remove (`allowed`), so
    a ByDef that takes all new twins at once is fine. `depth` is the remove_exact depth; None only reports (no
    removal), which is how a put-back inside a removal stops the recursion. Returns problems ([] when all are gone)."""
    keep = keep or Counter()
    problems: list[str] = []
    for _pass in range(len(reference) + 64):  # every pass removes at least one entry or stops; a hard stop anyway
        now = snapshot()
        extra = uid_changes(reference, now)[1] - keep
        if not extra:
            return problems
        if depth is None:
            problems.append(f"entries that appeared were left in the bank (UniqueIds {sorted(extra.elements())})")
            return problems
        pick = codes._take(diff(reference, now)[1], extra, lambda s: s.unique_id)[0]  # noqa: SLF001  (last one first)
        try:
            remove_exact(pick.index, pick.entry, depth=depth, allowed=extra - Counter({pick.unique_id: 1}))
        except ApiError as ex:
            problems.append(f"an entry with UniqueId {pick.unique_id} that appeared could not be removed again ({ex})")
            return problems
    problems.append("gave up removing the entries that appeared")
    return problems


def _put_back_one(snap: Snap, depth: int, out: PutBack) -> None:
    """Re-adds one entry and verifies it by snapshots: the bank ends up with exactly one more entry, with its UniqueId
    AND its identity (every definition field, codes.definition_signature); a same-UniqueId entry that is a different
    item is removed again and counts as NOT restored.

    An add that raised is never trusted: whatever it added is removed again and the entry counts as NOT restored. An
    add that returned False is accepted only when the snapshots show exactly the expected entry. When the add put in
    more than one entry, the extras are removed (one with the UniqueId is kept unless the add raised). Entries that
    vanished during the add are reported by code. UNVERIFIED game calls (decode, Add*FromDef).
    """
    uid = snap.unique_id
    if snap.data is None:
        out.failed.append(describe(snap))
        return
    try:
        definition, is_weapon, _path = codes.definition_of_code_bytes(snap.data)  # UNVERIFIED game decode
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: decoding a bank entry to put it back failed: {ex!r}")
        out.failed.append(describe(snap))
        return
    expected = codes.signature_or_none(definition, is_weapon)
    before = snapshot()
    raised: Exception | None = None
    said = False
    try:
        said = add_def_to_bank(definition, is_weapon)  # UNVERIFIED call
    except Exception as ex:  # noqa: BLE001
        raised = ex
        logging.error(f"RTSE: putting a bank entry back raised: {ex!r}")
    _lost, gained = uid_changes(before, snapshot())
    keep = Counter()
    if raised is None and gained[uid] >= 1:
        # Keep one only if one of the new entries with this UniqueId really is the expected item.
        candidates = [s for s in diff(before, snapshot())[1] if s.unique_id == uid]
        if any(codes.matches(identity_of(s), expected) for s in candidates):
            keep = Counter({uid: 1})
        else:
            out.notes.append(f"the entry put back with UniqueId {uid} was a different item, so it was removed again")
    # Extras are removed one level down (depth + 1, which may put back but never removes again); deeper, reported.
    out.notes.extend(remove_gained(before, keep, depth=depth + 1 if depth == 0 else None))
    final = snapshot()
    lost_f, gained_f = uid_changes(before, final)
    survivor = _gained_entry(before, uid) if gained_f == Counter({uid: 1}) else None
    if raised is None and survivor is not None and codes.matches(identity_of(survivor), expected):
        out.restored.append(f"UniqueId {uid}")
        if not said:
            logging.warning(f"RTSE: the bank add for UniqueId {uid} returned False, but the snapshots show the entry")
    else:
        out.failed.append(describe(snap))
        if gained_f[uid] > 0:
            out.notes.append(f"an entry with UniqueId {uid} is in the bank but could not be verified")
    if lost_f:  # the put-back add took something else out of the bank: name it by code
        vanished = codes._take(diff(before, final)[0], lost_f, lambda s: s.unique_id)  # noqa: SLF001
        out.failed.extend(describe(gone) for gone in vanished)


def put_back(gone: list[Snap], lost: Counter[int], *, depth: int = 0) -> PutBack:
    """Re-adds removed entries from their own serial bytes (game decode + Add*FromDef, so the UniqueId is kept).

    Only as many entries per UniqueId as the bank really lost are re-added, so an entry the game merely re-serialized
    is not doubled. Each re-add is verified on its own (_put_back_one); nothing counts as restored unless the
    snapshots show it. `depth` > 0 means this runs inside a removal that a put-back started: extra entries are then
    reported instead of removed, so the recursion stops there. UNVERIFIED game calls.
    """
    out = PutBack()
    wanted = Counter(lost)
    for snap in gone:
        if wanted[snap.unique_id] <= 0:
            continue
        wanted[snap.unique_id] -= 1
        try:
            _put_back_one(snap, depth, out)
        except Exception as ex:  # noqa: BLE001  (a snapshot failed mid-way: the entry is not known to be back)
            logging.error(f"RTSE: putting a bank entry back failed: {ex!r}")
            out.failed.append(describe(snap))
    return out


def restore_message(problem: str, result: PutBack) -> str:
    """A 409 message after a rollback: what was put back (verified), and the codes the user must re-import."""
    if not result.failed:
        restored = f" ({', '.join(result.restored)})" if result.restored else ""
        message = f"{problem}; everything it removed was put back{restored}"
    else:
        put = f"put back: {', '.join(result.restored)}; " if result.restored else ""
        message = f"{problem}; {put}these could NOT be put back, re-import them: {', '.join(result.failed)}"
    if result.notes:
        message += f"; {'; '.join(result.notes)}"
    return message


def _remove_by_def(bank: Any, entry: Any) -> None:
    definition, is_weapon = entry_definition(entry)
    if is_weapon:
        bank.RemoveWeaponsByDef(definition)  # UNVERIFIED call: may remove EVERY entry with this definition
    else:
        bank.RemoveItemsByDef(definition)  # UNVERIFIED call: may remove EVERY entry with this definition


def _changed(before: list[Snap], after: list[Snap]) -> bool:
    """The bank lost or gained an entry (an entry the game merely re-serialized is not a change)."""
    lost, gained = uid_changes(before, after)
    return bool(lost or gained)


def remove_exact(index: int, entry: Any, *, depth: int = 0, allowed: Counter[int] | None = None) -> str:
    """Removes the bank entry at `index` and nothing else. Returns how: METHOD_GAME or METHOD_DIRECT.

    1. Remove(Weapons|Items)ByDef, the game's own removal (UNVERIFIED). It may take every identical entry, so the
       extras it took are put back (each one verified). OK only if exactly the target ends up gone.
    2. Only when that call is missing, raises or removes nothing: `del TheChest[index]` (UNVERIFIED direct array edit;
       nobody has confirmed the bank UI or the save follow it), and the method is METHOD_DIRECT.
    Any other outcome puts back everything that went and raises ApiError 409, listing what was put back and, by code,
    what was NOT. 404 when the slot no longer holds that item; 409 "still in the bank" when neither way removed it.
    `allowed` (UniqueId counts) may go along with the target without being put back: remove_gained uses it when
    several new entries are being removed anyway. `depth` > 0 only when a put-back calls this (see put_back).
    """
    bank = _the_bank()
    before = snapshot()
    target = next((snap for snap in before if snap.index == index), None)
    if target is None or target.unique_id != entry_unique_id(entry):
        raise ApiError(404, "that bank item moved; reload")
    problems: list[str] = []
    method = METHOD_GAME
    try:
        _remove_by_def(bank, entry)
    except Exception as ex:  # noqa: BLE001
        problems.append(f"removing by definition failed ({ex})")
    after = snapshot()
    if not _changed(before, after):
        if not problems:
            problems.append("removing by definition removed nothing")
        method = METHOD_DIRECT
        try:
            del bank.TheChest[index]  # UNVERIFIED: direct array delete through pyunrealsdk (WrappedArray.__delitem__)
        except Exception as ex:  # noqa: BLE001
            problems.append(f"deleting the slot directly failed ({ex})")
        after = snapshot()
        if not _changed(before, after):
            raise ApiError(409, f"the game did not remove that bank item; it is still in the bank ({'; '.join(problems)})")
    return _settle(before, after, target, method, depth, allowed or Counter())


def _settle(before: list[Snap], after: list[Snap], target: Snap, method: str, depth: int, allowed: Counter[int]) -> str:
    """After a removal changed the bank: keep the change when exactly the target is gone (putting back, verified, any
    extras the game took); otherwise put back everything that went and raise 409."""
    wanted = Counter({target.unique_id: 1})
    lost, gained = uid_changes(before, after)
    first = PutBack()
    if lost[target.unique_id] >= 1 and not gained:
        extra_lost = lost - wanted - allowed
        if not extra_lost:
            return method
        gone = diff(before, after)[0]
        # Leave one entry like the target out of the put-back: that one is the removal we wanted. put_back re-adds
        # only `extra_lost` per UniqueId, so the allowed ones stay out too.
        mine = next((i for i, snap in enumerate(gone) if snap.key == target.key), None)
        if mine is None:
            mine = next(i for i, snap in enumerate(gone) if snap.unique_id == target.unique_id)
        first = put_back(gone[:mine] + gone[mine + 1:], extra_lost, depth=depth)
        lost_now, gained_now = uid_changes(before, snapshot())
        if lost_now[target.unique_id] >= 1 and not (lost_now - wanted - allowed) and not gained_now and not first.failed:
            return method
        problem = (
            f"removing one bank item also took {sum(extra_lost.values())} other entries and putting them back did not "
            "fully work, so the removal was undone"
        )
    elif gained:
        problem = (
            f"removing one bank item made entries appear (UniqueIds {sorted(gained)}, left in place), so it was undone"
        )
    elif lost:
        problem = f"removing one bank item took other entries instead (UniqueIds {sorted(lost)}), so it was undone"
    else:
        problem = "the bank changed while removing one item, so it was undone"
    final = snapshot()
    lost_f, _gained_f = uid_changes(before, final)
    undo = put_back(diff(before, final)[0], lost_f, depth=depth)
    # The undo re-tries anything the first put-back missed, so its `failed` is the full list of what is NOT back.
    undo.restored = [*first.restored, *undo.restored]
    undo.notes = [*first.notes, *undo.notes]
    raise ApiError(409, restore_message(problem, undo))


# --- Routes ----------------------------------------------------------------------------------------


def _closed(reason: str) -> dict[str, Any]:
    return {
        "available": False, "reason": reason, "slots": {"used": 0, "max": None}, "items": [], "warnings": [],
        "pending": 0, "unverified": UNVERIFIED,
    }


def bank_get(_params: Params) -> dict[str, Any]:
    """The bank as cards. Serial-only entries the card cache does not hold are decoded until this request's budget
    (codes.Budget: CHUNK_SECONDS, CHUNK_STEPS) runs out; the rest get placeholder cards and `pending` counts them."""
    try:
        bank = gear_inventory._manager().TheBank  # UNVERIFIED property
    except ApiError as ex:
        return _closed(str(ex))
    if bank is None:
        return _closed("the bank is not loaded: open it in the game first")
    entries = _content_entries(bank)
    budget = codes.Budget()
    cards: list[dict[str, Any]] = []
    warnings: list[str] = []
    pending = 0
    for index, entry in entries:
        decode = True
        if needs_decode(entry):
            if budget.spent():
                decode = False
                pending += 1
            else:
                budget.step()
        card, warning = entry_card(index, entry, decode=decode)
        cards.append(card)
        if warning:
            warnings.append(f"{card['name']}: {warning}")
    return {
        "available": True, "reason": None,
        "slots": {"used": len(entries), "max": max_slots(bank)},
        "items": cards, "warnings": warnings, "pending": pending, "unverified": UNVERIFIED,
    }


ROUTE_ERROR = ApiError

GET_ROUTES: dict[str, Route] = {"/api/bank": bank_get}
POST_ROUTES: dict[str, Route] = {}
