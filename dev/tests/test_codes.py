"""rtse/codes.py: preview of messy pasted text, imports with fresh UniqueIds, full and bad-code batches, the
export round trip through item_serial, the element-wise serial fallback, the 200-code cap and the debug dump.

The game is a fake (fakes_inventory.py) that decodes only the header, so these tests prove RTSE's own logic and the
offline code layer. Nothing here proves what the real game does.

    python -I -m pytest dev/tests -k codes
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fakes_inventory import (
    PISTOL_BYTES,
    PISTOL_CODE,
    SHIELD_BYTES,
    SHIELD_CODE,
    STRUCT_OPTIONS,
    Item,
    Obj,
    bad_checksum_code,
    chest_entry,
    install,
    make_definition,
)
from rtse import bank, codes
from rtse import item_serial as s
from rtse.routing import ApiError


@pytest.fixture(autouse=True)
def _reset_struct_options():
    STRUCT_OPTIONS.drop_buffer = False
    yield
    STRUCT_OPTIONS.drop_buffer = False


def _uid(data: bytes) -> int:
    return s.read_header(data)["unique_id"]


def _owned(world, data: bytes, *, weapon: bool, name: str = "Owned") -> Item:
    item = Item(make_definition(_uid(data), weapon=weapon), weapon=weapon, name=name, data=data)
    world.manager.Backpack.append(item)
    return item


# --- decode (preview) ------------------------------------------------------------------------------


def test_decode_messy_text_without_the_game(game):
    """No game objects: codes still parse, the checksum still checks, and the card is left out with a warning."""
    text = f"Pistol: `{PISTOL_CODE}`\n- {SHIELD_CODE}\n[code]{bad_checksum_code()}[/code]\nnot a code, BL2(!!!)\n"
    entries = codes.decode(text)
    assert [entry["ok"] for entry in entries] == [True, True, False]
    assert [entry["index"] for entry in entries] == [0, 1, 2]
    assert entries[0]["kind"] == "weapon" and entries[1]["kind"] == "item"
    assert entries[2]["error"] == codes.CHECKSUM_ERROR and entries[2]["card"] is None
    assert entries[0]["card"] is None
    assert "could not decode" in entries[0]["warning"]


def test_decode_builds_a_card_from_the_game(monkeypatch):
    install(monkeypatch)
    [entry] = codes.decode(PISTOL_CODE)
    card = entry["card"]
    assert entry["ok"] and entry["warning"] is None
    assert entry["unique_id"] == _uid(PISTOL_BYTES) and entry["uid_clash"] is False
    assert card["id"] == "preview:0" and card["location"] == "Preview"
    assert card["class"] == "WillowWeapon" and card["level"] == 30 and card["rarity"] is None
    assert card["unique_id"] == _uid(PISTOL_BYTES)
    assert card["type_path"] == "/Game/Weapons/WeaponType_Pistol"
    assert "Hyperion" in card["name"]


def test_decode_maps_an_item_class(monkeypatch):
    install(monkeypatch)
    [entry] = codes.decode(SHIELD_CODE)
    assert entry["kind"] == "item" and entry["card"]["class"] == "WillowShield"
    assert entry["card"]["type_path"] is None


def test_uid_clash_with_an_owned_item(monkeypatch):
    world = install(monkeypatch)
    _owned(world, PISTOL_BYTES, weapon=True)
    first, second = codes.decode(f"{PISTOL_CODE} {SHIELD_CODE}")
    assert first["uid_clash"] is True
    assert second["uid_clash"] is False


def test_bad_checksum_is_reported_not_raised(monkeypatch):
    install(monkeypatch)
    [entry] = codes.decode(bad_checksum_code())
    assert entry["ok"] is False and entry["error"] == codes.CHECKSUM_ERROR
    assert entry["card"] is None and entry["kind"] == "item"


def test_more_than_200_codes_is_a_400():
    with pytest.raises(ApiError) as info:
        codes.decode(" ".join([PISTOL_CODE] * 201))
    assert info.value.status == 400
    assert len(codes.decode(" ".join([PISTOL_CODE] * 200))) == 200


def test_preview_route_reports_found(monkeypatch):
    install(monkeypatch)
    out = codes.preview_route({"text": f"{PISTOL_CODE} junk {SHIELD_CODE}"})
    assert out["found"] == 2 and len(out["entries"]) == 2 and out["next"] is None


# --- import ----------------------------------------------------------------------------------------


def test_import_gives_every_item_a_fresh_unique_id(monkeypatch):
    world = install(monkeypatch)
    original = _uid(PISTOL_BYTES)
    result = codes.import_items([PISTOL_CODE, PISTOL_CODE, SHIELD_CODE], "backpack")
    assert result["imported"] == 3 and result["failed"] == 0 and result["path"] == "unpack"
    uids = [row["unique_id"] for row in result["results"]]
    assert len(set(uids)) == 3, "two copies of one code in one batch must get different UniqueIds"
    assert original not in uids and 0 not in uids
    assert [item.DefinitionData.UniqueId for item in world.manager.Backpack] == uids
    assert all(row["ok"] and row["id"] and row["name"] for row in result["results"])
    assert [call[0] for call in world.journal] == ["inventory.add"] * 3


def test_import_journal_undo_is_callable(monkeypatch):
    world = install(monkeypatch)
    result = codes.import_items([SHIELD_CODE], "backpack")
    *_rest, undo = world.journal[0][1:]
    undo()
    assert world.manager.Backpack == []
    assert result["results"][0]["ok"]


def test_import_refuses_when_the_backpack_is_full(monkeypatch):
    world = install(monkeypatch, slots=2)
    _owned(world, SHIELD_BYTES, weapon=False, name="Kept")
    result = codes.import_items([PISTOL_CODE, SHIELD_CODE], "backpack")
    assert [row["ok"] for row in result["results"]] == [True, False]
    assert result["results"][1]["error"] == "the backpack is full"
    assert len(world.manager.Backpack) == 2


def test_import_refuses_when_the_bank_is_full(monkeypatch):
    world = install(monkeypatch, bank_max=1)
    world.bank.TheChest.append(chest_entry(PISTOL_BYTES))
    result = codes.import_items([SHIELD_CODE], "bank")
    assert result["results"][0]["ok"] is False
    assert "bank refused" in result["results"][0]["error"]


def test_import_to_a_bank_that_is_not_loaded(monkeypatch):
    install(monkeypatch, with_bank=False)
    result = codes.import_items([SHIELD_CODE], "bank")
    assert result["results"][0]["ok"] is False
    assert "not loaded" in result["results"][0]["error"]


def test_bad_codes_do_not_abort_the_batch(monkeypatch):
    install(monkeypatch)
    result = codes.import_items(["not a code", PISTOL_CODE, bad_checksum_code()], "backpack")
    assert [row["ok"] for row in result["results"]] == [False, True, False]
    assert "BL2" in result["results"][0]["error"]
    assert result["results"][2]["error"] == codes.CHECKSUM_ERROR
    assert result["imported"] == 1 and result["failed"] == 2


def test_import_falls_back_to_create_when_unpack_refuses(monkeypatch):
    world = install(monkeypatch)
    world.game_decodes = False
    result = codes.import_items([PISTOL_CODE], "backpack")
    assert result["results"][0]["ok"] and result["path"] == "create"
    assert world.manager.Backpack[0].DefinitionData.UniqueId == result["results"][0]["unique_id"]
    assert world.destroyed == [True], "the temporary actor from the fallback is destroyed"


def test_import_rejects_an_unknown_destination(monkeypatch):
    install(monkeypatch)
    with pytest.raises(ApiError) as info:
        codes.import_items([PISTOL_CODE], "trunk")
    assert info.value.status == 400


def test_import_route_checks_its_body(monkeypatch):
    install(monkeypatch)
    with pytest.raises(ApiError) as info:
        codes.import_route({"codes": "BL2(...)"})
    assert info.value.status == 400


# --- serials ---------------------------------------------------------------------------------------


def test_serial_struct_uses_make_struct_when_it_keeps_the_buffer(monkeypatch):
    install(monkeypatch)
    serial = codes.serial_struct(PISTOL_BYTES)
    assert codes.serial_bytes(serial) == PISTOL_BYTES
    assert codes.LAST_PATHS["serial"] == "make_struct"


def test_serial_struct_falls_back_to_element_writes(monkeypatch):
    install(monkeypatch)
    STRUCT_OPTIONS.drop_buffer = True
    serial = codes.serial_struct(PISTOL_BYTES)
    assert codes.serial_bytes(serial) == PISTOL_BYTES
    assert codes.LAST_PATHS["serial"] == "elementwise"


def test_serial_struct_gives_up_when_neither_write_sticks(monkeypatch):
    install(monkeypatch)

    class Frozen(list):
        def __setitem__(self, index, value):  # the element writes do not stick either
            pass

    def write_nothing(name, **fields):
        return SimpleNamespace(EncryptedLength=len(PISTOL_BYTES), RunningCounter=0, State=0, Buffer=Frozen([0] * 40))

    monkeypatch.setattr(codes, "_make_struct", write_nothing)
    with pytest.raises(s.SerialError):
        codes.serial_struct(PISTOL_BYTES)


# --- export ----------------------------------------------------------------------------------------


def test_export_round_trips_through_item_serial(monkeypatch):
    world = install(monkeypatch)
    item = _owned(world, PISTOL_BYTES, weapon=True, name="Infinity")
    code, header, warnings = codes.export_code(item)
    assert s.code_to_bytes(code) == PISTOL_BYTES
    assert header["checksum_ok"] and header["is_weapon"] and warnings == []


def test_export_of_an_import_carries_the_new_unique_id(monkeypatch):
    world = install(monkeypatch)
    result = codes.import_items([PISTOL_CODE], "backpack")
    uid = result["results"][0]["unique_id"]
    code, header, _warnings = codes.export_code(world.manager.Backpack[0])
    assert s.code_to_bytes(code) == s.with_unique_id(PISTOL_BYTES, uid)
    assert header["unique_id"] == uid


def test_export_warns_when_the_checksum_does_not_match(monkeypatch):
    install(monkeypatch)
    bad_bytes = s.code_to_bytes(bad_checksum_code())
    item = Item(make_definition(1, weapon=False), weapon=False, name="Broken", data=bad_bytes)
    code, header, warnings = codes.export_code(item)
    assert header["checksum_ok"] is False
    assert codes.CHECKSUM_WARNING in warnings
    assert s.code_to_bytes(code) == bad_bytes


def test_export_of_a_chest_entry_uses_its_serial(monkeypatch):
    install(monkeypatch)
    code, header, _warnings = codes.export_code(chest_entry(PISTOL_BYTES))
    assert s.code_to_bytes(code) == PISTOL_BYTES
    assert header["unique_id"] == _uid(PISTOL_BYTES)


def test_export_route_by_item_id(monkeypatch):
    world = install(monkeypatch)
    item = _owned(world, PISTOL_BYTES, weapon=True, name="Infinity")
    out = codes.export_route({"id": item._path_name()})
    assert out["name"] == "Infinity"
    assert out["header"] == {"kind": "weapon", "unique_id": _uid(PISTOL_BYTES), "checksum_ok": True}
    assert s.code_to_bytes(out["code"]) == PISTOL_BYTES


def test_export_route_needs_an_id(monkeypatch):
    install(monkeypatch)
    with pytest.raises(ApiError) as info:
        codes.export_route({})
    assert info.value.status == 400


def test_copy_definition_copies_the_fields(monkeypatch):
    install(monkeypatch)
    source = make_definition(9, weapon=True)
    copy = codes.copy_definition(source, True)
    assert copy.UniqueId == 9
    assert copy.BalanceDefinition is source.BalanceDefinition
    assert copy is not source


# --- debug dump ------------------------------------------------------------------------------------


def test_debug_dump_round_trips_and_shows_the_bank(monkeypatch, tmp_path):
    world = install(monkeypatch)
    monkeypatch.setattr(codes, "DEBUG_CODES_FILE", tmp_path / "debug_codes.json")
    _owned(world, PISTOL_BYTES, weapon=True, name="Infinity")
    world.bank.TheChest.append(chest_entry(SHIELD_BYTES, weapon=False))

    out = codes.debug_codes({})
    assert out == {"saved_to": str(tmp_path / "debug_codes.json"), "items": 1, "bank": 1}
    report = json.loads((tmp_path / "debug_codes.json").read_text(encoding="utf-8"))
    row = report["items"][0]
    assert row["round_trip"]["mismatches"] == []
    assert row["round_trip"]["path"] == "unpack"
    assert row["export"]["header"]["checksum_ok"] is True
    assert report["bank"]["content_entries"] == 1
    assert report["bank"]["first_entries"][0]["header"]["checksum_ok"] is True
    assert report["last_paths"] == codes.LAST_PATHS


def test_debug_dump_survives_an_item_that_cannot_export(monkeypatch, tmp_path):
    world = install(monkeypatch)
    monkeypatch.setattr(codes, "DEBUG_CODES_FILE", tmp_path / "debug_codes.json")
    _owned(world, PISTOL_BYTES, weapon=True, name="Good")
    broken = Item(make_definition(5, weapon=False), weapon=False, name="No serial", data=None)
    world.manager.Backpack.append(broken)
    codes.debug_codes({})
    report = json.loads((tmp_path / "debug_codes.json").read_text(encoding="utf-8"))
    assert report["items"][0]["round_trip"]["mismatches"] == []
    assert str(report["items"][1]["export"]).startswith("<error")
    assert report["items"][1]["round_trip"].startswith("<error")


# --- chunking (one request does a bounded amount of game work) ----------------------------------------


def _codes_text(count: int) -> str:
    return "\n".join([PISTOL_CODE, SHIELD_CODE] * (count // 2) + [PISTOL_CODE] * (count % 2))


def _slow_clock(monkeypatch) -> None:
    clock = iter(range(0, 1_000_000, 5))  # every reading of the clock is 5 s after the last
    monkeypatch.setattr(codes, "_now", lambda: next(clock))


def test_decode_chunk_walks_45_codes_in_three_requests(monkeypatch):
    install(monkeypatch)
    text = _codes_text(45)
    first = codes.decode_chunk(text)
    assert first["found"] == 45 and first["next"] == 20
    assert [entry["index"] for entry in first["entries"]] == list(range(20))
    second = codes.decode_chunk(text, first["next"])
    assert second["next"] == 40 and [entry["index"] for entry in second["entries"]] == list(range(20, 40))
    assert second["entries"][0]["card"]["id"] == "preview:20"
    third = codes.decode_chunk(text, second["next"])
    assert third["next"] is None and [entry["index"] for entry in third["entries"]] == list(range(40, 45))


def test_decode_chunk_time_budget_still_does_one_code(monkeypatch):
    install(monkeypatch)
    _slow_clock(monkeypatch)
    out = codes.decode_chunk(_codes_text(3))
    assert len(out["entries"]) == 1 and out["next"] == 1


def test_decode_chunk_checks_start(monkeypatch):
    install(monkeypatch)
    assert codes.decode_chunk(PISTOL_CODE, 1) == {"entries": [], "found": 1, "next": None}
    for bad in (-1, 2):
        with pytest.raises(ApiError) as info:
            codes.decode_chunk(PISTOL_CODE, bad)
        assert info.value.status == 400
    with pytest.raises(ApiError):
        codes.preview_route({"text": PISTOL_CODE, "start": "x"})


def test_preview_route_reads_start_from_the_query_string(monkeypatch):
    install(monkeypatch)
    out = codes.preview_route({"text": _codes_text(25), "start": "20"})
    assert [entry["index"] for entry in out["entries"]] == [20, 21, 22, 23, 24] and out["next"] is None


def test_decode_without_budget_returns_everything(monkeypatch):
    install(monkeypatch)
    _slow_clock(monkeypatch)
    assert len(codes.decode(_codes_text(45))) == 45


def test_import_chunk_walks_45_codes_in_three_requests(monkeypatch):
    world = install(monkeypatch, slots=100)
    remaining = [PISTOL_CODE] * 45
    replies = []
    while remaining:
        reply = codes.import_route({"codes": remaining, "destination": "backpack"})
        replies.append(reply)
        assert len(reply["results"]) == reply["processed"]
        remaining = remaining[reply["processed"]:]
        assert reply["remaining"] == len(remaining)
    assert [(r["processed"], r["remaining"], r["imported"]) for r in replies] == [(20, 25, 20), (20, 5, 20), (5, 0, 5)]
    assert len(world.manager.Backpack) == 45
    assert len({item.DefinitionData.UniqueId for item in world.manager.Backpack}) == 45


def test_import_chunk_time_budget_still_imports_one(monkeypatch):
    world = install(monkeypatch)
    _slow_clock(monkeypatch)
    reply = codes.import_chunk([PISTOL_CODE, SHIELD_CODE, PISTOL_CODE], "backpack")
    assert reply["processed"] == 1 and reply["remaining"] == 2 and reply["imported"] == 1
    assert len(world.manager.Backpack) == 1


def test_import_items_has_no_budget(monkeypatch):
    world = install(monkeypatch, slots=100)
    _slow_clock(monkeypatch)
    result = codes.import_items([PISTOL_CODE] * 45, "backpack")
    assert result["processed"] == 45 and result["remaining"] == 0 and len(world.manager.Backpack) == 45


# --- add_verified -----------------------------------------------------------------------------------


def test_add_verified_returns_the_new_entry(monkeypatch):
    world = install(monkeypatch)
    added = codes.add_verified(make_definition(31, weapon=True), True, "bank", 31)
    assert added["id"] == "bank:0:31" and world.bank.used() == 1
    added = codes.add_verified(make_definition(32, weapon=True), True, "backpack", 32)
    assert added["id"] == world.manager.Backpack[0]._path_name()


def test_add_verified_bank_with_two_new_entries_removes_both(monkeypatch):
    world = install(monkeypatch)
    world.bank.TheChest.append(chest_entry(SHIELD_BYTES, weapon=False))
    real_add = world.bank._add

    def twice(definition, ignore_size, unique):
        real_add(definition, weapon=True)
        return real_add(definition, weapon=True)

    world.bank.AddWeaponFromDef = twice
    with pytest.raises(ApiError) as info:
        codes.add_verified(make_definition(31, weapon=True), True, "bank", 31)
    assert info.value.status == 409 and "added 2" in str(info.value)
    assert [bank.entry_unique_id(e) for e in world.bank.TheChest] == [_uid(SHIELD_BYTES)]


def test_add_verified_bank_true_but_nothing_added(monkeypatch):
    world = install(monkeypatch)
    world.bank.AddWeaponFromDef = lambda definition, ignore_size, unique: True
    with pytest.raises(ApiError) as info:
        codes.add_verified(make_definition(31, weapon=True), True, "bank", 31)
    assert info.value.status == 409 and "added 0" in str(info.value)


def test_add_verified_bank_wrong_unique_id_is_removed(monkeypatch):
    world = install(monkeypatch)
    real_add = world.bank._add
    world.bank.AddWeaponFromDef = lambda d, ignore_size, unique: real_add(make_definition(99, weapon=True), weapon=True)
    with pytest.raises(ApiError) as info:
        codes.add_verified(make_definition(31, weapon=True), True, "bank", 31)
    assert info.value.status == 409 and "removed again" in str(info.value)
    assert world.bank.used() == 0


def test_add_verified_backpack_with_two_new_items_removes_both(monkeypatch):
    world = install(monkeypatch)
    kept = _owned(world, SHIELD_BYTES, weapon=False, name="Kept")
    real_add = world.manager._add

    def twice(definition, mark, ready=False):
        real_add(definition, weapon=True, mark=mark)
        real_add(definition, weapon=True, mark=mark)

    world.manager.ClientAddWeaponToBackpack = twice
    with pytest.raises(ApiError) as info:
        codes.add_verified(make_definition(31, weapon=True), True, "backpack", 31)
    assert info.value.status == 409 and "added 2" in str(info.value)
    assert world.manager.Backpack == [kept]


def test_import_into_the_bank_fails_when_the_wrong_item_appears(monkeypatch):
    """The old import trusted the add; now the code fails and the bank is left as it was."""
    world = install(monkeypatch)
    real_add = world.bank._add
    world.bank.AddItemFromDef = lambda d, ignore_size, unique: real_add(make_definition(99, weapon=False), weapon=False)
    result = codes.import_items([SHIELD_CODE], "bank")
    assert result["imported"] == 0 and "UniqueId" in result["results"][0]["error"]
    assert world.bank.used() == 0 and world.journal == []


# --- verified_backup ---------------------------------------------------------------------------------


def test_verified_backup_returns_the_code(monkeypatch):
    world = install(monkeypatch)
    item = _owned(world, PISTOL_BYTES, weapon=True, name="Infinity")
    assert codes.verified_backup(item, item.DefinitionData, True, name="Infinity") == PISTOL_CODE


def test_verified_backup_refuses_a_bad_checksum(monkeypatch):
    install(monkeypatch)
    bad = s.code_to_bytes(bad_checksum_code())
    item = Item(make_definition(_uid(bad), weapon=False), weapon=False, name="Broken", data=bad)
    with pytest.raises(ApiError) as info:
        codes.verified_backup(item, item.DefinitionData, False, name="Broken")
    assert info.value.status == 409 and "could not make a restorable code for Broken" in str(info.value)
    assert "checksum" in str(info.value)


def test_verified_backup_refuses_an_unknown_version(monkeypatch):
    install(monkeypatch)
    data = bytearray(PISTOL_BYTES)
    data[0] = (data[0] & 0x80) | 6  # version 6
    item = Item(make_definition(_uid(PISTOL_BYTES), weapon=True), weapon=True, name="Old", data=bytes(data))
    with pytest.raises(ApiError) as info:
        codes.verified_backup(item, item.DefinitionData, True, name="Old")
    assert "version" in str(info.value)


def test_verified_backup_refuses_when_the_game_cannot_decode(monkeypatch):
    world = install(monkeypatch)
    item = _owned(world, PISTOL_BYTES, weapon=True)

    def no_classes(name):
        raise RuntimeError("no such class")

    monkeypatch.setattr(codes, "_cdo", no_classes)
    with pytest.raises(ApiError) as info:
        codes.verified_backup(item, item.DefinitionData, True)
    assert "could not decode" in str(info.value)


def test_verified_backup_refuses_a_different_level(monkeypatch):
    world = install(monkeypatch)
    item = _owned(world, PISTOL_BYTES, weapon=True)
    world.stage = 31  # the game decodes the code to a different level
    with pytest.raises(ApiError) as info:
        codes.verified_backup(item, item.DefinitionData, True, action="moved")
    assert "GameStage" in str(info.value) and "not moved" in str(info.value)


def test_verified_backup_refuses_a_different_part(monkeypatch):
    world = install(monkeypatch)
    item = _owned(world, PISTOL_BYTES, weapon=True)
    item.DefinitionData.BarrelPartDefinition = Obj("/Game/Parts/Barrel_Special")
    with pytest.raises(ApiError) as info:
        codes.verified_backup(item, item.DefinitionData, True)
    assert "BarrelPartDefinition" in str(info.value)


# --- review item 5: a crafted serial never reaches the game's decoder ------------------------------------


def _crafted_out_of_range_code() -> str:
    """A real weapon code whose body part index is one past its sublibrary, re-signed so the checksum passes."""
    from rtse import item_assets

    data = s.code_to_bytes(PISTOL_CODE)
    plain = s.decrypt(data)
    layout = {f[0]: f for f in s.field_layout(s.read_header(data)["is_weapon"])}
    _, group, start, width = layout["body"]
    sublibrary_bits, asset_bits = item_assets.CONFIGS[group]
    info = s.validate_assets(data)["fields"]["body"]
    count = item_assets.SETS[info["set"]][group][info["sublibrary"]]
    value = count | (info["sublibrary"] << asset_bits)
    if info["set"] != 0:
        value |= 1 << (asset_bits + sublibrary_bits - 1)
    bits = int.from_bytes(plain, "little")
    bits = (bits & ~(((1 << width) - 1) << start)) | (value << start)
    plain = bytearray(bits.to_bytes(len(plain), "little"))
    plain[5:7] = s.checksum(bytes(plain)).to_bytes(2, "big")
    crafted = s.encrypt(bytes(plain))
    assert s.read_header(crafted)["checksum_ok"]
    return s.bytes_to_code(crafted)


def test_crafted_asset_reference_never_reaches_the_game(monkeypatch):
    install(monkeypatch)

    def native(*_args, **_kwargs):
        raise AssertionError("the game decoder was called with an unchecked serial")

    monkeypatch.setattr(codes, "definition_from_serial", native)
    crafted = _crafted_out_of_range_code()
    [entry] = codes.decode(crafted)
    assert entry["ok"] is False and entry["card"] is None and "out of range" in entry["error"]
    result = codes.import_items([crafted], "backpack")
    assert result["imported"] == 0 and "out of range" in result["results"][0]["error"]
