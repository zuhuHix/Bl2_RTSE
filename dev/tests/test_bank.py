"""rtse/bank.py: the /api/bank card list, bank ids and their UniqueId check, adds into a full bank, snapshots and
their diff, and remove_exact: the exact-slot delete, the ByDef fallback, and the put-back (with the codes named when
it fails) whenever the bank does not lose exactly the target.

    python -I -m pytest dev/tests -k bank
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fakes_inventory import (
    PISTOL_BYTES,
    SHIELD_BYTES,
    Item,
    chest_entry,
    install,
    make_definition,
    serial_of,
)
from rtse import bank, codes
from rtse import item_serial as s
from rtse.routing import ApiError


def _uid(data: bytes) -> int:
    return s.read_header(data)["unique_id"]


# --- the bank as a card list -------------------------------------------------------------------------


def test_bank_is_unavailable_outside_a_save(game):
    out = bank.bank_get({})
    assert out["available"] is False and out["reason"] == "not in a save"
    assert out["slots"] == {"used": 0, "max": None} and out["items"] == []
    assert out["unverified"] == bank.UNVERIFIED


def test_bank_is_unavailable_when_the_bank_is_not_loaded(monkeypatch):
    install(monkeypatch, with_bank=False)
    out = bank.bank_get({})
    assert out["available"] is False and "not loaded" in out["reason"]


def test_bank_lists_serial_and_live_entries_and_skips_empty_slots(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))  # index 0: a serial
    world.bank.TheChest.append(SimpleNamespace(Inventory=None, InventorySerialNumber=serial_of(b""), InventoryClass=None))
    shield = Item(make_definition(77, weapon=False), weapon=False, name="Live shield", data=SHIELD_BYTES)
    world.bank.TheChest.append(SimpleNamespace(Inventory=shield, InventorySerialNumber=serial_of(b""), InventoryClass=None))
    out = bank.bank_get({})
    assert out["available"] is True and out["reason"] is None
    assert out["slots"] == {"used": 2, "max": 24}
    assert [card["id"] for card in out["items"]] == [f"bank:0:{_uid(PISTOL_BYTES)}", "bank:2:77"]
    assert out["items"][0]["class"] == "WillowWeapon" and out["items"][0]["location"] == "Bank"
    assert out["items"][1]["name"] == "Live shield" and out["items"][1]["unique_id"] == 77
    assert out["warnings"] == []


def test_undecodable_entry_is_listed_with_a_warning(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(SHIELD_BYTES, weapon=False))

    def no_classes(name):
        raise RuntimeError("no such class")

    monkeypatch.setattr(codes, "_cdo", no_classes)
    out = bank.bank_get({})
    assert out["items"][0]["name"] == "Unreadable item"
    assert out["items"][0]["id"].startswith("bank:0:")
    assert "could not decode" in out["warnings"][0]


def test_slots_max_is_none_when_the_game_gives_no_size(monkeypatch):
    world = install(monkeypatch, bank_max=None)
    out = bank.bank_get({})
    assert out["available"] is True and out["slots"]["max"] is None
    assert world.bank.TheChest == []


# --- ids -----------------------------------------------------------------------------------------------


def test_find_bank_entry_checks_the_unique_id(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    uid = _uid(PISTOL_BYTES)
    index, entry = bank.find_bank_entry(f"bank:0:{uid}")
    assert index == 0 and entry is world.bank.TheChest[0]
    with pytest.raises(ApiError) as moved:
        bank.find_bank_entry(f"bank:0:{uid + 1}")
    assert moved.value.status == 404 and "moved" in str(moved.value)
    with pytest.raises(ApiError) as malformed:
        bank.find_bank_entry("bank:x")
    assert malformed.value.status == 400


def test_bank_unique_ids_and_find_by_unique_id(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    world.bank.TheChest.append(chest_entry(SHIELD_BYTES, weapon=False))
    assert bank.bank_unique_ids() == {_uid(PISTOL_BYTES), _uid(SHIELD_BYTES)}
    index, _entry = bank.find_by_unique_id(_uid(SHIELD_BYTES))
    assert index == 1
    assert bank.find_by_unique_id(12345) is None


def test_bank_helpers_refuse_when_no_bank_is_loaded(monkeypatch):
    install(monkeypatch, with_bank=False)
    assert bank.bank_unique_ids() == set()
    with pytest.raises(ApiError) as info:
        bank.bank_entries()
    assert info.value.status == 409


# --- adds --------------------------------------------------------------------------------------------


def test_add_def_to_bank_reports_a_full_bank(monkeypatch):
    world = install(monkeypatch, bank_max=1)
    assert bank.add_def_to_bank(make_definition(3, weapon=True), True) is True
    assert bank.add_def_to_bank(make_definition(4, weapon=True), True) is False
    assert world.bank.used() == 1
    assert world.bank.calls[-1] == ("add", True, 4)


def test_capacity_reads_the_bank_size(monkeypatch):
    install(monkeypatch, bank_max=9)
    assert bank.capacity() == 9


# --- snapshots ---------------------------------------------------------------------------------------


def _uids(world) -> list[int]:
    return [bank.entry_unique_id(e) for e in world.bank.TheChest]


def test_snapshot_reads_index_uid_and_bytes_and_diff_counts_twins(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])
    before = bank.snapshot()
    assert [(snap.index, snap.unique_id, snap.data) for snap in before] == [
        (0, _uid(PISTOL_BYTES), PISTOL_BYTES), (1, _uid(PISTOL_BYTES), PISTOL_BYTES), (2, _uid(SHIELD_BYTES), SHIELD_BYTES),
    ]
    del world.bank.TheChest[0]
    gone, new = bank.diff(before, bank.snapshot())
    assert [snap.data for snap in gone] == [PISTOL_BYTES] and new == []
    lost, gained = bank.uid_changes(before, bank.snapshot())
    assert lost == {_uid(PISTOL_BYTES): 1} and not gained


# --- removals (review round 2: the game's ByDef is primary, `del TheChest[index]` only the fallback) -----------


def _paths(world) -> list[str]:
    """Which removal paths ran, in order: "by_def" and/or "del"."""
    return [{"remove_by_def": "by_def", "del": "del"}[c[0]] for c in world.bank.calls if c[0] in ("remove_by_def", "del")]


def test_remove_exact_uses_remove_by_def_first(monkeypatch):
    world = install(monkeypatch)
    other = s.with_unique_id(PISTOL_BYTES, 4242)
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(other), chest_entry(SHIELD_BYTES, weapon=False)])
    index, entry = bank.bank_entries()[1]
    assert bank.remove_exact(index, entry) == bank.METHOD_GAME == "game"
    assert _paths(world) == ["by_def"], "the direct array edit must not run when the game's removal worked"
    assert _uids(world) == [_uid(PISTOL_BYTES), _uid(SHIELD_BYTES)]


def test_by_def_taking_both_twins_puts_the_other_back_and_is_ok(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])
    index, entry = bank.bank_entries()[0]
    assert bank.remove_exact(index, entry) == "game"
    assert _paths(world) == ["by_def"]
    assert sorted(_uids(world)) == sorted([_uid(PISTOL_BYTES), _uid(SHIELD_BYTES)])
    assert ("add", True, _uid(PISTOL_BYTES)) in world.bank.calls, "the twin came back through a verified put-back"


def test_by_def_raising_falls_back_to_the_direct_array_edit(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])

    def boom(definition):
        raise RuntimeError("bank locked")

    world.bank.RemoveWeaponsByDef = boom
    index, entry = bank.bank_entries()[0]
    assert bank.remove_exact(index, entry) == bank.METHOD_DIRECT == "direct array edit (unverified)"
    assert _paths(world) == ["del"] and ("del", 0) in world.bank.calls
    assert _uids(world) == [_uid(SHIELD_BYTES)]


def test_by_def_removing_nothing_falls_back_to_the_direct_array_edit(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])
    world.bank.RemoveItemsByDef = lambda definition: world.bank.calls.append(("remove_by_def", False, "no-op"))
    index, entry = bank.bank_entries()[1]
    assert bank.remove_exact(index, entry) == bank.METHOD_DIRECT
    assert _paths(world) == ["by_def", "del"]
    assert _uids(world) == [_uid(PISTOL_BYTES)]


def test_both_removals_failing_is_a_409_with_the_bank_unchanged(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])
    world.bank.RemoveWeaponsByDef = lambda definition: world.bank.calls.append(("remove_by_def", True, "no-op"))
    before = [bank.codes.serial_bytes(e.InventorySerialNumber) for e in world.bank.TheChest]
    index, entry = bank.bank_entries()[0]
    with pytest.raises(ApiError) as info:
        bank.remove_exact(index, entry)
    assert info.value.status == 409 and "still in the bank" in str(info.value)
    assert "removed nothing" in str(info.value) and "deleting the slot directly failed" in str(info.value)
    assert _paths(world) == ["by_def", "del"], "ByDef first, then the direct edit"
    assert [bank.codes.serial_bytes(e.InventorySerialNumber) for e in world.bank.TheChest] == before


def test_a_by_def_that_takes_another_item_too_puts_it_back(monkeypatch):
    """The game's removal took the shield too: the shield is put back (verified) and only the pistol is gone."""
    world = install(monkeypatch)
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])
    world.bank.RemoveWeaponsByDef = lambda definition: world.bank.TheChest.clear()  # a game that over-removes
    index, entry = bank.bank_entries()[0]
    assert bank.remove_exact(index, entry) == "game"
    assert _uids(world) == [_uid(SHIELD_BYTES)]


def test_a_by_def_that_takes_another_item_it_cannot_put_back_is_undone(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])
    world.bank.RemoveWeaponsByDef = lambda definition: world.bank.TheChest.clear()
    world.bank.AddItemFromDef = lambda definition, ignore_size, unique: False  # the shield cannot come back
    index, entry = bank.bank_entries()[0]
    with pytest.raises(ApiError) as info:
        bank.remove_exact(index, entry)
    message = str(info.value)
    assert info.value.status == 409 and "undone" in message
    assert f"put back: UniqueId {_uid(PISTOL_BYTES)}" in message
    assert f"could NOT be put back, re-import them: {s.bytes_to_code(SHIELD_BYTES)}" in message
    assert message.count(s.bytes_to_code(SHIELD_BYTES)) == 1 and s.bytes_to_code(PISTOL_BYTES) not in message
    assert _uids(world) == [_uid(PISTOL_BYTES)], "the target itself is put back when the removal is undone"


def test_a_failed_put_back_names_the_codes(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(PISTOL_BYTES)])
    world.bank.AddWeaponFromDef = lambda definition, ignore_size, unique: False  # the put-back is refused
    index, entry = bank.bank_entries()[0]
    with pytest.raises(ApiError) as info:
        bank.remove_exact(index, entry)
    message = str(info.value)
    assert info.value.status == 409 and "could NOT be put back" in message
    assert message.count(s.bytes_to_code(PISTOL_BYTES)) == 2


def test_a_put_back_that_does_not_show_up_is_reported(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(PISTOL_BYTES)])
    world.bank.AddWeaponFromDef = lambda definition, ignore_size, unique: True  # says yes, adds nothing
    index, entry = bank.bank_entries()[0]
    with pytest.raises(ApiError) as info:
        bank.remove_exact(index, entry)
    assert s.bytes_to_code(PISTOL_BYTES) in str(info.value)


def test_a_removal_that_does_nothing_is_an_error(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    world.bank.RemoveWeaponsByDef = lambda definition: None
    index, entry = bank.bank_entries()[0]
    with pytest.raises(ApiError) as info:
        bank.remove_exact(index, entry)
    assert info.value.status == 409 and "still in the bank" in str(info.value)
    assert world.bank.used() == 1


def test_a_removal_the_game_raises_on_is_an_error(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))

    def boom(definition):
        raise RuntimeError("bank locked")

    world.bank.RemoveWeaponsByDef = boom
    index, entry = bank.bank_entries()[0]
    with pytest.raises(ApiError) as info:
        bank.remove_exact(index, entry)
    assert info.value.status == 409 and "bank locked" in str(info.value)
    assert world.bank.used() == 1


def test_remove_exact_refuses_a_slot_that_moved(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])
    _index, shield_entry = bank.bank_entries()[1]
    with pytest.raises(ApiError) as info:
        bank.remove_exact(0, shield_entry)  # index 0 holds the pistol, not the shield
    assert info.value.status == 404
    assert world.bank.used() == 2


# --- review round 2: every put-back is verified on its own --------------------------------------------------


def _taken(world, keep: list[bytes], take: list[bytes]):
    """The bank held keep + take; `take` is gone now. Returns put_back's (gone, lost) for it."""
    world.bank.TheChest.extend([chest_entry(d, weapon=s.read_header(d)["is_weapon"]) for d in keep + take])
    before = bank.snapshot()
    world.bank.TheChest[:] = world.bank.TheChest[: len(keep)]
    after = bank.snapshot()
    return bank.diff(before, after)[0], bank.uid_changes(before, after)[0]


def test_put_back_an_add_that_raises_after_adding_is_removed_and_reported(monkeypatch):
    world = install(monkeypatch)
    gone, lost = _taken(world, [SHIELD_BYTES], [PISTOL_BYTES])
    real_add = world.bank._add

    def add_then_raise(definition, ignore_size, unique):
        real_add(definition, weapon=True)
        raise RuntimeError("the bank UI crashed")

    world.bank.AddWeaponFromDef = add_then_raise
    result = bank.put_back(gone, lost)
    assert result.restored == [] and result.failed == [s.bytes_to_code(PISTOL_BYTES)]
    assert _uids(world) == [_uid(SHIELD_BYTES)], "what the raising add put in was removed again"


def test_put_back_an_add_that_returns_false_but_added_one_is_accepted_after_verifying(monkeypatch):
    world = install(monkeypatch)
    gone, lost = _taken(world, [SHIELD_BYTES], [PISTOL_BYTES])
    real_add = world.bank._add
    world.bank.AddWeaponFromDef = lambda definition, ignore_size, unique: real_add(definition, weapon=True) and False
    result = bank.put_back(gone, lost)
    assert result.restored == [f"UniqueId {_uid(PISTOL_BYTES)}"] and result.failed == []
    assert sorted(_uids(world)) == sorted([_uid(SHIELD_BYTES), _uid(PISTOL_BYTES)])


def test_put_back_an_add_that_returns_false_and_adds_the_wrong_item_is_not_restored(monkeypatch):
    world = install(monkeypatch)
    gone, lost = _taken(world, [SHIELD_BYTES], [PISTOL_BYTES])
    real_add = world.bank._add
    world.bank.AddWeaponFromDef = lambda d, ignore_size, unique: real_add(make_definition(99, weapon=True), weapon=True) and False
    result = bank.put_back(gone, lost)
    assert result.restored == [] and result.failed == [s.bytes_to_code(PISTOL_BYTES)]
    assert _uids(world) == [_uid(SHIELD_BYTES)], "the wrong entry was removed again"


def test_put_back_an_add_that_adds_two_gets_the_extra_removed(monkeypatch):
    world = install(monkeypatch)
    gone, lost = _taken(world, [SHIELD_BYTES], [PISTOL_BYTES])
    real_add = world.bank._add
    adds = []

    def twice_the_first_time(definition, ignore_size, unique):
        adds.append(definition.UniqueId)
        if len(adds) == 1:
            real_add(definition, weapon=True)
        return real_add(definition, weapon=True)

    world.bank.AddWeaponFromDef = twice_the_first_time
    result = bank.put_back(gone, lost)
    assert result.restored == [f"UniqueId {_uid(PISTOL_BYTES)}"] and result.failed == [] and result.notes == []
    assert sorted(_uids(world)) == sorted([_uid(SHIELD_BYTES), _uid(PISTOL_BYTES)]), "exactly one pistol is back"


def test_put_back_an_add_that_removes_an_unrelated_entry_reports_it_by_code(monkeypatch):
    world = install(monkeypatch)
    gone, lost = _taken(world, [SHIELD_BYTES], [PISTOL_BYTES])
    real_add = world.bank._add

    def add_and_drop_the_shield(definition, ignore_size, unique):
        world.bank.TheChest[:] = [e for e in world.bank.TheChest if bank.entry_unique_id(e) != _uid(SHIELD_BYTES)]
        return real_add(definition, weapon=True)

    world.bank.AddWeaponFromDef = add_and_drop_the_shield
    result = bank.put_back(gone, lost)
    assert result.restored == [f"UniqueId {_uid(PISTOL_BYTES)}"]
    assert result.failed == [s.bytes_to_code(SHIELD_BYTES)], "the shield that vanished is named by its code"


def test_put_back_an_add_that_says_yes_but_adds_nothing_is_never_restored(monkeypatch):
    world = install(monkeypatch)
    gone, lost = _taken(world, [], [PISTOL_BYTES])
    world.bank.AddWeaponFromDef = lambda definition, ignore_size, unique: True
    result = bank.put_back(gone, lost)
    assert result.restored == [] and result.failed == [s.bytes_to_code(PISTOL_BYTES)]
    code = s.bytes_to_code(PISTOL_BYTES)
    assert bank.restore_message("x", result) == f"x; these could NOT be put back, re-import them: {code}"


# --- review round 2: /api/bank is budgeted and cached; the export never decodes ------------------------------


def _big_bank(world, count: int) -> list[int]:
    uids = [5000 + i for i in range(count)]
    world.bank.max_size = count
    world.bank.TheChest.extend(chest_entry(s.with_unique_id(PISTOL_BYTES, uid)) for uid in uids)
    return uids


def test_a_big_bank_is_read_over_several_requests(monkeypatch):
    world = install(monkeypatch)
    uids = _big_bank(world, 50)
    clock = iter(range(0, 1_000_000, 1))  # 1 s per clock reading: each request decodes only a couple of entries
    monkeypatch.setattr(codes, "_now", lambda: next(clock))
    replies = [bank.bank_get({})]
    while replies[-1]["pending"] and len(replies) < 60:
        replies.append(bank.bank_get({}))
    pending = [reply["pending"] for reply in replies]
    assert len(replies) > 2 and pending[-1] == 0
    assert all(a > b for a, b in zip(pending, pending[1:])), f"pending must fall every request: {pending}"
    first = replies[0]
    waiting = [card for card in first["items"] if card.get("pending")]
    assert len(waiting) == first["pending"] == 50 - sum(1 for card in first["items"] if not card.get("pending"))
    uid = waiting[0]["unique_id"]
    assert waiting[0] == {
        "id": f"bank:{uids.index(uid)}:{uid}", "class": "WillowWeapon", "name": "Not decoded yet", "location": "Bank",
        "level": None, "rarity": None, "type_path": None, "build": [], "hide_bones": [], "unique_id": uid,
        "pending": True,
    }
    last = replies[-1]
    assert [card["unique_id"] for card in last["items"]] == uids and not any(c.get("pending") for c in last["items"])
    assert world.decodes == 50, "each entry was decoded exactly once"


def test_a_big_bank_without_a_slow_clock_takes_chunk_steps_per_request(monkeypatch):
    world = install(monkeypatch)
    _big_bank(world, 50)
    assert [bank.bank_get({})["pending"] for _ in range(3)] == [30, 10, 0]


def test_the_card_cache_means_a_second_read_decodes_nothing(monkeypatch):
    world = install(monkeypatch)
    _big_bank(world, 15)
    first = bank.bank_get({})
    assert first["pending"] == 0 and world.decodes == 15
    world.decodes = 0
    second = bank.bank_get({})
    assert world.decodes == 0 and second["pending"] == 0
    assert second["items"] == first["items"]
    del world.bank.TheChest[0]  # ids are re-applied per read: the same bytes at a new index get the new id
    third = bank.bank_get({})
    assert world.decodes == 0 and third["items"][0]["id"] == f"bank:0:{third['items'][0]['unique_id']}"


def test_the_card_cache_is_bounded(monkeypatch):
    world = install(monkeypatch)
    monkeypatch.setattr(bank, "CARD_CACHE_MAX", 5)
    _big_bank(world, 8)
    bank.bank_get({})
    assert len(bank._CARD_CACHE) == 5


def test_the_inventory_export_never_decodes(monkeypatch):
    from rtse import gear_inventory

    world = install(monkeypatch)
    uids = _big_bank(world, 30)
    out = gear_inventory.inventory_export({})
    assert world.decodes == 0
    assert [s.read_header(s.code_to_bytes(row["code"]))["unique_id"] for row in out["items"]] == uids
    assert {row["name"] for row in out["items"]} == {"Not decoded yet"} and out["items"][0]["class"] == "WillowWeapon"
    assert all("pending" not in row for row in out["items"])
    bank.bank_get({})  # fills the cache for the first 20 entries (CHUNK_STEPS)
    world.decodes = 0
    named = gear_inventory.inventory_export({})["items"]
    assert world.decodes == 0 and named[0]["name"] != "Not decoded yet" and named[0]["level"] == 30
    assert named[-1]["name"] == "Not decoded yet"
