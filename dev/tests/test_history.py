"""rtse/history.py: the timeline view and undo (replaying a module's own route with the recorded `before`)."""

from __future__ import annotations

from typing import Any

import pytest

from rtse import api, character, history, journal, skills
from rtse.routing import ApiError


ZERO = {"save_id": 10, "class": "GD_Assassin.Character.CharClass_Assassin", "name": "Zer0"}
MAYA = {"save_id": 3, "class": "GD_Siren.Character.CharClass_Siren", "name": "Maya"}


@pytest.fixture(autouse=True)
def _fresh_journal():
    journal.reset_for_tests()
    yield
    journal.reset_for_tests()


@pytest.fixture(autouse=True)
def loaded(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Which character the game has loaded: set loaded["now"] (None = can't be read)."""
    holder: dict[str, Any] = {"now": dict(ZERO)}
    monkeypatch.setattr(history, "current_identity", lambda: holder["now"] and dict(holder["now"]))
    return holder


@pytest.fixture
def cash(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake character with one field, written through a fake /api/character/set (wrapped like a real POST route)."""
    state: dict[str, Any] = {"money": 100, "skill_points": 3, "level": 5, "xp": 5000, "cap": None, "writes": [],
                             "raise_after_write": None, "sticky_raise": False}
    monkeypatch.setattr(character, "read", lambda: dict(state))

    def set_field(params: dict[str, Any]) -> dict[str, Any]:
        field = params["field"]
        state["writes"].append((field, params["value"]))
        if field in ("level", "xp"):  # level and XP move together, and character.set_field records both
            before = {"level": state["level"], "xp": state["xp"]}
            if field == "level":
                state["level"], state["xp"] = params["value"], params["value"] * 1000
            else:
                state["xp"], state["level"] = params["value"], params["value"] // 1000
            after = {"level": state["level"], "xp": state["xp"]}
            journal.record(f"character.{field}", target=field, before=before, after=after)
            return {"applied": {"field": field, "before": before, "after": after}}
        before = state[field]
        value = params["value"] if state["cap"] is None else min(params["value"], state["cap"])
        state[field] = value
        if state["raise_after_write"] == field:  # the write lands, then it fails without recording anything
            if not state["sticky_raise"]:
                state["raise_after_write"] = None
            raise ApiError(500, "the game crashed halfway through the write")
        journal.record(f"character.{field}", target=field, before=before, after=value)
        return {"applied": {"field": field, "before": before, "after": value}}

    monkeypatch.setitem(api.POST_ROUTES, "/api/character/set", api._journaled_write("/api/character/set", set_field))  # noqa: SLF001
    return state


def post(path: str, params: dict[str, Any]) -> dict[str, Any]:
    return api.POST_ROUTES[path](params)


def undo(entry_id: int) -> dict[str, Any]:
    return post(history.UNDO_ROUTE, {"id": entry_id})


def test_timeline_lists_newest_first_with_requested_and_kept(cash):
    cash["cap"] = 300
    post("/api/character/set", {"field": "money", "value": 500})
    post("/api/character/set", {"field": "skill_points", "value": 9})
    view = history.timeline()
    assert [e["label"] for e in view["entries"]] == ["/api/character/set"] * 2
    newest, oldest = view["entries"]
    assert newest["records"][0]["kind"] == "character.skill_points"
    rec = oldest["records"][0]
    assert (rec["requested"], rec["before"], rec["after"]) == (500, 100, 300)  # asked 500, the game kept 300
    assert rec["route"] == "/api/character/set" and rec["undoable"] and rec["why_not"] is None
    assert oldest["undo"] == {"possible": True, "reason": None}
    assert view["limit"] == journal.MAX_ENTRIES


def test_undo_replays_the_route_with_before_and_links_both_entries(cash):
    post("/api/character/set", {"field": "money", "value": 500})
    original = journal.entries()[-1]
    result = undo(original["id"])

    assert cash["money"] == 100
    assert cash["writes"][-1] == ("money", 100)
    undo_entry = journal.entries()[-1]
    assert undo_entry["label"] == history.UNDO_ROUTE and undo_entry["undo_of"] == original["id"]
    assert original["undone_by"] == undo_entry["id"]
    # the nested route's record names the nested route, so the undo can itself be undone (a redo)
    assert undo_entry["records"][0]["route"] == "/api/character/set"
    assert undo_entry["records"][0]["params"] == {"field": "money", "value": 100}
    # the response already includes the undo's own entry (it only exists once the route has returned)
    assert result["applied"]["entry"]["id"] == undo_entry["id"]
    assert result["entries"][0]["id"] == undo_entry["id"]
    assert result["entries"][1]["undone_by"] == undo_entry["id"]

    undo(undo_entry["id"])  # redo
    assert cash["money"] == 500


def test_undo_twice_is_refused(cash):
    post("/api/character/set", {"field": "money", "value": 500})
    first = journal.entries()[-1]["id"]
    undo(first)
    with pytest.raises(ApiError, match="Already undone") as err:
        undo(first)
    assert err.value.status == 409


def test_undo_is_refused_when_the_value_changed_since(cash):
    post("/api/character/set", {"field": "money", "value": 500})
    first = journal.entries()[-1]["id"]
    cash["money"] = 777  # changed by the game or another tool
    writes = len(cash["writes"])
    with pytest.raises(ApiError, match="now 777, but this edit left 500; undo refused") as err:
        undo(first)
    assert err.value.status == 409
    assert len(cash["writes"]) == writes and cash["money"] == 777
    assert journal.entries()[-1]["id"] == first  # the refused undo left no entry


def test_a_later_edit_to_the_same_value_blocks_undoing_the_earlier_one(cash):
    post("/api/character/set", {"field": "money", "value": 500})
    first = journal.entries()[-1]["id"]
    post("/api/character/set", {"field": "money", "value": 900})
    with pytest.raises(ApiError, match="now 900"):
        undo(first)
    undo(journal.entries()[-1]["id"])  # the newest one is fine, and then the earlier one is too
    undo(first)
    assert cash["money"] == 100


def test_unknown_id_is_404(cash):
    with pytest.raises(ApiError) as err:
        undo(12345)
    assert err.value.status == 404


def test_world_edits_are_listed_but_not_undoable():
    with journal.transaction("/api/world/mission", {"id": "M1", "state": "complete"}):
        journal.record("world.mission", target=["M1"], before="active", after="complete")
    entry = journal.entries()[-1]
    view = history.timeline()["entries"][0]
    assert view["undo"]["possible"] is False and view["undo"]["reason"] == history.WORLD_REASON
    assert view["records"][0]["why_not"] == history.WORLD_REASON
    with pytest.raises(ApiError) as err:
        undo(entry["id"])
    assert err.value.status == 400


def test_inspector_and_unknown_kinds_are_not_undoable():
    for kind in ("inspect.value", "something.new"):
        with journal.transaction("/api/x", {}):
            journal.record(kind, target="t", before=1, after=2)
    reasons = [e["undo"]["reason"] for e in history.timeline()["entries"]]
    assert "no undo for something.new" in reasons[0] and "Inspector" in reasons[1]


def test_earlier_session_entries_are_shown_but_never_undone(tmp_path, cash):
    log = tmp_path / "history.json"
    journal.use_log_file(log)
    post("/api/character/set", {"field": "money", "value": 500})
    journal.reset_for_tests()
    journal.use_log_file(log)
    (entry,) = history.timeline()["entries"]
    assert entry["session"] == "earlier" and entry["undo"]["reason"] == history.EARLIER_REASON
    with pytest.raises(ApiError) as err:
        undo(entry["id"])
    assert err.value.status == 400


def test_a_failing_step_aborts_the_whole_undo(monkeypatch, cash):
    post("/api/character/set", {"field": "money", "value": 500})
    first = journal.entries()[-1]["id"]

    def refuse(_params):
        raise ApiError(501, "this game build can't write cash")

    monkeypatch.setitem(api.POST_ROUTES, "/api/character/set", api._journaled_write("/api/character/set", refuse))  # noqa: SLF001
    with pytest.raises(ApiError, match="undo of #1 failed at step 1 .this game build can't write cash.; everything it had changed was put back") as err:
        undo(first)
    assert err.value.status == 501
    assert journal.entries()[-1]["id"] == first and journal.get(first)["undone_by"] is None


def test_gear_stat_undo_needs_the_old_override():
    with journal.transaction("/api/item/set_stat", {"id": "gun", "stat": "WeaponDamage", "value": 99}):
        journal.record("gear.stat", target="gun", before={"value": 10, "base": 10}, after={"value": 99, "base": 99})
        journal.record("gear.stat", target="gun", before={"value": 10, "base": 10, "override": None}, after={"value": 99, "base": 99})
    old_shape, new_shape = journal.entries()[-1]["records"]
    assert "without the old override" in history._why_not(old_shape)  # noqa: SLF001
    assert history._why_not(new_shape) is None  # noqa: SLF001
    # no override before: the undo clears it (value None) so the game's own value comes back
    assert history.ADAPTERS["gear.stat"].steps(new_shape) == [
        ("/api/item/set_stat", {"id": "gun", "stat": "WeaponDamage", "value": None}),
    ]


def test_gear_part_and_level_steps():
    with journal.transaction("/api/item/set_part", {"id": "gun", "slot": "Barrel", "part": "GD.Barrel_B", "mode": "legal"}):
        journal.record("gear.part", target="gun", before={"id": "GD.Barrel_A", "name": "Barrel_A"}, after={"id": "GD.Barrel_B"})
    with journal.transaction("/api/item/set_level", {"id": "gun", "level": 50}):
        journal.record("gear.level", target="gun", before={"exp_level": 30, "grade": 30, "game_stage": 30},
                       after={"exp_level": 50, "grade": 50, "game_stage": 50})
    part, level = (e["records"][0] for e in journal.entries())
    assert history.ADAPTERS["gear.part"].steps(part) == [
        ("/api/item/set_part", {"id": "gun", "slot": "Barrel", "part": "GD.Barrel_A", "mode": "everything"}),
    ]
    assert history.ADAPTERS["gear.level"].steps(level) == [("/api/item/set_level", {"id": "gun", "level": 30})]
    assert history._same(history._comparable(part, {"id": "GD.Barrel_B"}), history._comparable(part, part["after"]))  # noqa: SLF001


def test_same_tolerates_float_noise_but_not_real_changes():
    assert history._same(0.30000000001, 0.3) and history._same({"a": [1, 2.0]}, {"a": [1, 2]})  # noqa: SLF001
    assert not history._same(1, 2) and not history._same({"a": 1}, {"a": 1, "b": 2})  # noqa: SLF001
    assert not history._same(True, 1.0)  # noqa: SLF001


def test_routes_are_registered_once_through_one_module():
    assert history in api.ROUTE_MODULES
    for path in ("/api/history", "/api/debug/history", "/api/checkpoints"):
        assert path in api.GET_ROUTES
    for path in (history.UNDO_ROUTE, "/api/checkpoints/create", "/api/checkpoints/restore"):
        assert path in api.POST_ROUTES


def test_undo_redo_undo_chain_keeps_the_links_current(cash):
    post("/api/character/set", {"field": "money", "value": 500})
    edit = journal.entries()[-1]["id"]
    undo(edit)
    first_undo = journal.entries()[-1]["id"]
    undo(first_undo)  # redo
    redo = journal.entries()[-1]["id"]
    assert cash["money"] == 500
    views = {e["id"]: e for e in history.timeline()["entries"]}
    assert views[edit]["undone_by"] is None and views[edit]["undo"]["possible"]  # in effect again
    assert views[first_undo]["undone_by"] == redo and not views[first_undo]["undo"]["possible"]
    assert views[redo]["undone_by"] is None

    undo(edit)  # undo the original again
    again = journal.entries()[-1]["id"]
    assert cash["money"] == 100
    views = {e["id"]: e for e in history.timeline()["entries"]}
    assert views[edit]["undone_by"] == again
    assert views[redo]["undone_by"] == again  # the redo's effect is gone too, so it can't be undone a second time
    with pytest.raises(ApiError, match="Already undone"):
        undo(redo)

    undo(again)  # and redo once more
    assert cash["money"] == 500
    assert journal.get(edit)["undone_by"] is None and journal.get(again)["undone_by"] is not None


def test_the_undo_response_already_shows_the_new_links(cash):
    post("/api/character/set", {"field": "money", "value": 500})
    edit = journal.entries()[-1]["id"]
    first = undo(edit)["applied"]["entry"]["id"]
    reply = undo(first)
    views = {e["id"]: e for e in reply["entries"]}
    assert views[edit]["undone_by"] is None and views[first]["undone_by"] == reply["applied"]["entry"]["id"]


def _tree_record(levels: dict[int, int], points: int) -> dict[str, Any]:
    return {"spent": sum(levels.values()), "skill_points": points,
            "levels": [{"id": i, "name": f"S{i}", "level": lv} for i, lv in levels.items()]}


def _reset_entry(monkeypatch, cash, *, fail_skill: int | None = None, fail_compensation: bool = False) -> tuple[dict, int]:
    """A recorded skill-tree reset of skills 0-3 (ranks 5, 1, 3, 0; 2 points back), and a fake /api/skills/set that
    can fail for one skill, and fail again when the undo tries to put a finished step back."""
    tree = {"levels": {0: 0, 1: 0, 2: 0, 3: 0}, "calls": []}
    cash["skill_points"] = 11
    monkeypatch.setattr(skills, "read", lambda: {"spent": sum(tree["levels"].values()), "skill_points": cash["skill_points"],
                                                 "skills": [{"id": i, "name": f"S{i}", "level": lv} for i, lv in tree["levels"].items()]
                                                 + [{"id": 9, "name": "Action", "level": 1, "action": True}]})

    def set_skill(params):
        index = params["id"]
        tree["calls"].append((index, params["level"]))
        if index == fail_skill:
            raise ApiError(501, f"this game build can't write S{index}")
        if fail_compensation and params["level"] == 0:
            raise ApiError(501, f"the game refused to put S{index} back")
        before = tree["levels"][index]
        tree["levels"][index] = params["level"]
        journal.record("skills.level", target=f"S{index}", before=before, after=params["level"])
        return {"applied": {"after": params["level"]}}

    monkeypatch.setitem(api.POST_ROUTES, "/api/skills/set", api._journaled_write("/api/skills/set", set_skill))  # noqa: SLF001
    with journal.transaction("/api/skills/reset", {"confirm": True}):
        journal.record("skills.reset", target="skill tree", before=_tree_record({0: 5, 1: 1, 2: 3, 3: 0}, 2),
                       after=_tree_record({0: 0, 1: 0, 2: 0, 3: 0}, 11))
    return tree, journal.entries()[-1]["id"]


def test_skill_reset_undo_sets_every_changed_rank_then_the_points(monkeypatch, cash):
    tree, reset = _reset_entry(monkeypatch, cash)
    undo(reset)
    assert tree["levels"] == {0: 5, 1: 1, 2: 3, 3: 0} and cash["skill_points"] == 2
    assert tree["calls"] == [(0, 5), (1, 1), (2, 3)]  # S3 was 0 before and after: not touched
    kinds = [r["kind"] for r in journal.entries()[-1]["records"]]
    assert kinds == ["skills.level", "skills.level", "skills.level", "character.skill_points"]


def test_skill_reset_undo_compares_every_rank_not_just_the_totals(monkeypatch, cash):
    tree, reset = _reset_entry(monkeypatch, cash)
    tree["levels"][3] = 1  # a point put into S3 after the reset...
    cash["skill_points"] = 11  # ...with the unspent total looking untouched
    with pytest.raises(ApiError, match="undo refused so a newer change isn't overwritten") as err:
        undo(reset)
    assert err.value.status == 409 and tree["calls"] == []


def test_a_reset_recorded_without_every_rank_is_not_undoable():
    with journal.transaction("/api/skills/reset", {"confirm": True}):
        journal.record("skills.reset", target="skill tree", before={"spent": 6, "skill_points": 2, "levels": []},
                       after={"spent": 0, "skill_points": 8})  # the old shape: totals only
    view = history.timeline()["entries"][0]
    assert view["undo"]["possible"] is False and "every skill's rank" in view["undo"]["reason"]


def test_a_failing_step_puts_the_finished_steps_back(monkeypatch, cash):
    tree, reset = _reset_entry(monkeypatch, cash, fail_skill=2)
    with pytest.raises(ApiError, match=r"undo of #\d+ failed at step 3 .*everything it had changed was put back, so nothing changed") as err:
        undo(reset)
    assert err.value.status == 501
    assert tree["levels"] == {0: 0, 1: 0, 2: 0, 3: 0} and cash["skill_points"] == 11  # as before the undo
    assert tree["calls"][:5] == [(0, 5), (1, 1), (2, 3), (1, 0), (0, 0)]  # put back newest first
    assert journal.entries()[-1]["id"] == reset  # nothing kept: nothing changed


def test_a_compensation_that_fails_leaves_an_explicit_partial_entry(monkeypatch, cash):
    tree, reset = _reset_entry(monkeypatch, cash, fail_skill=2, fail_compensation=True)
    with pytest.raises(ApiError, match="couldn't all be put back: the skill tree is .*'partial' entry"):
        undo(reset)
    assert tree["levels"] == {0: 5, 1: 1, 2: 0, 3: 0}  # really half-changed in the game
    partial = journal.entries()[-1]
    assert partial["partial"] is True and partial["attempted_undo_of"] == reset and partial["undo_of"] is None
    assert partial["identity"] == ZERO
    assert [(r["kind"], r["target"], r["before"], r["after"]) for r in partial["records"]] == [
        ("skills.level", "S0", 0, 5), ("skills.level", "S1", 0, 1),
    ]
    assert "the game refused to put S1 back" in partial["error"]
    view = history.timeline()["entries"][0]
    assert view["undo"] == {"possible": False, "reason": history.PARTIAL_REASON}
    assert all(not r["undoable"] for r in view["records"])
    assert journal.get(reset)["undone_by"] is None  # the reset itself was not undone
    with pytest.raises(ApiError) as err:
        undo(partial["id"])
    assert err.value.status == 400


def test_a_step_that_changes_the_game_and_then_raises_is_read_back_and_put_back(cash):
    post("/api/character/set", {"field": "money", "value": 500})
    edit = journal.entries()[-1]["id"]
    cash["raise_after_write"] = "money"  # the undo's write lands (money 100) and then raises, recording nothing
    with pytest.raises(ApiError, match="everything it had changed was put back"):
        undo(edit)
    assert cash["money"] == 500  # read back, found changed, put back to what the edit left
    assert cash["writes"][-2:] == [("money", 100), ("money", 500)]


def test_a_step_that_changes_the_game_and_cant_be_put_back_is_reported_as_partial(cash):
    post("/api/character/set", {"field": "money", "value": 500})
    edit = journal.entries()[-1]["id"]
    cash["raise_after_write"] = "money"
    cash["sticky_raise"] = True  # every write of money lands as asked and then raises, including the put-back
    cash["cap"] = 100  # ...and the put-back can't reach 500
    with pytest.raises(ApiError, match="couldn't all be put back: money is 100, not 500"):
        undo(edit)
    partial = journal.entries()[-1]
    assert partial["partial"] is True and "money is 100, not 500" in partial["error"]


# --- Which character an undo runs on ---------------------------------------------------------------------------------


def test_every_entry_records_the_character_it_was_made_to(cash):
    post("/api/character/set", {"field": "money", "value": 500})
    assert journal.entries()[-1]["identity"] == ZERO


def test_undo_is_refused_on_a_different_character(cash, loaded):
    post("/api/character/set", {"field": "money", "value": 500})
    edit = journal.entries()[-1]["id"]
    loaded["now"] = dict(MAYA)  # switched characters; the checked field happens to match
    writes = len(cash["writes"])
    with pytest.raises(ApiError, match=r"made to Zer0 \(CharClass_Assassin, save 10\), but Maya .* is loaded now") as err:
        undo(edit)
    assert err.value.status == 409 and len(cash["writes"]) == writes
    view = history.timeline()["entries"][0]
    assert view["undo"]["possible"] is False and "Maya" in view["undo"]["reason"]
    assert history.timeline()["character"] == MAYA
    loaded["now"] = dict(ZERO)  # back on Zer0: allowed again
    undo(edit)
    assert cash["money"] == 100


def test_undo_is_refused_when_the_character_cant_be_read(cash, loaded):
    post("/api/character/set", {"field": "money", "value": 500})
    edit = journal.entries()[-1]["id"]
    loaded["now"] = None
    with pytest.raises(ApiError, match="can't tell which character is loaded right now") as err:
        undo(edit)
    assert err.value.status == 409


def test_an_entry_made_without_a_known_character_is_never_undone(cash, loaded):
    loaded["now"] = None
    post("/api/character/set", {"field": "money", "value": 500})
    edit = journal.entries()[-1]["id"]
    loaded["now"] = dict(ZERO)
    with pytest.raises(ApiError, match="couldn't tell which character this change was made to"):
        undo(edit)


def test_current_identity_reads_save_id_class_and_name(monkeypatch, game):
    from types import SimpleNamespace  # noqa: PLC0415

    monkeypatch.undo()  # the real current_identity, reading the stub's player controller
    read = history.current_identity
    player_class = SimpleNamespace(_path_name=lambda: "GD_Siren.Character.CharClass_Siren")
    game.pc = SimpleNamespace(SaveGameFileId=3, PlayerClass=player_class,
                              PlayerReplicationInfo=SimpleNamespace(PlayerName="Maya"))
    assert read() == MAYA
    game.pc = SimpleNamespace(SaveGameFileId=3, PlayerClass=None, PlayerReplicationInfo=SimpleNamespace(PlayerName="Maya"))
    assert read() is None  # all three or nothing
    game.pc = None
    assert read() is None


# --- Level and XP are one state ------------------------------------------------------------------------------------


def test_level_undo_puts_back_level_and_exact_xp(cash):
    cash["xp"] = 5321  # level 5, partway through it
    post("/api/character/set", {"field": "level", "value": 9})
    undo(journal.entries()[-1]["id"])
    assert (cash["level"], cash["xp"]) == (5, 5321)


def test_level_undo_is_refused_when_xp_was_earned_since(cash):
    post("/api/character/set", {"field": "level", "value": 9})
    edit = journal.entries()[-1]["id"]
    cash["xp"] += 250  # XP earned after the edit; the level still matches
    with pytest.raises(ApiError, match="Level and xp is now level 9, xp 9250, but this edit left level 9, xp 9000") as err:
        undo(edit)
    assert err.value.status == 409 and cash["xp"] == 9250


def test_a_level_edit_recorded_without_xp_is_not_undoable():
    with journal.transaction("/api/character/set", {"field": "level", "value": 9}):
        journal.record("character.level", target="level", before=5, after=9)  # the old shape
    assert "both the level and the XP" in history.timeline()["entries"][0]["undo"]["reason"]
