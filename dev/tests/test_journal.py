"""rtse/journal.py (the edit timeline) and rtse/session_guard.py behave as their docstrings say."""

from __future__ import annotations

import json

import pytest

from rtse import journal, session_guard


@pytest.fixture(autouse=True)
def _no_open_transaction():
    journal.reset_for_tests()
    yield
    journal.reset_for_tests()


def test_record_outside_a_transaction_is_dropped_and_never_raises():
    assert journal.record("gear.part", "item-1", "old", "new") is None
    assert journal.entries() == []


def test_committed_records_become_one_entry_with_json_safe_copies():
    before = {"parts": ["a"]}

    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    with journal.transaction("/api/item/set_part", {"id": "x"}):
        journal.record("gear.part", "item-1", before, Weird(), undo=lambda: None)
        journal.record("gear.level", "item-1", 1, float("nan"))
    before["parts"].append("changed later")  # history keeps what was true then
    (entry,) = journal.entries()
    assert entry["id"] == 1 and entry["label"] == "/api/item/set_part" and entry["params"] == {"id": "x"}
    assert entry["session"] == "current" and entry["undone_by"] is None and entry["undo_of"] is None
    first, second = entry["records"]
    assert first == {"kind": "gear.part", "target": "item-1", "before": {"parts": ["a"]}, "after": "<weird>",
                     "route": "/api/item/set_part", "params": {"id": "x"}}
    assert second["after"] == "nan"
    json.dumps(entry)  # always JSON-able
    assert journal.get(1) is entry and callable(journal.undo_callable(1, 0)) and journal.undo_callable(1, 1) is None


def test_records_name_the_innermost_route():
    with journal.transaction("/api/history/undo", {"id": 1}):
        with journal.transaction("/api/ammo/set", {"id": 2, "value": 5}):
            journal.record("ammo.value", "SMG", 9, 5)
        journal.record("note", "outer", None, 1)
    inner, outer = journal.entries()[0]["records"]
    assert (inner["route"], inner["params"]) == ("/api/ammo/set", {"id": 2, "value": 5})
    assert (outer["route"], outer["params"]) == ("/api/history/undo", {"id": 1})


def test_empty_and_aborted_transactions_leave_no_entry():
    with journal.transaction("/api/parts/rescan"):
        pass
    with pytest.raises(RuntimeError), journal.transaction("/api/ammo/set"):
        journal.record("ammo.value", "SMG", 1, 2)
        raise RuntimeError("write failed")
    assert journal.entries() == []


def test_the_timeline_is_capped(monkeypatch):
    monkeypatch.setattr(journal, "MAX_ENTRIES", 3)
    for n in range(5):
        with journal.transaction("/api/ammo/set", {"n": n}):
            journal.record("ammo.value", "SMG", n, n + 1)
    assert [e["params"]["n"] for e in journal.entries()] == [2, 3, 4]
    assert journal.status()["dropped"] == 2 and journal.undo_callable(1, 0) is None


def test_the_log_file_survives_a_restart_as_earlier_entries(tmp_path):
    log = tmp_path / "history.json"
    journal.use_log_file(log)
    with journal.transaction("/api/character/set", {"field": "money", "value": 5}):
        journal.record("character.money", "money", 1, 5)
    assert json.loads(log.read_text(encoding="utf-8"))["entries"][0]["id"] == 1
    journal.reset_for_tests()
    journal.use_log_file(log)
    (old,) = journal.entries()
    assert old["session"] == "earlier" and old["records"][0]["after"] == 5
    with journal.transaction("/api/character/set"):
        journal.record("character.money", "money", 5, 6)
    assert [e["id"] for e in journal.entries()] == [1, 2]  # ids keep counting
    assert journal.status()["log_file"] == str(log) and journal.status()["log_error"] is None


def test_an_unreadable_log_file_is_reported_not_fatal(tmp_path):
    log = tmp_path / "history.json"
    log.write_text("{not json", encoding="utf-8")
    journal.use_log_file(log)
    assert journal.entries() == [] and "couldn't read" in journal.status()["log_error"]


def test_tests_never_write_a_log_next_to_the_mod():
    assert journal.LOG_FILE is None and journal.DEFAULT_LOG_FILE.name == "history.json"


def test_begin_hooks_run_once_per_outermost_transaction_and_never_break_it():
    seen: list[str] = []
    remove = journal.add_begin_hook(seen.append)
    journal.add_begin_hook(lambda _label: 1 / 0)  # a broken hook is logged and ignored
    with journal.transaction("/api/ammo/set"), journal.transaction("/api/nested"):
        journal.record("ammo.value", "SMG", 1, 2)
    remove()
    with journal.transaction("/api/ammo/set"):
        pass
    assert seen == ["/api/ammo/set"] and len(journal.entries()) == 1


def test_preview_is_the_entry_a_commit_would_keep():
    tx = journal.begin("/api/history/undo", {"id": 1})
    assert journal.preview(tx) is None  # nothing recorded yet
    tx.meta["undo_of"] = 7
    journal.record("ammo.value", "SMG", 1, 2)
    preview = journal.preview(tx)
    journal.commit(tx)
    kept = journal.entries()[-1]
    assert preview["id"] == kept["id"] and preview["undo_of"] == kept["undo_of"] == 7
    assert preview["records"] == kept["records"]


def test_begin_commit_and_nesting_join_the_outer_transaction():
    outer = journal.begin("/api/item/set_part", {"id": "x"})
    inner = journal.begin("/api/item/set_level")
    assert inner is outer
    journal.commit(inner)
    assert journal._current is outer  # noqa: SLF001 - still open: only the inner level closed
    journal.commit(outer)
    assert journal._current is None  # noqa: SLF001


def test_transaction_context_manager_commits_and_aborts():
    with journal.transaction("/api/ammo/set", {"key": "smg"}) as tx:
        assert isinstance(tx, journal.Transaction)
        assert tx.label == "/api/ammo/set"
    assert journal._current is None  # noqa: SLF001

    assert tx.outcome == "committed" and not tx.failed

    with pytest.raises(ValueError, match="nope"), journal.transaction("/api/ammo/set") as failed:
        raise ValueError("nope")
    assert journal._current is None  # noqa: SLF001
    assert failed.outcome == "aborted" and isinstance(failed.error, ValueError)


def test_nested_abort_that_the_caller_catches_still_discards_the_whole_transaction():
    """All or nothing: the outer commit() of a transaction whose nested level aborted discards it."""
    with journal.transaction("/api/item/set_part") as outer:
        outer.records.append({"kind": "gear.part"})  # as the real journal would, before the nested failure
        try:
            with journal.transaction("/api/item/set_level"):
                outer.records.append({"kind": "gear.level"})
                raise RuntimeError("the nested write failed")
        except RuntimeError:
            pass  # the caller carries on...
        assert outer.failed and outer.outcome is None  # ...but the shared transaction is already marked failed
    assert outer.outcome == "aborted"
    assert outer.records == []
    assert str(outer.error) == "the nested write failed"


def test_explicit_nested_abort_then_outer_commit_is_aborted():
    outer = journal.begin("/api/world/mission")
    inner = journal.begin("/api/world/objective")
    journal.abort(inner, ValueError("first"))
    journal.abort(journal.begin("again"), ValueError("second"))  # a later error doesn't replace the first
    journal.commit(outer)
    assert outer.outcome == "aborted" and str(outer.error) == "first"
    assert journal._current is None  # noqa: SLF001


def test_session_guard_allows_everything_for_now():
    assert session_guard.can_write("/api/item/set_part") == (True, "")
    assert session_guard.BLOCKED_STATUS == 423
