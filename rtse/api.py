"""API routes: collects the route tables that the feature modules expose.

Every route runs on the game thread and returns JSON-serialisable data. Read routes (GET) and write routes (POST)
both take one dict: the query string or the JSON body.

Adding a feature: write rtse/<feature>.py with ROUTE_ERROR/GET_ROUTES/POST_ROUTES and add it to ROUTE_MODULES.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

from mods_base import get_pc
from unrealsdk import logging

from . import (
    ammo,
    character,
    events,
    gear_debug,
    gear_edit,
    gear_inventory,
    gear_parts,
    inspector,
    journal,
    sdu,
    session_guard,
    skills,
    world,
)
from .routing import ApiError, Params, Route  # noqa: F401 - ApiError is re-exported for server.py and idempotency.py

ROUTE_MODULES = (
    gear_inventory,
    gear_parts,
    gear_edit,
    gear_debug,
    character,
    ammo,
    sdu,
    skills,
    world,
    inspector,
)


def ping(_params: Params) -> dict[str, Any]:
    return {"ok": True, "in_game": get_pc(possibly_loading=True) is not None}


# ping belongs to no feature, so it is collected as if a module had declared it
_CORE = SimpleNamespace(ROUTE_ERROR=ApiError, GET_ROUTES={"/api/ping": ping}, POST_ROUTES={})


def _mapping_errors(route: Route, error: type[Exception]) -> Route:
    """Turns the module's own error (e.g. CharacterError) into the ApiError the server answers with."""

    @functools.wraps(route)
    def mapped(params: Params) -> dict[str, Any]:
        try:
            return route(params)
        except error as ex:
            if isinstance(ex, ApiError):
                raise
            raise ApiError(getattr(ex, "status", 500), str(ex)) from None

    return mapped


def _write_allowed(path: str) -> tuple[bool, str]:
    try:
        return session_guard.can_write(path)
    except Exception as ex:  # noqa: BLE001 - a broken guard must not block every write
        logging.error(f"RTSE: session guard failed for {path}, allowing the write: {ex!r}")
        return True, ""


def _journaled_write(path: str, route: Route) -> Route:
    """A POST route: refused while the game state blocks writes, otherwise run in one journal transaction."""

    @functools.wraps(route)
    def write(params: Params) -> dict[str, Any]:
        allowed, reason = _write_allowed(path)
        if not allowed:
            raise ApiError(session_guard.BLOCKED_STATUS, reason)
        try:
            with journal.transaction(path, params):
                result = route(params)
        except Exception:
            events.publish(events.EDIT_APPLIED, {"route": path, "ok": False})
            raise
        events.publish(events.EDIT_APPLIED, {"route": path, "ok": True})
        return result

    return write


def collect(modules: Iterable[Any]) -> tuple[dict[str, Route], dict[str, Route]]:
    """Merges every module's GET_ROUTES and POST_ROUTES. A path declared twice is a bug, so it raises."""
    gets: dict[str, Route] = {}
    posts: dict[str, Route] = {}
    for module in modules:
        name = getattr(module, "__name__", repr(module))
        error: type[Exception] = getattr(module, "ROUTE_ERROR", ApiError)
        for path, route in getattr(module, "GET_ROUTES", {}).items():
            if path in gets:
                raise RuntimeError(f"duplicate GET route {path} (in {name})")
            gets[path] = _mapping_errors(route, error)
        for path, route in getattr(module, "POST_ROUTES", {}).items():
            if path in posts:
                raise RuntimeError(f"duplicate POST route {path} (in {name})")
            posts[path] = _journaled_write(path, _mapping_errors(route, error))
    return gets, posts


GET_ROUTES, POST_ROUTES = collect((_CORE, *ROUTE_MODULES))
