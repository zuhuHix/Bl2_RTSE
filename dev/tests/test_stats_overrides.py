"""Stat overrides: the version 2 store, the migration from version 1, and how overrides follow individual guns.

Overrides are written to a tmp_path copy of the file (stats.OVERRIDES_FILE is monkeypatched), never the real one.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from rtse import stats


def make_gun(
    uid: int | None = 7,
    *,
    stage: int = 5,
    barrel: str = "Barrel.A",
    clip: int = 10,
    fire: float = 0.5,
) -> SimpleNamespace:
    """A weapon with every field the fingerprint reads, as plain strings."""
    data = SimpleNamespace(**{field: field.lower() for field in stats.FINGERPRINT_FIELDS})
    data.GameStage = stage
    data.ManufacturerGradeIndex = stage
    data.BarrelPartDefinition = barrel
    if uid is not None:
        data.UniqueId = uid
    return SimpleNamespace(
        Class=SimpleNamespace(Name="WillowWeapon"),
        DefinitionData=data,
        ClipSize=clip,
        ClipSizeBaseValue=clip,
        FireInterval=fire,
        FireIntervalBaseValue=fire,
        GetHumanReadableName=lambda: "Logan's Gun",
    )


@pytest.fixture(autouse=True)
def overrides_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    path = tmp_path / "overrides.json"
    monkeypatch.setattr(stats, "OVERRIDES_FILE", path)
    stats.load()
    yield path
    stats.load()  # Clears the module store before monkeypatch restores the real path


def saved(path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def legacy_file(path, gun: SimpleNamespace, stat_values: dict[str, float], *, indent: int | None = None) -> bytes:
    """Writes a version 1 file holding one entry for this gun's fingerprint, and returns the bytes."""
    raw = json.dumps({stats.fingerprint(gun): {"label": "Logan's Gun", "stats": stat_values}}, indent=indent)
    raw_bytes = raw.encode("utf-8")
    path.write_bytes(raw_bytes)
    return raw_bytes


# --- Identity ------------------------------------------------------------------------------------


def test_identity_is_uid_and_fingerprint(overrides_file):
    gun = make_gun(uid=42)
    assert stats.identity(gun) == f"42#{stats.fingerprint(gun)}"
    assert stats.unique_id(gun) == 42


def test_unreadable_uid_is_zero_and_identical_guns_share_it(overrides_file):
    first, second = make_gun(uid=None), make_gun(uid=None)
    assert stats.identity(first).startswith("0#")
    assert stats.identity(first) == stats.identity(second)


def test_identity_is_none_for_anything_that_is_not_a_weapon(overrides_file):
    assert stats.identity(SimpleNamespace(Class=SimpleNamespace(Name="ShieldItem"))) is None


# --- Migration -----------------------------------------------------------------------------------


def test_migration_copies_the_old_file_byte_for_byte_before_changing_it(overrides_file):
    original = b'{"Fp|one": {"label":"Gun","stats":{"ClipSize": 20}},\n "Fp|two": {"label": "Other", "stats": {"FireInterval": 0.25}}}'
    overrides_file.write_bytes(original)

    stats.load()

    assert overrides_file.with_name("overrides.v1.bak.json").read_bytes() == original
    data = saved(overrides_file)
    assert data["version"] == 2
    assert data["weapons"] == {}
    assert data["legacy"] == json.loads(original)  # Every old entry, unchanged


def test_a_second_load_does_not_back_up_again(overrides_file):
    overrides_file.write_bytes(b'{"Fp": {"label": "Gun", "stats": {"ClipSize": 20}}}')
    stats.load()
    after_migration = overrides_file.read_bytes()

    stats.load()

    backups = sorted(p.name for p in overrides_file.parent.glob("overrides.v1.bak*"))
    assert backups == ["overrides.v1.bak.json"]
    assert overrides_file.read_bytes() == after_migration


def test_an_existing_backup_is_never_overwritten(overrides_file):
    backup = overrides_file.with_name("overrides.v1.bak.json")
    backup.write_bytes(b"an earlier backup")
    overrides_file.write_bytes(b'{"Fp": {"label": "Gun", "stats": {"ClipSize": 20}}}')

    stats.load()

    assert backup.read_bytes() == b"an earlier backup"
    numbered = overrides_file.with_name("overrides.v1.bak.1.json")
    assert numbered.read_bytes() == b'{"Fp": {"label": "Gun", "stats": {"ClipSize": 20}}}'


def test_a_failed_backup_keeps_the_old_entries_applying_but_never_overwrites(overrides_file, monkeypatch):
    gun = make_gun(uid=7)
    original = legacy_file(overrides_file, gun, {"ClipSize": 20})

    def refuse(_raw: bytes, _tag: str):
        raise OSError("disk full")

    monkeypatch.setattr(stats, "_write_backup", refuse)
    stats.load()

    assert stats.overrides_for(gun) == {"ClipSize": 20}
    with pytest.raises(OSError):
        stats.set_override(gun, "FireInterval", 0.1)
    assert overrides_file.read_bytes() == original


# --- Legacy lookup and claiming --------------------------------------------------------------------


def test_a_legacy_entry_applies_to_every_identical_gun_as_before(overrides_file):
    first, second = make_gun(uid=7), make_gun(uid=8)
    legacy_file(overrides_file, first, {"ClipSize": 15})
    stats.load()

    assert stats.overrides_for(first) == {"ClipSize": 15}
    assert stats.overrides_for(second) == {"ClipSize": 15}
    assert stats.override_scope(first) == "legacy"
    assert stats.has_overrides()


def test_the_first_change_gives_that_gun_its_own_copy_and_leaves_the_others_alone(overrides_file):
    first, second = make_gun(uid=7), make_gun(uid=8)
    legacy_file(overrides_file, first, {"ClipSize": 15, "FireInterval": 0.25})
    stats.load()

    stats.set_override(first, "ClipSize", 20)

    assert stats.override_scope(first) == "own"
    assert stats.overrides_for(first) == {"ClipSize": 20, "FireInterval": 0.25}
    assert stats.overrides_for(second) == {"ClipSize": 15, "FireInterval": 0.25}
    assert stats.override_scope(second) == "legacy"
    data = saved(overrides_file)
    assert list(data["weapons"]) == [stats.identity(first)]
    assert data["legacy"] == {stats.fingerprint(first): {"label": "Logan's Gun", "stats": {"ClipSize": 15, "FireInterval": 0.25}}}


def test_clearing_the_last_stat_on_a_legacy_gun_keeps_it_cleared(overrides_file):
    gun, other = make_gun(uid=7), make_gun(uid=8)
    legacy_file(overrides_file, gun, {"ClipSize": 15})
    stats.load()

    stats.set_override(gun, "ClipSize", None)

    assert stats.overrides_for(gun) == {}  # The empty own entry hides the legacy one
    assert stats.overrides_for(other) == {"ClipSize": 15}


def test_clearing_an_own_entry_with_no_legacy_removes_it(overrides_file):
    gun = make_gun(uid=7)
    stats.set_override(gun, "ClipSize", 20)

    stats.set_override(gun, "ClipSize", None)

    assert saved(overrides_file)["weapons"] == {}
    assert not stats.has_overrides()


# --- Individual guns -------------------------------------------------------------------------------


def test_identical_guns_with_different_uids_keep_separate_overrides_across_a_reload(overrides_file):
    first, second = make_gun(uid=7), make_gun(uid=8)
    stats.set_override(first, "ClipSize", 20)
    stats.set_override(second, "ClipSize", 30)

    assert stats.overrides_for(first) == {"ClipSize": 20}
    assert stats.overrides_for(second) == {"ClipSize": 30}

    stats.load()

    assert stats.overrides_for(first) == {"ClipSize": 20}
    assert stats.overrides_for(second) == {"ClipSize": 30}
    assert stats.override_scope(first) == "own"


def test_move_overrides_carries_an_own_entry_to_the_new_identity(overrides_file):
    gun = make_gun(uid=7)
    stats.set_override(gun, "ClipSize", 20)
    old = stats.identity(gun)

    gun.DefinitionData.UniqueId = 99  # What a rebuild does
    stats.move_overrides(old, stats.identity(gun))

    assert stats.overrides_for(gun) == {"ClipSize": 20}
    assert old not in saved(overrides_file)["weapons"]
    assert stats.identity(gun) in saved(overrides_file)["weapons"]


def test_move_overrides_copies_legacy_stats_when_the_fingerprint_changed(overrides_file):
    gun = make_gun(uid=7, stage=5)
    legacy_file(overrides_file, gun, {"ClipSize": 15})
    stats.load()
    old = stats.identity(gun)

    gun.DefinitionData.GameStage = 6  # Level change: the fingerprint changes
    gun.DefinitionData.UniqueId = 99
    stats.move_overrides(old, stats.identity(gun))

    assert stats.override_scope(gun) == "own"
    assert stats.overrides_for(gun) == {"ClipSize": 15}
    legacy_key = old.partition("#")[2]
    assert legacy_key in saved(overrides_file)["legacy"]  # Still there for the level 5 guns


def test_move_overrides_does_nothing_when_only_the_uid_changed_for_a_legacy_gun(overrides_file):
    gun = make_gun(uid=7)
    legacy_file(overrides_file, gun, {"ClipSize": 15})
    stats.load()
    old = stats.identity(gun)

    gun.DefinitionData.UniqueId = 99
    stats.move_overrides(old, stats.identity(gun))

    assert stats.override_scope(gun) == "legacy"
    assert saved(overrides_file)["weapons"] == {}


def test_reapply_writes_the_gun_back_to_its_override(overrides_file):
    gun = make_gun(uid=7, clip=10)
    stats.set_override(gun, "ClipSize", 20)
    gun.ClipSize = 10  # The game recomputed it

    assert stats.reapply(gun, reason="test") == 1
    assert gun.ClipSize == 20


# --- A file the store cannot read ------------------------------------------------------------------


def test_a_corrupt_file_is_left_alone_until_the_first_edit(overrides_file):
    original = b"{not json"
    overrides_file.write_bytes(original)

    stats.load()

    assert overrides_file.read_bytes() == original
    assert not stats.has_overrides()
    gun = make_gun(uid=7)
    stats.set_override(gun, "ClipSize", 20)
    assert overrides_file.with_name("overrides.corrupt.bak.json").read_bytes() == original
    data = saved(overrides_file)
    assert data["version"] == 2
    assert stats.identity(gun) in data["weapons"]


def test_an_unknown_version_is_not_overwritten_either(overrides_file):
    original = b'{"version": 9, "weapons": {}}'
    overrides_file.write_bytes(original)

    stats.load()

    assert overrides_file.read_bytes() == original
    assert not stats.has_overrides()


# --- Saves that fail ---------------------------------------------------------------------------------


def failing_save(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Makes stats._save raise OSError, and returns the real one so the test can put it back."""
    real_save = stats._save  # noqa: SLF001

    def boom() -> None:
        raise OSError("disk full (test)")

    monkeypatch.setattr(stats, "_save", boom)
    return real_save


def test_move_overrides_save_failure_keeps_both_keys_and_warns(overrides_file, monkeypatch):
    gun = make_gun(uid=7)
    stats.set_override(gun, "ClipSize", 20)  # saved while the save still works
    old = stats.identity(gun)
    real_save = failing_save(monkeypatch)

    gun.DefinitionData.UniqueId = 99  # What a rebuild does
    new = stats.identity(gun)
    warning = stats.move_overrides(old, new)

    assert isinstance(warning, str) and "could not be saved" in warning
    assert stats.overrides_for(gun) == {"ClipSize": 20}
    assert old in stats._weapons and new in stats._weapons  # noqa: SLF001 - both keys kept in memory
    assert old in saved(overrides_file)["weapons"] and new not in saved(overrides_file)["weapons"]

    monkeypatch.setattr(stats, "_save", real_save)
    assert stats.move_overrides(old, new) is None  # a later save that works returns None
    assert new in saved(overrides_file)["weapons"] and old not in saved(overrides_file)["weapons"]


def test_set_override_save_failure_keeps_the_change_and_returns_a_warning(overrides_file, monkeypatch):
    gun = make_gun(uid=7)
    real_save = failing_save(monkeypatch)

    result = stats.set_override(gun, "ClipSize", 20)

    assert "could not be saved" in result["warning"]
    assert result["after"]["value"] == 20 and gun.ClipSize == 20
    assert stats.overrides_for(gun) == {"ClipSize": 20}  # kept in memory

    monkeypatch.setattr(stats, "_save", real_save)
    assert stats.set_override(gun, "ClipSize", 25)["warning"] is None


@pytest.mark.parametrize("broken", ["replace", "fsync"])
def test_a_failed_atomic_save_leaves_the_old_file_and_no_temp_file(overrides_file, monkeypatch, broken):
    gun = make_gun(uid=7)
    stats.set_override(gun, "ClipSize", 20)
    original = overrides_file.read_bytes()

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(f"{broken} failed (test)")

    if broken == "replace":
        monkeypatch.setattr(stats.os, "replace", boom)
    else:
        monkeypatch.setattr(stats.os, "fsync", boom)
    result = stats.set_override(gun, "ClipSize", 30)

    assert "could not be saved" in result["warning"]
    assert overrides_file.read_bytes() == original
    assert not overrides_file.with_name("overrides.json.tmp").exists()
