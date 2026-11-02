"""Edit journal: a record of every change RTSE makes to the game, for history and undo. CONTRACT ONLY (no-op).

Wave 1's history/undo stream fills this in; everything else already calls it, so writes are journaled from day one.

Every write route runs inside one transaction, opened by the route registry around each POST:

    with journal.transaction("/api/item/set_part", params):     # same as begin(...) ... commit() / abort()
        ...
        journal.record("gear.part", target=item_id, before=old_part, after=new_part, undo=lambda: restore())

`record(kind, target, before, after, undo=None) -> None`
    kind:   dotted name of what changed: "gear.part", "gear.level", "gear.stat", "character.<field>", "ammo.value",
            "sdu.<kind>", "skills.level", "skills.reset", "world.<what>", "inspect.value".
    target: what was changed, as a JSON-able id (item id, field key, pool key, skill name, object path, ...).
    before / after: JSON-able values the module read back from the game (not what was requested).
    undo:   optional zero-argument callable that puts `before` back. It will be called on the GAME THREAD only.
    Never raises: the journal must not be able to break a write.

`begin(label, params=None) -> Transaction` / `commit(tx) -> None` / `abort(tx, error) -> None`
    Group the records of one user action. Nested begin() calls join the outer transaction. `transaction(...)` is the
    context manager form (commit on success, abort with the exception on error, exception re-raised).

    Nesting rule (all or nothing): the nested levels share ONE transaction. An abort() at any level marks the whole
    transaction failed (`tx.failed = True`, `tx.error` = the first error), even if the caller catches the nested
    exception and carries on. When the outermost level closes, a failed transaction is discarded (no records kept,
    `tx.outcome = "aborted"`) whether that close is commit() or abort(); only a transaction that never saw an abort
    ends with `tx.outcome = "committed"` and keeps its records. So an undo history never holds half of a user action.

`entries() -> list[dict]`: committed transactions, newest last (always empty in this stub).

All functions are called on the game thread (routes run there), so the real implementation does not need locks for
in-game state, but must not block. Keep it in memory plus an optional bounded on-disk log.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

Undo = Callable[[], Any]


class Transaction:
    """One user action's worth of records. `records` stays empty while the journal is a stub.

    failed:  True once abort() was called at any nesting level (see the nesting rule in the module docstring).
    error:   the first error passed to abort(), if any.
    outcome: None while open; "committed" or "aborted" once the outermost level closed.
    """

    __slots__ = ("label", "params", "records", "depth", "failed", "error", "outcome")

    def __init__(self, label: str, params: dict[str, Any] | None) -> None:
        self.label = label
        self.params = params
        self.records: list[dict[str, Any]] = []
        self.depth = 1
        self.failed = False
        self.error: BaseException | None = None
        self.outcome: str | None = None


_current: Transaction | None = None


def record(kind: str, target: Any, before: Any, after: Any, undo: Undo | None = None) -> None:
    """Notes one change. No-op for now (contract only); never raises."""
    return


def begin(label: str, params: dict[str, Any] | None = None) -> Transaction:
    """Opens (or joins, when nested) the transaction for one user action."""
    global _current  # noqa: PLW0603
    if _current is not None:
        _current.depth += 1
        return _current
    _current = Transaction(label, params)
    return _current


def _close(tx: Transaction) -> bool:
    """Leaves one nesting level; when the outermost level closes, settles the outcome. True when it closed."""
    global _current  # noqa: PLW0603
    tx.depth -= 1
    if tx.depth > 0:
        return False
    if _current is tx:
        _current = None
    if tx.failed:
        tx.records.clear()  # all or nothing: a failed user action leaves no history
        tx.outcome = "aborted"
    else:
        tx.outcome = "committed"
    return True


def commit(tx: Transaction) -> None:
    """Closes one level of a transaction that succeeded. At the outermost level the transaction is kept, unless an
    abort() at any level already marked it failed, in which case it is discarded. No storage yet (stub)."""
    _close(tx)


def abort(tx: Transaction, error: BaseException | None = None) -> None:
    """Closes one level of a transaction that failed and marks the WHOLE transaction failed, so the outermost close
    discards every record, even if a caller caught the nested error and carried on."""
    tx.failed = True
    if tx.error is None:
        tx.error = error
    _close(tx)


@contextmanager
def transaction(label: str, params: dict[str, Any] | None = None) -> Iterator[Transaction]:
    """`with transaction(label, params):` = begin, then commit on success or abort on error (re-raised)."""
    tx = begin(label, params)
    try:
        yield tx
    except BaseException as ex:
        abort(tx, ex)
        raise
    commit(tx)


def entries() -> list[dict[str, Any]]:
    """Committed transactions, newest last. Always empty while the journal is a stub."""
    return []
