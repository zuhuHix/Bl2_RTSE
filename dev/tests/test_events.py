"""rtse/events.py: subscribe/unsubscribe/publish contract."""

from __future__ import annotations

import threading

import pytest

from rtse import events


@pytest.fixture(autouse=True)
def _clean():
    events.clear()
    yield
    events.clear()


def test_publish_calls_subscribers_in_order_with_payload():
    seen = []
    events.subscribe(events.DAMAGE, lambda p: seen.append(("a", p)))
    events.subscribe(events.DAMAGE, lambda p: seen.append(("b", p)))
    assert events.publish(events.DAMAGE, {"amount": 5.0}) == 2
    assert seen == [("a", {"amount": 5.0}), ("b", {"amount": 5.0})]


def test_publish_without_subscribers_or_payload():
    assert events.publish("nobody.listens") == 0
    got = []
    events.subscribe(events.PICKUP, got.append)
    events.publish(events.PICKUP)
    assert got == [{}]


def test_topics_are_independent():
    got = []
    events.subscribe(events.MISSION_CHANGED, got.append)
    events.publish(events.DAMAGE, {"amount": 1})
    assert got == []


def test_subscribe_twice_is_once_and_unsubscribe_reports():
    got = []
    events.subscribe(events.DAMAGE, got.append)
    events.subscribe(events.DAMAGE, got.append)
    events.publish(events.DAMAGE, {"n": 1})
    assert got == [{"n": 1}]
    assert events.unsubscribe(events.DAMAGE, got.append) is True
    assert events.unsubscribe(events.DAMAGE, got.append) is False
    events.publish(events.DAMAGE, {"n": 2})
    assert got == [{"n": 1}]


def test_subscribe_returns_unsubscriber():
    got = []
    stop = events.subscribe(events.PICKUP, got.append)
    stop()
    events.publish(events.PICKUP, {"item": "x"})
    assert got == []


def test_raising_subscriber_does_not_break_publish(game):
    got = []

    def broken(_payload):
        raise RuntimeError("boom")

    events.subscribe(events.DAMAGE, broken)
    events.subscribe(events.DAMAGE, got.append)
    assert events.publish(events.DAMAGE, {"amount": 2}) == 1
    assert got == [{"amount": 2}]
    assert any("boom" in message for level, message in game.log if level == "error")


def test_unsubscribe_during_publish_affects_next_publish_only():
    got = []

    def once(payload):
        got.append(("once", payload))
        events.unsubscribe(events.DAMAGE, once)

    events.subscribe(events.DAMAGE, once)
    events.subscribe(events.DAMAGE, lambda p: got.append(("always", p)))
    events.publish(events.DAMAGE, {"n": 1})
    events.publish(events.DAMAGE, {"n": 2})
    assert got == [("once", {"n": 1}), ("always", {"n": 1}), ("always", {"n": 2})]


def test_non_callable_refused():
    with pytest.raises(TypeError):
        events.subscribe(events.DAMAGE, "not a function")  # type: ignore[arg-type]


def test_thread_safe_subscribe_and_publish():
    counter = []
    lock = threading.Lock()

    def make(i):
        def fn(_payload):
            with lock:
                counter.append(i)

        return fn

    subscribers = [make(i) for i in range(50)]
    threads = [threading.Thread(target=events.subscribe, args=(events.PICKUP, fn)) for fn in subscribers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    publishers = [threading.Thread(target=events.publish, args=(events.PICKUP, {})) for _ in range(10)]
    for t in publishers:
        t.start()
    for t in publishers:
        t.join()
    assert len(counter) == 50 * 10


def test_clear_one_topic_or_all():
    got = []
    events.subscribe(events.DAMAGE, got.append)
    events.subscribe(events.PICKUP, got.append)
    events.clear(events.DAMAGE)
    events.publish(events.DAMAGE, {})
    events.publish(events.PICKUP, {"p": 1})
    assert got == [{"p": 1}]
    events.clear()
    events.publish(events.PICKUP, {"p": 2})
    assert got == [{"p": 1}]
