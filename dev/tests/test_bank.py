"""rtse/bank.py: the /api/bank card list, bank ids and their UniqueId check, adds into a full bank, removals by
definition, the exact-slot removal for identical twins, and the warnings when the bank does not change as expected.

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


# --- removals ----------------------------------------------------------------------------------------


def test_remove_uses_remove_by_def_for_a_single_entry(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    world.bank.TheChest.append(chest_entry(SHIELD_BYTES, weapon=False))
    index, entry = bank.bank_entries()[0]
    assert bank.remove_bank_entry(index, entry) == []
    assert world.bank.calls[-1] == ("remove_by_def", True, _uid(PISTOL_BYTES))
    assert [bank.entry_unique_id(e) for e in world.bank.TheChest] == [_uid(SHIELD_BYTES)]


def test_identical_twins_are_removed_by_index(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    index, entry = bank.bank_entries()[1]
    assert bank.remove_bank_entry(index, entry) == []
    assert len(world.bank.TheChest) == 1
    assert not any(call[0] == "remove_by_def" for call in world.bank.calls)


def test_removing_more_than_one_is_reported(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    world.bank.TheChest.append(chest_entry(SHIELD_BYTES, weapon=False))
    world.bank.RemoveWeaponsByDef = lambda definition: world.bank.TheChest.clear()  # a game that over-removes
    index, entry = bank.bank_entries()[0]
    warnings = bank.remove_bank_entry(index, entry)
    assert warnings and "went from 2 to 0" in warnings[0]


def test_a_removal_that_does_nothing_is_an_error(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    world.bank.RemoveWeaponsByDef = lambda definition: None
    index, entry = bank.bank_entries()[0]
    with pytest.raises(ApiError) as info:
        bank.remove_bank_entry(index, entry)
    assert info.value.status == 409 and "still in the bank" in str(info.value)


def test_a_removal_the_game_raises_on_is_an_error(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))

    def boom(definition):
        raise RuntimeError("bank locked")

    world.bank.RemoveWeaponsByDef = boom
    index, entry = bank.bank_entries()[0]
    with pytest.raises(ApiError) as info:
        bank.remove_bank_entry(index, entry)
    assert info.value.status == 409 and "bank locked" in str(info.value)
