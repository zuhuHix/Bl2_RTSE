"""rtse/farmguard.py: the farm guard (snapshots, guard alerts, auto-block, co-op refusal, pickups, the farm tracker,
quick reload), driven through fake game objects shaped like the real ones, plus rtse/world.py's snapshot restore.

Game functions are simulated by call_game(): it runs the registered pre-hook (a returned BLOCK stops the function),
then the function, then the post-hook, in the order the SDK does.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace as NS

import pytest
from unrealsdk.hooks import Type

from rtse import api, events, farmguard, gamethread, world
from rtse.routing import ApiError

M = "GD_Z2_DoctorsOrders.M_DoctorsOrders"
OBJ = f"{M}:CollectNotes"
SET1 = f"{M}:Set1"
SET2 = f"{M}:Set2"
OTHER = "GD_Other.M_Other"
PROFILE = "wep_loot_midgets"
OTHER_PROFILE = "ffs_space_cowboy_potty"
PICKUP_DEF = "GD_Z2_DoctorsOrdersData.Weapon_Pistol"
BLOCK = "BLOCK"  # stands in for unrealsdk.hooks.Block (the stub SDK has none)

SET_STATUS = "WillowGame.MissionTracker:SetMissionStatus"
UPDATE_OBJ = "WillowGame.MissionTracker:UpdateObjective"
COMPLETE = "WillowGame.WillowPlayerController:ServerCompleteMission"
GIVE_TO = "WillowGame.WillowPickup:GiveTo"
START = "WillowGame.FrontendGFxMovie:Start"

STATUS_NAMES = ("MS_NotStarted", "MS_Active", "MS_RequiredObjectivesComplete", "MS_ReadyToTurnIn", "MS_Complete", "MS_Failed")


# ---------- fakes ----------


class Obj:
    """A game object stand-in: its path, plus whatever fields a test gives it."""

    def __init__(self, path, **fields):
        self._p = path
        self.__dict__.update(fields)

    def _path_name(self):
        return self._p


class Entry(Obj):
    """An IMission.MissionData entry. Fields listed in `frozen` silently ignore writes (a game that drops them)."""

    def __setattr__(self, name, value):
        if name in self.__dict__.get("frozen", ()):
            return
        object.__setattr__(self, name, value)


class EMissionStatus:
    def __init__(self, name):
        self.name = name
        self.value = STATUS_NAMES.index(name)

    def __int__(self):
        return self.value

    def __repr__(self):
        return self.name


EMissionStatus.__members__ = {name: EMissionStatus(name) for name in STATUS_NAMES}


def st(name):
    return EMissionStatus.__members__[name]


def _key(obj):
    return None if obj is None else obj._path_name()


def call_game(game, path, obj, args, run):
    """Runs a game function the way the SDK does: pre-hook (BLOCK stops it), the function, then the post-hook."""
    pre = game.hooks.get((path, Type.PRE, farmguard.HOOK_ID))
    if pre is not None and pre(obj, args, None, None) is not None:
        return None
    result = run()
    post = game.hooks.get((path, Type.POST, farmguard.HOOK_ID))
    if post is not None:
        post(obj, args, result, None)
    return result


def hook(game, path, kind):
    return game.hooks[(path, kind, farmguard.HOOK_ID)]


class Tracker:
    """WillowGame.MissionTracker: MissionList of entries; the game functions fire their hooks."""

    def __init__(self, game, entries):
        self.game = game
        self.MissionList = entries
        self.calls = []

    def _entry(self, mission):
        return next((e for e in self.MissionList if _key(e.MissionDef) == _key(mission)), None)

    def _set_status(self, mission, status):
        self._entry(mission).Status = status

    def GetMissionStatus(self, mission):
        entry = self._entry(mission)
        return entry.Status if entry is not None else None

    def UpdateObjective(self, MissionObjective=None, ObjectiveBit=None):
        def run():
            entry = self._entry(MissionObjective.Outer)
            index = [_key(o) for o in entry.MissionDef.ObjectiveDefs].index(_key(MissionObjective))
            entry.ObjectivesProgress[index] += 1
            self.calls.append(("UpdateObjective", _key(MissionObjective)))

        return call_game(self.game, UPDATE_OBJ, self, NS(MissionObjective=MissionObjective, ObjectiveBit=ObjectiveBit), run)

    def SetMissionStatus(self, InMission=None, MissionStatus=None, WillowPC=None):
        def run():
            self._set_status(InMission, MissionStatus)
            self.calls.append(("SetMissionStatus", _key(InMission), MissionStatus.name))

        return call_game(self.game, SET_STATUS, self, NS(InMission=InMission, MissionStatus=MissionStatus, WillowPC=WillowPC), run)

    def OnMissionObjectivesChanged(self, MissionThatChanged=None):
        self.calls.append(("OnMissionObjectivesChanged", _key(MissionThatChanged)))


class PlayerController:
    """pc: WorldInfo.GRI.MissionTracker, MissionPlaythroughs (the saved copy), a character name."""

    def __init__(self, game, tracker, saved_entries, character):
        self.game = game
        self.raw_playthrough = 0
        self.calls = []
        self.WorldInfo = NS(GRI=NS(MissionTracker=tracker, PRIArray=[NS(PlayerName=character)],
                                   GetCurrPlaythrough=lambda: self.raw_playthrough),
                            NetMode=0, TimeSeconds=10.0)
        self.MissionPlaythroughs = [NS(PlayThroughNumber=0, MissionList=saved_entries)]
        self.PlayerReplicationInfo = NS(PlayerName=character)
        # the save identity (world.save_identity) and the quick-reload safety state (farmguard.reload_safety)
        self.SaveGameFileId = 7
        self.SaveGameName = "Save0007"
        self.PlayerClass = Obj("GD_Soldier.Character.CharClass_Soldier")
        self.save = NS(SaveGuid=NS(A=1, B=2, C=3, D=4))
        self.Pawn = NS()
        self.loading = False
        self.bWaitingForSaveGameId = False
        self.save_manager = NS(CurrentState=0)

    def GetCachedSaveGame(self):
        return self.save

    def IsLoadingMoviePlaying(self):
        return self.loading

    def GetWillowGlobals(self):
        return NS(GetWillowSaveGameManager=lambda: self.save_manager)

    def GetCurrentPlaythrough(self):
        return self.raw_playthrough

    def ServerCompleteMission(self, Mission=None, MissionDirector=None):
        def run():
            self.WorldInfo.GRI.MissionTracker._set_status(Mission, st("MS_Complete"))

        return call_game(self.game, COMPLETE, self, NS(Mission=Mission, MissionDirector=MissionDirector), run)

    def ReturnToTitleScreen(self, bSkipSave=None, bRemoveSplitPlayer=None):
        self.calls.append(("ReturnToTitleScreen", bSkipSave, bRemoveSplitPlayer))


class Movie:
    """FrontendGFxMovie: only LaunchSaveGame matters here."""

    def __init__(self):
        self.calls = []

    def LaunchSaveGame(self, PlayThrough=None, PlayersWithoutDlc=None):
        self.calls.append((PlayThrough, list(PlayersWithoutDlc)))


class Empty:
    """Has none of the attributes a hook reads."""


def make_pickup(definition=PICKUP_DEF, cls="WillowWeapon", rarity=None, name="Jakobs Pistol", path="Pickups.Pickup_1"):
    field = "ItemDefinition" if cls == "WillowMissionItem" else "WeaponTypeDefinition"
    inventory = Obj("Inventory.Item_1", Class=NS(Name=cls), DefinitionData=NS(**{field: Obj(definition)}),
                    GetShortHumanReadableName=lambda: name)
    return Obj(path, Inventory=inventory, InventoryRarityLevel=rarity)


def give(game, pickup):
    return call_game(game, GIVE_TO, pickup, NS(P=None, bReady=True, Waypoint=None), lambda: None)


def profile_text(key, profile=PROFILE):
    found = next(p for p in farmguard.load_profiles() if p["id"] == profile)
    return found["consequence"][key]


def enable(profile=PROFILE, **extra):
    return farmguard.set_enabled({"profile": profile, "enabled": True, **extra})


def alerts():
    return farmguard.read()["alerts"]


def only_alert():
    found = alerts()
    assert len(found) == 1, found
    return found[0]


def resolve(alert_id, action):
    return farmguard.resolve({"alert": alert_id, "action": action})


def view(result, profile=PROFILE):
    return next(p for p in result["profiles"] if p["id"] == profile)


# ---------- fixtures ----------


@pytest.fixture(autouse=True)
def _reset_farmguard(monkeypatch):
    """farmguard keeps module-level state; put all of it back between tests."""
    monkeypatch.setattr(farmguard, "BLOCK", None)  # the stub SDK has no Block; tests that need one patch it
    for state in farmguard._state.values():
        state.update({"enabled": False, "auto_block": False, "snapshot": None, "snapshot_at": None})
    farmguard.load_profiles()
    farmguard._alerts.clear()
    farmguard._reset_session()
    farmguard._settings["experimental_reload"] = False
    farmguard._reload.update({"pending": None, "last": None})
    farmguard._pending_pickups.clear()
    farmguard._trace.clear()
    farmguard._hook_errors.clear()
    farmguard._installed.clear()
    farmguard._failed.clear()
    farmguard._last_world_time = None
    farmguard._restoring = False
    events.clear()
    for _ in range(100):
        gamethread.pump()
    yield
    events.clear()
    for _ in range(100):
        gamethread.pump()


@pytest.fixture
def farm(game):
    """A save in progress: Doctor's Orders (tracker + saved copy, both active), an unrelated mission, a host."""
    mdef = Obj(M, MissionName="Doctor's Orders")
    obj = Obj(OBJ, ObjectiveCount=5, Outer=mdef)
    set1 = Obj(SET1, Class=NS(Name="MissionObjectiveSetDefinition"))
    set2 = Obj(SET2, Class=NS(Name="MissionObjectiveSetDefinition"))
    mdef.ObjectiveDefs = [obj]
    mdef.ObjectiveSetDefs = [set1, set2]
    omdef = Obj(OTHER, MissionName="Other")
    oobj = Obj(f"{OTHER}:Foo", ObjectiveCount=1, Outer=omdef)
    omdef.ObjectiveDefs = [oobj]
    omdef.ObjectiveSetDefs = []
    entry = Entry("tracker entry", MissionDef=mdef, Status=st("MS_Active"), ObjectivesProgress=[0], ActiveObjectiveSet=set1, SubObjectiveSets=[])
    saved = Entry("saved entry", MissionDef=mdef, Status=st("MS_Active"), ObjectivesProgress=[0], ActiveObjectiveSet=set1, SubObjectiveSets=[])
    other = Entry("other entry", MissionDef=omdef, Status=st("MS_Active"), ObjectivesProgress=[0], ActiveObjectiveSet=None, SubObjectiveSets=[])
    tracker = Tracker(game, [entry, other])
    pc = PlayerController(game, tracker, [saved], character="Axton")
    game.pc = pc
    farmguard.install_hooks()
    return NS(pc=pc, tracker=tracker, mdef=mdef, obj=obj, set1=set1, set2=set2, entry=entry, saved=saved,
              omdef=omdef, oobj=oobj, other=other, movie=Movie())


# ---------- 1. hooks ----------


def test_install_registers_all_eleven_hooks(game):
    farmguard.install_hooks()
    assert len(game.hooks) == 11
    assert {key[2] for key in game.hooks} == {farmguard.HOOK_ID}
    assert {(p, k) for (p, k, _cb) in farmguard.HOOKS} == {(p, k) for (p, k, _i) in game.hooks}
    state = farmguard.read()["hooks"]
    assert (state["installed"], state["total"], state["failed"]) == (11, 11, [])


def test_installing_twice_still_registers_eleven(game):
    farmguard.install_hooks()
    farmguard.install_hooks()
    assert len(game.hooks) == 11
    assert farmguard.read()["hooks"]["installed"] == 11


def test_remove_hooks_removes_all_and_install_works_again(game):
    farmguard.install_hooks()
    farmguard.remove_hooks()
    assert game.hooks == {}
    assert farmguard.read()["hooks"]["installed"] == 0
    farmguard.install_hooks()
    assert len(game.hooks) == 11


# ---------- 2. enable ----------


def test_enable_snapshots_both_the_tracker_and_the_saved_entry(farm):
    result = enable()
    assert result["applied"]["snapshot"] == "taken"
    assert result["applied"]["after"] is True
    snap = farmguard._state[PROFILE]["snapshot"]
    assert (snap["playthrough"], snap["character"]) == (1, "Axton")
    assert snap["set_classes"] == {SET1: "MissionObjectiveSetDefinition"}
    for source in ("tracker", "saved"):
        entry = snap["entries"][source]
        assert entry["status"] == "active"
        assert entry["progress"] == [0]
        assert entry["active_set"] == SET1
        assert entry["sub_sets"] == []
    shown = view(result)
    assert shown["snapshot"]["status"] == "active" and shown["snapshot"]["character"] == "Axton"


def test_enabling_again_keeps_the_snapshot_unless_resnapshot(farm):
    enable()
    farm.entry.ObjectivesProgress = [3]  # live state moves on while the profile is on
    again = enable()
    assert again["applied"]["snapshot"] == "kept"
    assert farmguard._state[PROFILE]["snapshot"]["entries"]["tracker"]["progress"] == [0]
    retaken = enable(resnapshot=True)
    assert retaken["applied"]["snapshot"] == "taken"
    assert farmguard._state[PROFILE]["snapshot"]["entries"]["tracker"]["progress"] == [3]


def test_disable_clears_the_snapshot_and_the_profiles_alerts(farm):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    assert len(alerts()) == 1
    result = farmguard.set_enabled({"profile": PROFILE, "enabled": False})
    assert farmguard._state[PROFILE]["snapshot"] is None
    assert result["applied"]["after"] is False
    assert result["alerts"] == []


def test_enabling_a_mission_missing_from_the_list_is_404(farm):
    farm.tracker.MissionList = []
    farm.pc.MissionPlaythroughs = []
    with pytest.raises(farmguard.FarmError) as err:
        enable()
    assert err.value.status == 404
    assert farmguard._state[PROFILE]["enabled"] is False


def test_enabling_a_completed_mission_adds_a_warning(farm):
    farm.entry.Status = st("MS_Complete")
    farm.saved.Status = st("MS_Complete")
    result = enable()
    assert "already complete" in result["applied"]["warning"]


# ---------- 3. guard detection ----------


def test_objective_change_raises_one_alert_with_the_consequence(farm):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    alert = only_alert()
    assert alert["change"] == "objective"
    assert alert["message"] == profile_text("objective")
    assert alert["objective"] == OBJ
    assert alert["objective_name"] == "Collect Notes"
    assert alert["before"]["progress"] == [0]
    assert alert["after"]["progress"] == [1]
    assert alert["blocked"] is False
    assert alert["actions"] == ["keep", "undo", "undo_reload"]
    assert alert["count"] == 1


def test_a_second_change_folds_into_the_same_alert(farm):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    alert = only_alert()
    assert alert["count"] == 2
    assert alert["before"]["progress"] == [0]
    assert alert["after"]["progress"] == [2]


def test_unrelated_mission_publishes_but_raises_no_alert(farm):
    got = []
    events.subscribe(events.MISSION_CHANGED, got.append)
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.oobj, ObjectiveBit=1)
    assert alerts() == []
    assert got == [{"mission": OTHER, "status": "active", "objective": f"{OTHER}:Foo", "change": "objective",
                    "name": "Other", "guarded": False}]


def test_unchanged_live_state_raises_no_alert_but_publishes_the_event(farm, game):
    got = []
    events.subscribe(events.MISSION_CHANGED, got.append)
    enable()
    hook(game, UPDATE_OBJ, Type.POST)(farm.tracker, NS(MissionObjective=farm.obj, ObjectiveBit=0), None, None)
    assert alerts() == []
    assert len(got) == 1
    assert set(got[0]) == {"mission", "status", "objective", "change", "name", "guarded"}
    assert got[0] == {"mission": M, "status": "active", "objective": OBJ, "change": "objective",
                      "name": "Doctors Orders", "guarded": True}


# ---------- 4. resolve ----------


def test_undo_puts_back_both_entries_and_closes_the_alerts(farm):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    farm.saved.ObjectivesProgress = [1]  # the save copy follows the game
    farm.tracker.SetMissionStatus(InMission=farm.mdef, MissionStatus=st("MS_RequiredObjectivesComplete"), WillowPC=farm.pc)
    farm.saved.Status = st("MS_RequiredObjectivesComplete")
    for entry in (farm.entry, farm.saved):
        entry.ActiveObjectiveSet = farm.set2
        entry.SubObjectiveSets = [farm.set2]
    alert = only_alert()
    assert alert["change"] == "status" and alert["count"] == 2

    result = resolve(alert["id"], "undo")
    restore = result["applied"]["restore"]
    assert restore["matches"] is True
    assert restore["mismatch"] == []
    for entry in (farm.entry, farm.saved):
        assert entry.ObjectivesProgress == [0]
        assert entry.Status is st("MS_Active")
        assert entry.ActiveObjectiveSet is farm.set1
        assert entry.SubObjectiveSets == []
    assert ("SetMissionStatus", M, "MS_Active") in farm.tracker.calls
    assert result["alerts"] == []


def test_undo_runs_the_game_and_publishes_nothing_while_the_hooks_are_suppressed(farm, monkeypatch):
    enable()
    farm.tracker.SetMissionStatus(InMission=farm.mdef, MissionStatus=st("MS_ReadyToTurnIn"), WillowPC=farm.pc)
    alert = only_alert()
    monkeypatch.setattr(farmguard, "BLOCK", BLOCK)
    farmguard.set_settings({"profile": PROFILE, "auto_block": True})  # would block the restore's own status write
    got = []
    events.subscribe(events.MISSION_CHANGED, got.append)

    result = resolve(alert["id"], "undo")

    assert ("SetMissionStatus", M, "MS_Active") in farm.tracker.calls  # the pre-hook did not block it
    assert farm.entry.Status is st("MS_Active")
    assert got == []  # the post-hook published nothing
    assert result["alerts"] == []
    assert farmguard._restoring is False


def test_keep_retakes_the_snapshot_from_the_new_state(farm):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    alert = only_alert()
    result = resolve(alert["id"], "keep")
    assert result["alerts"] == []
    assert result["applied"]["after"]["progress"] == [1]
    assert farmguard._state[PROFILE]["snapshot"]["entries"]["tracker"]["progress"] == [1]
    assert farm.entry.ObjectivesProgress == [1]


def test_dismiss_is_only_for_blocked_alerts(farm, monkeypatch):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    with pytest.raises(farmguard.FarmError) as err:
        resolve(only_alert()["id"], "dismiss")
    assert err.value.status == 400

    monkeypatch.setattr(farmguard, "BLOCK", BLOCK)
    farmguard.set_settings({"profile": PROFILE, "auto_block": True})
    farmguard._alerts.clear()  # the open (unblocked) alert is set aside; the next change gets blocked
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)  # now blocked: progress stays put
    blocked = only_alert()
    assert blocked["blocked"] is True and blocked["actions"] == ["dismiss"]
    assert farm.entry.ObjectivesProgress == [1]
    result = resolve(blocked["id"], "dismiss")
    assert result["alerts"] == []
    assert farm.entry.ObjectivesProgress == [1]  # nothing to undo: the change never happened


def test_an_action_the_alert_does_not_offer_is_400(farm):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    with pytest.raises(farmguard.FarmError) as err:
        resolve(only_alert()["id"], "dismiss")
    assert err.value.status == 400


def test_bad_action_is_400_and_unknown_alert_is_404(farm):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    alert = only_alert()
    with pytest.raises(farmguard.FarmError) as bad:
        resolve(alert["id"], "explode")
    assert bad.value.status == 400
    with pytest.raises(farmguard.FarmError) as gone:
        resolve(999, "keep")
    assert gone.value.status == 404


def test_undo_reload_is_refused_while_experimental_reload_is_off(farm):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    alert = only_alert()
    with pytest.raises(farmguard.FarmError) as err:
        resolve(alert["id"], "undo_reload")
    assert err.value.status == 409
    assert farm.entry.ObjectivesProgress == [1]  # refused before anything was restored


def test_undo_reload_restores_then_saves_quits_and_relaunches_the_same_playthrough(farm, game):
    farmguard.set_settings({"experimental_reload": True})
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    result = resolve(only_alert()["id"], "undo_reload")
    assert result["applied"]["reload"] == "queued"
    assert result["applied"]["restore"]["matches"] is True
    assert farm.entry.ObjectivesProgress == [0]
    assert farm.pc.calls == []  # the save-quit waits for the game thread

    gamethread.pump()
    assert farm.pc.calls == [("ReturnToTitleScreen", False, False)]
    assert farmguard.read()["reload"]["pending"] is True

    hook(game, START, Type.POST)(farm.movie, NS(), None, None)  # the main menu started
    assert farm.movie.calls == [(0, [])]
    assert farmguard._reload["pending"] is None
    assert farmguard._reload["last"] == "relaunched playthrough 1"


def test_a_stale_pending_reload_is_dropped(farm, game):
    farmguard._reload["pending"] = {"pt_raw": 0, "at": time.time() - farmguard.RELOAD_TIMEOUT - 1}
    hook(game, START, Type.POST)(farm.movie, NS(), None, None)
    assert farm.movie.calls == []
    assert farmguard._reload["pending"] is None
    assert "timed out" in farmguard._reload["last"]


# ---------- 5. world.restore_mission_snapshot ----------


def test_restore_refuses_a_snapshot_from_another_playthrough(farm):
    snap = world.mission_snapshot(M)
    snap["playthrough"] = 2
    with pytest.raises(world.WorldError) as err:
        world.restore_mission_snapshot(snap)
    assert err.value.status == 409


def test_restore_refuses_a_snapshot_from_another_character(farm):
    snap = world.mission_snapshot(M)
    farm.pc.PlayerReplicationInfo.PlayerName = "Salvador"
    with pytest.raises(world.WorldError) as err:
        world.restore_mission_snapshot(snap)
    assert err.value.status == 409


def test_restore_refuses_another_save_whose_character_has_the_same_name(farm):
    snap = world.mission_snapshot(M)
    assert snap["identity"] == {"save_id": 7, "save_name": "Save0007", "player_class": "GD_Soldier.Character.CharClass_Soldier",
                                "character": "Axton", "save_guid": "00000001-00000002-00000003-00000004"}
    for field, value in (("SaveGameFileId", 8), ("PlayerClass", Obj("GD_Mercenary.Character.CharClass_Mercenary"))):
        original = getattr(farm.pc, field)
        setattr(farm.pc, field, value)  # same name "Axton", different save
        with pytest.raises(world.WorldError) as err:
            world.restore_mission_snapshot(snap)
        assert err.value.status == 409 and "not this save" in str(err.value)
        setattr(farm.pc, field, original)
    farm.pc.save = NS(SaveGuid=NS(A=9, B=9, C=9, D=9))  # a different save GUID alone is enough to refuse
    with pytest.raises(world.WorldError):
        world.restore_mission_snapshot(snap)


def test_no_snapshot_or_restore_when_the_save_cannot_be_identified(farm):
    snap = world.mission_snapshot(M)
    farm.pc.SaveGameFileId = None
    for attempt in (lambda: world.mission_snapshot(M), lambda: world.restore_mission_snapshot(snap)):
        with pytest.raises(world.WorldError) as err:
            attempt()
        assert err.value.status == 409 and "can't be identified" in str(err.value)
    with pytest.raises(farmguard.FarmError) as err:
        enable()
    assert err.value.status == 409


def _no_playthrough(farm):
    def unreadable():
        raise RuntimeError("no playthrough")

    farm.pc.GetCurrentPlaythrough = unreadable
    farm.pc.WorldInfo.GRI.GetCurrPlaythrough = unreadable


def test_no_snapshot_or_restore_when_the_playthrough_is_unknown(farm):
    snap = world.mission_snapshot(M)
    farm.entry.ObjectivesProgress = [1]
    _no_playthrough(farm)
    assert set(world._snapshot_entries(world._owners(), M)) == {"tracker"}  # never guesses the saved playthrough
    for attempt in (lambda: world.mission_snapshot(M), lambda: world.restore_mission_snapshot(snap)):
        with pytest.raises(world.WorldError) as err:
            attempt()
        assert err.value.status == 409 and "playthrough" in str(err.value)
    assert farm.entry.ObjectivesProgress == [1]  # nothing was written
    with pytest.raises(farmguard.FarmError):
        enable()


def test_a_write_the_game_silently_ignores_fails_the_restore_and_rolls_back(farm):
    snap = world.mission_snapshot(M)  # progress [0], set1
    farm.entry.ObjectivesProgress = [1]
    farm.entry.ActiveObjectiveSet = farm.set2
    farm.entry.frozen = ("ObjectivesProgress",)  # writes to this field vanish without an error
    with pytest.raises(world.WorldError) as err:
        world.restore_mission_snapshot(snap)
    assert err.value.status == 500 and "tracker.progress" in str(err.value) and "put back exactly" in str(err.value)
    assert farm.entry.ActiveObjectiveSet is farm.set2  # the field that DID take was rolled back to the pre-restore value
    assert farm.saved.ObjectivesProgress == [0] and farm.saved.ActiveObjectiveSet is farm.set1


class RefusingEntry(Entry):
    """An entry whose ActiveObjectiveSet write raises once armed (a game that refuses the write)."""

    def __setattr__(self, name, value):
        if name == "ActiveObjectiveSet" and self.__dict__.get("armed"):
            raise RuntimeError("refused")
        super().__setattr__(name, value)


def test_a_write_failing_in_the_middle_rolls_back_every_entry_and_keeps_the_alert(farm):
    saved = RefusingEntry("saved entry", **{k: v for k, v in vars(farm.saved).items() if not k.startswith("_")})
    farm.pc.MissionPlaythroughs[0].MissionList[0] = saved
    enable()  # snapshot: progress [0]
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    saved.ObjectivesProgress = [1]
    saved.armed = True  # the tracker entry is written first and succeeds, then the saved copy fails
    alert = only_alert()

    with pytest.raises(farmguard.FarmError) as err:
        resolve(alert["id"], "undo")
    assert err.value.status == 500 and "refused" in str(err.value)
    assert farm.entry.ObjectivesProgress == [1]  # the tracker write was rolled back: nothing half restored
    assert saved.ObjectivesProgress == [1]
    still = only_alert()  # the alert stays open with the reason
    assert still["id"] == alert["id"] and "undo failed" in still["failed"]
    assert farmguard._restoring is False


def test_a_set_that_cannot_be_found_stops_the_restore_before_any_write(farm):
    snap = world.mission_snapshot(M)
    snap["entries"]["saved"]["active_set"] = f"{M}:Gone"  # only the second entry names a missing set
    farm.entry.ObjectivesProgress = [1]
    with pytest.raises(world.WorldError) as err:
        world.restore_mission_snapshot(snap)
    assert err.value.status == 409 and "Gone" in str(err.value)
    assert farm.entry.ObjectivesProgress == [1]  # preflight: the tracker entry was not touched


# ---------- 6. auto-block ----------


def test_auto_block_stops_guarded_changes_and_adds_a_blocked_alert(farm, game, monkeypatch):
    monkeypatch.setattr(farmguard, "BLOCK", BLOCK)
    enable()
    farmguard.set_settings({"profile": PROFILE, "auto_block": True})

    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    assert farm.entry.ObjectivesProgress == [0]
    alert = only_alert()
    assert alert["blocked"] is True and alert["actions"] == ["dismiss"] and alert["change"] == "objective"

    farm.tracker.SetMissionStatus(InMission=farm.mdef, MissionStatus=st("MS_ReadyToTurnIn"), WillowPC=farm.pc)
    assert farm.entry.Status is st("MS_Active")
    assert only_alert()["count"] == 2

    farm.pc.ServerCompleteMission(Mission=farm.mdef, MissionDirector=None)
    assert farm.entry.Status is st("MS_Active")
    blocked = only_alert()
    assert blocked["count"] == 3 and blocked["change"] == "complete" and blocked["blocked"] is True


def test_the_pre_hooks_return_block_for_the_guarded_mission(farm, game, monkeypatch):
    monkeypatch.setattr(farmguard, "BLOCK", BLOCK)
    enable()
    farmguard.set_settings({"profile": PROFILE, "auto_block": True})
    assert hook(game, UPDATE_OBJ, Type.PRE)(farm.tracker, NS(MissionObjective=farm.obj, ObjectiveBit=1), None, None) == BLOCK
    assert hook(game, SET_STATUS, Type.PRE)(farm.tracker, NS(InMission=farm.mdef, MissionStatus=st("MS_Complete"), WillowPC=farm.pc), None, None) == BLOCK
    assert hook(game, COMPLETE, Type.PRE)(farm.pc, NS(Mission=farm.mdef, MissionDirector=None), None, None) == BLOCK


def test_without_auto_block_the_pre_hooks_return_none_and_changes_go_through(farm, game, monkeypatch):
    monkeypatch.setattr(farmguard, "BLOCK", BLOCK)
    enable()
    checks = (
        (UPDATE_OBJ, farm.tracker, NS(MissionObjective=farm.obj, ObjectiveBit=1)),
        (SET_STATUS, farm.tracker, NS(InMission=farm.mdef, MissionStatus=st("MS_Complete"), WillowPC=farm.pc)),
        (COMPLETE, farm.pc, NS(Mission=farm.mdef, MissionDirector=None)),
    )
    for path, obj, args in checks:
        assert hook(game, path, Type.PRE)(obj, args, None, None) is None
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    assert farm.entry.ObjectivesProgress == [1]
    assert only_alert()["blocked"] is False


def test_auto_block_is_refused_when_this_sdk_cannot_block(farm):
    with pytest.raises(farmguard.FarmError) as err:
        farmguard.set_settings({"profile": PROFILE, "auto_block": True})
    assert err.value.status == 501
    assert farmguard._state[PROFILE]["auto_block"] is False


def test_pickup_is_blocked_only_with_auto_block_and_a_matching_definition(farm, game, monkeypatch):
    monkeypatch.setattr(farmguard, "BLOCK", BLOCK)
    enable()
    matching = make_pickup(definition=PICKUP_DEF, path="Pickups.Match")
    other = make_pickup(definition="GD_Weap.Other", path="Pickups.Other")
    pre = hook(game, GIVE_TO, Type.PRE)
    args = NS(P=None, bReady=True, Waypoint=None)
    assert pre(matching, args, None, None) is None  # auto-block is off

    farmguard.set_settings({"profile": PROFILE, "auto_block": True})
    assert pre(matching, args, None, None) == BLOCK
    alert = only_alert()
    assert alert["blocked"] is True and alert["change"] == "pickup" and alert["actions"] == ["dismiss"]
    assert alert["objective"] is None and alert["objective_name"] is None and alert["item"]  # the item, not an objective
    assert pre(other, args, None, None) is None  # a pickup the profile doesn't name is never blocked


# ---------- 7. co-op client ----------


def test_a_co_op_client_is_refused_every_farm_write_with_423(farm):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    alert = only_alert()
    farm.pc.WorldInfo.NetMode = 3
    attempts = (
        lambda: enable(),
        lambda: farmguard.retake_snapshot({"profile": PROFILE}),
        lambda: resolve(alert["id"], "keep"),
        lambda: resolve(alert["id"], "undo"),
    )
    for attempt in attempts:
        with pytest.raises(farmguard.FarmError) as err:
            attempt()
        assert err.value.status == 423
        assert "client" in str(err.value)
    assert len(alerts()) == 1  # the refused resolves left the alert alone
    assert farm.entry.ObjectivesProgress == [1]


def test_a_co_op_client_sees_writes_disallowed_in_the_state(farm):
    farm.pc.WorldInfo.NetMode = 3
    state = farmguard.read()
    assert state["coop"]["client"] is True
    assert state["writes"]["allowed"] is False
    assert state["writes"]["reason"]


def test_pre_hooks_never_block_for_a_co_op_client(farm, game, monkeypatch):
    monkeypatch.setattr(farmguard, "BLOCK", BLOCK)
    enable()
    farmguard.set_settings({"profile": PROFILE, "auto_block": True})
    farm.pc.WorldInfo.NetMode = 3
    assert hook(game, UPDATE_OBJ, Type.PRE)(farm.tracker, NS(MissionObjective=farm.obj, ObjectiveBit=1), None, None) is None
    assert hook(game, SET_STATUS, Type.PRE)(farm.tracker, NS(InMission=farm.mdef, MissionStatus=st("MS_Complete"), WillowPC=farm.pc), None, None) is None
    assert hook(game, COMPLETE, Type.PRE)(farm.pc, NS(Mission=farm.mdef, MissionDirector=None), None, None) is None
    assert hook(game, GIVE_TO, Type.PRE)(make_pickup(), NS(P=None, bReady=True, Waypoint=None), None, None) is None
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    assert farm.entry.ObjectivesProgress == [1]
    assert alerts() == []  # and the post-hook never guards anything on a client


# ---------- 8. pickups ----------


def test_a_pickup_publishes_the_pickup_event(farm, game):
    got = []
    events.subscribe(events.PICKUP, got.append)
    give(game, make_pickup(rarity=4))
    assert got == [{"item": "Jakobs Pistol", "kind": "weapon", "amount": None, "definition": PICKUP_DEF, "rarity": 4}]


def test_a_running_session_records_drops_at_or_above_min_rarity(farm, game):
    farmguard.session({"action": "start", "profile": PROFILE})
    give(game, make_pickup(rarity=2, path="Pickups.Low"))
    give(game, make_pickup(rarity=3, path="Pickups.High"))
    shown = farmguard.read()["session"]
    assert shown["pickups"] == 2
    assert shown["drop_count"] == 1
    assert shown["drops"][0]["rarity"] == 3 and shown["drops"][0]["kind"] == "weapon"
    assert shown["drops"][0]["attempt"] == 0


def test_mission_items_are_never_drops(farm, game):
    got = []
    events.subscribe(events.PICKUP, got.append)
    farmguard.session({"action": "start", "profile": PROFILE})
    give(game, make_pickup(definition="GD_Z2_DoctorsOrdersData.Notes", cls="WillowMissionItem", rarity=5, name="Recorder"))
    assert got[0]["kind"] == "mission_item"
    shown = farmguard.read()["session"]
    assert shown["drop_count"] == 0
    assert shown["pickups"] == 1


def test_nothing_is_recorded_while_the_session_is_stopped(farm, game):
    give(game, make_pickup(rarity=5))
    shown = farmguard.read()["session"]
    assert shown["pickups"] == 0 and shown["drop_count"] == 0


# ---------- 9. session ----------


def test_elapsed_time_accumulates_across_stop_and_start(farm, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(farmguard, "time", NS(time=lambda: clock[0]))
    farmguard.session({"action": "start", "profile": PROFILE})
    clock[0] = 1010.0
    assert farmguard.session({"action": "stop"})["applied"]["after"]["elapsed"] == 10.0
    clock[0] = 1500.0  # time passes while stopped: not counted
    assert farmguard.read()["session"]["elapsed"] == 10.0
    farmguard.session({"action": "start"})
    clock[0] = 1505.0
    assert farmguard.read()["session"]["elapsed"] == 15.0
    farmguard.session({"action": "stop"})
    assert farmguard.read()["session"]["running"] is False


def test_reset_clears_the_counters_and_the_timer(farm):
    farmguard.session({"action": "start", "profile": PROFILE})
    for _ in range(3):
        farmguard.session({"action": "attempt"})
    farmguard.session({"action": "reset"})
    shown = farmguard.read()["session"]
    assert (shown["running"], shown["attempts"], shown["pickups"], shown["drop_count"], shown["elapsed"]) == (False, 0, 0, 0, 0.0)
    assert shown["profile"] == PROFILE


def test_attempt_action_counts_up(farm):
    farmguard.session({"action": "start", "profile": PROFILE})
    farmguard.session({"action": "attempt"})
    farmguard.session({"action": "attempt"})
    assert farmguard.read()["session"]["attempts"] == 2


def test_starting_another_profile_resets_the_counters_but_the_same_one_does_not(farm):
    farmguard.session({"action": "start", "profile": PROFILE})
    for _ in range(3):
        farmguard.session({"action": "attempt"})
    farmguard.session({"action": "start", "profile": PROFILE})
    assert farmguard.read()["session"]["attempts"] == 3
    farmguard.session({"action": "start", "profile": OTHER_PROFILE})
    shown = farmguard.read()["session"]
    assert (shown["attempts"], shown["profile"], shown["running"]) == (0, OTHER_PROFILE, True)


def test_attempts_count_up_when_the_world_clock_goes_backwards_while_running(farm, game):
    farmguard.session({"action": "start", "profile": PROFILE})
    game.pc.WorldInfo.TimeSeconds = 500.0
    farmguard.read()
    game.pc.WorldInfo.TimeSeconds = 12.0  # a fresh map
    farmguard.read()
    assert farmguard.read()["session"]["attempts"] == 1  # reading again doesn't count it twice


def test_no_automatic_attempt_while_the_session_is_stopped(farm, game):
    game.pc.WorldInfo.TimeSeconds = 500.0
    farmguard.read()
    game.pc.WorldInfo.TimeSeconds = 12.0
    farmguard.read()
    assert farmguard.read()["session"]["attempts"] == 0


# ---------- 10. hooks never raise ----------


def test_every_hook_returns_none_and_never_raises_on_bad_arguments(farm, game, monkeypatch):
    monkeypatch.setattr(farmguard, "BLOCK", BLOCK)
    enable()
    farmguard.set_settings({"profile": PROFILE, "auto_block": True})
    callbacks = [callback for (_path, _kind, _id), callback in game.hooks.items()]
    assert len(callbacks) == 11
    for callback in callbacks:
        for obj in (None, Empty()):
            assert callback(obj, Empty(), None, None) is None


def test_hook_errors_are_logged_three_times_then_every_hundredth(game):
    farmguard.install_hooks()
    callback = hook(game, SET_STATUS, Type.PRE)
    for _ in range(250):
        assert callback(None, Empty(), None, None) is None
    logged = [message for level, message in game.log if level == "error" and "SetMissionStatus pre" in message]
    assert len(logged) == 5  # calls 1, 2, 3, 100 and 200


# ---------- 11. routes through the api registry ----------


def test_farm_routes_go_through_the_api_and_map_errors_to_api_errors(farm):
    result = api.POST_ROUTES["/api/farm/enable"]({"profile": PROFILE, "enabled": True})
    assert result["applied"]["snapshot"] == "taken"
    with pytest.raises(ApiError) as missing:
        api.POST_ROUTES["/api/farm/enable"]({"profile": "no_such_profile", "enabled": True})
    assert missing.value.status == 404
    with pytest.raises(ApiError) as gone:
        api.POST_ROUTES["/api/farm/resolve"]({"alert": 999, "action": "keep"})
    assert gone.value.status == 404


def test_a_route_write_outside_a_save_is_409(game):
    with pytest.raises(ApiError) as err:
        api.POST_ROUTES["/api/farm/enable"]({"profile": PROFILE, "enabled": True})
    assert err.value.status == 409


def test_get_farm_works_outside_a_save(game):
    state = api.GET_ROUTES["/api/farm"]({})
    assert state["in_game"] is False
    assert [p["id"] for p in state["profiles"]] == [PROFILE, OTHER_PROFILE]
    assert all(p["live"] is None for p in state["profiles"])
    assert state["writes"]["allowed"] is False


# ---------- 12. farm_profiles.json ----------


def test_every_profile_has_the_required_fields():
    profiles = json.loads(farmguard.PROFILES_FILE.read_text(encoding="utf-8"))["profiles"]
    assert profiles
    for profile in profiles:
        for key in ("id", "name", "mission", "guard", "consequence"):
            assert profile.get(key), (profile.get("id"), key)
    assert len(farmguard.load_profiles()) == len(profiles)


def test_every_guarded_objective_starts_with_its_missions_path():
    for profile in farmguard.load_profiles():
        for objective in profile["guard"].get("objectives") or ():
            assert objective.startswith(profile["mission"] + ":"), (profile["id"], objective)


# ---------- review round 1: no undo after a turn-in, safe quick reload, net mode fails closed ----------


def test_after_a_turn_in_the_alert_offers_keep_only(farm):
    enable()
    farm.pc.ServerCompleteMission(Mission=farm.mdef)
    alert = only_alert()
    assert alert["actions"] == ["keep"] and alert["note"] == farmguard.NO_UNDO_AFTER_TURN_IN
    with pytest.raises(farmguard.FarmError) as err:
        resolve(alert["id"], "undo")
    assert err.value.status == 400
    assert farm.entry.Status is st("MS_Complete")  # nothing was "undone" while the rewards stay


def test_undo_is_refused_when_the_mission_was_turned_in_after_the_alert(farm):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    alert = only_alert()
    assert "undo" in alert["actions"]
    farm.entry.Status = st("MS_Complete")  # turned in without a hook firing
    with pytest.raises(farmguard.FarmError) as err:
        resolve(alert["id"], "undo")
    assert err.value.status == 409 and "Rewards" in str(err.value)
    assert farm.entry.ObjectivesProgress == [1]
    assert only_alert()["actions"] == ["keep"]


@pytest.mark.parametrize("unsafe", ["loading", "no_pawn", "saving", "waiting_for_save", "unreadable_state"])
def test_undo_reload_is_refused_before_anything_changes_when_not_safe(farm, unsafe):
    farmguard.set_settings({"experimental_reload": True})
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    alert = only_alert()
    if unsafe == "loading":
        farm.pc.loading = True
    elif unsafe == "no_pawn":
        farm.pc.Pawn = None
    elif unsafe == "saving":
        farm.pc.save_manager.CurrentState = 2
    elif unsafe == "waiting_for_save":
        farm.pc.bWaitingForSaveGameId = True
    else:
        farm.pc.save_manager = Empty()
    with pytest.raises(farmguard.FarmError) as err:
        resolve(alert["id"], "undo_reload")
    assert err.value.status == 409 and "isn't safe" in str(err.value)
    assert farm.entry.ObjectivesProgress == [1]  # nothing restored
    for _ in range(3):
        gamethread.pump()
    assert farm.pc.calls == []


def test_quick_reload_is_skipped_when_it_became_unsafe_before_the_tick(farm):
    farmguard.set_settings({"experimental_reload": True})
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    resolve(only_alert()["id"], "undo_reload")
    assert farm.entry.ObjectivesProgress == [0]  # the undo itself happened
    farm.pc.loading = True
    for _ in range(3):
        gamethread.pump()
    assert farm.pc.calls == []  # no ReturnToTitleScreen
    state = farmguard.read()["reload"]
    assert state["pending"] is False and "skipped" in state["last"] and "loading screen" in state["last"]


@pytest.mark.parametrize(("mode", "allowed"), [(0, True), (2, True), (1, False), (3, False), ("garbage", False), (None, False)])
def test_farm_writes_need_a_confirmed_solo_or_host_game(farm, mode, allowed):
    farm.pc.WorldInfo.NetMode = mode
    state = farmguard.read()
    assert state["writes"]["allowed"] is allowed and state["coop"]["host"] is allowed
    if allowed:
        enable()
        return
    assert state["writes"]["reason"]
    with pytest.raises(farmguard.FarmError) as err:
        enable()
    assert err.value.status == 423 and err.value.args[0] == state["writes"]["reason"]


def test_an_unreadable_net_mode_never_blocks(farm, monkeypatch):
    monkeypatch.setattr(farmguard, "BLOCK", BLOCK)
    enable()
    farmguard.set_settings({"profile": PROFILE, "auto_block": True})
    del farm.pc.WorldInfo.NetMode  # reading it now raises
    assert "can't tell" in farmguard.read()["writes"]["reason"]
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    assert farm.entry.ObjectivesProgress == [1]  # not blocked: auto-block needs a confirmed host


# ---------- review round 2: missing records, null fields, field-by-field rollback, undo reads every record ----------


def test_a_record_the_snapshot_holds_but_the_game_lost_refuses_before_any_write(farm):
    enable()  # snapshot holds tracker + saved
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    alert = only_alert()
    farm.pc.MissionPlaythroughs[0].MissionList.clear()  # the saved copy is gone
    with pytest.raises(farmguard.FarmError) as err:
        resolve(alert["id"], "undo")
    assert err.value.status == 409 and "saved record" in str(err.value) and "partial" in str(err.value)
    assert farm.entry.ObjectivesProgress == [1]  # the tracker copy was not touched
    assert only_alert()["id"] == alert["id"]  # still open


def test_a_record_the_snapshot_did_not_capture_refuses_too(farm):
    saved = farm.pc.MissionPlaythroughs[0].MissionList.pop()
    snap = world.mission_snapshot(M)  # tracker only
    assert snap["entries"]["saved"] is None
    farm.pc.MissionPlaythroughs[0].MissionList.append(saved)
    farm.entry.ObjectivesProgress = [1]
    with pytest.raises(world.WorldError) as err:
        world.restore_mission_snapshot(snap)
    assert err.value.status == 409 and "didn't capture" in str(err.value)
    assert farm.entry.ObjectivesProgress == [1]


def test_a_null_active_set_is_verified_like_any_other_value(farm):
    farm.entry.ActiveObjectiveSet = None
    farm.saved.ActiveObjectiveSet = None
    snap = world.mission_snapshot(M)
    assert snap["entries"]["tracker"]["active_set"] is None and snap["entries"]["tracker"]["unreadable"] == []
    farm.entry.ActiveObjectiveSet = farm.set2
    farm.entry.frozen = ("ActiveObjectiveSet",)  # the game ignores the clear
    with pytest.raises(world.WorldError) as err:
        world.restore_mission_snapshot(snap)
    assert err.value.status == 500 and "tracker.active_set" in str(err.value)
    del farm.entry.__dict__["frozen"]
    world.restore_mission_snapshot(snap)  # and a clear that takes succeeds
    assert farm.entry.ActiveObjectiveSet is None


class UnreadableSubSets(Entry):
    @property
    def SubObjectiveSets(self):  # noqa: N802 - the game's field name
        raise RuntimeError("unreadable")


def test_a_snapshot_with_an_unreadable_field_is_refused(farm):
    fields = {k: v for k, v in vars(farm.entry).items() if not k.startswith("_") and k != "SubObjectiveSets"}
    farm.tracker.MissionList[0] = UnreadableSubSets("tracker entry", **fields)
    with pytest.raises(world.WorldError) as err:
        world.mission_snapshot(M)
    assert err.value.status == 409 and "tracker.sub_sets" in str(err.value)
    with pytest.raises(farmguard.FarmError):
        enable()


def test_an_unknown_status_value_is_unreadable_and_refused(farm):
    farm.saved.Status = object()  # not an EMissionStatus
    with pytest.raises(world.WorldError) as err:
        world.mission_snapshot(M)
    assert "saved.status" in str(err.value)


def test_restore_refuses_when_the_current_state_cannot_be_read(farm):
    snap = world.mission_snapshot(M)
    farm.entry.ObjectivesProgress = [1]
    farm.saved.Status = object()  # a failed undo couldn't put this back
    with pytest.raises(world.WorldError) as err:
        world.restore_mission_snapshot(snap)
    assert err.value.status == 409 and "saved.status" in str(err.value) and "nothing was changed" in str(err.value)
    assert farm.entry.ObjectivesProgress == [1]


class FlakyEntry(Entry):
    """Refuses ObjectivesProgress writes once `refuse_progress` is set (kept as a tuple, so the element fallback fails too)."""

    def __setattr__(self, name, value):
        if name == "ObjectivesProgress":
            if self.__dict__.get("refuse_progress"):
                raise RuntimeError("refused")
            value = tuple(value)
        super().__setattr__(name, value)


class TrippingEntry(Entry):
    """Raises on its ActiveObjectiveSet write once armed, and then makes `partner` refuse progress writes."""

    def __setattr__(self, name, value):
        if name == "ActiveObjectiveSet" and self.__dict__.get("armed"):
            self.__dict__["partner"].__dict__["refuse_progress"] = True
            raise RuntimeError("refused")
        super().__setattr__(name, value)


def test_rollback_continues_through_every_field_and_reports_all_that_stay_off(farm):
    fields = {k: v for k, v in vars(farm.entry).items() if not k.startswith("_")}
    tracker = FlakyEntry("tracker entry", **fields)
    farm.tracker.MissionList[0] = tracker
    saved = TrippingEntry("saved entry", **{k: v for k, v in vars(farm.saved).items() if not k.startswith("_")})
    farm.pc.MissionPlaythroughs[0].MissionList[0] = saved
    snap = world.mission_snapshot(M)  # progress [0], set1, active
    tracker.ObjectivesProgress = [1]
    tracker.ActiveObjectiveSet = farm.set2
    tracker.Status = st("MS_ReadyToTurnIn")
    saved.ObjectivesProgress = [1]
    saved.partner = tracker
    saved.armed = True  # forward: tracker fully restored, then saved raises; rollback: tracker refuses its progress

    with pytest.raises(world.WorldError) as err:
        world.restore_mission_snapshot(snap)
    text = str(err.value)
    assert err.value.status == 500 and "ALSO failed" in text
    # the whole-array write raises and so does the element-by-element fallback (a tuple); saved still refuses its set
    assert "tracker.progress (" in text and "saved.active_set (RuntimeError)" in text
    assert "still not as before: tracker.progress)" in text  # the only field left off, every other one was put back
    # every other field of the tracker record was still rolled back after its progress write failed
    assert tracker.ActiveObjectiveSet is farm.set2 and tracker.Status is st("MS_ReadyToTurnIn")
    assert "tracker.active_set" not in text.split("still not as before:")[1] and "tracker.status" not in text.split("still not as before:")[1]
    assert saved.ObjectivesProgress == [1]


def test_undo_is_off_when_the_saved_record_is_complete_even_if_the_tracker_is_not(farm):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    assert "undo" in only_alert()["actions"]
    farm.saved.Status = st("MS_Complete")  # the tracker still says Active
    with pytest.raises(farmguard.FarmError) as err:
        resolve(only_alert()["id"], "undo")
    assert err.value.status == 409 and err.value.args[0] == farmguard.NO_UNDO_AFTER_TURN_IN
    assert farm.entry.ObjectivesProgress == [1]
    assert only_alert()["actions"] == ["keep"]


@pytest.mark.parametrize("record", ["tracker", "saved"])
def test_undo_is_off_when_any_record_status_is_unknown(farm, record):
    enable()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    alert = only_alert()
    (farm.entry if record == "tracker" else farm.saved).Status = object()
    with pytest.raises(farmguard.FarmError) as err:
        resolve(alert["id"], "undo")
    assert err.value.status == 409 and err.value.args[0] == farmguard.NO_UNDO_UNREADABLE
    assert farm.entry.ObjectivesProgress == [1]
    assert only_alert()["actions"] == ["keep"] and only_alert()["note"] == farmguard.NO_UNDO_UNREADABLE


def test_a_new_alert_offers_keep_only_when_a_record_is_unreadable(farm):
    enable()
    farm.saved.Status = object()
    farm.tracker.UpdateObjective(MissionObjective=farm.obj, ObjectiveBit=1)
    alert = only_alert()
    assert alert["actions"] == ["keep"] and alert["note"] == farmguard.NO_UNDO_UNREADABLE
