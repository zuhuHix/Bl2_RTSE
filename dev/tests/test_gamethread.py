"""gamethread: queued jobs run on pump(), and a future cancelled before its tick never runs."""

from __future__ import annotations

import pytest

from rtse import gamethread


@pytest.fixture(autouse=True)
def _empty_queue():
    """The job queue is module-global: run out whatever an earlier test left behind."""
    for _ in range(100):
        gamethread.pump()
    yield


def test_pump_runs_job_and_sets_result():
    future = gamethread.submit(lambda: {"ok": 1})
    assert not future.done()
    gamethread.pump()
    assert future.result(timeout=0) == {"ok": 1}


def test_job_exception_is_stored_on_future():
    def boom():
        raise ValueError("nope")

    future = gamethread.submit(boom)
    gamethread.pump()
    with pytest.raises(ValueError, match="nope"):
        future.result(timeout=0)


def test_cancelled_pending_job_never_runs():
    ran = []
    future = gamethread.submit(lambda: ran.append(1))
    assert future.cancel()
    gamethread.pump()
    assert ran == []
    assert future.cancelled()


def test_cancelling_one_job_leaves_the_others_alone():
    ran = []
    cancelled = gamethread.submit(lambda: ran.append("a"))
    kept = gamethread.submit(lambda: ran.append("b"))
    assert cancelled.cancel()
    gamethread.pump()
    assert ran == ["b"]
    assert kept.result(timeout=0) is None


def test_cancel_fails_once_the_job_is_running():
    holder = {}
    seen = []

    def job():
        seen.append(holder["future"].cancel())  # called from inside the job: state is RUNNING

    holder["future"] = gamethread.submit(job)
    gamethread.pump()
    assert seen == [False]


def test_pump_runs_at_most_max_jobs_per_tick():
    ran = []
    count = gamethread.MAX_JOBS_PER_TICK + 2
    for i in range(count):
        gamethread.submit(lambda i=i: ran.append(i))
    gamethread.pump()
    assert len(ran) == gamethread.MAX_JOBS_PER_TICK
    gamethread.pump()
    assert len(ran) == count
