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


# --- removals ----------------------------------------------------------------------------------------


def test_remove_exact_deletes_only_that_slot(monkeypatch):
    world = install(monkeypatch)
    other = s.with_unique_id(PISTOL_BYTES, 4242)
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(other), chest_entry(SHIELD_BYTES, weapon=False)])
    index, entry = bank.bank_entries()[1]
    bank.remove_exact(index, entry)
    assert _uids(world) == [_uid(PISTOL_BYTES), _uid(SHIELD_BYTES)]
    assert not any(call[0] == "remove_by_def" for call in world.bank.calls)


def test_remove_exact_removes_one_of_identical_twins(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(PISTOL_BYTES)])
    index, entry = bank.bank_entries()[1]
    bank.remove_exact(index, entry)
    assert _uids(world) == [_uid(PISTOL_BYTES)]


def test_remove_exact_falls_back_to_remove_by_def(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])
    index, entry = bank.bank_entries()[0]
    bank.remove_exact(index, entry)
    assert world.bank.calls[-1] == ("remove_by_def", True, _uid(PISTOL_BYTES))
    assert _uids(world) == [_uid(SHIELD_BYTES)]


def test_by_def_removing_both_twins_puts_both_back_and_is_a_409(monkeypatch):
    """The old code removed both twins by definition and only kept one code; now both come back."""
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])
    index, entry = bank.bank_entries()[0]
    with pytest.raises(ApiError) as info:
        bank.remove_exact(index, entry)
    assert info.value.status == 409 and "everything it removed was put back" in str(info.value)
    assert sorted(_uids(world)) == sorted([_uid(PISTOL_BYTES), _uid(PISTOL_BYTES), _uid(SHIELD_BYTES)])
    assert [bank.codes.serial_bytes(e.InventorySerialNumber) for e in world.bank.TheChest].count(PISTOL_BYTES) == 2


def test_a_removal_that_takes_another_item_too_is_undone(monkeypatch):
    """The old code only warned ("went from 2 to 0"); now the bank is put back and the removal fails."""
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])
    world.bank.RemoveWeaponsByDef = lambda definition: world.bank.TheChest.clear()  # a game that over-removes
    index, entry = bank.bank_entries()[0]
    with pytest.raises(ApiError) as info:
        bank.remove_exact(index, entry)
    assert info.value.status == 409
    assert sorted(_uids(world)) == sorted([_uid(PISTOL_BYTES), _uid(SHIELD_BYTES)])


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
