"""rtse/journal.py and rtse/session_guard.py: the Wave 1 contracts (stubs) behave as documented."""

from __future__ import annotations

import pytest

from rtse import journal, session_guard


@pytest.fixture(autouse=True)
def _no_open_transaction():
    journal._current = None  # noqa: SLF001
    yield
    journal._current = None  # noqa: SLF001


def test_record_is_a_noop_that_never_raises():
    assert journal.record("gear.part", "item-1", "old", "new") is None
    assert journal.record("gear.part", object(), object(), object(), undo=lambda: None) is None
    assert journal.entries() == []


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
