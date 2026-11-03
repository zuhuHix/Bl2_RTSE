"""rtse/damage.py against fake controller, pawns, weapons and damage structs: hook install and removal, the session
gate, the local-player filter, recorded hits and events, the ring buffer, the fallback hook, summary math, the routes,
and snapshot / compare with a fake item.

    python -I -m pytest dev/tests -k damage
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from rtse import api, damage, events
from rtse.routing import ApiError


class Fake:
    """A game object stand-in: identity equality (like a UObject), a path name and whatever attributes are given."""

    def __init__(self, path: str, **attrs: Any) -> None:
        self._p = path
        self.__dict__.update(attrs)

    def _path_name(self) -> str:
        return self._p


class Boom:
    """Args whose every attribute read raises, so a hook that touches them shows up as an error."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"args read: {name}")


@pytest.fixture(autouse=True)
def _fresh(game, monkeypatch, tmp_path):
    damage._clear_all()
    events.clear()
    monkeypatch.setattr(damage, "DEBUG_LAB_FILE", tmp_path / "debug_lab.json")
    yield
    events.clear()
    damage._clear_all()


@pytest.fixture
def world(game):
    """A local player with a pistol, one enemy, and the hooks installed."""
    gun = Fake("WillowWeapon.Pistol", ProjectilesPerShot=1, ShotCost=1)
    pawn = Fake("Pawn.Me", Weapon=gun)
    gun.Owner = pawn
    pc = Fake("PlayerController.Me", Pawn=pawn)
    enemy = Fake("Enemy.Bandit", Class=SimpleNamespace(Name="Bandit"))
    gun.Class = SimpleNamespace(Name="WillowWeapon")
    game.pc = pc
    damage.install_hooks()
    post = {path: fn for path, kind, fn in damage.HOOKS if kind == damage.Type.POST}
    pre = {path: fn for path, kind, fn in damage.HOOKS if kind == damage.Type.PRE}
    return SimpleNamespace(pc=pc, pawn=pawn, gun=gun, enemy=enemy, hooks=post, pre=pre)


@pytest.fixture
def clock(monkeypatch):
    """A controllable damage._now(): clock.t is the time; clock.tick(dt) moves it on."""
    box = SimpleNamespace(t=1000.0)
    box.tick = lambda dt=1.0: setattr(box, "t", box.t + dt)
    monkeypatch.setattr(damage, "_now", lambda: box.t)
    return box


def summary_args(**fields: Any) -> SimpleNamespace:
    base = {"FinalDamage": 100.0, "DamageDealtToShields": 0.0, "InitialDamage": 100.0, "bWasCrit": False,
            "bWasOneShotKill": False, "PreviousHealth": 500.0}  # fmt: skip
    base.update(fields)
    return SimpleNamespace(DamageSummary=SimpleNamespace(**base))


def notify(world, amount=100.0, *, crit=False, kill=False, target=None, obj=None):
    args = summary_args(FinalDamage=amount, bWasCrit=crit, bWasOneShotKill=kill)
    args.DamagedPawn = world.enemy if target is None else target
    world.hooks[damage.PRIMARY](world.pc if obj is None else obj, args, None, None)


def take(world, amount=100.0, *, crit=False, target=None, instigator=None, obj=None, causer=None, inside=None):
    """One TakeDamage call as the game makes it: PRE hook, the call's body (`inside`, e.g. a nested notify), POST hook."""
    victim = world.enemy if target is None else target
    victim.bWasLastDamageACriticalHit = crit
    args = SimpleNamespace(Damage=amount, InstigatedBy=world.pc if instigator is None else instigator, DamageCauser=causer)
    world.pre[damage.FALLBACK](victim if obj is None else obj, args, None, None)
    if inside is not None:
        inside()
    world.hooks[damage.FALLBACK](victim if obj is None else obj, args, None, None)


def shoot(world, *, fire_mode=0):
    world.hooks[damage.SHOTS](world.gun, SimpleNamespace(FireModeNum=fire_mode), None, None)


def start(cap: int | None = None, weapon: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if cap is not None:
        body["cap"] = cap
    if weapon is not None:
        body["weapon"] = weapon
    return api.POST_ROUTES["/api/lab/session/start"](body)


def hits_route(**query: Any) -> dict[str, Any]:
    return api.GET_ROUTES["/api/lab/hits"](query)


def hit(seq: int, amount: float, *, crit: bool = False, t: float = 0.0, target_id: str = "T", via: str = "notify", **kw) -> dict[str, Any]:
    base = {"seq": seq, "t": t, "amount": amount, "shield": 0.0, "initial": None, "critical": crit, "kill": False,
            "target": "Bandit", "target_id": target_id, "source": "W", "via": via, "estimated": via == "take_damage"}  # fmt: skip
    base.update(kw)
    return base


# --- Install and remove -----------------------------------------------------------------------------------------------


def test_install_twice_registers_each_hook_once(game):
    damage.install_hooks()
    damage.install_hooks()
    assert len(game.hooks) == len(damage.HOOKS)
    assert {path for path, _kind, _id in game.hooks} == set(damage.HOOK_PATHS)
    assert damage._installed == list(damage.HOOK_PATHS)
    damage.remove_hooks()
    assert game.hooks == {}
    assert damage._installed == []


def test_remove_clears_every_path_even_if_not_remembered(game):
    game.hooks[(damage.PRIMARY, 1, damage.HOOK_ID)] = object()  # left behind by an earlier run of the module
    damage.remove_hooks()
    assert game.hooks == {}


def test_remove_ends_the_session(game, world):
    start()
    assert damage._session_active
    damage.remove_hooks()
    assert not damage._session_active


# --- The session gate ---------------------------------------------------------------------------------------------------


def test_hooks_return_without_touching_args_when_no_session(world):
    for _path, _kind, fn in damage.HOOKS:
        fn(world.pc, Boom(), None, None)  # would raise inside the hook if it read the args
    assert damage._active_hits() == []
    assert damage._fired == dict.fromkeys(damage.HOOK_PATHS, 0)
    assert damage._errors == 0


def test_non_local_controller_is_ignored(world):
    start()
    other = Fake("PlayerController.Other", Pawn=Fake("Pawn.Other"))
    notify(world, obj=other)
    args = SimpleNamespace(Damage=50.0, InstigatedBy=other)
    world.hooks[damage.FALLBACK](world.enemy, args, None, None)
    assert damage._active_hits() == []


def test_shot_by_someone_else_is_not_counted(world):
    start()
    stranger = Fake("Pawn.Stranger")
    world.gun.Owner = stranger
    shoot(world)
    assert damage._s["shots_seen"] == 0


def test_other_weapon_is_skipped_when_session_has_a_weapon(world):
    start(weapon="Weapon.Shotgun")
    take(world, causer=world.gun, inside=lambda: notify(world))  # the causer says: the pistol, not the shotgun
    shoot(world)  # the pistol consuming ammo: exact, skipped
    assert damage._active_hits() == []
    assert damage._s["shots_seen"] == 0


def test_a_held_weapon_hit_is_never_dropped_by_the_weapon_filter(world):
    start(weapon="Weapon.Shotgun")
    notify(world)  # no TakeDamage call around it: only the held weapon is known, which is approximate
    hits = damage._active_hits()
    assert len(hits) == 1
    assert hits[0]["attribution"] == "held"
    assert hits[0]["source"] == "WillowWeapon.Pistol"
    assert damage.state()["session"]["approximate"] == 1


def test_session_weapon_is_resolved_through_renamed_ids(world, monkeypatch):
    from rtse import gear_inventory

    monkeypatch.setitem(gear_inventory._id_aliases, "Weapon.OldPistol", "WillowWeapon.Pistol")
    start(weapon="Weapon.OldPistol")
    notify(world)
    assert len(damage._active_hits()) == 1


# --- Recorded hits and events -------------------------------------------------------------------------------------------


def test_recorded_hit_has_the_payload_keys_and_is_published(world):
    seen: list[dict[str, Any]] = []
    events.subscribe(events.DAMAGE, seen.append)
    start()
    notify(world, 120.0, crit=True)

    body = hits_route()
    assert body["session"] == 1
    assert len(body["hits"]) == 1
    recorded = body["hits"][0]
    assert set(recorded) == {
        "seq", "t", "amount", "shield", "initial", "critical", "kill", "target", "target_id", "source", "attribution",
        "via", "estimated",
    }  # fmt: skip
    assert recorded["amount"] == 120.0
    assert recorded["critical"] is True
    assert recorded["via"] == "notify"
    assert recorded["estimated"] is False
    assert recorded["target"] == "Bandit"
    assert recorded["target_id"] == "Enemy.Bandit"
    assert recorded["source"] == "WillowWeapon.Pistol"
    assert body["last"] == recorded["seq"]
    assert seen == [recorded]


def test_no_event_is_published_without_a_session(world):
    seen: list[dict[str, Any]] = []
    events.subscribe(events.DAMAGE, seen.append)
    notify(world)
    assert seen == []


def test_crit_is_counted(world):
    start()
    notify(world, 100.0, crit=False)
    notify(world, 250.0, crit=True)
    summary = damage.state()["summary"]
    assert summary["hits"] == 2
    assert summary["crits"] == 1
    assert summary["crit_rate"] == 0.5


def test_ring_buffer_drops_oldest_and_counts_dropped(world):
    start(cap=10)
    for n in range(12):
        notify(world, float(n + 1))
    state = damage.state()
    assert state["session"]["recorded"] == 12
    assert state["session"]["kept"] == 10
    assert state["session"]["dropped"] == 2
    kept = hits_route()["hits"]
    assert [h["seq"] for h in kept] == list(range(3, 13))
    assert kept[0]["amount"] == 3.0


# --- Fallback hook ------------------------------------------------------------------------------------------------------


def test_fallback_hit_waits_out_the_window_then_is_estimated(world, clock):
    seen: list[dict[str, Any]] = []
    events.subscribe(events.DAMAGE, seen.append)
    start()
    take(world, 80.0, crit=True)
    assert damage.state()["session"]["pending"] == 1
    assert damage._active_hits() == [] and seen == []
    clock.tick(damage.DEDUPE_WINDOW + 0.01)
    state = damage.state()
    assert state["session"]["via"] == "take_damage"
    assert state["summary"]["hits"] == 1
    assert state["summary"]["crits"] == 1
    assert "total" in state["summary"]["estimated"]
    assert hits_route()["hits"][0]["estimated"] is True
    assert len(seen) == 1


def test_a_primary_report_inside_the_call_is_the_only_record(world, clock):
    seen: list[dict[str, Any]] = []
    events.subscribe(events.DAMAGE, seen.append)
    start()
    take(world, 999.0, causer=world.gun, inside=lambda: notify(world, 40.0))
    clock.tick(5)
    hits = hits_route()["hits"]
    assert [(h["via"], h["amount"], h["attribution"], h["source"]) for h in hits] == [("notify", 40.0, "weapon", "WillowWeapon.Pistol")]
    assert len(seen) == 1


def test_a_primary_report_after_the_call_discards_the_waiting_fallback(world, clock):
    seen: list[dict[str, Any]] = []
    events.subscribe(events.DAMAGE, seen.append)
    start()
    take(world, 100.0)  # pre-mitigation 100 ...
    clock.tick(0.05)
    notify(world, 40.0)  # ... the same hit (InitialDamage 100, FinalDamage 40), reported after TakeDamage returned
    clock.tick(5)
    take(world, 999.0)  # once the primary has fired, the fallback is off
    clock.tick(5)
    assert [h["via"] for h in damage._active_hits()] == ["notify"]
    assert len(seen) == 1
    assert damage.state()["summary"]["total"] == 40.0
    assert "total" not in damage.state()["summary"]["estimated"]


def test_fallback_hits_committed_before_the_primary_stay_visible(world, clock):
    start()
    take(world, 80.0)
    clock.tick(1)
    notify(world, 40.0)
    hits = damage._active_hits()
    assert [h["via"] for h in hits] == ["take_damage", "notify"]
    assert damage.state()["summary"]["hits"] == 2
    assert "total" in damage.state()["summary"]["estimated"]  # one of them is an estimate


def test_one_buffer_one_cap_for_both_hooks(world, clock):
    start(cap=10)
    for _ in range(6):
        take(world, 1.0)
        clock.tick(1)
    for _ in range(6):
        notify(world, 2.0)
    session = damage.state()["session"]
    assert (session["recorded"], session["kept"], session["dropped"]) == (12, 10, 2)


def test_a_projectile_causer_credits_the_weapon_that_owns_it(world, clock):
    start()
    projectile = Fake("Projectile.Rocket", Class=SimpleNamespace(Name="WillowProjectile"), Owner=world.gun)
    world.pawn.Weapon = Fake("WillowWeapon.Other", Class=SimpleNamespace(Name="WillowWeapon"))  # swapped since firing
    take(world, causer=projectile, inside=lambda: notify(world, 10.0))
    hit = damage._active_hits()[0]
    assert (hit["source"], hit["attribution"]) == ("WillowWeapon.Pistol", "projectile")


def test_fallback_ignores_hits_on_the_local_pawn_and_other_instigators(world):
    start()
    take(world, 30.0, target=world.pawn, instigator=world.pc)
    take(world, 30.0, instigator=Fake("PlayerController.Other"))
    assert damage._active_hits() == []


# --- Shots ----------------------------------------------------------------------------------------------------------------


def test_shots_record_pellets_and_cost(world):
    start()
    world.gun.ProjectilesPerShot = 8
    world.gun.ShotCost = 2
    shoot(world, fire_mode=1)
    shot = damage._s["shots"][-1]
    assert shot["pellets"] == 8
    assert shot["cost"] == 2.0
    assert shot["fire_mode"] == 1
    assert shot["weapon"] == "WillowWeapon.Pistol"


# --- Errors ---------------------------------------------------------------------------------------------------------------


def test_broken_args_do_not_raise_and_are_counted_once_logged(world, game):
    start()
    for _ in range(3):
        world.hooks[damage.PRIMARY](world.pc, Boom(), None, None)
    world.pre[damage.FALLBACK](world.enemy, Boom(), None, None)
    state = damage.state()["hooks"]
    assert state["errors"] == 4
    assert "RuntimeError" in state["last_error"]
    assert state["fired"][damage.PRIMARY] == 3
    world.hooks[damage.FALLBACK](world.enemy, Boom(), None, None)  # POST without its PRE: not the player's call
    assert damage.state()["hooks"]["errors"] == 4
    lab_errors = [message for level, message in game.log if level == "error" and "damage lab" in message]
    assert len(lab_errors) == 2  # one per distinct message: the notify failure and the fallback failure


def test_a_shot_with_no_pawn_is_ignored(world):
    start()
    world.pc.Pawn = None
    world.gun.Owner = None
    shoot(world)
    assert damage._s["shots_seen"] == 0
    assert damage._hooks_state()["errors"] == 0


# --- summarize() -------------------------------------------------------------------------------------------------------


def test_summarize_math():
    hits = [
        hit(1, 100.0, t=0.0, target_id="A"),
        hit(2, 200.0, crit=True, t=1.0, target_id="A"),
        hit(3, 300.0, crit=True, t=2.0, target_id="B"),
    ]
    hits[2]["kill"] = True
    hits[2]["shield"] = 10.0
    shots = [{"t": 0.0, "pellets": 1}, {"t": 2.0, "pellets": 4}]
    out = damage.summarize(hits, shots)
    assert out["hits"] == 3
    assert out["crits"] == 2
    assert out["crit_rate"] == pytest.approx(2 / 3)
    assert out["total"] == 600.0
    assert out["shield"] == 10.0
    assert out["kills"] == 1
    assert (out["max_hit"], out["min_hit"]) == (300.0, 100.0)
    assert out["avg_hit"] == 200.0
    assert out["avg_crit"] == 250.0
    assert out["avg_noncrit"] == 100.0
    assert out["crit_multiplier"] == pytest.approx(2.5)
    assert out["duration"] == 2.0
    assert out["dps"] == 300.0
    assert out["shots"] == 2
    assert out["pellets_per_shot"] == 4
    assert out["pellets_fired"] == 5
    assert out["hits_per_shot"] == 1.5
    assert out["per_pellet"] == 120.0
    assert {row["target_id"]: row["hits"] for row in out["targets"]} == {"A": 2, "B": 1}
    assert out["targets"][0]["total"] == 300.0
    assert out["estimated"] == ["crit_multiplier", "per_pellet", "hits_per_shot", "kills"]


def test_summarize_dps_needs_half_a_second():
    hits = [hit(1, 100.0, t=1.0), hit(2, 100.0, t=1.2)]
    out = damage.summarize(hits, [])
    assert out["duration"] == pytest.approx(0.2)
    assert out["dps"] is None
    assert out["pellets_per_shot"] is None
    assert out["hits_per_shot"] is None


def test_summarize_empty():
    out = damage.summarize([], [])
    assert out["hits"] == 0
    assert out["total"] == 0
    assert out["crit_rate"] is None
    assert out["dps"] is None
    assert out["duration"] is None
    assert out["per_pellet"] is None


def test_summarize_fallback_marks_damage_fields_estimated():
    out = damage.summarize([hit(1, 90.0, via="take_damage")], [])
    for name in ("total", "max_hit", "avg_hit", "dps", "crit_rate", "shield"):
        assert name in out["estimated"]


# --- Routes: session lifecycle and validation ------------------------------------------------------------------------------


def test_start_stop_reset_routes(world):
    body = start(cap=50)
    assert body["session"]["active"] is True
    assert body["session"]["id"] == 1
    assert body["session"]["cap"] == 50

    notify(world)
    body = api.POST_ROUTES["/api/lab/session/reset"]({})
    assert body["session"]["active"] is True
    assert body["session"]["id"] == 2
    assert body["session"]["cap"] == 50
    assert body["session"]["recorded"] == 0

    body = api.POST_ROUTES["/api/lab/session/stop"]({})
    assert body["session"]["active"] is False
    notify(world)
    assert api.GET_ROUTES["/api/lab"]({})["session"]["recorded"] == 0


def test_start_while_active_restarts_with_new_id(world):
    start()
    notify(world)
    body = start()
    assert body["session"]["id"] == 2
    assert body["session"]["kept"] == 0


def test_start_stores_weapon_and_defaults_cap(world):
    body = start(weapon="WillowWeapon.Pistol")
    assert body["session"]["weapon"] == "WillowWeapon.Pistol"
    assert body["session"]["cap"] == damage.DEFAULT_CAP


def test_stop_keeps_buffers_for_reading(world):
    start()
    notify(world, 70.0)
    api.POST_ROUTES["/api/lab/session/stop"]({})
    assert hits_route()["hits"][0]["amount"] == 70.0


@pytest.mark.parametrize("cap", [5, 20001, "abc", True, 10.5])
def test_cap_validation_is_400(cap):
    with pytest.raises(ApiError) as caught:
        start(cap=cap)
    assert caught.value.status == 400
    assert damage._session_active is False


def test_cap_accepts_numeric_strings_from_a_query():
    body = api.POST_ROUTES["/api/lab/session/start"]({"cap": "100"})
    assert body["session"]["cap"] == 100


def test_weapon_must_be_a_string():
    with pytest.raises(ApiError) as caught:
        start(weapon=7)  # type: ignore[arg-type]
    assert caught.value.status == 400


def test_hits_route_after_limit_and_target(world):
    start()
    for n in range(5):
        notify(world, float(n + 1))
    take(world, 9.0)  # estimated; ignored because notify hits exist
    all_hits = hits_route()["hits"]
    assert [h["seq"] for h in all_hits] == [1, 2, 3, 4, 5]

    assert [h["seq"] for h in hits_route(after=2)["hits"]] == [3, 4, 5]
    assert [h["seq"] for h in hits_route(after="2")["hits"]] == [3, 4, 5]
    assert [h["seq"] for h in hits_route(limit=2)["hits"]] == [1, 2]
    assert [h["seq"] for h in hits_route(after=3, limit="1")["hits"]] == [4]
    assert hits_route(after=5)["hits"] == []
    assert hits_route(after=5)["last"] == 5
    assert hits_route(target="Nobody")["hits"] == []
    assert hits_route(target="Enemy.Bandit")["hits"][0]["seq"] == 1


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 2001}, {"after": "x"}, {"limit": True}])
def test_hits_route_rejects_bad_numbers(query):
    with pytest.raises(ApiError) as caught:
        hits_route(**query)
    assert caught.value.status == 400


def test_hits_route_without_session_is_empty():
    assert hits_route() == {"session": 0, "hits": [], "last": -1}


def test_state_route_target_filter(world):
    start()
    notify(world, 10.0)
    notify(world, 20.0, target=Fake("Enemy.Other", Class=SimpleNamespace(Name="Psycho")))
    body = api.GET_ROUTES["/api/lab"]({"target": "Enemy.Other"})
    assert body["summary"]["hits"] == 1
    assert body["summary"]["total"] == 20.0
    assert set(body) == {"session", "summary", "hooks", "snapshots", "unverified"}
    assert set(body["unverified"]) == {"hooks", "shield", "dps_clock", "pellets"}
    assert body["hooks"]["installed"] == list(damage.HOOK_PATHS)


# --- Snapshots and compare ---------------------------------------------------------------------------------------------


def detail_for(item_id: str, current: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": "Pistol",
        "stats": [
            {"name": "InstantHitDamage", "label": "Damage per shot / pellet", "value": current["dmg"], "base": 10.0, "override": None},
            {"name": "ProjectilesPerShot", "label": "Pellets per shot", "value": current["pellets"], "base": 1, "override": None},
            {"name": "FireInterval", "label": "Time between shots (s) - lower is faster", "value": current["interval"], "base": 0.5, "override": None},  # noqa: E501
        ],
        "slots": [
            {"slot": "Barrel", "current": {"name": current["barrel"]}, "options": []},
            {"slot": "Sight", "current": None, "options": []},
        ],
    }  # fmt: skip


@pytest.fixture
def item(monkeypatch):
    """damage.item_detail replaced by a fake: the item's current state is the dict `current`."""
    current = {"dmg": 10.0, "pellets": 1, "interval": 0.5, "barrel": "Barrel.A"}

    def fake_detail(params):
        assert params["id"] == "Item.Gun#1"
        return detail_for("Item.Gun#1", current)

    monkeypatch.setattr(damage, "item_detail", fake_detail)
    return current


def snapshot(slot: str) -> dict[str, Any]:
    return api.POST_ROUTES["/api/lab/snapshot"]({"id": "Item.Gun#1", "slot": slot})


def test_snapshot_shape(item):
    body = snapshot("a")
    snap = body["snapshots"]["a"]
    assert snap["slot"] == "a"
    assert snap["id"] == "Item.Gun#1"
    assert snap["name"] == "Pistol"
    assert isinstance(snap["taken_at"], float)
    assert snap["stats"][0] == {"name": "InstantHitDamage", "label": "Damage per shot / pellet", "value": 10.0, "base": 10.0}
    assert snap["parts"] == [{"slot": "Barrel", "name": "Barrel.A"}, {"slot": "Sight", "name": None}]
    json.dumps(body)  # plain values only


def test_compare_without_a_is_409(item):
    with pytest.raises(ApiError) as caught:
        api.GET_ROUTES["/api/lab/compare"]({})
    assert caught.value.status == 409


def test_compare_with_snapshot_b(item):
    snapshot("a")
    item.update(dmg=20.0, barrel="Barrel.B")
    snapshot("b")
    item.update(dmg=99.0)  # changes after B; must not show, B is a stored snapshot

    out = api.GET_ROUTES["/api/lab/compare"]({})
    assert out["b_live"] is False
    rows = {row["name"]: row for row in out["rows"]}
    dmg = rows["InstantHitDamage"]
    assert (dmg["a"], dmg["b"], dmg["delta"], dmg["pct"], dmg["changed"], dmg["estimated"]) == (10.0, 20.0, 10.0, 100.0, True, False)
    assert rows["ProjectilesPerShot"]["changed"] is False
    assert rows["ProjectilesPerShot"]["pct"] == 0.0
    shot_row = rows["est_shot_damage"]
    assert (shot_row["a"], shot_row["b"], shot_row["estimated"]) == (10.0, 20.0, True)
    assert shot_row["label"] == "Damage per shot (estimated: damage x pellets)"
    card = rows["est_card_dps"]
    assert (card["a"], card["b"], card["delta"], card["estimated"]) == (20.0, 40.0, 20.0, True)
    assert card["label"] == "Card DPS (estimated: per shot / fire interval)"
    parts = {p["slot"]: p for p in out["parts"]}
    assert parts["Barrel"] == {"slot": "Barrel", "a": "Barrel.A", "b": "Barrel.B", "changed": True}
    assert parts["Sight"]["changed"] is False


def test_compare_live_reads_item_now(item):
    snapshot("a")
    snapshot("b")
    item.update(dmg=30.0)
    out = api.GET_ROUTES["/api/lab/compare"]({"live": "1"})
    assert out["b_live"] is True
    assert out["b"]["stats"][0]["value"] == 30.0
    rows = {row["name"]: row for row in out["rows"]}
    assert rows["InstantHitDamage"]["delta"] == 20.0
    # the stored B is untouched
    assert api.GET_ROUTES["/api/lab"]({})["snapshots"]["b"]["stats"][0]["value"] == 10.0


def test_compare_uses_live_when_b_is_missing(item):
    snapshot("a")
    item.update(pellets=4)
    out = api.GET_ROUTES["/api/lab/compare"]({})
    assert out["b_live"] is True
    rows = {row["name"]: row for row in out["rows"]}
    assert rows["est_shot_damage"]["b"] == 40.0
    assert rows["est_card_dps"]["b"] == 80.0


def test_derived_rows_are_none_without_interval_or_pellets(item):
    item.update(interval=0)
    snapshot("a")
    out = api.GET_ROUTES["/api/lab/compare"]({})
    rows = {row["name"]: row for row in out["rows"]}
    assert rows["est_card_dps"]["a"] is None
    assert rows["est_card_dps"]["pct"] is None


def test_live_flag_rejects_other_values(item):
    snapshot("a")
    with pytest.raises(ApiError) as caught:
        api.GET_ROUTES["/api/lab/compare"]({"live": "yes"})
    assert caught.value.status == 400


def test_snapshot_slot_and_id_validation(item):
    with pytest.raises(ApiError) as caught:
        api.POST_ROUTES["/api/lab/snapshot"]({"id": "Item.Gun#1", "slot": "c"})
    assert caught.value.status == 400
    with pytest.raises(ApiError) as caught:
        api.POST_ROUTES["/api/lab/snapshot"]({"slot": "a"})
    assert caught.value.status == 400


def test_snapshot_clear_one_slot_and_both(item):
    snapshot("a")
    snapshot("b")
    body = api.POST_ROUTES["/api/lab/snapshot/clear"]({"slot": "a"})
    assert body["snapshots"]["a"] is None
    assert body["snapshots"]["b"] is not None
    body = api.POST_ROUTES["/api/lab/snapshot/clear"]({})
    assert body["snapshots"] == {"a": None, "b": None}


def test_compare_snapshots_pure_unchanged_within_tolerance():
    a = {"slot": "a", "id": "x", "name": "n", "taken_at": 0.0,
         "stats": [{"name": "ClipSize", "label": "Magazine size", "value": 10.0, "base": 10.0}], "parts": []}  # fmt: skip
    b = {**a, "slot": "b", "stats": [{"name": "ClipSize", "label": "Magazine size", "value": 10.0 + 1e-9, "base": 10.0}]}
    out = damage.compare_snapshots(a, b, False)
    assert out["rows"][0]["changed"] is False


# --- Debug dump --------------------------------------------------------------------------------------------------------


def test_debug_dump_writes_json_to_the_patched_path(world, tmp_path):
    start()
    notify(world, 55.0)
    out = api.GET_ROUTES["/api/debug/lab"]({})
    assert out == {"saved_to": str(tmp_path / "debug_lab.json")}
    data = json.loads((tmp_path / "debug_lab.json").read_text(encoding="utf-8"))
    assert {"session", "hooks", "raw_summaries", "pc", "pawn", "held_weapon", "summary"} <= set(data)
    assert data["pc"] == "PlayerController.Me"
    assert data["pawn"] == "Pawn.Me"
    assert data["held_weapon"] == "WillowWeapon.Pistol"
    assert data["summary"]["total"] == 55.0
    assert data["raw_summaries"][-1]["FinalDamage"] == 55.0


def test_raw_summaries_are_kept_only_while_active(world):
    notify(world)
    assert list(damage._raw) == []
    start()
    notify(world)
    assert len(damage._raw) == 1


def test_raw_summaries_are_capped(world):
    start()
    for _ in range(damage.RAW_KEEP + 5):
        notify(world)
    assert len(damage._raw) == damage.RAW_KEEP


def test_clock_drives_event_times_and_dps(world, monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    start()  # started at 100.0
    clock["now"] = 101.0
    notify(world, 100.0)  # t = 1.0
    clock["now"] = 103.0
    notify(world, 300.0)  # t = 3.0
    hits = hits_route()["hits"]
    assert [h["t"] for h in hits] == [1.0, 3.0]
    summary = damage.state()["summary"]
    assert summary["duration"] == 2.0
    assert summary["dps"] == 200.0


def test_killing_blow_is_estimated_from_health_before_the_hit():
    from types import SimpleNamespace as NS

    assert damage._killing_blow(NS(PreviousHealth=100.0, FinalDamage=100.0, bWasOneShotKill=False))
    assert not damage._killing_blow(NS(PreviousHealth=100.0, FinalDamage=99.0, bWasOneShotKill=False))
    assert not damage._killing_blow(NS(PreviousHealth=0.0, FinalDamage=50.0, bWasOneShotKill=False))  # shield-only hit
    assert damage._killing_blow(NS(PreviousHealth=900.0, FinalDamage=10.0, bWasOneShotKill=True))


def test_a_target_filter_drops_the_per_shot_ratios(monkeypatch):
    damage._clear_all()
    damage._start(100, None)
    damage._s["shots"].append({"seq": 1, "t": 0.0, "weapon": None, "pellets": 1, "cost": 1.0, "fire_mode": 0})
    hit = {"seq": 2, "t": 0.1, "amount": 10.0, "shield": 0.0, "initial": 10.0, "critical": False, "kill": False,
           "target": "A", "target_id": "A", "source": None, "via": "notify", "estimated": False}
    damage._s["hits"].append({**hit, "attribution": "weapon"})
    damage._s["recorded"] += 1
    assert damage.state()["summary"]["hits_per_shot"] == 1.0
    filtered = damage.state("A")["summary"]
    assert filtered["hits"] == 1 and filtered["hits_per_shot"] is None and filtered["per_pellet"] is None
    damage._clear_all()


def test_a_target_summary_spans_that_targets_hits_only(world, clock):
    start()
    other = Fake("Enemy.Rakk", Class=SimpleNamespace(Name="Rakk"))
    shoot(world)
    notify(world, 100.0)  # t = 0
    clock.tick(4)
    notify(world, 100.0, target=other)
    clock.tick(1)
    notify(world, 100.0)  # t = 5
    shoot(world)
    whole = damage.state()["summary"]
    assert whole["scope"] == "session" and whole["shots"] == 2 and whole["session_wide"] == []
    one = damage.state("Enemy.Bandit")["summary"]
    assert one["scope"] == "target"
    assert one["hits"] == 2 and one["duration"] == 5.0 and one["dps"] == 40.0
    for name in damage.SESSION_WIDE:
        assert one[name] is None
    assert set(one["session_wide"]) == set(damage.SESSION_WIDE)


def test_hits_tail_returns_the_newest_hits(world):
    start()
    for n in range(5):
        shoot(world)  # shots take seq numbers too
        notify(world, float(n))
    assert [h["amount"] for h in hits_route(tail=2)["hits"]] == [3.0, 4.0]
    assert [h["amount"] for h in hits_route(tail="2", after=0)["hits"]] == [3.0, 4.0]
    assert hits_route(tail=2)["last"] == 10
    with pytest.raises(ApiError):
        hits_route(tail=5000)


# --- Call frames (PRE/POST pairing) ------------------------------------------------------------------------------------


def test_a_nested_untracked_call_does_not_steal_the_players_frame(world):
    start()
    stranger = Fake("PlayerController.Other")

    def body():
        take(world, 5.0, instigator=stranger)  # an untracked TakeDamage on the same victim, inside the player's call
        notify(world, 40.0)

    take(world, 100.0, causer=world.gun, inside=body)
    hit = damage._active_hits()[0]
    assert (hit["source"], hit["attribution"]) == ("WillowWeapon.Pistol", "weapon")
    assert damage._calls == []
    assert damage._hooks_state()["unpaired"] == 0


def test_a_frame_whose_post_never_ran_is_dropped_at_the_next_tick(world):
    start()
    args = SimpleNamespace(Damage=1.0, InstigatedBy=world.pc, DamageCauser=world.gun)
    world.pre[damage.FALLBACK](world.enemy, args, None, None)  # PRE only: the POST never comes
    world.hooks[damage.TICK](None, None, None, None)
    assert damage._calls == []
    assert damage._hooks_state()["stale_ticks"] == 1
    world.pawn.Weapon = Fake("WillowWeapon.Other", Class=SimpleNamespace(Name="WillowWeapon"))
    notify(world, 10.0)  # outside any call now: must not look exactly attributed to the stale frame's gun
    hit = damage._active_hits()[0]
    assert (hit["source"], hit["attribution"]) == ("WillowWeapon.Other", "held")


def test_a_post_that_finds_another_victims_frame_clears_the_stack(world):
    start()
    other = Fake("Enemy.Rakk", Class=SimpleNamespace(Name="Rakk"))
    args = SimpleNamespace(Damage=1.0, InstigatedBy=world.pc, DamageCauser=world.gun)
    world.pre[damage.FALLBACK](world.enemy, args, None, None)
    world.hooks[damage.FALLBACK](other, args, None, None)
    assert damage._calls == []
    assert damage._hooks_state()["unpaired"] == 1


def test_session_start_and_stop_clear_the_stack(world):
    start()
    damage._calls.append({"victim": world.enemy, "player": True, "weapon": "X", "how": "weapon", "notified": False})
    api.POST_ROUTES["/api/lab/session/stop"]({})
    assert damage._calls == []
    damage._calls.append({"victim": world.enemy, "player": True, "weapon": "X", "how": "weapon", "notified": False})
    start()
    assert damage._calls == []


def test_the_tick_hook_is_free_with_an_empty_stack(world):
    world.hooks[damage.TICK](Boom(), Boom(), None, None)
    assert damage._hooks_state()["stale_ticks"] == 0


# --- One source per mod lifetime ---------------------------------------------------------------------------------------


def test_once_the_primary_has_fired_the_fallback_is_off_in_later_sessions(world, clock):
    start()
    notify(world, 40.0)
    api.POST_ROUTES["/api/lab/session/stop"]({})
    start()
    take(world, 80.0)
    clock.tick(5)
    assert damage._active_hits() == []
    assert damage.state()["hooks"]["primary_ever"] is True


def test_pending_fallback_hits_the_primary_does_not_report_are_kept(world, clock):
    seen: list[dict[str, Any]] = []
    events.subscribe(events.DAMAGE, seen.append)
    start()
    other = Fake("Enemy.Rakk", Class=SimpleNamespace(Name="Rakk"))
    take(world, 80.0, target=other)  # a hit the primary never reports
    take(world, 100.0)
    clock.tick(0.05)
    notify(world, 40.0)  # reports only the 100 on the bandit
    clock.tick(5)
    hits = damage._active_hits()
    assert sorted((h["via"], h["target_id"]) for h in hits) == [("notify", "Enemy.Bandit"), ("take_damage", "Enemy.Rakk")]
    assert len(seen) == 2


def test_a_pending_hit_with_a_different_amount_is_not_taken_as_the_same_hit(world, clock):
    start()
    take(world, 300.0)
    clock.tick(0.05)
    notify(world, 40.0)  # InitialDamage 100: a different hit on the same target
    clock.tick(5)
    assert sorted(h["via"] for h in damage._active_hits()) == ["notify", "take_damage"]


# --- Round 3: dedupe ordering, idle primary, full queue, tick -----------------------------------------------------------


def test_an_expired_pending_hit_is_committed_not_consumed(world, clock):
    start()
    take(world, 100.0)
    clock.tick(damage.DEDUPE_WINDOW + 0.01)  # expired, but nothing has flushed it yet
    notify(world, 40.0)  # same target and amount: must not take the expired hit
    assert sorted(h["via"] for h in damage._active_hits()) == ["notify", "take_damage"]


def test_a_primary_hit_from_another_weapon_does_not_take_the_pending_hit(world, clock):
    start(weapon="WillowWeapon.Pistol")
    take(world, 100.0, causer=world.gun)  # waiting fallback hit from the pistol
    clock.tick(0.05)
    other = Fake("WillowWeapon.Other", Class=SimpleNamespace(Name="WillowWeapon"))
    take(world, 1.0, causer=other, inside=lambda: notify(world, 40.0))  # weapon B: filtered out, but after dedupe
    clock.tick(5)
    hits = damage._active_hits()
    assert [(h["via"], h["source"]) for h in hits] == [("take_damage", "WillowWeapon.Pistol")]


def test_the_primary_counts_as_working_even_when_it_fires_while_idle(world, clock):
    notify(world, 40.0)  # no session yet
    assert damage._primary_ever is True
    assert damage._active_hits() == [] and damage._fired[damage.PRIMARY] == 0  # the idle path recorded nothing else
    start()
    take(world, 80.0)
    clock.tick(5)
    assert damage._active_hits() == []


def test_a_full_pending_queue_commits_its_oldest_hit_and_counts_it(world, monkeypatch):
    monkeypatch.setattr(damage, "MAX_PENDING", 3)
    start()
    for amount in (1.0, 2.0, 3.0, 4.0):
        take(world, amount)
    session = damage.state()["session"]
    assert session["pending_early"] == 1 and session["pending"] == 3
    assert [h["amount"] for h in damage._active_hits()] == [1.0]


def test_the_tick_hook_never_raises(world, monkeypatch):
    start()
    damage._calls.append({"victim": world.enemy, "player": False, "weapon": None, "how": "held", "notified": False})

    class Broken(list):
        def clear(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(damage, "_calls", Broken(damage._calls))
    world.hooks[damage.TICK](None, None, None, None)
    assert damage._hooks_state()["errors"] == 1
    monkeypatch.setattr(damage, "_calls", [])  # a plain list again before teardown clears it
