"""Route registry: api.collect() merges the feature modules' routes, guards and journals every POST, and publishes
EDIT_APPLIED after each one.

The real route table is checked against the paths the app serves. Everything else uses small fake modules, so no
game objects are needed.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from rtse import api, events, journal, session_guard
from rtse.routing import ApiError

GET_PATHS = [
    "/api/ammo",
    "/api/bank",
    "/api/character",
    "/api/checkpoints",
    "/api/codes/export",
    "/api/codes/preview",
    "/api/debug/ammo",
    "/api/debug/character",
    "/api/debug/codes",
    "/api/debug/coverage",
    "/api/debug/farm",
    "/api/debug/gestalt",
    "/api/debug/gestalts",
    "/api/debug/history",
    "/api/debug/inventory",
    "/api/debug/item",
    "/api/debug/lab",
    "/api/debug/loadouts",
    "/api/debug/meshes",
    "/api/debug/part",
    "/api/debug/sdu",
    "/api/debug/skills",
    "/api/debug/validation",
    "/api/debug/world",
    "/api/farm",
    "/api/history",
    "/api/inspect",
    "/api/inventory/export",
    "/api/item",
    "/api/items",
    "/api/lab",
    "/api/lab/compare",
    "/api/lab/hits",
    "/api/loadouts",
    "/api/loadouts/export_character",
    "/api/loadouts/get",
    "/api/loadouts/link",
    "/api/loadouts/plan",
    "/api/loadouts/run",
    "/api/part",
    "/api/part_owners",
    "/api/ping",
    "/api/sdu",
    "/api/skills",
    "/api/world",
    "/api/world/objectives",
]
POST_PATHS = [
    "/api/ammo/set",
    "/api/character/set",
    "/api/checkpoints/cancel_restore",
    "/api/checkpoints/create",
    "/api/checkpoints/restore",
    "/api/checkpoints/settings",
    "/api/checkpoints/skip_backup",
    "/api/checkpoints/unpin",
    "/api/codes/import",
    "/api/farm/enable",
    "/api/farm/resolve",
    "/api/farm/session",
    "/api/farm/settings",
    "/api/farm/snapshot",
    "/api/history/undo",
    "/api/inspect/set",
    "/api/inventory/delete",
    "/api/inventory/duplicate",
    "/api/inventory/import",
    "/api/inventory/move",
    "/api/item/set_level",
    "/api/item/set_part",
    "/api/item/set_stat",
    "/api/lab/session/reset",
    "/api/lab/session/start",
    "/api/lab/session/stop",
    "/api/lab/snapshot",
    "/api/lab/snapshot/clear",
    "/api/loadouts/apply",
    "/api/loadouts/delete",
    "/api/loadouts/import",
    "/api/loadouts/import_missing",
    "/api/loadouts/save",
    "/api/parts/rescan",
    "/api/sdu/ammo_max",
    "/api/sdu/set",
    "/api/skills/reset",
    "/api/skills/set",
    "/api/world/challenge",
    "/api/world/mission",
    "/api/world/missions/reset",
    "/api/world/objective",
    "/api/world/objective_set",
    "/api/world/playthrough",
    "/api/world/stations",
    "/api/world/track",
]


class FeatureError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def fake_module(name: str, *, get: dict[str, Any] | None = None, post: dict[str, Any] | None = None, error: type = ApiError) -> Any:
    return types.SimpleNamespace(__name__=name, ROUTE_ERROR=error, GET_ROUTES=dict(get or {}), POST_ROUTES=dict(post or {}))


def ok_route(params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "params": params}


@pytest.fixture(autouse=True)
def _no_leftover_edit_subscribers():
    events.clear(events.EDIT_APPLIED)
    yield
    events.clear(events.EDIT_APPLIED)


@pytest.fixture
def journal_log(monkeypatch: pytest.MonkeyPatch) -> tuple[list[tuple], object]:
    """Records journal begin/commit/abort calls instead of the real journal. Returns (calls, the transaction begin returns)."""
    log: list[tuple] = []
    tx = object()
    monkeypatch.setattr(journal, "begin", lambda label, params=None: (log.append(("begin", label, params)), tx)[1])
    monkeypatch.setattr(journal, "commit", lambda t: log.append(("commit", t)))
    monkeypatch.setattr(journal, "abort", lambda t, error=None: log.append(("abort", t, error)))
    return log, tx


# --- the real table ------------------------------------------------------------------------------


def test_real_route_table_has_the_expected_paths():
    assert sorted(api.GET_ROUTES) == GET_PATHS
    assert sorted(api.POST_ROUTES) == POST_PATHS


def test_every_listed_module_declares_its_routes():
    for module in api.ROUTE_MODULES:
        assert isinstance(module.GET_ROUTES, dict), module.__name__
        assert isinstance(module.POST_ROUTES, dict), module.__name__
        assert issubclass(module.ROUTE_ERROR, Exception), module.__name__


def test_ping_still_answers_outside_the_game():
    assert api.GET_ROUTES["/api/ping"]({}) == {"ok": True, "in_game": False}


def test_real_write_outside_a_save_is_409_and_published_as_failed():
    seen: list[dict[str, Any]] = []
    events.subscribe(events.EDIT_APPLIED, seen.append)
    with pytest.raises(ApiError) as info:
        api.POST_ROUTES["/api/item/set_part"]({"id": "x", "slot": "Barrel", "part": "Barrel.New"})
    assert info.value.status == 409
    assert seen == [{"route": "/api/item/set_part", "ok": False}]


# --- collect() -----------------------------------------------------------------------------------


def test_duplicate_get_path_raises():
    a = fake_module("a", get={"/x": ok_route})
    b = fake_module("b", get={"/x": ok_route})
    with pytest.raises(RuntimeError, match="/x"):
        api.collect((a, b))


def test_duplicate_post_path_raises():
    a = fake_module("a", post={"/x/set": ok_route})
    b = fake_module("b", post={"/x/set": ok_route})
    with pytest.raises(RuntimeError, match="/x/set"):
        api.collect((a, b))


def test_module_error_becomes_api_error_with_the_same_status():
    def boom(_params):
        raise FeatureError(409, "nope")

    gets, _posts = api.collect((fake_module("feat", get={"/api/feat": boom}, error=FeatureError),))
    with pytest.raises(ApiError) as info:
        gets["/api/feat"]({})
    assert info.value.status == 409
    assert str(info.value) == "nope"


def test_api_error_from_a_route_passes_through_unchanged():
    err = ApiError(404, "gone")

    def route(_params):
        raise err

    gets, _posts = api.collect((fake_module("feat", get={"/api/feat": route}),))
    with pytest.raises(ApiError) as info:
        gets["/api/feat"]({})
    assert info.value is err


def test_a_fake_module_added_to_route_modules_shows_up(monkeypatch: pytest.MonkeyPatch):
    extra = fake_module("extra", get={"/api/extra": lambda _p: {"x": 1}}, post={"/api/extra/set": ok_route})
    monkeypatch.setattr(api, "ROUTE_MODULES", (*api.ROUTE_MODULES, extra))
    gets, posts = api.collect(api.ROUTE_MODULES)
    assert set(gets) == (set(api.GET_ROUTES) - {"/api/ping"}) | {"/api/extra"}  # ping is core, not a module
    assert set(posts) == set(api.POST_ROUTES) | {"/api/extra/set"}


# --- writes: guard, journal, events --------------------------------------------------------------


def test_blocked_write_is_423_and_the_route_is_not_called(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, Any]] = []
    _gets, posts = api.collect((fake_module("feat", post={"/api/feat/set": lambda p: calls.append(p) or {}}),))
    monkeypatch.setattr(session_guard, "can_write", lambda path: (False, "you are in a multiplayer session"))
    with pytest.raises(ApiError) as info:
        posts["/api/feat/set"]({"v": 1})
    assert info.value.status == session_guard.BLOCKED_STATUS == 423
    assert str(info.value) == "you are in a multiplayer session"
    assert calls == []


def test_guard_is_asked_about_the_route_path(monkeypatch: pytest.MonkeyPatch):
    asked: list[str] = []
    monkeypatch.setattr(session_guard, "can_write", lambda path: (asked.append(path), (True, ""))[1])
    _gets, posts = api.collect((fake_module("feat", post={"/api/feat/set": ok_route}),))
    posts["/api/feat/set"]({})
    assert asked == ["/api/feat/set"]


def test_guard_that_raises_allows_the_write_and_logs(monkeypatch: pytest.MonkeyPatch):
    def broken(_path):
        raise RuntimeError("guard broke")

    logged: list[str] = []
    monkeypatch.setattr(session_guard, "can_write", broken)
    monkeypatch.setattr(api.logging, "error", logged.append)
    _gets, posts = api.collect((fake_module("feat", post={"/api/feat/set": ok_route}),))
    assert posts["/api/feat/set"]({"v": 2}) == {"ok": True, "params": {"v": 2}}
    assert any("guard broke" in line for line in logged)


def test_write_runs_in_one_journal_transaction_that_commits(journal_log):
    log, tx = journal_log
    _gets, posts = api.collect((fake_module("feat", post={"/api/feat/set": ok_route}),))
    posts["/api/feat/set"]({"v": 1})
    assert log == [("begin", "/api/feat/set", {"v": 1}), ("commit", tx)]


def test_failed_write_aborts_its_journal_transaction_and_re_raises(journal_log):
    log, tx = journal_log

    def bad(_params):
        raise FeatureError(501, "the game refused")

    _gets, posts = api.collect((fake_module("feat", post={"/api/feat/set": bad}, error=FeatureError),))
    with pytest.raises(ApiError) as info:
        posts["/api/feat/set"]({})
    assert info.value.status == 501
    assert [entry[0] for entry in log] == ["begin", "abort"]
    _kind, aborted_tx, error = log[1]
    assert aborted_tx is tx
    assert isinstance(error, ApiError) and error.status == 501


def test_successful_write_publishes_edit_applied_with_ok_true():
    seen: list[dict[str, Any]] = []
    events.subscribe(events.EDIT_APPLIED, seen.append)
    _gets, posts = api.collect((fake_module("feat", post={"/api/feat/set": ok_route}),))
    posts["/api/feat/set"]({})
    assert seen == [{"route": "/api/feat/set", "ok": True}]


def test_failed_write_publishes_edit_applied_with_ok_false_then_re_raises():
    seen: list[dict[str, Any]] = []
    events.subscribe(events.EDIT_APPLIED, seen.append)

    def bad(_params):
        raise FeatureError(409, "no")

    _gets, posts = api.collect((fake_module("feat", post={"/api/feat/set": bad}, error=FeatureError),))
    with pytest.raises(ApiError):
        posts["/api/feat/set"]({})
    assert seen == [{"route": "/api/feat/set", "ok": False}]


def test_blocked_write_publishes_nothing_and_opens_no_transaction(monkeypatch: pytest.MonkeyPatch, journal_log):
    log, _tx = journal_log
    seen: list[dict[str, Any]] = []
    events.subscribe(events.EDIT_APPLIED, seen.append)
    monkeypatch.setattr(session_guard, "can_write", lambda path: (False, "loading"))
    _gets, posts = api.collect((fake_module("feat", post={"/api/feat/set": ok_route}),))
    with pytest.raises(ApiError):
        posts["/api/feat/set"]({})
    assert seen == []
    assert log == []
