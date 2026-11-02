"""gear_inventory's write routes against the fake inventory: duplicate, delete (the code comes back for undo), move both
ways with rollback, export and JSON import with replace, the 200 cap, and the UniqueIds in use.

    python -I -m pytest dev/tests -k inventory
"""

from __future__ import annotations

import pytest

from fakes_inventory import (
    PISTOL_BYTES,
    PISTOL_CODE,
    SHIELD_BYTES,
    SHIELD_CODE,
    Item,
    chest_entry,
    install,
    make_definition,
)
from rtse import bank, gear_inventory
from rtse import item_serial as s
from rtse.routing import ApiError


def _uid(data: bytes) -> int:
    return s.read_header(data)["unique_id"]


def add_backpack(world, data: bytes, *, weapon: bool, name: str) -> Item:
    item = Item(make_definition(_uid(data), weapon=weapon), weapon=weapon, name=name, data=data)
    world.manager.Backpack.append(item)
    return item


def add_equipped(world, data: bytes, *, weapon: bool, name: str) -> Item:
    item = Item(make_definition(_uid(data), weapon=weapon), weapon=weapon, name=name, data=data)
    world.manager.InventoryChain = item
    return item


# --- reads -----------------------------------------------------------------------------------------


def test_items_carry_the_unique_id(monkeypatch):
    world = install(monkeypatch)
    add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    out = gear_inventory.items({})
    assert out["items"][0]["unique_id"] == _uid(PISTOL_BYTES)
    assert out["items"][0]["location"] == "Backpack" and out["items"][0]["name"] == "Infinity"


def test_used_unique_ids_covers_equipped_backpack_and_bank(monkeypatch):
    world = install(monkeypatch)
    add_equipped(world, PISTOL_BYTES, weapon=True, name="Worn")
    add_backpack(world, SHIELD_BYTES, weapon=False, name="Shield")
    world.bank.TheChest.append(chest_entry(s.with_unique_id(PISTOL_BYTES, 4242)))
    assert gear_inventory.used_unique_ids() == {_uid(PISTOL_BYTES), _uid(SHIELD_BYTES), 4242}


def test_used_unique_ids_is_empty_outside_a_save(game):
    assert gear_inventory.used_unique_ids() == set()


# --- duplicate -------------------------------------------------------------------------------------


def test_duplicate_a_backpack_item_gets_a_fresh_unique_id(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    out = gear_inventory.duplicate({"id": item._path_name()})
    assert out["ok"] and out["destination"] == "backpack"
    assert out["unique_id"] != _uid(PISTOL_BYTES)
    assert len(world.manager.Backpack) == 2
    assert world.manager.Backpack[1].DefinitionData.UniqueId == out["unique_id"]
    assert world.journal[-1][0] == "inventory.add"


def test_duplicate_a_bank_entry_goes_into_the_bank(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    out = gear_inventory.duplicate({"id": f"bank:0:{_uid(PISTOL_BYTES)}"})
    assert out["destination"] == "bank" and out["id"].startswith("bank:1:")
    assert world.bank.used() == 2


def test_duplicate_refuses_when_the_backpack_is_full(monkeypatch):
    world = install(monkeypatch, slots=1)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    with pytest.raises(ApiError) as info:
        gear_inventory.duplicate({"id": item._path_name()})
    assert info.value.status == 409 and info.value.args[0] == "the backpack is full"
    assert len(world.manager.Backpack) == 1


# --- delete ----------------------------------------------------------------------------------------


def test_delete_an_equipped_item_is_refused(monkeypatch):
    world = install(monkeypatch)
    item = add_equipped(world, PISTOL_BYTES, weapon=True, name="Worn")
    with pytest.raises(ApiError) as info:
        gear_inventory.delete({"id": item._path_name()})
    assert info.value.status == 409 and "unequip it first" in str(info.value)


def test_delete_returns_a_code_and_undo_puts_it_back(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    out = gear_inventory.delete({"id": item._path_name()})
    assert out["ok"] and out["deleted"]["unique_id"] == _uid(PISTOL_BYTES)
    assert s.code_to_bytes(out["deleted"]["code"]) == PISTOL_BYTES
    assert world.manager.Backpack == []

    kind, _target, before, after, undo = world.journal[-1]
    assert kind == "inventory.delete" and before["code"] == out["deleted"]["code"] and after is None
    undo()
    assert len(world.manager.Backpack) == 1
    assert s.read_header(s.code_to_bytes(out["deleted"]["code"]))["checksum_ok"]


def test_delete_refuses_when_no_code_can_be_made(monkeypatch):
    world = install(monkeypatch)
    item = Item(make_definition(5, weapon=True), weapon=True, name="No serial", data=None)
    world.manager.Backpack.append(item)
    with pytest.raises(ApiError) as info:
        gear_inventory.delete({"id": item._path_name()})
    assert info.value.status == 409 and "not deleted" in str(info.value)
    assert world.manager.Backpack == [item]


def test_delete_a_bank_entry_by_its_id(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    out = gear_inventory.delete({"id": f"bank:0:{_uid(PISTOL_BYTES)}"})
    assert out["deleted"]["unique_id"] == _uid(PISTOL_BYTES)
    assert s.code_to_bytes(out["deleted"]["code"]) == PISTOL_BYTES
    assert world.bank.used() == 0


def test_delete_a_bank_id_that_moved_is_a_404(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    with pytest.raises(ApiError) as info:
        gear_inventory.delete({"id": f"bank:0:{_uid(PISTOL_BYTES) + 1}"})
    assert info.value.status == 404
    assert world.bank.used() == 1


# --- move ------------------------------------------------------------------------------------------


def test_move_backpack_to_bank_keeps_the_unique_id(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    out = gear_inventory.move({"id": item._path_name(), "to": "bank"})
    assert out["ok"] and out["to"] == "bank" and out["id"] == f"bank:0:{_uid(PISTOL_BYTES)}"
    assert world.manager.Backpack == [] and world.bank.used() == 1
    assert world.journal[-1][0] == "inventory.move"


def test_move_backpack_to_bank_undo_moves_it_back(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    gear_inventory.move({"id": item._path_name(), "to": "bank"})
    world.journal[-1][4]()  # the undo
    assert len(world.manager.Backpack) == 1 and world.bank.used() == 0


def test_move_bank_to_backpack(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    out = gear_inventory.move({"id": f"bank:0:{_uid(PISTOL_BYTES)}", "to": "backpack"})
    assert out["to"] == "backpack" and out["id"] == world.manager.Backpack[0]._path_name()
    assert len(world.manager.Backpack) == 1 and world.bank.used() == 0
    assert world.manager.Backpack[0].DefinitionData.UniqueId == _uid(PISTOL_BYTES)


def test_move_bank_to_a_full_backpack_is_refused(monkeypatch):
    world = install(monkeypatch, slots=1)
    add_backpack(world, SHIELD_BYTES, weapon=False, name="Shield")
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    with pytest.raises(ApiError) as info:
        gear_inventory.move({"id": f"bank:0:{_uid(PISTOL_BYTES)}", "to": "backpack"})
    assert info.value.status == 409 and info.value.args[0] == "the backpack is full"
    assert world.bank.used() == 1


def test_move_bank_to_backpack_rolls_back_the_copy_when_the_bank_removal_fails(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))

    def refuse(index, entry):
        raise ApiError(409, "bank refused the removal")

    monkeypatch.setattr(bank, "remove_bank_entry", refuse)
    with pytest.raises(ApiError):
        gear_inventory.move({"id": f"bank:0:{_uid(PISTOL_BYTES)}", "to": "backpack"})
    assert world.manager.Backpack == [], "the backpack copy must be rolled back"
    assert world.bank.used() == 1


def test_move_backpack_to_bank_rolls_back_when_the_backpack_keeps_the_item(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    world.manager.refuse_remove = True
    with pytest.raises(ApiError):
        gear_inventory.move({"id": item._path_name(), "to": "bank"})
    assert world.bank.used() == 0, "the bank copy must be rolled back"
    assert len(world.manager.Backpack) == 1


def test_move_to_the_same_place_is_a_400(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    with pytest.raises(ApiError) as info:
        gear_inventory.move({"id": f"bank:0:{_uid(PISTOL_BYTES)}", "to": "bank"})
    assert info.value.status == 400


def test_move_needs_a_valid_destination(monkeypatch):
    install(monkeypatch)
    with pytest.raises(ApiError) as info:
        gear_inventory.move({"id": "x", "to": "trunk"})
    assert info.value.status == 400


# --- export and import -------------------------------------------------------------------------------


def test_export_lists_equipped_backpack_and_bank_in_that_order(monkeypatch):
    world = install(monkeypatch)
    add_equipped(world, PISTOL_BYTES, weapon=True, name="Worn")
    add_backpack(world, SHIELD_BYTES, weapon=False, name="Shield")
    world.bank.TheChest.append(chest_entry(s.with_unique_id(PISTOL_BYTES, 4242)))
    out = gear_inventory.inventory_export({})
    assert out["format"] == "rtse-inventory" and out["version"] == 1 and out["warnings"] == []
    assert [row["location"] for row in out["items"]] == ["Equipped", "Backpack", "Bank"]
    assert [row["unique_id"] for row in out["items"]] == [_uid(PISTOL_BYTES), _uid(SHIELD_BYTES), 4242]
    assert all(s.code_to_bytes(row["code"]) for row in out["items"])


def test_export_then_import_into_original_with_replace(monkeypatch):
    """The whole save: export, change it, import the export with replace. The changes are removed, the export comes back."""
    world = install(monkeypatch)
    add_backpack(world, SHIELD_BYTES, weapon=False, name="Old shield")
    world.bank.TheChest.append(chest_entry(s.with_unique_id(PISTOL_BYTES, 4242)))
    exported = gear_inventory.inventory_export({})["items"]

    add_backpack(world, s.with_unique_id(SHIELD_BYTES, 555), weapon=False, name="Changed shield")
    world.bank.TheChest.append(chest_entry(s.with_unique_id(PISTOL_BYTES, 777)))

    out = gear_inventory.inventory_import({"items": exported, "destination": "original", "replace": True})
    # replace removes everything the receiving places hold: the two backpack items and the two bank entries
    assert out["imported"] == 2 and out["failed"] == 0 and out["removed"] == 4
    assert out["path"] == "unpack"
    assert [row["location"] for row in exported] == ["Backpack", "Bank"]
    assert len(world.manager.Backpack) == 1 and world.bank.used() == 1
    assert 555 not in {i.DefinitionData.UniqueId for i in world.manager.Backpack}
    assert 777 not in {bank.entry_unique_id(e) for _i, e in bank.bank_entries()}
    kinds = [call[0] for call in world.journal]
    assert "inventory.replace" in kinds


def test_import_original_sends_bank_rows_to_the_bank(monkeypatch):
    world = install(monkeypatch)
    out = gear_inventory.inventory_import(
        {"items": [{"code": PISTOL_CODE, "location": "Bank"}, {"code": SHIELD_CODE, "location": "Backpack"}],
         "destination": "original"},
    )
    assert out["imported"] == 2
    assert world.bank.used() == 1 and len(world.manager.Backpack) == 1


def test_replace_never_touches_equipped_items(monkeypatch):
    world = install(monkeypatch)
    worn = add_equipped(world, PISTOL_BYTES, weapon=True, name="Worn")
    add_backpack(world, SHIELD_BYTES, weapon=False, name="Shield")
    out = gear_inventory.inventory_import({"items": [{"code": SHIELD_CODE}], "destination": "backpack", "replace": True})
    assert out["removed"] == 1 and out["imported"] == 1
    assert world.manager.InventoryChain is worn


def test_replace_refuses_an_invalid_code_before_removing_anything(monkeypatch):
    world = install(monkeypatch)
    add_backpack(world, SHIELD_BYTES, weapon=False, name="Shield")
    with pytest.raises(ApiError) as info:
        gear_inventory.inventory_import({"items": [{"code": "BL2(nope)"}], "destination": "backpack", "replace": True})
    assert info.value.status == 400
    assert len(world.manager.Backpack) == 1


def test_replace_refuses_when_the_backpack_would_be_too_small(monkeypatch):
    world = install(monkeypatch, slots=2)
    add_backpack(world, SHIELD_BYTES, weapon=False, name="Shield")
    items = [{"code": PISTOL_CODE}, {"code": SHIELD_CODE}, {"code": PISTOL_CODE}]
    with pytest.raises(ApiError) as info:
        gear_inventory.inventory_import({"items": items, "destination": "backpack", "replace": True})
    assert info.value.status == 409 and "room for 2" in str(info.value)
    assert len(world.manager.Backpack) == 1


def test_import_takes_at_most_200_items(monkeypatch):
    install(monkeypatch)
    with pytest.raises(ApiError) as info:
        gear_inventory.inventory_import({"items": [{"code": PISTOL_CODE}] * 201, "destination": "backpack"})
    assert info.value.status == 400


def test_import_needs_a_known_destination(monkeypatch):
    install(monkeypatch)
    with pytest.raises(ApiError) as info:
        gear_inventory.inventory_import({"items": [], "destination": "trunk"})
    assert info.value.status == 400
