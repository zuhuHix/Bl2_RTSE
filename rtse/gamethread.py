"""Runs callables on the game thread, which is the only thread allowed to touch Unreal objects."""

from __future__ import annotations

from concurrent.futures import Future
from queue import Empty, SimpleQueue
from typing import Any, Callable

_queue: SimpleQueue[tuple[Callable[[], Any], Future[Any]]] = SimpleQueue()

MAX_JOBS_PER_TICK = 8


def submit(fn: Callable[[], Any]) -> Future[Any]:
    """Queues `fn` to run on the next game tick. Safe to call from any thread."""
    future: Future[Any] = Future()
    _queue.put((fn, future))
    return future


def pump() -> None:
    """Drains queued jobs. Must be called from the game thread, once per tick."""
    for _ in range(MAX_JOBS_PER_TICK):
        try:
            fn, future = _queue.get_nowait()
        except Empty:
            return
        if not future.set_running_or_notify_cancel():
            continue
        try:
            future.set_result(fn())
        except BaseException as ex:  # noqa: BLE001
            future.set_exception(ex)
