"""rtse/checkpoints.py: save-file checkpoints, the first-write backup, the staged restore and the retention limit.

Every save folder and backup folder lives under pytest's tmp_path. The module's paths are monkeypatched, so the live
Borderlands 2 save folder is never read or written.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from rtse import checkpoints, journal

PUMP_EVERY = checkpoints.PUMP_EVERY
SAVE_DIR_NAME = "76561198000000000"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    clock = itertools.count(1_700_000_000.0, 10.0)  # every checkpoint gets a later time, so ordering is exact
    monkeypatch.setattr(checkpoints, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(checkpoints, "GUESS_ROOT", tmp_path / "SaveData")
    monkeypatch.setattr(checkpoints, "_now", lambda: next(clock))
    checkpoints._reset_for_tests()  # noqa: SLF001
    journal.reset_for_tests()
    yield
    checkpoints._reset_for_tests()  # noqa: SLF001
    journal.reset_for_tests()


def _save_dir() -> Path:
    return checkpoints.GUESS_ROOT / SAVE_DIR_NAME


def _char() -> Path:
    return _save_dir() / "Save000A.sav"


def _make_tree() -> Path:
    """A SaveData tree: an older Save0001.sav, the newest Save000A.sav, a newer Save000A.sav.bak and a profile."""
    folder = _save_dir()
    folder.mkdir(parents=True)
    old = folder / "Save0001.sav"
    new = folder / "Save000A.sav"
    bak = folder / "Save000A.sav.bak"
    old.write_bytes(b"old-save")
    new.write_bytes(b"new-save-contents")
    bak.write_bytes(b"bak-file")
    (folder / "profile.bin").write_bytes(b"profile")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    os.utime(bak, (3_000_000, 3_000_000))
    return folder


def _snapshot(folder: Path) -> dict[str, tuple[bytes, int]]:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir() if p.is_file()}


def _checkpoint_ids() -> list[str]:
    root = checkpoints.BACKUPS_DIR
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _error_status(call) -> int:
    with pytest.raises(checkpoints.CheckpointError) as info:
        call()
    return info.value.status


def _manager_pc(last_loaded=None, file_id=None):
    manager = SimpleNamespace(LastLoadedFilePath=last_loaded)
    return SimpleNamespace(
        GetWillowGlobals=lambda: SimpleNamespace(GetWillowSaveGameManager=lambda: manager),
        SaveGameFileId=file_id,
    )


def _menu(monkeypatch, name: str) -> None:
    monkeypatch.setattr(checkpoints, "_map_name", lambda: name)


def _pump(times: int) -> None:
    for _ in range(times):
        checkpoints.pump()


# --- Locating the save --------------------------------------------------------------------------------------------


def test_guessed_locate_picks_the_newest_sav_and_ignores_bak():
    _make_tree()
    save = checkpoints.locate()
    assert save["source"] == "guessed"
    assert save["character_file"] == str(_char())
    assert save["dir"] == str(_save_dir())
    assert save["profile_file"] == str(_save_dir() / "profile.bin")
    assert save["notes"]


def test_game_path_wins_over_the_guess(game):
    _make_tree()
    game.pc = _manager_pc(last_loaded=str(_save_dir() / "Save0001.sav"), file_id=10)
    save = checkpoints.locate()
    assert save["source"] == "game"
    assert save["character_file"] == str(_save_dir() / "Save0001.sav")


def test_bare_game_name_is_looked_up_in_the_guessed_folder(game):
    _make_tree()
    game.pc = _manager_pc(last_loaded="Save0001.sav")
    save = checkpoints.locate()
    assert save["source"] == "game"
    assert save["character_file"] == str(_save_dir() / "Save0001.sav")


def test_default_save_manager_is_skipped_when_searching(game):
    _make_tree()
    default = SimpleNamespace(
        _path_name=lambda: "Default__WillowSaveGameManager",
        LastLoadedFilePath=str(_char()),
    )
    real = SimpleNamespace(
        _path_name=lambda: "WillowGame.Transient.WillowSaveGameManager_0",
        LastLoadedFilePath=str(_save_dir() / "Save0001.sav"),
    )
    game.all["WillowSaveGameManager"] = [default, real]
    save = checkpoints.locate()
    assert save["source"] == "game"
    assert save["character_file"] == str(_save_dir() / "Save0001.sav")


def test_save_file_id_builds_the_uppercase_hex_name(game):
    _make_tree()
    game.pc = SimpleNamespace(SaveGameFileId=10)  # no save manager: the id is the only game value
    save = checkpoints.locate()
    assert save["source"] == "game_id"
    assert save["character_file"] == str(_char())
    assert any("save file id" in note for note in save["notes"])


def test_nothing_found_gives_nulls_and_never_raises(game):
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError(f"no {name} for you")

    game.pc = Boom()
    save = checkpoints.locate()
    assert save["dir"] is None
    assert save["character_file"] is None
    assert save["profile_file"] is None
    assert save["source"] is None
    assert save["notes"]


def test_nothing_found_with_a_plain_object_for_the_controller(game):
    game.pc = object()
    save = checkpoints.locate()
    assert save["character_file"] is None and save["source"] is None


# --- Creating checkpoints -----------------------------------------------------------------------------------------


def test_create_copies_the_files_and_writes_a_manifest_without_touching_the_source():
    _make_tree()
    source_before = _snapshot(_save_dir())

    checkpoint = checkpoints.create("manual", "before the boss")

    folder = checkpoints.BACKUPS_DIR / checkpoint["id"]
    assert (folder / "Save000A.sav").read_bytes() == b"new-save-contents"
    assert (folder / "profile.bin").read_bytes() == b"profile"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == checkpoint["id"]
    assert manifest["reason"] == "manual"
    assert manifest["note"] == "before the boss"
    assert manifest["source"] == "guessed"
    assert manifest["character_file"] == str(_char())
    assert manifest["profile_file"] == str(_save_dir() / "profile.bin")
    files = {f["name"]: f for f in manifest["files"]}
    assert files["Save000A.sav"]["kind"] == "character"
    assert files["Save000A.sav"]["original"] == str(_char())
    assert files["Save000A.sav"]["size"] == len(b"new-save-contents")
    assert files["Save000A.sav"]["sha256"] == hashlib.sha256(b"new-save-contents").hexdigest()
    assert files["profile.bin"]["kind"] == "profile"

    assert checkpoint["reason"] == "manual" and checkpoint["pending"] is False
    assert "original" not in checkpoint["files"][0]
    assert _snapshot(_save_dir()) == source_before  # same bytes and the same mtime_ns


def test_checkpoint_ids_get_a_suffix_when_the_folder_exists(monkeypatch):
    fake_time = SimpleNamespace(strftime=lambda _fmt: "20260101-000000", time=lambda: 0.0)
    monkeypatch.setattr(checkpoints, "time", fake_time)
    _make_tree()
    ids = [checkpoints.create("manual")["id"] for _ in range(3)]
    assert ids == ["20260101-000000", "20260101-000000-2", "20260101-000000-3"]


def test_create_without_a_save_is_404():
    assert _error_status(lambda: checkpoints.create("manual")) == 404


def test_retention_prunes_the_oldest_first():
    _make_tree()
    checkpoints.set_retention({"retention": 2})
    ids = [checkpoints.create("manual")["id"] for _ in range(3)]
    listed = [c["id"] for c in checkpoints.state()["checkpoints"]]
    assert listed == [ids[2], ids[1]]  # newest first, the oldest was pruned
    assert not (checkpoints.BACKUPS_DIR / ids[0]).exists()


def test_prune_never_deletes_the_checkpoint_a_pending_restore_names():
    _make_tree()
    checkpoints.set_retention({"retention": 1})
    first = checkpoints.create("manual")
    checkpoints.stage_restore({"confirm": True, "id": first["id"]})
    second = checkpoints.create("manual")
    assert _checkpoint_ids() == sorted([first["id"], second["id"]])
    states = {c["id"]: c["pending"] for c in checkpoints.state()["checkpoints"]}
    assert states == {first["id"]: True, second["id"]: False}


# --- The first-write backup ---------------------------------------------------------------------------------------


def _verified(game) -> None:
    """The game names the save's full path, so locate() is verified."""
    game.pc = _manager_pc(last_loaded=str(_char()))


def test_first_write_backs_up_a_verified_save_once_per_character_and_reports_it(game):
    _make_tree()
    _verified(game)
    notice = checkpoints.before_first_write("/api/character/set")
    assert notice == {"backup": {"checkpoint": _checkpoint_ids()[0], "file": str(_char())}}
    assert checkpoints.before_first_write("/api/ammo/set") is None  # once per character
    assert len(_checkpoint_ids()) == 1
    auto = checkpoints.state()["auto"]
    assert auto["done"] is True and auto["error"] is None and auto["blocking"] is False


def test_first_write_refuses_a_guessed_save_with_423_until_backed_up_or_skipped():
    _make_tree()  # no game: the save is only a guess
    assert checkpoints.state()["auto"]["blocking"] is True
    with pytest.raises(journal.WriteBlocked, match="Couldn't back up your save before the first change: RTSE couldn't confirm") as err:
        checkpoints.before_first_write("/api/character/set")
    assert err.value.status == 423
    assert _checkpoint_ids() == []  # a guess is never copied automatically
    assert "only guessed Save000A.sav" in checkpoints.state()["auto"]["error"]

    # "Back up now": the user saw the path and asked for the copy, so it counts
    checkpoints.POST_ROUTES["/api/checkpoints/create"]({"note": ""})
    assert checkpoints.before_first_write("/api/character/set") is None
    assert checkpoints.state()["auto"]["blocking"] is False


def test_continue_without_a_backup_is_an_explicit_per_session_choice():
    _make_tree()
    assert _error_status(lambda: checkpoints.POST_ROUTES["/api/checkpoints/skip_backup"]({})) == 400
    result = checkpoints.POST_ROUTES["/api/checkpoints/skip_backup"]({"confirm": True})
    assert result["applied"] == {"skipped": True} and result["auto"]["skipped"] and not result["auto"]["blocking"]
    assert checkpoints.before_first_write("/api/character/set") is None
    assert _checkpoint_ids() == []
    checkpoints._reset_for_tests()  # noqa: SLF001 - a new session asks again
    with pytest.raises(journal.WriteBlocked):
        checkpoints.before_first_write("/api/character/set")


def test_a_failed_copy_of_a_verified_save_blocks_the_write_and_is_retried(monkeypatch, game):
    _make_tree()
    _verified(game)
    real_snapshot = checkpoints._snapshot  # noqa: SLF001

    def full_disk(*_args, **_kwargs):
        raise checkpoints.CheckpointError(409, "the disk is full")

    monkeypatch.setattr(checkpoints, "_snapshot", full_disk)
    with pytest.raises(journal.WriteBlocked, match="the copy failed \\(the disk is full\\)"):
        checkpoints.before_first_write("/api/character/set")
    assert checkpoints.state()["auto"]["blocking"] is True
    monkeypatch.setattr(checkpoints, "_snapshot", real_snapshot)
    assert checkpoints.before_first_write("/api/character/set")["backup"]["file"] == str(_char())
    assert checkpoints.state()["auto"]["error"] is None


def test_an_unexpected_failure_blocks_rather_than_writing_without_a_backup(monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(checkpoints, "locate", boom)
    with pytest.raises(journal.WriteBlocked, match="unexpectedly"):
        checkpoints.before_first_write("/api/character/set")


def test_no_save_found_blocks_too():
    with pytest.raises(journal.WriteBlocked, match="no save file was found"):
        checkpoints.before_first_write("/api/character/set")


def test_first_write_skips_checkpoint_routes_and_parts_rescan():
    _make_tree()
    for label in ("/api/checkpoints/create", "/api/parts/rescan", "/api/lab/session/start", "/api/lab/snapshot"):
        assert checkpoints.before_first_write(label) is None
    assert _checkpoint_ids() == []


def test_an_undo_is_a_game_write_so_it_goes_through_the_backup_gate():
    _make_tree()  # guessed: not verified
    # an undo, a part swap made from the weapon lab, and the Bank & codes writes (#10)
    for label in ("/api/history/undo", "/api/item/set_part", "/api/codes/import", "/api/inventory/duplicate",
                  "/api/inventory/delete", "/api/inventory/move", "/api/inventory/import"):
        with pytest.raises(journal.WriteBlocked):
            checkpoints.before_first_write(label)


def test_the_hook_blocks_a_real_write_through_the_route_registry(game):
    """End to end: the begin hook refuses the POST with 423 before the route runs; once verified, the write's
    response carries the backup it made."""
    from rtse import api  # noqa: PLC0415
    from rtse.routing import ApiError  # noqa: PLC0415

    ran: list[dict] = []

    def route(params):
        ran.append(params)
        journal.record("character.money", "money", 1, 2)
        return {"ok": True}

    write = api._journaled_write("/api/character/set", route)  # noqa: SLF001
    _make_tree()
    checkpoints.enable()
    with pytest.raises(ApiError) as err:
        write({"field": "money", "value": 2})
    assert err.value.status == 423 and ran == [] and journal.entries() == []
    _verified(game)
    result = write({"field": "money", "value": 2})
    assert result["ok"] and result["backup"]["file"] == str(_char()) and len(ran) == 1
    assert "backup" not in write({"field": "money", "value": 3})  # only the write that made it
    checkpoints.disable()


def test_game_id_in_two_folders_is_not_verified(game):
    _make_tree()
    other = checkpoints.GUESS_ROOT / "76561198000000001"
    other.mkdir()
    (other / "Save000A.sav").write_bytes(b"someone else")
    game.pc = _manager_pc(file_id=10)
    save = checkpoints.locate()
    assert save["source"] == "game_id" and save["verified"] is False


def test_enable_registers_the_begin_hook_and_disable_removes_it(game):
    _make_tree()
    _verified(game)
    checkpoints.enable()
    checkpoints.enable()  # idempotent
    with journal.transaction("/api/character/set") as tx:
        journal.record("character.money", "money", 1, 2)
    assert tx.notices and len(_checkpoint_ids()) == 1
    checkpoints.disable()
    checkpoints._backed_up.clear()  # noqa: SLF001
    with journal.transaction("/api/character/set"):
        pass
    assert len(_checkpoint_ids()) == 1


# --- Staged restore -----------------------------------------------------------------------------------------------


class Game:
    """Drives the restore gate: the map, whether a character is loaded, and the clocks."""

    def __init__(self, monkeypatch) -> None:
        self.map = "SanctuaryMap"
        self.loaded: bool | None = True
        self.mono = 1000.0
        self.wall = 1_800_000_000.0
        monkeypatch.setattr(checkpoints, "_map_name", lambda: self.map)
        monkeypatch.setattr(checkpoints, "character_loaded", lambda: self.loaded)
        monkeypatch.setattr(checkpoints, "_clock", lambda: self.mono)
        monkeypatch.setattr(checkpoints, "_now", lambda: self.wall)

    def at_menu(self) -> None:
        self.map, self.loaded = "MenuMap", False

    def tick(self, seconds: float = 1.0) -> None:
        """One pump check, `seconds` after the last one."""
        self.mono += seconds
        self.wall += seconds
        _pump(PUMP_EVERY)


def _staged(include_profile: bool = False) -> str:
    """A tree, a backup of it, then the live save changed in game; the backup is staged. Returns the checkpoint id."""
    _make_tree()
    checkpoint = checkpoints.create("manual")["id"]
    _char().write_bytes(b"changed in game")
    (_save_dir() / "profile.bin").write_bytes(b"profile changed")
    checkpoints.stage_restore({"confirm": True, "id": checkpoint, "include_profile": include_profile})
    return checkpoint


def _pending() -> dict:
    return checkpoints.state()["pending_restore"]


def _last() -> dict:
    return checkpoints.state()["last_restore"]


def test_stage_restore_needs_an_explicit_confirm():
    _make_tree()
    checkpoint = checkpoints.create("manual")
    assert _error_status(lambda: checkpoints.stage_restore({"id": checkpoint["id"]})) == 400
    assert _error_status(lambda: checkpoints.stage_restore({"id": checkpoint["id"], "confirm": "yes"})) == 400


def test_stage_restore_unknown_id_is_404():
    _make_tree()
    assert _error_status(lambda: checkpoints.stage_restore({"confirm": True, "id": "20200101-000000"})) == 404
    assert _error_status(lambda: checkpoints.stage_restore({"confirm": True, "id": "../etc"})) == 404


def test_stage_restore_checksum_mismatch_is_409_and_writes_nothing():
    _make_tree()
    checkpoint = checkpoints.create("manual")
    (checkpoints.BACKUPS_DIR / checkpoint["id"] / "Save000A.sav").write_bytes(b"tampered")
    before = _snapshot(_save_dir())
    assert _error_status(lambda: checkpoints.stage_restore({"confirm": True, "id": checkpoint["id"]})) == 409
    assert _snapshot(_save_dir()) == before and _pending() is None


def test_stage_restore_missing_backup_file_is_409():
    _make_tree()
    checkpoint = checkpoints.create("manual")
    (checkpoints.BACKUPS_DIR / checkpoint["id"] / "Save000A.sav").unlink()
    assert _error_status(lambda: checkpoints.stage_restore({"confirm": True, "id": checkpoint["id"]})) == 409


def test_stage_restore_writes_only_the_pending_file(monkeypatch):
    Game(monkeypatch)
    _make_tree()
    checkpoint = checkpoints.create("manual")["id"]
    before = _snapshot(_save_dir())
    pending = checkpoints.stage_restore({"confirm": True, "id": checkpoint})
    assert _snapshot(_save_dir()) == before
    assert pending["status"] == "staged" and pending["files"][0]["to"] == str(_char())
    assert pending["files"][0]["sha256"] == hashlib.sha256(b"new-save-contents").hexdigest()


def test_cancel_restore_removes_the_pending_file():
    checkpoint = _staged()
    assert checkpoints.cancel_restore() == checkpoint
    assert _pending() is None and checkpoints.cancel_restore() is None


def test_restore_waits_for_the_menu_with_no_character_and_stable_files_then_verifies(monkeypatch):
    game = Game(monkeypatch)
    checkpoint = _staged(include_profile=True)
    game.tick()
    assert _pending()["status"] == "staged" and "main menu" in _pending()["detail"]
    game.map = "MenuMap"  # at the menu, but a character is still loaded
    game.tick()
    assert _pending()["status"] == "staged" and "character" in _pending()["detail"]
    game.loaded = None  # can't tell: never write
    game.tick()
    assert "can't tell" in _pending()["detail"] and _char().read_bytes() == b"changed in game"

    game.at_menu()
    game.tick()  # first look at the files: they must stay unchanged for STABLE_SECONDS
    assert _pending()["status"] == "waiting"
    game.tick(3)
    _char().write_bytes(b"the game saved again")  # still saving: the clock starts over
    game.tick(3)
    assert _pending()["status"] == "waiting" and _char().read_bytes() == b"the game saved again"
    game.tick(3)
    assert _pending()["status"] == "waiting"
    game.tick(3)  # 6 s unchanged: written
    assert _char().read_bytes() == b"new-save-contents" and (_save_dir() / "profile.bin").read_bytes() == b"profile"
    pending = _pending()
    assert pending["status"] == "verifying" and pending["safety_checkpoint"]
    safety = pending["safety_checkpoint"]
    assert (checkpoints.BACKUPS_DIR / safety / "Save000A.sav").read_bytes() == b"the game saved again"
    game.tick(1)
    assert _pending()["status"] == "verifying"  # not yet: VERIFY_SECONDS
    game.tick(5)
    assert _pending() is None
    last = _last()
    assert last["ok"] is True and last["status"] == "ok" and last["id"] == checkpoint
    assert last["files"] == ["Save000A.sav", "profile.bin"] and last["safety_checkpoint"] == safety
    assert not list(_save_dir().glob("*.rtse-tmp"))


def _to_writing(game) -> None:
    game.at_menu()
    game.tick()
    game.tick(6)


def test_a_failure_on_the_second_file_puts_the_first_back_and_is_persisted(monkeypatch):
    game = Game(monkeypatch)
    checkpoint = _staged(include_profile=True)
    real = checkpoints._replace  # noqa: SLF001

    failed: list[str] = []

    def fail_on_profile(source, dest):  # the first write of profile.bin fails; the rollback's writes work
        if dest.name == "profile.bin" and not failed:
            failed.append(dest.name)
            raise OSError(28, "No space left on device")
        real(source, dest)

    monkeypatch.setattr(checkpoints, "_replace", fail_on_profile)
    _to_writing(game)
    # the .sav had been replaced; the rollback put the live one back, so the files are not mixed
    assert _char().read_bytes() == b"changed in game"
    assert (_save_dir() / "profile.bin").read_bytes() == b"profile changed"
    pending = _pending()
    assert pending["status"] == "failed" and "Writing profile.bin failed (No space left on device)" in pending["error"]
    assert "put back" in pending["error"]
    assert _last()["status"] == "rolled_back" and _last()["ok"] is False

    # on disk, so a restart doesn't retry it by itself
    checkpoints._reset_for_tests()  # noqa: SLF001
    monkeypatch.setattr(checkpoints, "_replace", real)
    game.tick(10)
    game.tick(10)
    assert _char().read_bytes() == b"changed in game" and _pending()["status"] == "failed"
    # both the staged checkpoint and the safety copy stay pinned until it is cancelled or staged again
    pins = {c["id"]: c["pinned"] for c in checkpoints.state()["checkpoints"]}
    assert pins[checkpoint] and pins[pending["safety_checkpoint"]]


def test_a_restart_in_the_middle_of_a_restore_rolls_back(monkeypatch):
    game = Game(monkeypatch)
    _staged(include_profile=True)
    real = checkpoints._replace  # noqa: SLF001

    class Crash(BaseException):  # not an Exception: like the game closing, nothing catches it
        pass

    def crash_on_profile(source, dest):
        if dest.name == "profile.bin" and checkpoints._read_pending()["status"] == "applying":  # noqa: SLF001
            raise Crash
        real(source, dest)

    monkeypatch.setattr(checkpoints, "_replace", crash_on_profile)
    game.at_menu()
    game.tick()
    with pytest.raises(Crash):
        game.tick(6)
    assert _char().read_bytes() == b"new-save-contents"  # half done: the .sav is restored, profile.bin isn't
    assert _pending()["status"] == "applying"

    checkpoints._reset_for_tests()  # noqa: SLF001 - the game restarts: memory is gone, the files remain
    monkeypatch.setattr(checkpoints, "_replace", real)
    game.tick()
    assert _char().read_bytes() == b"changed in game" and (_save_dir() / "profile.bin").read_bytes() == b"profile changed"
    assert _pending()["status"] == "failed" and "stopped in the middle" in _pending()["error"]
    assert _last()["status"] == "rolled_back"


def test_with_retention_1_the_staged_checkpoint_and_the_safety_copy_survive(monkeypatch):
    game = Game(monkeypatch)
    checkpoint = _staged()
    checkpoints.set_retention({"retention": 1})
    for _ in range(3):
        checkpoints.create("manual")  # each prunes to 1 unpinned
    assert checkpoint in _checkpoint_ids()
    real = checkpoints._replace  # noqa: SLF001

    failed: list[str] = []

    def fail_once(source, dest):  # the restore's write fails; the rollback's works
        if not failed:
            failed.append(dest.name)
            raise OSError(5, "Access is denied")
        real(source, dest)

    monkeypatch.setattr(checkpoints, "_replace", fail_once)
    _to_writing(game)
    safety = _pending()["safety_checkpoint"]
    for _ in range(3):
        checkpoints.create("manual")
    ids = _checkpoint_ids()
    assert checkpoint in ids and safety in ids
    assert len([i for i in ids if i not in (checkpoint, safety)]) == 1
    checkpoints.cancel_restore()
    checkpoints.create("manual")  # unpinned now, so retention applies again
    assert checkpoint not in _checkpoint_ids() and safety not in _checkpoint_ids()


def test_a_rollback_that_fails_pins_the_safety_copy_and_says_so(monkeypatch):
    game = Game(monkeypatch)
    _staged(include_profile=True)
    real = checkpoints._replace  # noqa: SLF001
    calls = {"n": 0}

    def fail_after_the_first(source, dest):
        if checkpoints._read_pending()["status"] == "applying":  # noqa: SLF001
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError(5, "Access is denied")
        real(source, dest)

    monkeypatch.setattr(checkpoints, "_replace", fail_after_the_first)
    _to_writing(game)
    pending = _pending()
    assert pending["status"] == "failed" and "failed too" in pending["error"] and "by hand" in pending["error"]
    checkpoints.cancel_restore()
    safety = next(c for c in checkpoints.state()["checkpoints"] if c["id"] == pending["safety_checkpoint"])
    assert safety["pinned"] and "failed halfway" in safety["pinned"]  # still kept after the cancel
    assert _error_status(lambda: checkpoints.POST_ROUTES["/api/checkpoints/unpin"]({"id": "20200101-000000"})) == 404
    checkpoints.POST_ROUTES["/api/checkpoints/unpin"]({"id": safety["id"]})
    assert next(c for c in checkpoints.state()["checkpoints"] if c["id"] == safety["id"])["pinned"] is None


def test_the_game_writing_over_the_restore_is_reported_and_the_safety_copy_kept(monkeypatch):
    game = Game(monkeypatch)
    _staged()
    _to_writing(game)
    assert _pending()["status"] == "verifying"
    _char().write_bytes(b"the game saved over it")
    game.tick(6)
    pending = _pending()
    assert pending["status"] == "failed" and "The game wrote over Save000A.sav" in pending["error"]
    assert _last()["status"] == "overwritten"
    safety = next(c for c in checkpoints.state()["checkpoints"] if c["id"] == pending["safety_checkpoint"])
    assert "wrote over" in safety["pinned"]
    assert _char().read_bytes() == b"the game saved over it"  # RTSE doesn't fight the game


def test_cancel_and_restage_are_refused_while_writing(monkeypatch):
    game = Game(monkeypatch)
    checkpoint = _staged()
    _to_writing(game)
    assert _pending()["status"] == "verifying"
    assert _error_status(checkpoints.cancel_restore) == 409
    assert _error_status(lambda: checkpoints.stage_restore({"confirm": True, "id": checkpoint})) == 409


def test_a_failed_restore_staged_again_tries_again(monkeypatch):
    game = Game(monkeypatch)
    checkpoint = _staged()
    real = checkpoints._replace  # noqa: SLF001

    def fail(source, dest):
        if checkpoints._read_pending()["status"] == "applying":  # noqa: SLF001
            raise OSError(5, "Access is denied")
        real(source, dest)

    monkeypatch.setattr(checkpoints, "_replace", fail)
    _to_writing(game)
    assert _pending()["status"] == "failed"
    monkeypatch.setattr(checkpoints, "_replace", real)
    checkpoints.stage_restore({"confirm": True, "id": checkpoint})
    game.tick()
    game.tick(6)
    game.tick(6)
    assert _char().read_bytes() == b"new-save-contents" and _last()["ok"] is True


def test_restore_into_a_missing_file_needs_no_safety_copy_and_rolls_back_by_deleting(monkeypatch):
    game = Game(monkeypatch)
    _staged()
    _char().unlink()  # the live save was deleted
    _to_writing(game)
    game.tick(6)
    assert _char().read_bytes() == b"new-save-contents" and _last()["ok"] is True and _last()["safety_checkpoint"] is None


def test_pump_survives_a_corrupt_pending_file(monkeypatch):
    game = Game(monkeypatch)
    _make_tree()
    checkpoints.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    (checkpoints.BACKUPS_DIR / "pending_restore.json").write_text("{not json", encoding="utf-8")
    game.at_menu()
    game.tick()
    assert _pending() is None


def test_interrupted_copies_without_a_manifest_are_pruned_after_a_grace_period():
    _make_tree()
    checkpoints.BACKUPS_DIR.mkdir(parents=True)
    old = checkpoints.BACKUPS_DIR / "20200101-000000"
    recent = checkpoints.BACKUPS_DIR / "20200101-000001"
    for folder in (old, recent):
        folder.mkdir()
        (folder / "Save000A.sav").write_bytes(b"half")
    long_ago = 1_000_000
    os.utime(old, (long_ago, long_ago))
    checkpoints.create("manual")
    ids = _checkpoint_ids()
    assert old.name not in ids and recent.name in ids  # recent: still inside the grace period


def test_the_newest_folder_is_never_pruned_even_without_a_manifest():
    _make_tree()
    checkpoints.BACKUPS_DIR.mkdir(parents=True)
    only = checkpoints.BACKUPS_DIR / "20200101-000000"
    only.mkdir()
    os.utime(only, (1_000_000, 1_000_000))
    checkpoints._prune()  # noqa: SLF001
    assert only.exists()


def test_checkpoints_say_whether_they_target_the_current_save(game):
    _make_tree()
    _verified(game)
    mine = checkpoints.create("manual")["id"]
    game.pc = _manager_pc(last_loaded=str(_save_dir() / "Save0001.sav"))  # now another character is loaded
    other = checkpoints.create("manual")["id"]
    views = {c["id"]: c for c in checkpoints.state()["checkpoints"]}
    assert views[mine]["matches_current"] is False and views[other]["matches_current"] is True
    assert views[mine]["character_file"] == str(_char())


# --- Settings -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, 201, "5", True, 2.5, None])
def test_set_retention_rejects_values_outside_1_to_200(bad):
    assert _error_status(lambda: checkpoints.set_retention({"retention": bad})) == 400


def test_set_retention_saves_the_setting_and_reports_pruned_ids():
    _make_tree()
    ids = [checkpoints.create("manual")["id"] for _ in range(3)]
    pruned = checkpoints.set_retention({"retention": 1})
    assert sorted(pruned) == sorted(ids[:2])
    settings = json.loads((checkpoints.BACKUPS_DIR / "settings.json").read_text(encoding="utf-8"))
    assert settings == {"retention": 1}
    assert checkpoints.state()["retention"] == 1


def test_default_retention_is_20():
    assert checkpoints.state()["retention"] == 20
    assert checkpoints.state()["retention_range"] == [1, 200]


# --- State, debug and routes --------------------------------------------------------------------------------------


def test_state_has_the_contract_shape(monkeypatch):
    _make_tree()
    checkpoints.create("manual", "first")
    _menu(monkeypatch, "MenuMap")
    state = checkpoints.GET_ROUTES["/api/checkpoints"]({})
    assert set(state) == {
        "save", "backups_dir", "retention", "retention_range", "auto", "checkpoints",
        "pending_restore", "last_restore", "at_main_menu", "restore_gate",
    }  # fmt: skip
    assert set(state["save"]) == {"dir", "character_file", "profile_file", "source", "verified", "notes"}
    assert state["at_main_menu"] is True
    assert set(state["checkpoints"][0]) == {"id", "time", "reason", "note", "character_file", "files", "pending",
                                            "pinned", "matches_current"}
    assert state["checkpoints"][0]["note"] == "first"
    assert set(state["auto"]) == {"done", "error", "checkpoint", "skipped", "blocking"}
    gate = state["restore_gate"]
    assert set(gate) == {"at_main_menu", "character_loaded", "stable_seconds", "verify_seconds", "rule"}
    assert "unverified" in gate["rule"] and "no character is loaded" in gate["rule"]


def test_at_main_menu_is_none_when_the_map_cannot_be_read():
    assert checkpoints.at_main_menu() is None  # no ENGINE in the stub, so the read fails


def test_debug_info_shows_the_raw_game_values_and_never_raises(game):
    _make_tree()
    game.pc = _manager_pc(last_loaded="C:/somewhere/Save000A.sav", file_id=10)
    info = checkpoints.debug_info()
    assert "error" not in info
    assert info["game"]["LastLoadedFilePath"] == "C:/somewhere/Save000A.sav"
    assert info["game"]["SaveGameFileId"] == 10
    assert info["guessed_folders"] == [str(_save_dir())]
    assert {Path(f["path"]).name for f in info["save_files_seen"]} == {"Save0001.sav", "Save000A.sav"}
    assert info["locate"]["source"] == "game_id"


def test_debug_info_survives_a_broken_game_object(game):
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError(name)

    game.pc = Boom()
    info = checkpoints.debug_info()
    assert isinstance(info, dict)


def test_routes_record_checkpoint_changes_in_the_journal():
    _make_tree()
    with journal.transaction("/api/checkpoints/create", {"note": "x"}):
        result = checkpoints.POST_ROUTES["/api/checkpoints/create"]({"note": "x"})
    assert result["applied"]["checkpoint"]["note"] == "x"
    assert result["checkpoints"][0]["id"] == result["applied"]["checkpoint"]["id"]
    created = journal.entries()[-1]
    assert [r["kind"] for r in created["records"]] == ["checkpoint.create"]

    checkpoint_id = result["applied"]["checkpoint"]["id"]
    with journal.transaction("/api/checkpoints/restore", {"id": checkpoint_id}):
        staged = checkpoints.POST_ROUTES["/api/checkpoints/restore"](
            {"confirm": True, "id": checkpoint_id, "include_profile": False}
        )
    assert staged["applied"]["staged"]["id"] == checkpoint_id
    restored = journal.entries()[-1]
    assert restored["records"][0]["kind"] == "checkpoint.restore"
    assert restored["records"][0]["after"] == {"staged": True, "include_profile": False}

    with journal.transaction("/api/checkpoints/cancel_restore", {}):
        cancelled = checkpoints.POST_ROUTES["/api/checkpoints/cancel_restore"]({})
    assert cancelled["applied"] == {"cancelled": checkpoint_id}

    with journal.transaction("/api/checkpoints/settings", {"retention": 2}):
        settings = checkpoints.POST_ROUTES["/api/checkpoints/settings"]({"retention": 2})
    assert settings["applied"]["retention"] == 2 and settings["applied"]["pruned"] == []


def test_route_errors_are_api_errors_the_registry_passes_through():
    from rtse.routing import ApiError  # noqa: PLC0415

    assert checkpoints.ROUTE_ERROR is checkpoints.CheckpointError
    assert issubclass(checkpoints.CheckpointError, ApiError)
    with pytest.raises(checkpoints.CheckpointError) as info:
        checkpoints.POST_ROUTES["/api/checkpoints/restore"]({"id": "x"})
    assert info.value.status == 400


def test_the_safety_checkpoint_keeps_the_file_being_overwritten_not_the_newest_save(monkeypatch):
    """The restore target is Save000A.sav; if another character (Save0001.sav) is now the newest save, the safety copy
    must still be of Save000A.sav, the file the restore overwrites."""
    game = Game(monkeypatch)
    _staged()
    other = _save_dir() / "Save0001.sav"
    os.utime(other, (9_000_000, 9_000_000))  # now the newest: locate() would pick it
    _to_writing(game)
    safety = checkpoints._load_manifest(_pending()["safety_checkpoint"])  # noqa: SLF001
    assert [f["name"] for f in safety["files"]] == ["Save000A.sav"]
    assert (checkpoints.BACKUPS_DIR / safety["id"] / "Save000A.sav").read_bytes() == b"changed in game"
    assert other.read_bytes() == b"old-save"


def test_a_manifest_cannot_send_a_restore_somewhere_else(tmp_path):
    _make_tree()
    checkpoint = checkpoints.create("manual")
    manifest_path = checkpoints.BACKUPS_DIR / checkpoint["id"] / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["files"][0]["original"] = str(tmp_path / "elsewhere" / "evil.dll")
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(checkpoints.CheckpointError, match="unexpected place") as err:
        checkpoints.stage_restore({"confirm": True, "id": checkpoint["id"]})
    assert err.value.status == 409


# --- Review round 2 -----------------------------------------------------------------------------------------------


def test_a_target_that_cant_be_stated_is_never_ready(monkeypatch):
    game = Game(monkeypatch)
    _staged()
    real = checkpoints._stat  # noqa: SLF001

    def denied(path):
        if path.name == "Save000A.sav":
            raise PermissionError(13, "Access is denied")
        return real(path)

    monkeypatch.setattr(checkpoints, "_stat", denied)
    game.at_menu()
    for _ in range(5):
        game.tick(5)  # 25 s at the menu: a stable-looking error must never open the gate
    pending = _pending()
    assert pending["status"] == "waiting" and "can't read Save000A.sav (Access is denied)" in pending["detail"]
    assert _char().read_bytes() == b"changed in game"
    monkeypatch.setattr(checkpoints, "_stat", real)
    game.tick(1)
    game.tick(6)
    assert _pending()["status"] == "verifying"  # readable again: it waits the full time from then, then writes


def test_a_missing_target_is_explicitly_stable_and_said(monkeypatch):
    game = Game(monkeypatch)
    _staged()
    _char().unlink()
    game.at_menu()
    game.tick()
    assert "isn't in the save folder; the restore will create it" in _pending()["detail"]


def test_a_failed_safety_pin_blocks_cancelling_until_it_is_on_disk(monkeypatch):
    game = Game(monkeypatch)
    checkpoint = _staged()
    _to_writing(game)
    _char().write_bytes(b"the game saved over it")
    real = checkpoints._set_pin  # noqa: SLF001

    def broken(_checkpoint_id, _reason):
        raise checkpoints.CheckpointError(409, "writing the manifest failed (disk full)")

    monkeypatch.setattr(checkpoints, "_set_pin", broken)
    game.tick(6)
    pending = _pending()
    safety = pending["safety_checkpoint"]
    assert pending["status"] == "failed" and "couldn't mark the safety backup" in pending["error"]
    assert pending["pin_required"] == safety

    with pytest.raises(checkpoints.CheckpointError, match=f"Couldn't mark the safety backup {safety} as kept") as err:
        checkpoints.cancel_restore()
    assert err.value.status == 409 and _pending() is not None
    with pytest.raises(checkpoints.CheckpointError, match="Couldn't mark the safety backup"):
        checkpoints.stage_restore({"confirm": True, "id": checkpoint})  # replacing it would drop it too
    checkpoints.set_retention({"retention": 1})
    for _ in range(3):
        checkpoints.create("manual")
    assert safety in _checkpoint_ids()  # still protected by the pending restore

    monkeypatch.setattr(checkpoints, "_set_pin", real)  # the disk works again
    assert checkpoints.cancel_restore() == checkpoint  # the pin is written first, then the restore is dropped
    view = next(c for c in checkpoints.state()["checkpoints"] if c["id"] == safety)
    assert "wrote over" in view["pinned"]
    for _ in range(3):
        checkpoints.create("manual")
    assert safety in _checkpoint_ids()


def test_state_json_is_fsynced_before_it_replaces_the_old_file(monkeypatch, tmp_path):
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(checkpoints.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(checkpoints.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    checkpoints._write_json(tmp_path / "state.json", {"a": 1})  # noqa: SLF001
    assert order == ["fsync", "replace"] and json.loads((tmp_path / "state.json").read_text()) == {"a": 1}


# --- Final round: the restore runs on the game thread, gate and write in one call -------------------------------


def test_the_gate_is_read_and_the_restore_written_in_the_same_call(monkeypatch):
    game = Game(monkeypatch)
    _staged()
    game.at_menu()
    game.tick()  # first look at the files
    game.mono += 6
    game.wall += 6  # now they have been unchanged long enough
    events: list[tuple[str, int]] = []
    call = {"n": 0}
    real_replace = checkpoints._replace  # noqa: SLF001
    monkeypatch.setattr(checkpoints, "_map_name", lambda: (events.append(("gate", call["n"])), game.map)[1])
    monkeypatch.setattr(checkpoints, "character_loaded",
                        lambda: (events.append(("pawn", call["n"])), game.loaded)[1])
    monkeypatch.setattr(checkpoints, "_replace", lambda a, b: (events.append(("write", call["n"])), real_replace(a, b))[1])
    for n in range(PUMP_EVERY):
        call["n"] = n
        checkpoints.pump()
    writes = [i for i, e in enumerate(events) if e[0] == "write"]
    assert writes and {events[i][1] for i in writes} == {PUMP_EVERY - 1}  # written inside one pump() call...
    gate = events.index(("gate", PUMP_EVERY - 1))
    pawn = events.index(("pawn", PUMP_EVERY - 1))
    assert gate < writes[0] and pawn < writes[0]  # ...the same call that read the gate, before writing
    assert _pending()["status"] == "verifying" and _char().read_bytes() == b"new-save-contents"


def test_a_character_loaded_between_ticks_stops_the_write(monkeypatch):
    game = Game(monkeypatch)
    _staged()
    game.at_menu()
    game.tick()
    game.loaded = True  # the player loads a character just before the tick that would have written
    game.tick(6)
    assert _char().read_bytes() == b"changed in game" and _pending()["status"] == "staged"
    game.loaded = False
    game.tick()  # the files have been unchanged all along, so the first tick with no character writes
    assert _pending()["status"] == "verifying" and _char().read_bytes() == b"new-save-contents"


def test_the_restore_result_says_how_long_it_paused_the_game(monkeypatch):
    game = Game(monkeypatch)
    _staged()
    _to_writing(game)
    assert isinstance(_pending()["paused_ms"], int)
    game.tick(6)
    last = _last()
    assert last["ok"] is True and isinstance(last["paused_ms"], int) and last["paused_ms"] >= 0
    assert any("paused the game for" in line for _level, line in checkpoints_log())


def test_disable_leaves_a_pending_restore_for_the_next_enable(monkeypatch):
    game = Game(monkeypatch)
    _staged()
    checkpoints.enable()
    checkpoints.disable()
    assert _pending()["status"] == "staged"  # untouched on disk
    checkpoints.enable()
    _to_writing(game)
    assert _pending()["status"] == "verifying"
    checkpoints.disable()


def checkpoints_log() -> list[tuple[str, str]]:
    import unreal_stub  # noqa: PLC0415

    return list(unreal_stub.STATE.log)
