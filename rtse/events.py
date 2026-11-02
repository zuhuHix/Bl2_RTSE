"""Tiny in-process publish/subscribe, so game hooks and features can talk without importing each other.

    from rtse import events
    events.subscribe(events.DAMAGE, on_damage)      # on_damage(payload: dict) -> None
    events.publish(events.DAMAGE, {"amount": 120.0, "target": "...", "source": "..."})
    events.unsubscribe(events.DAMAGE, on_damage)

Contract (Wave 1 streams rely on it):
  `subscribe(topic: str, fn: Callable[[dict], None]) -> Callable[[], None]`
      Registers fn for topic (once: subscribing the same fn twice is a no-op). Returns a zero-argument function that
      unsubscribes it.
  `unsubscribe(topic: str, fn) -> bool`  True if fn was subscribed.
  `publish(topic: str, payload: dict | None = None) -> int`
      Calls every subscriber of topic synchronously, in subscription order, on the CALLING thread (game hooks publish
      from the game thread, so subscribers may touch game objects there). Returns how many subscribers ran without
      error. A subscriber that raises is logged and skipped; it never stops the others or the publisher.
      Subscribing/unsubscribing during publish is allowed (it affects the next publish).
  `clear(topic: str | None = None) -> None`  drops subscribers (all topics when None); for mod disable and tests.

Thread-safe: the subscriber table is guarded by a lock, but callbacks run outside it.

Topics (payloads are plain JSON-able dicts; the hooks that publish them are written by Wave 1 streams):
  DAMAGE           "game.damage"   {"amount": float, "target": str, "source": str | None, "critical": bool}
  PICKUP           "game.pickup"   {"item": str, "kind": "weapon" | "item" | "ammo" | "money" | ..., "amount": float | None}
  MISSION_CHANGED  "game.mission"  {"mission": str, "status": str, "objective": str | None}
  EDIT_APPLIED     "rtse.edit"     {"route": str, "ok": bool}   published by the route registry after each write
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

Payload = dict[str, Any]
Subscriber = Callable[[Payload], Any]

DAMAGE = "game.damage"
PICKUP = "game.pickup"
MISSION_CHANGED = "game.mission"
EDIT_APPLIED = "rtse.edit"
TOPICS = (DAMAGE, PICKUP, MISSION_CHANGED, EDIT_APPLIED)

_lock = threading.Lock()
_subscribers: dict[str, list[Subscriber]] = {}


def _log_error(message: str) -> None:
    try:
        from unrealsdk import logging  # noqa: PLC0415 - optional outside the game

        logging.error(message)
    except Exception:  # noqa: BLE001
        print(message)  # noqa: T201


def subscribe(topic: str, fn: Subscriber) -> Callable[[], None]:
    """Registers fn for topic; returns a function that unsubscribes it."""
    if not callable(fn):
        raise TypeError("subscriber must be callable")
    with _lock:
        listeners = _subscribers.setdefault(topic, [])
        if fn not in listeners:
            listeners.append(fn)
    return lambda: unsubscribe(topic, fn)


def unsubscribe(topic: str, fn: Subscriber) -> bool:
    """Removes fn from topic. True if it was subscribed."""
    with _lock:
        listeners = _subscribers.get(topic)
        if not listeners or fn not in listeners:
            return False
        listeners.remove(fn)
        if not listeners:
            del _subscribers[topic]
        return True


def publish(topic: str, payload: Payload | None = None) -> int:
    """Calls each subscriber of topic with payload; returns how many ran without raising."""
    with _lock:
        listeners = list(_subscribers.get(topic, ()))
    ok = 0
    for fn in listeners:
        try:
            fn({} if payload is None else payload)
        except Exception as ex:  # noqa: BLE001 - one broken subscriber must not break the rest
            _log_error(f"RTSE: event subscriber {getattr(fn, '__name__', fn)!r} for {topic!r} failed: {ex!r}")
        else:
            ok += 1
    return ok


def clear(topic: str | None = None) -> None:
    """Drops subscribers of one topic, or of every topic."""
    with _lock:
        if topic is None:
            _subscribers.clear()
        else:
            _subscribers.pop(topic, None)
