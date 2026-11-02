"""Idempotency keys for POST requests, and the timeout/cancel rules every game-thread request follows.

The browser sends `Idempotency-Key: <key>` on POST requests. The key is 8-128 characters of A-Z, a-z,
0-9, `_` or `-`; anything else is refused with 400. The key is remembered for `ttl` seconds after the job
finishes, so a retry of the same request can never apply the edit twice.

Responses from `run_job`:
- 200, 4xx or 500: the job's own result. Stored under the key, so a retry gets the same answer again.
  500s are stored too: the edit may have half-run, and replaying the error is safer than running it again.
- 503 {"error": ..., "applied": false}: the job had not started when the wait timed out. It was cancelled,
  so nothing was changed. The key is forgotten, so a retry runs fresh.
- 504 {"error": ..., "pending": true}: the job started and is still applying the change after the grace
  period. It is never submitted again. Retry with the same key to get its result.
- 422 {"error": "Idempotency-Key reused for a different request"}: the key was already used with another
  route or another body.
- Header `Idempotent-Replay: true`: the response was served from a finished job; nothing ran again.
  A retry that finds the job still running waits on the same job and gets no replay header.

Browser rule: the client generates one key per user action and, on 504 with pending:true, retries the
same request with the same key, e.g. every 1 s up to 30 s.

Requests without the header behave the same way on timeout (503 or 504), but without a key there is no
replay: a second request is a second run.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, wait
from dataclasses import dataclass
from typing import Any, Callable

from unrealsdk import logging

from . import api

HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replay"
NOT_APPLIED_ERROR = "game did not respond (loading or paused?) - nothing was changed"
STILL_RUNNING_ERROR = "the game is still applying this change - retry with the same Idempotency-Key to get the result"
REUSED_ERROR = "Idempotency-Key reused for a different request"
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,128}")

Response = tuple[int, dict[str, Any]]
Outcome = tuple[int, dict[str, Any], dict[str, str]]


def valid_key(key: str) -> bool:
    """True if `key` is an acceptable Idempotency-Key header value."""
    return KEY_PATTERN.fullmatch(key) is not None


def _outcome(future: Future[Any]) -> Response:
    """The status and JSON body of a finished (not cancelled) job."""
    try:
        return 200, future.result()
    except api.ApiError as ex:
        return ex.status, {"error": str(ex)}
    except BaseException as ex:  # noqa: BLE001
        return 500, {"error": repr(ex)}


def _log_failure(route_path: str) -> Callable[[Future[Any]], None]:
    """Done-callback that logs a job's unexpected exception once, whether or not anyone is waiting."""

    def log(done: Future[Any]) -> None:
        if done.cancelled():
            return
        ex = done.exception()
        if ex is not None and not isinstance(ex, api.ApiError):
            logging.error(f"RTSE: {route_path} failed: {ex!r}")

    return log


@dataclass
class _Entry:
    route: str
    body_hash: str
    future: Future[Any]
    created: float
    response: Response | None = None
    finished: float | None = None  # the cache clock when the job finished; the TTL counts from here


class IdempotencyCache:
    """Remembers recent Idempotency-Keys. Thread-safe.

    A key maps to its route, the sha256 of the raw body, the job's future and, once the job finished,
    the response to replay. Finished entries expire `ttl` seconds after they finished (a job that ran
    longer than `ttl` is still remembered for `ttl` after it ends); when there are more than
    `max_entries`, the oldest finished entries (by insertion) go first. Pending jobs are never evicted,
    because forgetting one would let a retry run it a second time.
    """

    def __init__(
        self, max_entries: int = 256, ttl: float = 600.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.max_entries = max_entries
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def begin(self, key: str, route: str, body_hash: str, submit: Callable[[], Future[Any]]) -> tuple[_Entry, bool]:
        """Returns the entry for `key` and whether this call submitted its job (False: the key was known)."""
        with self._lock:
            self._evict_locked()
            entry = self._entries.get(key)
            if entry is not None:
                return entry, False
            future = submit()
            entry = _Entry(route, body_hash, future, self._clock())
            self._entries[key] = entry
            self._evict_locked()
        # Outside the lock: a finished future runs its callbacks at once, in this thread.
        future.add_done_callback(lambda done: self._finished(key, done))
        return entry, True

    def _finished(self, key: str, future: Future[Any]) -> None:
        if future.cancelled():
            # Cancelled before it ran: nothing happened, so a retry must run fresh.
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry.future is future:
                    del self._entries[key]
            return
        response = _outcome(future)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.future is future:
                entry.response = response
                entry.finished = self._clock()

    def _evict_locked(self) -> None:
        now = self._clock()
        for key, entry in list(self._entries.items()):
            if entry.future.done() and entry.finished is not None and now - entry.finished >= self.ttl:
                del self._entries[key]
        excess = len(self._entries) - self.max_entries
        for key, entry in list(self._entries.items()):
            if excess <= 0:
                break
            if entry.future.done():
                del self._entries[key]
                excess -= 1


def run_job(
    submit: Callable[[], Future[Any]],
    cache: IdempotencyCache | None,
    key: str | None,
    route_path: str,
    body: bytes,
    timeout: float,
    grace: float,
) -> Outcome:
    """Runs one game-thread job for an HTTP request and returns (status, payload, extra headers).

    `submit` queues the job and returns its future. Without a key (`cache` or `key` is None) the job is
    submitted every time. With a key, a known key replays its finished response, waits on its pending
    job, or is refused with 422 if the route or body differ. See the module docstring for the statuses.
    """
    if cache is None or key is None:
        future = submit()
        future.add_done_callback(_log_failure(route_path))
        return _wait(future, timeout, grace)

    body_hash = hashlib.sha256(body).hexdigest()
    entry, created = cache.begin(key, route_path, body_hash, submit)
    if created:
        entry.future.add_done_callback(_log_failure(route_path))
    if entry.route != route_path or entry.body_hash != body_hash:
        return 422, {"error": REUSED_ERROR}, {}
    if not created and entry.response is not None:  # a job this call did not submit, already finished
        status, payload = entry.response
        return status, payload, {REPLAY_HEADER: "true"}
    return _wait(entry.future, timeout, grace)


def _wait(future: Future[Any], timeout: float, grace: float) -> Outcome:
    wait([future], timeout=timeout)
    if not future.done():
        if future.cancel():  # not started yet: the game thread will now skip it
            return 503, {"error": NOT_APPLIED_ERROR, "applied": False}, {}
        wait([future], timeout=grace)  # already running: give it a little longer
        if not future.done():
            return 504, {"error": STILL_RUNNING_ERROR, "pending": True}, {}
    if future.cancelled():  # another request waiting on the same key cancelled it
        return 503, {"error": NOT_APPLIED_ERROR, "applied": False}, {}
    status, payload = _outcome(future)
    return status, payload, {}
