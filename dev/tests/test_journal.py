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

    with pytest.raises(ValueError, match="nope"), journal.transaction("/api/ammo/set"):
        raise ValueError("nope")
    assert journal._current is None  # noqa: SLF001


def test_session_guard_allows_everything_for_now():
    assert session_guard.can_write("/api/item/set_part") == (True, "")
    assert session_guard.BLOCKED_STATUS == 423
