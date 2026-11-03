"""gear_inventory's write routes against the fake inventory: duplicate, delete (the code comes back for undo), move both
ways with verified adds and rollback, export and the additive (chunked) JSON import with its equipped rule, the
200 cap, and the UniqueIds in use.

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
    bad_checksum_code,
    chest_entry,
    install,
    make_definition,
)
from rtse import bank, codes, gear_inventory
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

    monkeypatch.setattr(bank, "remove_exact", refuse)
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


def test_import_original_sends_bank_rows_to_the_bank(monkeypatch):
    world = install(monkeypatch)
    out = gear_inventory.inventory_import(
        {"items": [{"code": PISTOL_CODE, "location": "Bank"}, {"code": SHIELD_CODE, "location": "Backpack"}],
         "destination": "original"},
    )
    assert out["imported"] == 2
    assert world.bank.used() == 1 and len(world.manager.Backpack) == 1


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


# --- review round 1: replace is gone ------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, "false", 0, None, "yes"])
def test_import_refuses_any_replace_but_false(monkeypatch, value):
    world = install(monkeypatch)
    add_backpack(world, SHIELD_BYTES, weapon=False, name="Shield")
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    with pytest.raises(ApiError) as info:
        gear_inventory.inventory_import({"items": [{"code": PISTOL_CODE}], "destination": "backpack", "replace": value})
    assert info.value.status == 400 and info.value.args[0] == gear_inventory.REPLACE_REMOVED
    assert len(world.manager.Backpack) == 1 and world.bank.used() == 1


def test_import_with_replace_false_only_adds(monkeypatch):
    world = install(monkeypatch)
    add_backpack(world, SHIELD_BYTES, weapon=False, name="Shield")
    out = gear_inventory.inventory_import({"items": [{"code": PISTOL_CODE}], "destination": "backpack", "replace": False})
    assert out["imported"] == 1 and "removed" not in out
    assert len(world.manager.Backpack) == 2


# --- review round 1: equipped rows ---------------------------------------------------------------------


def test_export_then_import_original_skips_equipped_by_default(monkeypatch):
    """Re-importing an export used to put the equipped gear in the backpack too (a duplicate)."""
    world = install(monkeypatch)
    add_equipped(world, PISTOL_BYTES, weapon=True, name="Worn")
    add_backpack(world, SHIELD_BYTES, weapon=False, name="Shield")
    world.bank.TheChest.append(chest_entry(s.with_unique_id(PISTOL_BYTES, 4242)))
    exported = gear_inventory.inventory_export({})["items"]
    out = gear_inventory.inventory_import({"items": exported, "destination": "original"})
    assert out["skipped"] == 1 and out["imported"] == 2 and out["failed"] == 0
    assert out["processed"] == 3 and out["remaining"] == 0
    assert out["results"][0] == {
        "code": exported[0]["code"], "ok": False, "skipped": True, "error": "equipped item skipped",
        "id": None, "name": None, "unique_id": None,
    }
    assert out["warnings"] == ["1 equipped items were skipped (they are still equipped)"]
    assert len(world.manager.Backpack) == 2 and world.bank.used() == 2


def test_equipped_backpack_sends_equipped_rows_to_the_backpack(monkeypatch):
    world = install(monkeypatch)
    rows = [{"code": PISTOL_CODE, "location": "Equipped"}, {"code": SHIELD_CODE, "location": "Bank"}]
    out = gear_inventory.inventory_import({"items": rows, "destination": "original", "equipped": "backpack"})
    assert out["skipped"] == 0 and out["imported"] == 2 and out["warnings"] == []
    assert len(world.manager.Backpack) == 1 and world.bank.used() == 1


def test_equipped_rule_applies_to_a_fixed_destination_too(monkeypatch):
    world = install(monkeypatch)
    rows = [{"code": PISTOL_CODE, "location": "Equipped"}, {"code": SHIELD_CODE, "location": "Backpack"}]
    out = gear_inventory.inventory_import({"items": rows, "destination": "bank"})
    assert out["skipped"] == 1 and out["imported"] == 1 and world.bank.used() == 1


def test_equipped_must_be_skip_or_backpack(monkeypatch):
    install(monkeypatch)
    with pytest.raises(ApiError) as info:
        gear_inventory.inventory_import({"items": [], "destination": "backpack", "equipped": "bank"})
    assert info.value.status == 400


# --- review round 1: chunked import -------------------------------------------------------------------


def test_inventory_import_walks_45_items_in_three_requests(monkeypatch):
    world = install(monkeypatch, slots=100)
    remaining = [{"code": PISTOL_CODE, "location": "Backpack"}] * 45
    replies = []
    while remaining:
        reply = gear_inventory.inventory_import({"items": remaining, "destination": "original"})
        replies.append(reply)
        remaining = remaining[reply["processed"]:]
        assert reply["remaining"] == len(remaining) and len(reply["results"]) == reply["processed"]
    assert [(r["processed"], r["remaining"]) for r in replies] == [(20, 25), (20, 5), (5, 0)]
    assert len(world.manager.Backpack) == 45


def test_inventory_import_time_budget_still_imports_one(monkeypatch):
    world = install(monkeypatch)
    clock = iter(range(0, 1_000_000, 5))
    monkeypatch.setattr(codes, "_now", lambda: next(clock))
    rows = [{"code": PISTOL_CODE}, {"code": SHIELD_CODE}]
    out = gear_inventory.inventory_import({"items": rows, "destination": "backpack"})
    assert out["processed"] == 1 and out["remaining"] == 1 and len(world.manager.Backpack) == 1


# --- review round 1: delete needs a restorable backup ---------------------------------------------------


def test_delete_refuses_a_bad_checksum_backup(monkeypatch):
    world = install(monkeypatch)
    bad = s.code_to_bytes(bad_checksum_code())
    item = Item(make_definition(_uid(bad), weapon=False), weapon=False, name="Broken", data=bad)
    world.manager.Backpack.append(item)
    with pytest.raises(ApiError) as info:
        gear_inventory.delete({"id": item._path_name()})
    assert info.value.status == 409 and "restorable code for Broken, so it was not deleted" in str(info.value)
    assert world.manager.Backpack == [item]


def test_delete_refuses_when_the_game_cannot_decode_the_backup(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")

    def no_classes(name):
        raise RuntimeError("no such class")

    monkeypatch.setattr(codes, "_cdo", no_classes)
    with pytest.raises(ApiError) as info:
        gear_inventory.delete({"id": item._path_name()})
    assert info.value.status == 409 and "not deleted" in str(info.value)
    assert world.manager.Backpack == [item]


def test_delete_refuses_when_the_backup_decodes_to_another_item(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    world.stage = 12
    with pytest.raises(ApiError) as info:
        gear_inventory.delete({"id": item._path_name()})
    assert "GameStage" in str(info.value)
    assert world.manager.Backpack == [item]


def test_delete_a_bank_entry_whose_serial_cannot_be_decoded_is_refused(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    world.game_decodes = False
    monkeypatch.setattr(codes, "_cdo", lambda name: (_ for _ in ()).throw(RuntimeError("no decode")))
    with pytest.raises(ApiError) as info:
        gear_inventory.delete({"id": f"bank:0:{_uid(PISTOL_BYTES)}"})
    assert info.value.status == 409 and "not deleted" in str(info.value)
    assert world.bank.used() == 1


def test_delete_one_bank_twin_keeps_the_other(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True  # only ByDef works, and it takes both twins
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(PISTOL_BYTES)])
    with pytest.raises(ApiError) as info:
        gear_inventory.delete({"id": f"bank:1:{_uid(PISTOL_BYTES)}"})
    assert info.value.status == 409
    assert world.bank.used() == 2 and world.journal == []


def test_delete_a_bank_entry_when_the_game_removes_more(monkeypatch):
    """The old delete reported success with a warning while the shield was gone too; now both stay."""
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(SHIELD_BYTES, weapon=False)])
    world.bank.RemoveWeaponsByDef = lambda definition: world.bank.TheChest.clear()
    with pytest.raises(ApiError) as info:
        gear_inventory.delete({"id": f"bank:0:{_uid(PISTOL_BYTES)}"})
    assert info.value.status == 409
    assert sorted(bank.entry_unique_id(e) for e in world.bank.TheChest) == sorted([_uid(PISTOL_BYTES), _uid(SHIELD_BYTES)])
    assert world.journal == []


def test_delete_only_takes_the_target_from_the_backpack(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    other = add_backpack(world, SHIELD_BYTES, weapon=False, name="Shield")
    world.manager.RemoveFromInventory = lambda target, can_drop: world.manager.Backpack.clear()  # over-removes
    with pytest.raises(ApiError) as info:
        gear_inventory.delete({"id": item._path_name()})
    assert info.value.status == 409 and SHIELD_CODE in str(info.value)
    assert PISTOL_CODE in str(info.value), "the deleted item itself is gone, so its code must be in the reply"
    assert other not in world.manager.Backpack and world.journal == []


# --- review round 1: move adds and verifies first ------------------------------------------------------


def test_move_to_bank_when_the_add_says_yes_but_nothing_appears(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    world.bank.AddWeaponFromDef = lambda d, ignore_size, unique: True
    with pytest.raises(ApiError) as info:
        gear_inventory.move({"id": item._path_name(), "to": "bank"})
    assert info.value.status == 409
    assert world.manager.Backpack == [item] and world.bank.used() == 0 and world.journal == []


def test_move_to_bank_when_the_wrong_item_appears(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    real_add = world.bank._add
    world.bank.AddWeaponFromDef = lambda d, ignore_size, unique: real_add(make_definition(99, weapon=True), weapon=True)
    with pytest.raises(ApiError):
        gear_inventory.move({"id": item._path_name(), "to": "bank"})
    assert world.manager.Backpack == [item] and world.bank.used() == 0


def test_move_to_bank_removes_the_bank_copy_when_the_source_stays(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    world.manager.refuse_remove = True
    with pytest.raises(ApiError) as info:
        gear_inventory.move({"id": item._path_name(), "to": "bank"})
    assert "bank copy was removed again" in str(info.value)
    assert world.manager.Backpack == [item] and world.bank.used() == 0


def test_move_to_bank_refuses_an_unrestorable_item(monkeypatch):
    world = install(monkeypatch)
    item = add_backpack(world, PISTOL_BYTES, weapon=True, name="Infinity")
    world.stage = 12
    with pytest.raises(ApiError) as info:
        gear_inventory.move({"id": item._path_name(), "to": "bank"})
    assert "not moved" in str(info.value)
    assert world.bank.calls == [] and world.manager.Backpack == [item]


def test_move_to_backpack_when_the_add_says_yes_but_nothing_appears(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    world.manager.ClientAddWeaponToBackpack = lambda definition, mark, ready=False: None
    with pytest.raises(ApiError) as info:
        gear_inventory.move({"id": f"bank:0:{_uid(PISTOL_BYTES)}", "to": "backpack"})
    assert info.value.status == 409
    assert world.bank.used() == 1 and world.manager.Backpack == []


def test_move_to_backpack_when_the_wrong_item_appears(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    real_add = world.manager._add

    def wrong(definition, mark, ready=False):
        real_add(make_definition(99, weapon=True), weapon=True, mark=mark)

    world.manager.ClientAddWeaponToBackpack = wrong
    with pytest.raises(ApiError) as info:
        gear_inventory.move({"id": f"bank:0:{_uid(PISTOL_BYTES)}", "to": "backpack"})
    assert info.value.status == 409 and "removed again" in str(info.value)
    assert world.bank.used() == 1 and world.manager.Backpack == []


def test_move_to_backpack_removes_the_copy_when_the_bank_keeps_the_item(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True
    world.bank.RemoveWeaponsByDef = lambda definition: None  # the bank never lets go
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    with pytest.raises(ApiError) as info:
        gear_inventory.move({"id": f"bank:0:{_uid(PISTOL_BYTES)}", "to": "backpack"})
    assert "backpack copy was removed again" in str(info.value)
    assert world.bank.used() == 1 and world.manager.Backpack == []


def test_move_to_backpack_with_twins_put_back_keeps_one_copy_each(monkeypatch):
    """ByDef takes both twins; both are put back, so the backpack copy is removed again: nothing lost, nothing doubled."""
    world = install(monkeypatch)
    world.bank.TheChest.refuse_delete = True
    world.bank.TheChest.extend([chest_entry(PISTOL_BYTES), chest_entry(PISTOL_BYTES)])
    with pytest.raises(ApiError):
        gear_inventory.move({"id": f"bank:0:{_uid(PISTOL_BYTES)}", "to": "backpack"})
    assert world.bank.used() == 2 and world.manager.Backpack == []
