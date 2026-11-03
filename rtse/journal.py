"""Edit journal: a record of every change RTSE makes to the game, for the History page and undo.

Every write route runs inside one transaction, opened by the route registry around each POST:

    with journal.transaction("/api/item/set_part", params):     # same as begin(...) ... commit() / abort()
        ...
        journal.record("gear.part", target=item_id, before=old_part, after=new_part)

`record(kind, target, before, after, undo=None) -> None`
    kind:   dotted name of what changed: "gear.part", "gear.level", "gear.stat", "character.<field>", "ammo.value",
            "sdu.<kind>", "skills.level", "skills.reset", "world.<what>", "inspect.value", "checkpoint.<what>".
    target: what was changed, as a JSON-able id (item id, field key, pool key, skill name, object path, ...).
    before / after: JSON-able values the module read back from the game (not what was requested).
    undo:   optional zero-argument callable that puts `before` back, called on the GAME THREAD only. Kept in memory for
            this session only (it can't be saved). rtse/history.py prefers replaying the route with `before` instead.
    Each record also keeps the route and params of the innermost transaction level it was made in, so an undo can
    replay that route (a nested route's records name the nested route, not the outer one).
    Never raises: the journal must not be able to break a write.
    Each entry also keeps "identity": the character the write was made to (identity_provider, read when it started).

`begin(label, params=None) -> Transaction` / `commit(tx) -> None` / `abort(tx, error) -> None`
    Group the records of one user action. Nested begin() calls join the outer transaction. `transaction(...)` is the
    context manager form (commit on success, abort with the exception on error, exception re-raised).

    Nesting rule (all or nothing): the nested levels share ONE transaction. An abort() at any level marks the whole
    transaction failed (`tx.failed = True`, `tx.error` = the first error), even if the caller catches the nested
    exception and carries on. When the outermost level closes, a failed transaction is discarded (no records kept,
    `tx.outcome = "aborted"`) whether that close is commit() or abort(); only a transaction that never saw an abort
    ends with `tx.outcome = "committed"` and keeps its records. So an undo history never holds half of a user action.
    A committed transaction with no records (a POST that changed nothing) is not added to the timeline.

    The one exception is `note_partial()`: when a multi-step action failed AND putting its finished steps back failed
    too, the game really was left half-changed. Discarding that would hide changes that are in the game, so the
    caller records them as a separate, explicit entry (`partial: True`, with the error and every record in order).
    It isn't the aborted transaction being kept: it's a report of what the failure left behind. It is never undoable.

`add_begin_hook(fn) -> remove_fn`: fn(label) is called when an OUTERMOST transaction opens, before the route touches the
    game (rtse/checkpoints.py backs the save up before the session's first write this way). The hook may:
      - return None, or a dict that is added to `tx.notices` (api.py copies notices into the write's response);
      - raise WriteBlocked (an ApiError, normally 423) to refuse the write: begin() re-raises it, no transaction opens
        and the route never runs;
      - raise anything else, which is logged and ignored (a broken hook must not block every write).

`entries() -> list[dict]`: committed transactions, newest last (see `_make_entry` for the shape). `get(entry_id)` finds one.
    `undone_by` always shows the CURRENT state, worked out from the `undo_of` links (see `links()`): after edit #1,
    undo #2 and redo #3 (an undo of #2), #1 is in effect again (undone_by None) and #2 is undone by #3.
    The timeline is capped at MAX_ENTRIES (oldest dropped) and mirrored to LOG_FILE (rtse/history.json, not in git), so
    the History page can still list the previous session's edits; those are marked session="earlier" and never undone.

All functions are called on the game thread (routes run there), so no locks are needed.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from unrealsdk import logging

from .routing import ApiError

Undo = Callable[[], Any]
BeginHook = Callable[[str], Any]

MAX_ENTRIES = 200  # in-session timeline cap; the log file holds the same entries
DEFAULT_LOG_FILE = Path(__file__).with_name("history.json")
LOG_FILE: Path | None = None  # off until the mod is enabled (use_log_file), so tests never write next to the mod
LOG_VERSION = 1


class WriteBlocked(ApiError):
    """Raised by a begin hook to refuse a write before it starts (see add_begin_hook)."""


class Transaction:
    """One user action's worth of records.

    failed:  True once abort() was called at any nesting level (see the nesting rule in the module docstring).
    error:   the first error passed to abort(), if any.
    outcome: None while open; "committed" or "aborted" once the outermost level closed.
    meta:    extra fields copied onto the timeline entry (rtse/history.py sets "undo_of").
    entry:   the timeline entry once committed with records, else None.
    notices: dicts the begin hooks returned (e.g. {"backup": {...}}); api.py adds them to the write's response.
    """

    __slots__ = ("label", "params", "records", "depth", "failed", "error", "outcome", "meta", "entry", "stack", "undos",
                 "notices")

    def __init__(self, label: str, params: dict[str, Any] | None) -> None:
        self.label = label
        self.params = params
        self.records: list[dict[str, Any]] = []
        self.depth = 1
        self.failed = False
        self.error: BaseException | None = None
        self.outcome: str | None = None
        self.meta: dict[str, Any] = {}
        self.entry: dict[str, Any] | None = None
        self.stack: list[tuple[str, Any]] = [(label, _plain(params))]  # (route, params) per open nesting level
        self.undos: list[Undo | None] = []
        self.notices: list[dict[str, Any]] = []


_current: Transaction | None = None
# Returns the loaded character ({save_id, class, name}) or None; rtse/history.py sets it. Each entry keeps it as
# "identity", read when the write starts, so an undo can refuse to run on a different character.
identity_provider: Callable[[], Any] | None = None
_entries: list[dict[str, Any]] = []
_undos: dict[int, list[Undo | None]] = {}  # entry id -> per-record undo callables (this session only)
_hooks: list[BeginHook] = []
_next_id = 1
_dropped = 0
_log_error: str | None = None
_loaded = False


def _plain(value: Any, depth: int = 0) -> Any:
    """A JSON-safe deep copy (so a later change to the game object can't rewrite history). Never raises."""
    try:
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if depth > 8:
            return repr(value)
        if isinstance(value, dict):
            return {str(k): _plain(v, depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_plain(v, depth + 1) for v in value]
        path_name = getattr(value, "_path_name", None)
        if callable(path_name):
            return path_name()
        return repr(value)
    except Exception:  # noqa: BLE001
        return "<unreadable>"


def record(kind: str, target: Any, before: Any, after: Any, undo: Undo | None = None) -> None:
    """Notes one change in the open transaction (outside one it is logged and dropped). Never raises."""
    try:
        tx = _current
        if tx is None:
            logging.warning(f"RTSE: journal.record({kind!r}) outside a transaction; not kept")
            return
        route, params = tx.stack[-1]
        tx.records.append({
            "kind": str(kind),
            "target": _plain(target),
            "before": _plain(before),
            "after": _plain(after),
            "route": route,
            "params": params,
        })
        tx.undos.append(undo if callable(undo) else None)
    except Exception as ex:  # noqa: BLE001
        try:
            logging.error(f"RTSE: journal.record failed: {ex!r}")
        except Exception:  # noqa: BLE001, S110
            pass


def add_begin_hook(fn: BeginHook) -> Callable[[], None]:
    """Calls fn(label) whenever an outermost transaction opens. Returns a function that removes the hook."""
    if fn not in _hooks:
        _hooks.append(fn)

    def remove() -> None:
        if fn in _hooks:
            _hooks.remove(fn)

    return remove


def begin(label: str, params: dict[str, Any] | None = None) -> Transaction:
    """Opens (or joins, when nested) the transaction for one user action."""
    global _current  # noqa: PLW0603
    if _current is not None:
        _current.depth += 1
        _current.stack.append((label, _plain(params)))
        return _current
    notices: list[dict[str, Any]] = []
    for hook in list(_hooks):
        try:
            notice = hook(label)
        except WriteBlocked:
            raise  # the hook refused this write; no transaction is opened
        except Exception as ex:  # noqa: BLE001
            logging.error(f"RTSE: journal begin hook {getattr(hook, '__name__', hook)!r} failed: {ex!r}")
            continue
        if isinstance(notice, dict):
            notices.append(notice)
    _current = Transaction(label, params)
    _current.notices = notices
    _current.meta["identity"] = _identity()
    return _current


def _identity() -> Any:
    if identity_provider is None:
        return None
    try:
        return _plain(identity_provider())
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: journal couldn't read the character's identity: {ex!r}")
        return None


def _close(tx: Transaction) -> bool:
    """Leaves one nesting level; when the outermost level closes, settles the outcome. True when it closed."""
    global _current  # noqa: PLW0603
    tx.depth -= 1
    if len(tx.stack) > 1:
        tx.stack.pop()
    if tx.depth > 0:
        return False
    if _current is tx:
        _current = None
    if tx.failed:
        tx.records.clear()  # all or nothing: a failed user action leaves no history
        tx.undos.clear()
        tx.outcome = "aborted"
    else:
        tx.outcome = "committed"
        if tx.records:
            try:
                _keep(tx)
            except Exception as ex:  # noqa: BLE001
                logging.error(f"RTSE: journal could not keep {tx.label}: {ex!r}")
    return True


def _make_entry(tx: Transaction) -> dict[str, Any]:
    return {
        "id": _next_id,
        "time": time.time(),
        "label": tx.label,
        "params": _plain(tx.params),
        "session": "current",
        "records": list(tx.records),
        "undone_by": None,
        "undo_of": None,
        **_plain(tx.meta),
    }


def preview(tx: Transaction | None) -> dict[str, Any] | None:
    """The timeline entry an open transaction will become if it commits now (None if it has nothing to keep). Lets a
    route's response include its own entry, which only exists once the route has returned."""
    if tx is None or tx.failed or not tx.records:
        return None
    _load_once()
    return _make_entry(tx)


def _keep(tx: Transaction) -> None:
    entry = _make_entry(tx)
    _append(entry, tx.undos)
    tx.entry = entry


def _append(entry: dict[str, Any], undos: list[Undo | None]) -> None:
    global _next_id, _dropped  # noqa: PLW0603
    _load_once()
    entry["id"] = _next_id
    _next_id += 1
    _entries.append(entry)
    _undos[entry["id"]] = undos
    while len(_entries) > MAX_ENTRIES:
        old = _entries.pop(0)
        _undos.pop(old["id"], None)
        _dropped += 1
    _relink()
    _persist()


def note_partial(label: str, params: Any, records: list[dict[str, Any]], error: str, **meta: Any) -> dict[str, Any] | None:
    """Adds an explicit "partial" entry: a failed action left these changes in the game and they couldn't be put back.
    `records` are journal records (e.g. a failed transaction's tx.records, copied before it closes). Never raises."""
    try:
        entry = {
            "id": 0,
            "time": time.time(),
            "label": label,
            "params": _plain(params),
            "session": "current",
            "records": _plain(records),
            "undone_by": None,
            "undo_of": None,
            "partial": True,
            "error": str(error),
            **_plain(meta),
        }
        _append(entry, [None] * len(records))
        return entry
    except Exception as ex:  # noqa: BLE001
        logging.error(f"RTSE: journal could not note a partial change ({label}): {ex!r}")
        return None


def links(found: list[dict[str, Any]]) -> dict[int, int | None]:
    """entry id -> the id of the entry that currently undoes it (None when it is in effect), from the undo_of links.

    An entry is undone when some undo of it is itself in effect. Undoers are always newer, so walking newest first
    settles each undoer before the entry it undoes. A redo R (an undo of an undo of X) also counts as undone when X
    was undone again by an entry newer than R: R's effect, putting X back, has been reverted.
    """
    by_id = {e["id"]: e for e in found}
    undoers: dict[Any, list[int]] = {}
    for e in found:
        if e.get("undo_of") is not None:
            undoers.setdefault(e["undo_of"], []).append(e["id"])
    live: dict[int, int | None] = {}
    for e in reversed(found):
        in_effect = [u for u in undoers.get(e["id"], []) if live.get(u) is None]
        live[e["id"]] = max(in_effect) if in_effect else None
    for e in found:
        undo = by_id.get(e.get("undo_of"))
        redone = by_id.get(undo.get("undo_of")) if undo else None
        if redone is not None and live[e["id"]] is None:
            again = live.get(redone["id"])
            if again is not None and again > e["id"]:
                live[e["id"]] = again
    return live


def _relink() -> None:
    current = links(_entries)
    for e in _entries:
        e["undone_by"] = current[e["id"]]


def commit(tx: Transaction) -> None:
    """Closes one level of a transaction that succeeded. At the outermost level the transaction is kept, unless an
    abort() at any level already marked it failed, in which case it is discarded."""
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


def current() -> Transaction | None:
    """The open transaction, if any."""
    return _current


def entries() -> list[dict[str, Any]]:
    """Committed transactions, newest last (the live dicts: callers must not change them)."""
    _load_once()
    return list(_entries)


def get(entry_id: Any) -> dict[str, Any] | None:
    if entry_id is None:
        return None
    return next((e for e in _entries if e["id"] == entry_id), None)


def undo_callable(entry_id: int, index: int) -> Undo | None:
    """The undo callable a call site passed for record `index` of entry `entry_id` (this session only)."""
    undos = _undos.get(entry_id) or []
    return undos[index] if 0 <= index < len(undos) else None


def status() -> dict[str, Any]:
    """Cap and log-file state for the History page."""
    return {
        "limit": MAX_ENTRIES,
        "dropped": _dropped,
        "log_file": str(LOG_FILE) if LOG_FILE else None,
        "log_error": _log_error,
    }


# --- The log file -------------------------------------------------------------------------------------------------


def _load_once() -> None:
    """Reads the previous sessions' entries from LOG_FILE once (they are shown, never undone)."""
    global _loaded, _next_id, _log_error  # noqa: PLW0603
    if _loaded:
        return
    _loaded = True
    if LOG_FILE is None or not LOG_FILE.is_file():
        return
    try:
        data = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        old = [e for e in data.get("entries", []) if isinstance(e, dict) and isinstance(e.get("id"), int)]
    except (OSError, ValueError, AttributeError) as ex:
        _log_error = f"couldn't read {LOG_FILE.name} ({ex}); starting a new one"
        return
    for entry in old:
        entry["session"] = "earlier"
    _entries[:0] = old[-MAX_ENTRIES:]
    del _entries[: max(0, len(_entries) - MAX_ENTRIES)]
    _next_id = max([_next_id, *(e["id"] + 1 for e in _entries)])
    _relink()


def _persist() -> None:
    global _log_error  # noqa: PLW0603
    if LOG_FILE is None:
        return
    try:
        tmp = LOG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": LOG_VERSION, "entries": _entries}, indent=1), encoding="utf-8")
        os.replace(tmp, LOG_FILE)
        _log_error = None
    except (OSError, TypeError, ValueError) as ex:
        _log_error = f"couldn't save {LOG_FILE.name} ({ex}); the timeline is kept in memory only"
        logging.error(f"RTSE: {_log_error}")


def use_log_file(path: Path | None) -> None:
    """Mirrors the timeline to `path` (None turns it off) and loads what an earlier session left there."""
    global LOG_FILE, _loaded  # noqa: PLW0603
    if path != LOG_FILE:
        LOG_FILE = path
        _loaded = False
        _load_once()


def reset_for_tests(log_file: Path | None = None) -> None:
    """Forgets everything (tests only)."""
    global _current, _next_id, _dropped, _log_error, _loaded, LOG_FILE  # noqa: PLW0603
    _current = None
    _entries.clear()
    _undos.clear()
    _hooks.clear()
    _next_id = 1
    _dropped = 0
    _log_error = None
    _loaded = False
    LOG_FILE = log_file
