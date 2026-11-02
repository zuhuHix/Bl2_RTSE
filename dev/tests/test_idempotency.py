"""idempotency.run_job: cancel on timeout, grace wait, replay, key reuse, TTL and eviction. No HTTP.

Jobs go through the real gamethread queue. Tests that need the job to run while the request waits
start a background thread that pumps the queue, the way the game does each tick.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from typing import Any

import pytest

from rtse import api, gamethread, idempotency
from rtse.idempotency import IdempotencyCache, run_job

ROUTE = "/api/item/set_level"
BODY = b'{"level": 5}'
KEY = "abcdefgh-key-1"
FAST = 0.05  # seconds: small waits keep the tests quick
REPLAY = {"Idempotent-Replay": "true"}


@pytest.fixture(autouse=True)
def _empty_queue():
    for _ in range(100):
        gamethread.pump()
    yield


@pytest.fixture
def game_thread():
    """A background thread that pumps the game-thread queue every millisecond, like the game."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            gamethread.pump()
            time.sleep(0.001)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(5)


def queued(fn):
    """submit() that puts fn on the real game-thread queue."""
    return lambda: gamethread.submit(fn)


def completed(fn):
    """submit() that runs fn immediately, as if the game ran it on the spot."""

    def submit() -> Future[Any]:
        future: Future[Any] = Future()
        future.set_running_or_notify_cancel()
        try:
            future.set_result(fn())
        except BaseException as ex:  # noqa: BLE001
            future.set_exception(ex)
        return future

    return submit


def never() -> Future[Any]:
    raise AssertionError("the job was submitted again")


def counting(result: Any):
    calls: list[int] = []

    def job() -> Any:
        calls.append(1)
        return result

    return job, calls


def test_valid_key_accepts_8_to_128_safe_characters():
    assert idempotency.valid_key("a" * 8)
    assert idempotency.valid_key("a" * 128)
    assert idempotency.valid_key("A-b_9-x-z")
    assert not idempotency.valid_key("a" * 7)
    assert not idempotency.valid_key("a" * 129)
    assert not idempotency.valid_key("")
    assert not idempotency.valid_key("bad key!!")
    assert not idempotency.valid_key("abcdefg\n")  # a trailing newline must not sneak past
    assert not idempotency.valid_key("abc.defghi")


def test_pending_job_is_cancelled_and_never_runs():
    cache = IdempotencyCache()
    job, calls = counting({"ok": True})

    status, payload, headers = run_job(queued(job), cache, KEY, ROUTE, BODY, FAST, FAST)

    assert (status, payload, headers) == (503, {"error": idempotency.NOT_APPLIED_ERROR, "applied": False}, {})
    gamethread.pump()  # the game thread reaches the cancelled job
    assert calls == []
    assert len(cache) == 0  # the key is forgotten, so a retry runs fresh


def test_retry_after_cancel_runs_the_job_once():
    cache = IdempotencyCache()
    job, calls = counting({"ok": True})
    assert run_job(queued(job), cache, KEY, ROUTE, BODY, FAST, FAST)[0] == 503

    # The game thread picks up the retry's job a moment after it is queued.
    threading.Timer(0.1, gamethread.pump).start()
    status, payload, headers = run_job(queued(job), cache, KEY, ROUTE, BODY, 5, 5)

    assert (status, payload, headers) == (200, {"ok": True}, {})
    assert calls == [1]


def test_running_job_gets_the_grace_period_and_its_result(game_thread):
    release = threading.Event()
    calls: list[int] = []

    def job() -> dict[str, Any]:
        calls.append(1)
        release.wait(5)
        return {"ok": True}

    threading.Timer(0.6, release.set).start()  # the job is still running when the request times out
    status, payload, headers = run_job(queued(job), IdempotencyCache(), KEY, ROUTE, BODY, 0.3, 5)

    assert (status, payload, headers) == (200, {"ok": True}, {})
    assert calls == [1]


def test_still_running_after_grace_is_504_and_retry_waits_without_resubmitting(game_thread):
    cache = IdempotencyCache()
    release = threading.Event()
    calls: list[int] = []

    def job() -> dict[str, Any]:
        calls.append(1)
        release.wait(10)
        return {"ok": True}

    try:
        first = run_job(queued(job), cache, KEY, ROUTE, BODY, 0.2, 0.1)
        assert first == (504, {"error": idempotency.STILL_RUNNING_ERROR, "pending": True}, {})

        again = run_job(never, cache, KEY, ROUTE, BODY, 0.2, 0.1)
        assert again == (504, {"error": idempotency.STILL_RUNNING_ERROR, "pending": True}, {})
    finally:
        release.set()

    status, payload, _ = run_job(never, cache, KEY, ROUTE, BODY, 5, 5)
    assert (status, payload) == (200, {"ok": True})
    assert calls == [1]


def test_finished_job_is_replayed_without_running_again():
    cache = IdempotencyCache()
    job, calls = counting({"ok": True})

    first = run_job(completed(job), cache, KEY, ROUTE, BODY, 1, 1)
    second = run_job(never, cache, KEY, ROUTE, BODY, 1, 1)

    assert first == (200, {"ok": True}, {})
    assert second == (200, {"ok": True}, REPLAY)
    assert calls == [1]


def test_error_results_are_stored_and_replayed():
    cache = IdempotencyCache()

    def conflict() -> dict[str, Any]:
        raise api.ApiError(409, "already changed")

    def crash() -> dict[str, Any]:
        raise RuntimeError("half applied")

    assert run_job(completed(conflict), cache, KEY, ROUTE, BODY, 1, 1) == (409, {"error": "already changed"}, {})
    assert run_job(never, cache, KEY, ROUTE, BODY, 1, 1) == (409, {"error": "already changed"}, REPLAY)

    crash_key = "abcdefgh-crash"
    first = run_job(completed(crash), cache, crash_key, ROUTE, BODY, 1, 1)
    assert first[0] == 500 and "half applied" in first[1]["error"]
    assert run_job(never, cache, crash_key, ROUTE, BODY, 1, 1) == (500, first[1], REPLAY)


def test_key_reused_with_another_body_or_route_is_422():
    cache = IdempotencyCache()
    job, calls = counting({"ok": True})
    run_job(completed(job), cache, KEY, ROUTE, BODY, 1, 1)

    expected = (422, {"error": idempotency.REUSED_ERROR}, {})
    assert run_job(never, cache, KEY, ROUTE, b'{"level": 6}', 1, 1) == expected
    assert run_job(never, cache, KEY, "/api/item/set_stat", BODY, 1, 1) == expected
    assert calls == [1]


def test_expired_entries_are_forgotten_after_ttl():
    now = [0.0]
    cache = IdempotencyCache(ttl=10.0, clock=lambda: now[0])
    job, calls = counting({"n": 1})
    run_job(completed(job), cache, KEY, ROUTE, BODY, 1, 1)

    now[0] = 9.9
    assert run_job(never, cache, KEY, ROUTE, BODY, 1, 1)[2] == REPLAY

    now[0] = 10.0
    job2, calls2 = counting({"n": 2})
    assert run_job(completed(job2), cache, KEY, ROUTE, BODY, 1, 1) == (200, {"n": 2}, {})
    assert calls == [1] and calls2 == [1]


def _running_future() -> Future[Any]:
    """A future the game thread has started, so cancel() no longer works on it (the job is 'applying')."""
    future: Future[Any] = Future()
    future.set_running_or_notify_cancel()
    return future


def test_ttl_counts_from_when_the_job_finished_not_when_it_was_submitted():
    """A job submitted at t=0 that finishes at t=110 (TTL 10) is replayed at t=111, not run a second time."""
    now = [0.0]
    cache = IdempotencyCache(ttl=10.0, clock=lambda: now[0])
    futures: list[Future[Any]] = []

    def submit() -> Future[Any]:
        futures.append(_running_future())
        return futures[-1]

    assert run_job(submit, cache, KEY, ROUTE, BODY, 0, 0)[0] == 504  # still running: the call gives up waiting

    now[0] = 110.0
    futures[0].set_result({"n": 1})  # long after the TTL from submission, but just finished
    now[0] = 111.0
    assert run_job(never, cache, KEY, ROUTE, BODY, 0, 0) == (200, {"n": 1}, REPLAY)


def test_retry_a_ttl_after_the_job_finished_runs_fresh():
    now = [0.0]
    cache = IdempotencyCache(ttl=10.0, clock=lambda: now[0])
    futures: list[Future[Any]] = []

    def submit() -> Future[Any]:
        futures.append(_running_future())
        return futures[-1]

    run_job(submit, cache, KEY, ROUTE, BODY, 0, 0)
    now[0] = 110.0
    futures[0].set_result({"n": 1})

    now[0] = 121.0  # finish + TTL + 1
    job, calls = counting({"n": 2})
    assert run_job(completed(job), cache, KEY, ROUTE, BODY, 1, 1) == (200, {"n": 2}, {})
    assert calls == [1]


def test_max_entries_evicts_the_oldest_finished_entry():
    cache = IdempotencyCache(max_entries=2)
    job, calls = counting({"ok": True})
    keys = ["abcdefgh-k1", "abcdefgh-k2", "abcdefgh-k3"]
    for key in keys:
        run_job(completed(job), cache, key, ROUTE, BODY, 1, 1)
    assert len(cache) == 2

    # k1 was the oldest, so it was evicted and runs again; k3 is still remembered.
    assert run_job(completed(job), cache, keys[0], ROUTE, BODY, 1, 1)[2] == {}
    assert run_job(never, cache, keys[2], ROUTE, BODY, 1, 1)[2] == REPLAY
    assert calls == [1, 1, 1, 1]


def test_concurrent_duplicates_run_the_job_once(game_thread):
    cache = IdempotencyCache()
    calls: list[int] = []

    def job() -> dict[str, Any]:
        time.sleep(0.05)
        calls.append(1)
        return {"n": len(calls)}

    results: list[tuple[int, dict[str, Any], dict[str, str]]] = []

    def send() -> None:
        results.append(run_job(queued(job), cache, KEY, ROUTE, BODY, 5, 5))

    threads = [threading.Thread(target=send) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert calls == [1]
    assert len(results) == 8
    assert all(status == 200 and payload == {"n": 1} for status, payload, _ in results)


def test_requests_without_a_key_always_run_and_still_cancel_on_timeout():
    job, calls = counting({"ok": True})
    assert run_job(queued(job), None, None, ROUTE, BODY, FAST, FAST)[:2] == (
        503,
        {"error": idempotency.NOT_APPLIED_ERROR, "applied": False},
    )
    gamethread.pump()
    assert calls == []

    assert run_job(completed(job), None, None, ROUTE, BODY, 1, 1) == (200, {"ok": True}, {})
    assert run_job(completed(job), None, None, ROUTE, BODY, 1, 1) == (200, {"ok": True}, {})
    assert calls == [1, 1]


def test_over_cap_eviction_skips_entries_whose_response_is_not_recorded_yet():
    """A future is done a moment before _finished records its response. Under cache pressure that entry must stay,
    or a same-key retry in that window would run the edit a second time."""
    cache = IdempotencyCache(max_entries=1)
    finished = cache._finished  # noqa: SLF001
    cache._finished = lambda key, future: None  # noqa: SLF001 - freeze the window: done, response not recorded
    job, calls = counting({"n": 1})
    run_job(completed(job), cache, KEY, ROUTE, BODY, 1, 1)
    cache._finished = finished  # noqa: SLF001

    other, _ = counting({"other": True})
    run_job(completed(other), cache, "abcdefgh-k2", ROUTE, BODY, 1, 1)  # over the cap: something must go, not KEY
    assert run_job(never, cache, KEY, ROUTE, BODY, 1, 1)[:2] == (200, {"n": 1})
    assert calls == [1]


def test_keyless_request_still_running_after_grace_is_told_not_to_resend():
    future = _running_future()
    status, payload, _ = run_job(lambda: future, None, None, ROUTE, BODY, 0, 0)
    assert status == 504
    assert payload == {"error": idempotency.STILL_RUNNING_UNKEYED_ERROR, "pending": True, "retry": False}
    assert "Idempotency-Key" not in payload["error"] and "don't resend" in payload["error"]
    future.set_result({"ok": True})
