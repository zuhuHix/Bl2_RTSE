"""rtse/loadouts.py: validation, the link codec, storage, matching, the plan, apply (rollback, journal outcome, readback),
importing missing items from their codes, and the route tables. The save is a FakeSave: an in-memory character, skill tree and inventory whose write
functions are patched in over the real modules, so nothing touches the game and nothing is written under rtse/.

    python -I -m pytest dev/tests -k loadouts
"""

from __future__ import annotations

import base64
import json
import zlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from rtse import api, character, codes, gear_edit, gear_inventory, gear_parts, item_serial, journal, loadouts, skills
from rtse.gear_parts import PartRef, Slot
from rtse.routing import ApiError
from rtse.loadouts import LoadoutError


def xp_for(level: int) -> int:
    return level * 100  # any monotonic table; the fake game uses it for both reads and writes


class Named:
    """A part or definition as far as RTSE reads it: a path name and a class."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.Class = SimpleNamespace(Name="WeaponPartDefinition")

    def _path_name(self) -> str:
        return self.path


def make_gun(path: str, *, balance: str | None = "Balance.Pistol", level: int = 10, parts: dict[str, str] | None = None,
             name: str = "Logan's Gun") -> SimpleNamespace:
    fields: dict[str, Any] = {f"{slot}PartDefinition": None for slot in gear_parts.WEAPON_SLOTS}
    for slot, part in (parts or {}).items():
        fields[f"{slot}PartDefinition"] = Named(part)
    data = SimpleNamespace(
        BalanceDefinition=Named(balance) if balance else None,
        WeaponTypeDefinition=Named("WeaponType.Pistol"),
        ManufacturerDefinition=Named("Manufacturer.Jakobs"),
        **fields,
    )
    item = SimpleNamespace(
        Class=SimpleNamespace(Name="WillowWeapon"), DefinitionData=data, ExpLevel=level, path=path,
        GetHumanReadableName=lambda: name,
    )
    item._path_name = lambda: item.path  # noqa: SLF001
    return item


def gun_entry(*, balance: str | None = "Balance.Pistol", level: int = 10, parts: dict[str, str | None] | None = None,
              location: str = "Equipped", name: str | None = "Logan's Gun") -> dict[str, Any]:
    full: dict[str, Any] = dict.fromkeys(gear_parts.WEAPON_SLOTS)
    full.update(parts or {})
    entry: dict[str, Any] = {
        "kind": "weapon", "class": "WillowWeapon", "location": location, "balance": balance,
        "definition": "WeaponType.Pistol", "manufacturer": "Manufacturer.Jakobs", "level": level, "parts": full,
    }
    if name is not None:
        entry["name"] = name
    return entry


def build(**sections: Any) -> dict[str, Any]:
    return {"format": "rtse.loadout", "version": 1, "kind": "loadout", "name": "Test build", "class_key": "assassin",
            **sections}


class FakeSave:
    """An in-memory save. `explode(name, params) -> True` makes that write raise; names in `silent` are writes that don't
    stick (their readback then differs); `renames` makes each gear rebuild change the item's id, as the game may do."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.character: dict[str, Any] = {"class_key": "assassin", "level": 10, "xp": xp_for(10), "skill_points": 3,
                                          "money": 100, "eridium": 0, "seraph": 0, "torgue": 0}
        self.cap = 72
        self.skill_tree: dict[str, dict[str, Any]] = {
            "GD_Assassin_Skills.Sniping.Headshot": {"level": 2, "max": 5},
            "GD_Assassin_Skills.Sniping.Velocity": {"level": 0, "max": None},
        }
        self.items: list[tuple[SimpleNamespace, str]] = []
        self.offers: dict[str, dict[str, list[str]]] = {
            "legal": {"Barrel": ["Barrel.A", "Barrel.B"], "Body": ["Body.A"]},
            "same_slot": {"Barrel": ["Barrel.A", "Barrel.B", "Barrel.C"], "Body": ["Body.A"]},
            "everything": {"Barrel": ["Barrel.A", "Barrel.B", "Barrel.C"], "Body": ["Body.A"]},
        }
        self.limits = {"min": 1, "max": 90, "character_max": 80, "overpower_max": 10}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.explode: Callable[[str, dict[str, Any]], bool] | None = None
        self.silent: set[str] = set()
        self.renames = False
        self._renamed = 0
        monkeypatch.setattr(character, "read", self.character_read)
        monkeypatch.setattr(character, "set_field", self.set_field)
        monkeypatch.setattr(skills, "read", self.skills_read)
        monkeypatch.setattr(skills, "set_level", self.skills_set_level)
        monkeypatch.setattr(gear_inventory, "_manager", lambda: object())
        monkeypatch.setattr(gear_inventory, "_inventory", lambda _manager: (list(self.items), []))
        monkeypatch.setattr(gear_edit, "_gear_limits", lambda: dict(self.limits))
        monkeypatch.setattr(gear_edit, "set_part", self.set_part)
        monkeypatch.setattr(gear_edit, "set_level", self.set_level)
        monkeypatch.setattr(gear_parts, "_slots", self.slots)

    # --- the save ---
    def add(self, item: SimpleNamespace, location: str = gear_inventory.EQUIPPED) -> SimpleNamespace:
        self.items.append((item, location))
        return item

    def _item(self, path: Any) -> SimpleNamespace:
        for item, _location in self.items:
            if item.path == path:
                return item
        raise ApiError(404, f"that item is no longer in your inventory ({path})")

    def _offered(self, mode: str, slot: str) -> list[str]:
        return self.offers.get(mode, {}).get(slot, [])

    def _begin(self, name: str, params: dict[str, Any]) -> bool:
        self.calls.append((name, dict(params)))
        if self.explode is not None and self.explode(name, params):
            raise RuntimeError("the game crashed")
        return name in self.silent

    def _rebuilt(self, item: SimpleNamespace) -> None:
        if self.renames:
            self._renamed += 1
            item.path = f"{item.path.split('#')[0]}#{self._renamed}"

    def _detail(self, item: SimpleNamespace) -> dict[str, Any]:
        slots = []
        for slot in gear_parts.WEAPON_SLOTS:
            part = getattr(item.DefinitionData, f"{slot}PartDefinition")
            slots.append({"slot": slot, "current": None if part is None else {"id": part.path}})
        return {"id": item.path, "level": item.ExpLevel, "slots": slots}

    # --- character and skills ---
    def character_read(self) -> dict[str, Any]:
        c = self.character
        return {
            "class_key": c["class_key"], "level": c["level"], "xp": c["xp"], "skill_points": c["skill_points"],
            "currencies": [{"key": k, "value": c[k]} for k in ("money", "eridium", "seraph", "torgue")],
            "limits": {"level": [character.MIN_LEVEL, self.cap]},
        }

    def set_field(self, params: dict[str, Any]) -> dict[str, Any]:
        silent = self._begin("character.set_field", params)
        field, value = params["field"], params["value"]
        if not silent:
            if field == "level":
                self.character["level"], self.character["xp"] = value, xp_for(value)
            else:
                self.character[field] = value
        return {**self.character_read(), "applied": {"field": field, "after": self.character[field]}}

    def skills_read(self) -> dict[str, Any]:
        return {
            "available": True, "reason": None, "class_key": self.character["class_key"],
            "skills": [{"path": p, "name": p.rpartition(".")[2], "level": v["level"], "max": v["max"],
                        "hidden": v.get("hidden", False), "action": v.get("action", False)}
                       for p, v in self.skill_tree.items()],
        }

    def skills_set_level(self, params: dict[str, Any]) -> dict[str, Any]:
        silent = self._begin("skills.set_level", params)
        path, level = params["id"], params["level"]
        if path not in self.skill_tree:
            raise skills.SkillsError(400, "unknown skill")
        top = 255 if params.get("over_max") is True else (self.skill_tree[path]["max"] or 5)
        if not 0 <= level <= top:
            raise skills.SkillsError(400, f"must be 0-{top}")
        if not silent:
            self.skill_tree[path]["level"] = level
        return {**self.skills_read(), "applied": {"name": path, "requested": level, "after": self.skill_tree[path]["level"]}}

    # --- gear ---
    def slots(self, _item: Any, mode: str) -> list[Slot]:
        return [Slot(s, f"{s}PartDefinition", [PartRef(p, "WeaponPartDefinition") for p in self._offered(mode, s)])
                for s in gear_parts.WEAPON_SLOTS]

    def set_part(self, params: dict[str, Any]) -> dict[str, Any]:
        silent = self._begin("set_part", params)
        item = self._item(params["id"])
        slot, part, mode = params["slot"], params.get("part"), params.get("mode", "legal")
        if part is not None and part not in self._offered(mode, slot):
            raise ApiError(400, "that part is not available in this mode")
        if not silent:
            setattr(item.DefinitionData, f"{slot}PartDefinition", None if part is None else Named(part))
            self._rebuilt(item)
        return self._detail(item)

    def set_level(self, params: dict[str, Any]) -> dict[str, Any]:
        silent = self._begin("set_level", params)
        item = self._item(params["id"])
        level = params["level"]
        if not self.limits["min"] <= level <= self.limits["max"]:
            raise ApiError(400, "level out of range")
        if not silent:
            item.ExpLevel = level
            self._rebuilt(item)
        return self._detail(item)


@pytest.fixture(autouse=True)
def stored_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Builds and the debug dump go to tmp_path, never into rtse/."""
    monkeypatch.setattr(loadouts, "PRESETS_FILE", tmp_path / "loadouts.json")
    monkeypatch.setattr(loadouts, "DEBUG_FILE", tmp_path / "debug_loadouts.json")
    monkeypatch.setattr(loadouts, "_last_report", None)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_runs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(loadouts, "_runs", {})


@pytest.fixture(autouse=True)
def _no_open_transaction():
    journal._current = None  # noqa: SLF001
    yield
    journal._current = None  # noqa: SLF001


@pytest.fixture
def save(monkeypatch: pytest.MonkeyPatch) -> FakeSave:
    return FakeSave(monkeypatch)


def store(preset: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return loadouts.import_preset({"preset": preset, **kwargs})


def preview(name: str, **options: Any) -> dict[str, Any]:
    return loadouts.plan_route({"name": name, **options})


def statuses(report: dict[str, Any]) -> list[str]:
    return [step["status"] for step in report["steps"]]


# --- validation ----------------------------------------------------------------------------------


def test_a_valid_preset_round_trips_through_validate():
    preset = build(character={"level": 72, "xp": 123, "money": 1}, skills=[{"path": "GD_A.B.C", "level": 5}],
                   gear=[gun_entry(parts={"Barrel": "Barrel.A"})])
    assert loadouts.validate(preset) == {**preset, "version": 2}  # a version 1 build is upgraded as it loads


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({**build(), "extra": 1}, "unknown key 'extra' in the preset"),
        (build(character={"level": 5, "mood": 1}), "unknown key 'mood' in character"),
        (build(skills=[{"path": "GD_A.B.C", "level": 1, "action": True}]), "unknown key 'action' in skill #1"),
        (build(gear=[{**gun_entry(), "colour": "red"}]), "unknown key 'colour' in gear #1"),
        (build(gear=[gun_entry(parts={"Wings": None})]), "unknown key 'Wings' in gear #1 parts"),
        ({**build(), "version": True}, "version must be a whole number"),
        ({**build(), "version": 3}, "unsupported preset version 3"),
        ({**build(), "format": "other"}, "not a build preset"),
        (build(character={"level": True}), "level must be a whole number"),
        (build(character={"level": 101}), "level must be 1-100"),
        (build(skills=[{"path": "GD_A.B.C", "level": 256}]), "must be 0-255"),
        (build(skills=[{"path": "bad path!", "level": 1}]), "isn't a valid object path"),
        (build(gear=[gun_entry(balance="a/b")]), "balance isn't a valid object path"),
        (build(gear=[gun_entry(parts={"Barrel": "x" * 300})]), "isn't a valid object path"),
        (build(gear=[{**gun_entry(), "class": "Gun"}]), "class must be a Willow class name"),
        (build(gear=[{**gun_entry(), "location": "Bank"}]), "location must be"),
        ({**build(), "name": "bad\nname"}, "control characters"),
        ({**build(), "name": "   "}, "name must be 1-64 characters"),
        ({**build(), "class_key": "wizard"}, "class_key must be null"),
        ({k: v for k, v in build().items() if k != "class_key"}, "class_key is required"),
    ],
)
def test_validate_rejects_bad_input_with_a_400_that_names_the_problem(data, message):
    with pytest.raises(LoadoutError) as caught:
        loadouts.validate(data)
    assert caught.value.status == 400
    assert message in str(caught.value)


def test_validate_rejects_a_slot_key_that_does_not_belong_to_the_kind():
    item = {**gun_entry(), "kind": "item", "class": "WillowInventory"}  # an item's parts are Alpha..Material
    with pytest.raises(LoadoutError, match="unknown key 'Body' in gear #1 parts"):  # a weapon slot on an item
        loadouts.validate(build(gear=[item]))


def test_validate_rejects_too_many_gear_and_too_many_skills():
    with pytest.raises(LoadoutError, match="too many gear"):
        loadouts.validate(build(gear=[gun_entry() for _ in range(loadouts.MAX_GEAR + 1)]))
    skills_list = [{"path": f"GD_A.B.S{i}", "level": 1} for i in range(loadouts.MAX_SKILLS + 1)]
    with pytest.raises(LoadoutError, match="too many skills"):
        loadouts.validate(build(skills=skills_list))


def test_validate_rejects_a_duplicate_skill_path():
    with pytest.raises(LoadoutError, match="listed twice"):
        loadouts.validate(build(skills=[{"path": "GD_A.B.C", "level": 1}, {"path": "GD_A.B.C", "level": 2}]))


def test_validate_rejects_an_oversize_preset_before_walking_it():
    huge = build(name="x" * 10, created="y" * 40, gear=[gun_entry(name="g" * 120) for _ in range(loadouts.MAX_GEAR)],
                 skills=[{"path": f"GD_A.B.S{i:05d}", "level": 1} for i in range(loadouts.MAX_SKILLS)],
                 character={"level": 5, "junk": "z" * (loadouts.MAX_PRESET_BYTES)})
    with pytest.raises(LoadoutError, match="the preset is .* bytes; the limit is"):
        loadouts.validate(huge)


def test_a_non_object_preset_is_a_400():
    with pytest.raises(LoadoutError, match="must be an object"):
        loadouts.validate(["not", "a", "preset"])


# --- the compact link ----------------------------------------------------------------------------


def test_link_round_trip_gives_back_the_same_preset():
    preset = loadouts.validate(build(character={"level": 72}, gear=[gun_entry(parts={"Barrel": "Barrel.A"})]))
    link = loadouts.to_link(preset)
    assert link.startswith("RTSE-L1:")
    assert "=" not in link
    assert loadouts.from_link(f"  {link[:20]}\n{link[20:]}  ") == preset


def test_link_accepts_any_zlib_stream_as_the_browser_sends_it():
    preset = build(skills=[{"path": "GD_A.B.C", "level": 3}])
    raw = json.dumps(preset, separators=(",", ":")).encode()
    for level in (1, 6):  # CompressionStream("deflate") is zlib format at whatever level the browser picks
        packed = base64.urlsafe_b64encode(zlib.compress(raw, level)).decode().rstrip("=")
        assert loadouts.from_link("RTSE-L1:" + packed)["skills"] == preset["skills"]


def test_link_rejects_a_bad_prefix_and_bad_characters():
    with pytest.raises(LoadoutError, match="not a build link"):
        loadouts.from_link("XYZ-L1:abc")
    with pytest.raises(LoadoutError, match="bad characters"):
        loadouts.from_link("RTSE-L1:!!!!")


def test_link_rejects_a_link_over_the_length_limit():
    with pytest.raises(LoadoutError, match="the limit is 62000"):
        loadouts.from_link("RTSE-L1:" + "A" * loadouts.IMPORT_LINK_LIMIT)


def test_link_rejects_a_zip_bomb_without_inflating_it_all():
    bomb = zlib.compress(b"\x00" * (loadouts.MAX_PRESET_BYTES * 8), 9)
    link = "RTSE-L1:" + base64.urlsafe_b64encode(bomb).decode().rstrip("=")
    assert len(link) < loadouts.IMPORT_LINK_LIMIT  # small enough to be accepted by length alone
    with pytest.raises(LoadoutError, match="larger than"):
        loadouts.from_link(link)


def test_link_rejects_a_truncated_stream_and_trailing_data():
    raw = json.dumps(build(), separators=(",", ":")).encode()
    packed = zlib.compress(raw, 9)
    truncated = "RTSE-L1:" + base64.urlsafe_b64encode(packed[:-6]).decode().rstrip("=")
    with pytest.raises(LoadoutError, match="incomplete|not compressed"):
        loadouts.from_link(truncated)
    padded = "RTSE-L1:" + base64.urlsafe_b64encode(packed + b"junk").decode().rstrip("=")
    with pytest.raises(LoadoutError, match="extra data"):
        loadouts.from_link(padded)


def test_link_rejects_a_valid_stream_that_is_not_a_valid_preset():
    packed = zlib.compress(b'{"format": "rtse.loadout"}', 9)
    with pytest.raises(LoadoutError, match="version must be a whole number"):
        loadouts.from_link("RTSE-L1:" + base64.urlsafe_b64encode(packed).decode().rstrip("="))


# --- storage -------------------------------------------------------------------------------------


def test_saving_writes_the_file_atomically_and_leaves_no_temporary(stored_in_tmp):
    store(build(name="First"))
    store(build(name="Second"))
    assert not (stored_in_tmp / "loadouts.json.tmp").exists()
    data = json.loads((stored_in_tmp / "loadouts.json").read_text(encoding="utf-8"))
    assert data["format"] == "rtse.loadouts" and data["version"] == 1
    assert [p["name"] for p in data["presets"]] == ["First", "Second"]


def test_a_duplicate_name_is_a_409_unless_overwriting(stored_in_tmp):
    store(build(name="Zer0 crit"))
    with pytest.raises(LoadoutError) as caught:
        store(build(name="zer0 CRIT"))
    assert caught.value.status == 409
    assert "already exists" in str(caught.value)
    result = store(build(name="zer0 CRIT", character={"level": 9}), overwrite=True)
    assert [p["name"] for p in result["presets"]] == ["zer0 CRIT"]


def test_the_store_is_capped_at_100_builds(stored_in_tmp):
    for index in range(loadouts.MAX_PRESETS):
        store(build(name=f"Build {index}"))
    with pytest.raises(LoadoutError, match="100 builds"):
        store(build(name="One too many"))


def test_a_corrupt_file_is_copied_aside_and_reported(stored_in_tmp):
    (stored_in_tmp / "loadouts.json").write_text("{not json", encoding="utf-8")
    listing = loadouts.list_presets({})
    assert listing["presets"] == []
    assert "copied to loadouts.bad.json" in listing["warnings"][0]
    assert (stored_in_tmp / "loadouts.bad.json").read_text(encoding="utf-8") == "{not json"


def test_one_invalid_stored_entry_is_skipped_with_a_warning(stored_in_tmp):
    store(build(name="Good"))
    data = json.loads((stored_in_tmp / "loadouts.json").read_text(encoding="utf-8"))
    data["presets"].append({"format": "rtse.loadout", "version": 1, "kind": "loadout", "name": "Bad", "class_key": None,
                            "mystery": True})
    (stored_in_tmp / "loadouts.json").write_text(json.dumps(data), encoding="utf-8")
    listing = loadouts.list_presets({})
    assert [p["name"] for p in listing["presets"]] == ["Good"]
    assert "skipped build #2" in listing["warnings"][0]


def test_delete_and_get_are_404_for_an_unknown_name(stored_in_tmp):
    store(build(name="Only"))
    with pytest.raises(LoadoutError) as caught:
        loadouts.get_preset({"name": "Nope"})
    assert caught.value.status == 404
    assert loadouts.delete({"name": "only"})["presets"] == []


# --- capture ------------------------------------------------------------------------------------


def test_capture_takes_equipped_gear_skills_and_character_and_leaves_out_hidden_skills(save, monkeypatch):
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}, level=12))
    save.add(make_gun("Guns.Pistol.B", parts={"Barrel": "Barrel.B"}, level=7), gear_inventory.BACKPACK)
    save.skill_tree["GD_Assassin_Skills.Sniping.Hidden"] = {"level": 1, "max": 1, "hidden": True}
    preset, _warnings = loadouts.capture("Mine", {"equipped": True, "skills": True, "character": True})
    assert preset["kind"] == "loadout" and preset["name"] == "Mine"
    assert [g["location"] for g in preset["gear"]] == ["Equipped"]
    assert preset["gear"][0]["parts"]["Barrel"] == "Barrel.A"
    assert preset["gear"][0]["level"] == 12
    assert {s["path"] for s in preset["skills"]} == {"GD_Assassin_Skills.Sniping.Headshot", "GD_Assassin_Skills.Sniping.Velocity"}
    assert preset["character"] == {"level": 10, "skill_points": 3}


def test_capture_with_nothing_ticked_is_refused(save):
    with pytest.raises(LoadoutError, match="tick something"):
        loadouts.capture("Empty", {})


def test_the_whole_character_export_has_every_section_and_key(save):
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}))
    preset, _warnings = loadouts.capture(None, {}, whole=True)
    assert preset["kind"] == "character"
    assert set(preset) >= {"character", "skills", "gear"}
    assert set(preset["character"]) == set(loadouts.CHARACTER_KEYS)
    assert preset["name"].startswith("Assassin L10 ")


def test_capture_outside_a_save_is_a_409(monkeypatch):
    def not_in_save() -> None:
        raise character.CharacterError(409, "not in a save")

    monkeypatch.setattr(character, "read", not_in_save)
    with pytest.raises(LoadoutError) as caught:
        loadouts.capture("X", {"character": True})
    assert caught.value.status == 409


# --- matching and the plan -----------------------------------------------------------------------


def test_an_exact_match_is_unchanged_and_plans_no_steps(save):
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}, level=10))
    result = loadouts.plan(loadouts.validate(build(gear=[gun_entry(parts={"Barrel": "Barrel.A"}, level=10)])))
    assert result["steps"] == []
    assert result["unchanged"] == [{"section": "gear", "label": "Logan's Gun (Equipped)", "reason": "already matches"}]
    assert result["summary"] == {"changes": 0, "unchanged": 1, "missing": 0, "blocked": 0, "importable": 0}


def test_the_best_remaining_candidate_wins_on_score(save):
    save.add(make_gun("Guns.Pistol.X", parts={"Barrel": "Barrel.B"}, level=5))
    save.add(make_gun("Guns.Pistol.Y", parts={"Barrel": "Barrel.A"}, level=9))
    result = loadouts.plan(build(gear=[gun_entry(parts={"Barrel": "Barrel.A"}, level=10)]))
    assert [(s["kind"], s["target"]) for s in result["steps"]] == [("gear.level", "Guns.Pistol.Y")]
    assert result["steps"][0]["before"] == 9 and result["steps"][0]["after"] == 10


def test_each_live_item_is_used_once_and_the_extra_entry_is_missing(save):
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}))
    gear = [gun_entry(parts={"Barrel": "Barrel.A"}), gun_entry(parts={"Barrel": "Barrel.A"})]
    result = loadouts.plan(build(gear=gear))
    assert [m["entry"] for m in result["missing"]] == [1]
    assert result["missing"][0]["can_import"] is False and result["missing"][0]["has_code"] is False
    assert len(result["unchanged"]) == 1


def test_an_entry_with_no_candidate_is_missing_with_its_details(save):
    save.add(make_gun("Guns.Pistol.A", balance="Balance.Other"))
    result = loadouts.plan(build(gear=[gun_entry(balance="Balance.Pistol", level=40, name="Zer0's Gun")]))
    assert result["missing"] == [{"entry": 0, "name": "Zer0's Gun", "kind": "weapon", "class": "WillowWeapon",
                                  "location": "Equipped", "balance": "Balance.Pistol", "level": 40, "has_code": False,
                                  "can_import": False}]


def test_a_gun_in_the_backpack_is_a_location_note_and_its_parts_are_still_planned(save):
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}, level=10), gear_inventory.BACKPACK)
    result = loadouts.plan(build(gear=[gun_entry(parts={"Barrel": "Barrel.B"}, level=10, location="Equipped")]))
    assert [b["kind"] for b in result["blocked"]] == ["location"]
    assert "equip or unequip it yourself" in result["blocked"][0]["reason"]
    assert [s["kind"] for s in result["steps"]] == ["gear.part"]


def test_a_skipped_entry_is_unchanged_and_does_not_take_an_item(save):
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}))
    gear = [gun_entry(parts={"Barrel": "Barrel.A"}), gun_entry(parts={"Barrel": "Barrel.A"}, name="Second")]
    result = loadouts.plan(build(gear=gear), skip=[0])
    assert {"section": "gear", "label": "Logan's Gun", "reason": "skipped by you"} in result["unchanged"]
    assert [m["entry"] for m in result["missing"]] == []  # the live gun went to the second entry


def test_the_plan_is_read_only(save, stored_in_tmp):
    store(build(gear=[gun_entry(parts={"Barrel": "Barrel.B"})]))
    before_file = (stored_in_tmp / "loadouts.json").read_bytes()
    preview("Test build")
    assert save.calls == []
    assert (stored_in_tmp / "loadouts.json").read_bytes() == before_file


def test_a_class_mismatch_blocks_the_whole_skills_section(save):
    save.character["class_key"] = "siren"
    result = loadouts.plan(build(class_key="assassin", skills=[{"path": "GD_Assassin_Skills.Sniping.Headshot", "level": 4}]))
    assert result["class"] == {"preset": "assassin", "current": "siren", "match": False}
    assert result["steps"] == []
    assert result["blocked"][0]["kind"] == "class"
    assert "this build is for assassin, and you are siren" in result["blocked"][0]["reason"]


def test_a_skill_above_its_maximum_needs_allow_above_max(save):
    skill = {"path": "GD_Assassin_Skills.Sniping.Headshot", "level": 7}
    blocked = loadouts.plan(build(skills=[skill]))
    assert blocked["blocked"][0]["kind"] == "max" and "tick Allow above max" in blocked["blocked"][0]["reason"]
    allowed = loadouts.plan(build(skills=[skill]), over_max=True)
    assert [s["params"] for s in allowed["steps"]] == [{"id": skill["path"], "level": 7, "over_max": True}]


def test_a_skill_missing_from_the_tree_is_blocked(save):
    result = loadouts.plan(build(skills=[{"path": "GD_Assassin_Skills.Sniping.Nope", "level": 1}]))
    assert result["blocked"][0]["kind"] == "missing"


def test_a_level_above_the_game_cap_is_blocked(save):
    result = loadouts.plan(build(character={"level": 90}))
    assert result["blocked"] == [{"section": "character", "label": "Character level",
                                  "reason": "level 90 is above this game's cap of 72", "kind": "limit"}]


def test_a_part_that_is_not_loaded_in_any_mode_is_blocked(save):
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}))
    result = loadouts.plan(build(gear=[gun_entry(parts={"Barrel": "Barrel.Z"})]))
    assert [b["kind"] for b in result["blocked"]] == ["part"]
    assert result["steps"] == []


def test_a_part_offered_only_in_same_slot_mode_is_planned_in_that_mode(save):
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}))
    result = loadouts.plan(build(gear=[gun_entry(parts={"Barrel": "Barrel.C"})]))
    assert result["steps"][0]["params"]["mode"] == "same_slot"


def test_a_gear_level_outside_the_range_is_blocked(save, monkeypatch):
    save.add(make_gun("Guns.Pistol.A", level=5))
    save.limits["max"] = 50
    result = loadouts.plan(build(gear=[gun_entry(level=60)]))
    assert [b["kind"] for b in result["blocked"]] == ["level"]
    assert "outside this game's gear range 1-50" in result["blocked"][0]["reason"]


def test_too_many_steps_is_a_400(save, monkeypatch):
    for index in range(3):
        save.add(make_gun(f"Guns.Pistol.{index}", parts={"Barrel": "Barrel.B"}, level=1))
    preset = loadouts.validate(build(gear=[gun_entry(parts={"Barrel": "Barrel.A"}, level=5) for _ in range(3)]))
    monkeypatch.setattr(loadouts, "MAX_STEPS", 2)
    with pytest.raises(LoadoutError, match="steps; the limit is 2"):
        loadouts.plan(preset)


def test_plan_id_changes_when_the_save_changes(save):
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}))
    preset = build(gear=[gun_entry(parts={"Barrel": "Barrel.B"})])
    first = loadouts.plan(preset)["plan_id"]
    assert loadouts.plan(preset)["plan_id"] == first
    save.items[0][0].DefinitionData.BarrelPartDefinition = Named("Barrel.B")  # the game changed underneath the preview
    assert loadouts.plan(preset)["plan_id"] != first


# --- apply ---------------------------------------------------------------------------------------


def scenario(save: FakeSave, stored_in_tmp) -> dict[str, Any]:
    """A save with one gun on Barrel A; the build moves skill points, Headshot, the barrel and the level."""
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}, level=10))
    store(build(character={"skill_points": 5},
                skills=[{"path": "GD_Assassin_Skills.Sniping.Headshot", "level": 4}],
                gear=[gun_entry(parts={"Barrel": "Barrel.B"}, level=12)]), overwrite=True)
    return {"name": "Test build"}


def test_apply_all_ok_changes_the_save_and_commits_the_journal(save, stored_in_tmp):
    request = scenario(save, stored_in_tmp)
    preview_result = preview(request["name"])
    assert [s["kind"] for s in preview_result["steps"]] == ["character.skill_points", "skills.level", "gear.part", "gear.level"]
    with journal.transaction("test/apply") as tx:
        report = loadouts.apply({**request, "plan_id": preview_result["plan_id"]})
    assert report["ok"] is True and report["rolled_back"] is False
    assert statuses(report) == ["ok", "ok", "ok", "ok"]
    assert report["summary"] == {"ok": 4, "failed": 0, "rolled_back": 0, "rollback_failed": 0, "not_run": 0, "pending": 0}
    assert report["state"] == "done"
    assert report["chunks"] == [{"chunk": 1, "from": 1, "to": 4, "status": "applied", "in_history": None}]
    assert [(s["before"], s["after"]) for s in report["steps"]] == [(3, 5), (2, 4), ("Barrel.A", "Barrel.B"), (10, 12)]
    assert save.character["skill_points"] == 5
    assert save.skill_tree["GD_Assassin_Skills.Sniping.Headshot"]["level"] == 4
    item = save._item("Guns.Pistol.A")
    assert item.DefinitionData.BarrelPartDefinition.path == "Barrel.B" and item.ExpLevel == 12
    assert tx.outcome == "committed"
    assert loadouts._last_report is report


def test_a_failure_rolls_back_the_ok_steps_in_reverse_and_aborts_the_journal(save, stored_in_tmp):
    request = scenario(save, stored_in_tmp)
    result = preview(request["name"])
    save.explode = lambda name, _params: name == "set_part"
    with journal.transaction("test/apply") as tx:
        report = loadouts.apply({**request, "plan_id": result["plan_id"]})

    assert report["ok"] is False and report["rolled_back"] is True
    assert statuses(report) == ["rolled_back", "rolled_back", "failed", "not_run"]
    assert "the game crashed" in report["steps"][2]["error"]
    # The undo calls come after the failing forward call, last ok step first, each back to its `before`.
    undo = [(name, params) for name, params in save.calls[3:]]
    assert [name for name, _ in undo] == ["skills.set_level", "character.set_field"]
    assert undo[0][1] == {"id": "GD_Assassin_Skills.Sniping.Headshot", "level": 2, "over_max": False}
    assert undo[1][1] == {"field": "skill_points", "value": 3}
    assert save.character["skill_points"] == 3 and save.skill_tree["GD_Assassin_Skills.Sniping.Headshot"]["level"] == 2
    assert tx.outcome == "aborted"


def test_keep_mode_does_not_roll_back_and_the_journal_keeps_what_happened(save, stored_in_tmp):
    request = scenario(save, stored_in_tmp)
    result = preview(request["name"])
    save.explode = lambda name, _params: name == "set_part"
    with journal.transaction("test/apply") as tx:
        report = loadouts.apply({**request, "plan_id": result["plan_id"], "on_failure": "keep"})
    assert report["ok"] is False and report["rolled_back"] is False and report["on_failure"] == "keep"
    assert statuses(report) == ["ok", "ok", "failed", "not_run"]
    assert save.character["skill_points"] == 5 and save.skill_tree["GD_Assassin_Skills.Sniping.Headshot"]["level"] == 4
    assert not any(name == "character.set_field" and params["value"] == 3 for name, params in save.calls)
    assert tx.outcome == "committed"


def test_a_stale_plan_id_is_a_409_and_nothing_runs(save, stored_in_tmp):
    request = scenario(save, stored_in_tmp)
    with pytest.raises(LoadoutError) as caught:
        loadouts.apply({**request, "plan_id": "0" * 64})
    assert caught.value.status == 409
    assert "preview again" in str(caught.value)
    assert save.calls == []


def test_a_readback_mismatch_counts_as_a_failure(save, stored_in_tmp):
    request = scenario(save, stored_in_tmp)
    result = preview(request["name"])
    save.silent = {"skills.set_level"}  # the write goes through but reads back the old level
    report = loadouts.apply({**request, "plan_id": result["plan_id"]})
    assert statuses(report) == ["rolled_back", "failed", "not_run", "not_run"]
    assert "the game reads 2, not 4" in report["steps"][1]["error"]


def test_a_gear_id_that_changes_after_a_rebuild_is_followed_by_later_steps(save, stored_in_tmp):
    save.renames = True
    request = scenario(save, stored_in_tmp)
    result = preview(request["name"])
    report = loadouts.apply({**request, "plan_id": result["plan_id"]})
    assert statuses(report) == ["ok", "ok", "ok", "ok"]
    level_call = next(params for name, params in save.calls if name == "set_level")
    assert level_call["id"] == "Guns.Pistol.A#1"  # set_part renamed the gun; set_level used the new id


def test_a_rollback_that_cannot_undo_says_to_reload_the_save(save, stored_in_tmp):
    request = scenario(save, stored_in_tmp)
    result = preview(request["name"])
    # The gear step fails, and then the undo of the skill points (value 3) fails too.
    save.explode = lambda name, params: name == "set_part" or (name == "character.set_field" and params["value"] == 3)
    report = loadouts.apply({**request, "plan_id": result["plan_id"]})
    assert statuses(report) == ["rollback_failed", "rolled_back", "failed", "not_run"]
    rollback_failed = [s for s in report["steps"] if s["status"] == "rollback_failed"]
    assert [s["label"] for s in rollback_failed] == ["Skill points"]
    assert rollback_failed[0]["error"].endswith("reload the save before playing")


def test_apply_rejects_a_bad_on_failure_and_needs_a_plan_id(save, stored_in_tmp):
    request = scenario(save, stored_in_tmp)
    with pytest.raises(LoadoutError, match="on_failure"):
        loadouts.apply({**request, "plan_id": "x", "on_failure": "yolo"})
    with pytest.raises(LoadoutError, match="plan_id is required"):
        loadouts.apply(request)


# --- import missing items from their codes (rtse/codes.py) --------------------------------------
# Real published Gibbed codes (also used by test_item_serial.py): they pass the same offline checks B runs.
WEAPON_CODE = "BL2(h2/V3n+mGlXGeO8zxX8PVUyUhQuLayUPJYlikvHB65/Ell/prsQL)"  # Infinity (pistol)
ITEM_CODE = "BL2(B5RV2vkR7ysJhQXfl8v1nRLlvtqvzJp2ZBtfyYW/kw/XD2axxG8=)"  # Fabled Tortoise (shield)
BAD_CODE = WEAPON_CODE.replace("h2/V3n", "h2/V3m")  # one character off: the checksum fails


def coded_build(save: FakeSave) -> dict[str, Any]:
    """The save holds the gun of entry 0; entry 1 (another pistol, with a code) and entry 2 (no code) are missing."""
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}))
    gear = [gun_entry(parts={"Barrel": "Barrel.A"}, name="Kept"),
            {**gun_entry(balance="Balance.Infinity", name="Infinity"), "code": WEAPON_CODE},
            gun_entry(balance="Balance.Other", name="No code")]
    return store({**build(gear=gear), "version": 2}, overwrite=True)


class FakeCodes:
    """codes.decode_chunk / import_chunk / undo_add with B's reply shapes, at most `per_call` codes per call."""

    def __init__(self, save: FakeSave, monkeypatch: pytest.MonkeyPatch, per_call: int = 1) -> None:
        self.save, self.per_call = save, per_call
        self.imported: dict[int, SimpleNamespace] = {}
        self.balance = "Balance.Infinity"  # what an imported code turns into
        self.undone: list[int] = []
        self.undo_works = True
        self.fail: set[str] = set()
        self.next_uid = 500
        monkeypatch.setattr(codes, "decode_chunk", self.decode_chunk)
        monkeypatch.setattr(codes, "import_chunk", self.import_chunk)
        monkeypatch.setattr(codes, "undo_add", self.undo_add)
        monkeypatch.setattr(gear_inventory, "backpack_item", self.imported.get)

    def decode_chunk(self, text: str, start: int = 0) -> dict[str, Any]:
        found = item_serial.extract_codes(text)
        entries = []
        for index in range(start, min(len(found), start + self.per_call)):
            card = {"name": "Infinity", "class": "WillowWeapon", "level": 50, "build": [], "type_path": None}
            entries.append({"index": index, "code": found[index], "ok": True, "error": None, "kind": "weapon",
                            "unique_id": 1, "card": card, "uid_clash": False, "warning": None})
        end = start + len(entries)
        return {"entries": entries, "found": len(found), "next": end if end < len(found) else None}

    def import_chunk(self, code_list: list[str], destination: str = "backpack") -> dict[str, Any]:
        assert destination == "backpack"
        results = []
        for code in code_list[: self.per_call]:
            if code in self.fail:
                results.append({"code": code, "ok": False, "error": "the game refused to add it", "id": None,
                                "name": None, "unique_id": None})
                continue
            self.next_uid += 1
            gun = make_gun(f"Guns.Imported.{self.next_uid}", balance=self.balance, name="Infinity")
            self.imported[self.next_uid] = gun
            results.append({"code": code, "ok": True, "error": None, "id": gun.path, "name": "Infinity",
                            "unique_id": self.next_uid})
        return {"results": results, "processed": len(results), "remaining": len(code_list) - len(results),
                "imported": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results),
                "path": "unpack"}

    def undo_add(self, destination: str, unique_id: int) -> None:
        self.undone.append(unique_id)
        if self.undo_works:
            self.imported.pop(unique_id, None)


def test_a_v2_entry_keeps_its_code_and_bad_codes_are_refused():
    entry = {**gun_entry(), "code": WEAPON_CODE}
    assert loadouts.validate({**build(gear=[entry]), "version": 2})["gear"][0]["code"] == WEAPON_CODE
    with pytest.raises(LoadoutError, match="code isn't valid"):  # a malformed code (checksum)
        loadouts.validate({**build(gear=[{**entry, "code": BAD_CODE}]), "version": 2})
    with pytest.raises(LoadoutError, match="code isn't valid"):
        loadouts.validate({**build(gear=[{**entry, "code": "BL2(not base64!)"}]), "version": 2})
    with pytest.raises(LoadoutError, match="not a weapon"):  # a shield's code on a weapon entry
        loadouts.validate({**build(gear=[{**entry, "code": ITEM_CODE}]), "version": 2})


def test_a_version_1_build_still_loads_without_codes_and_cannot_carry_one():
    old = build(gear=[gun_entry()])
    assert old["version"] == 1
    assert loadouts.validate(old)["version"] == 2  # upgraded; nothing else changes
    with pytest.raises(LoadoutError, match="unknown key 'code'"):
        loadouts.validate(build(gear=[{**gun_entry(), "code": WEAPON_CODE}]))
    with pytest.raises(LoadoutError, match="unsupported preset version 3"):
        loadouts.validate({**old, "version": 3})


def test_capture_saves_each_items_code_and_notes_the_ones_without(save, monkeypatch):
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}))
    save.add(make_gun("Guns.Pistol.B", parts={"Barrel": "Barrel.A"}, name="Broken"))

    def export(item: Any) -> tuple[str, dict[str, Any], list[str]]:
        if item.path == "Guns.Pistol.B":
            raise RuntimeError("CreateSerialNumber failed")
        return WEAPON_CODE, {"checksum_ok": True}, []

    monkeypatch.setattr(codes, "export_code", export)
    notes: list[str] = []
    preset, warnings = loadouts.capture("Coded", {"equipped": True}, notes=notes)
    assert preset["version"] == 2 and warnings == [] and "left_out" not in preset  # not a partial build
    assert [g.get("code") for g in preset["gear"]] == [WEAPON_CODE, None]
    assert notes and notes[0].startswith("1 item was saved without a code") and "Broken" in notes[0]


def test_capture_stops_asking_for_codes_after_its_time(save, monkeypatch):
    for i in range(3):
        save.add(make_gun(f"Guns.Pistol.{i}"))
    monkeypatch.setattr(codes, "export_code", lambda _item: (WEAPON_CODE, {"checksum_ok": True}, []))
    ticks = iter(range(1000))
    monkeypatch.setattr(loadouts, "_clock", lambda: next(ticks) * 0.9)
    notes: list[str] = []
    preset, _warnings = loadouts.capture("Slow", {"equipped": True}, notes=notes)
    assert 1 <= sum("code" in g for g in preset["gear"]) < 3
    assert "stopped asking the game for codes" in notes[0]


def test_the_plan_marks_missing_items_that_have_a_code(save, stored_in_tmp):
    coded_build(save)
    result = preview("Test build")
    assert [(m["name"], m["has_code"], m["can_import"]) for m in result["missing"]] == [
        ("Infinity", True, True), ("No code", False, False)]
    assert result["summary"]["importable"] == 1


def test_import_preview_then_import_into_the_backpack(save, stored_in_tmp, monkeypatch):
    coded_build(save)
    fake = FakeCodes(save, monkeypatch)
    first = loadouts.import_preview({"name": "Test build", "start": "0"})
    assert first["found"] == 1 and first["next"] is None
    (row,) = first["entries"]
    assert (row["entry"], row["name"], row["ok"], row["matches"]) == (1, "Infinity", True, True)
    assert first["skipped"] == [{"entry": 2, "name": "No code", "location": "Equipped", "level": 10,
                                 "reason": "no code was saved for this item"}]
    with journal.transaction("/api/loadouts/import_missing") as tx:
        reply = loadouts.import_missing({"name": "Test build", "plan_id": first["plan_id"], "entries": [1]})
    assert tx.outcome == "committed" and tx.label == "Build 'Test build': import missing 1"
    assert reply["state"] == "done" and reply["summary"]["imported"] == 1
    assert reply["results"][0]["status"] == "imported" and reply["results"][0]["id"] == "Guns.Imported.501"
    assert "code" not in reply["results"][0]
    # the imported gun is now in the save, so the build preview matches it
    save.add(fake.imported[501], gear_inventory.BACKPACK)
    assert [m["name"] for m in preview("Test build")["missing"]] == ["No code"]


def test_import_runs_in_slices_with_a_cursor(save, stored_in_tmp, monkeypatch):
    save.add(make_gun("Guns.Pistol.A"))
    gear = [{**gun_entry(balance="Balance.Infinity", name=f"Infinity {i}"), "code": WEAPON_CODE} for i in range(3)]
    store({**build(gear=gear), "version": 2}, overwrite=True)
    FakeCodes(save, monkeypatch, per_call=2)
    first = loadouts.import_preview({"name": "Test build", "start": 0})
    assert first["next"] == 2 and len(first["entries"]) == 2
    rest = loadouts.import_preview({"name": "Test build", "start": "2", "plan_id": first["plan_id"]})
    assert rest["next"] is None and [e["index"] for e in rest["entries"]] == [2] and rest["skipped"] == []
    reply = loadouts.import_missing({"name": "Test build", "plan_id": first["plan_id"], "entries": [0, 1, 2]})
    assert (reply["state"], reply["cursor"], reply["summary"]["pending"]) == ("running", 2, 1)
    with pytest.raises(LoadoutError) as caught:
        loadouts.import_missing({"run_id": reply["run_id"], "cursor": 0})
    assert caught.value.status == 409
    assert loadouts.import_run_route({"run_id": reply["run_id"]})["cursor"] == 2
    with pytest.raises(LoadoutError) as caught:  # an import run is not an apply run
        loadouts.run_route({"run_id": reply["run_id"]})
    assert caught.value.status == 404
    done = loadouts.import_missing({"run_id": reply["run_id"], "cursor": 2})
    assert done["state"] == "done" and done["summary"]["imported"] == 3


def test_an_imported_item_that_is_not_the_builds_item_is_removed_again(save, stored_in_tmp, monkeypatch):
    coded_build(save)
    fake = FakeCodes(save, monkeypatch)
    fake.balance = "Balance.SomethingElse"
    plan_id = loadouts.import_preview({"name": "Test build"})["plan_id"]
    reply = loadouts.import_missing({"name": "Test build", "plan_id": plan_id, "entries": [1]})
    assert reply["results"][0]["status"] == "removed" and fake.undone == [501] and fake.imported == {}
    assert "removed again" in reply["results"][0]["error"]


def test_a_removal_that_does_not_verify_is_remove_failed(save, stored_in_tmp, monkeypatch):
    coded_build(save)
    fake = FakeCodes(save, monkeypatch)
    fake.balance, fake.undo_works = "Balance.SomethingElse", False
    plan_id = loadouts.import_preview({"name": "Test build"})["plan_id"]
    reply = loadouts.import_missing({"name": "Test build", "plan_id": plan_id, "entries": [1]})
    assert reply["results"][0]["status"] == "remove_failed" and "check your backpack" in reply["results"][0]["error"]


def test_a_code_the_importer_refuses_is_failed_with_its_reason(save, stored_in_tmp, monkeypatch):
    coded_build(save)
    fake = FakeCodes(save, monkeypatch)
    fake.fail.add(WEAPON_CODE)
    plan_id = loadouts.import_preview({"name": "Test build"})["plan_id"]
    reply = loadouts.import_missing({"name": "Test build", "plan_id": plan_id, "entries": [1]})
    assert reply["results"][0] == {"entry": 1, "name": "Infinity", "status": "failed", "id": None, "item_name": None,
                                   "error": "the game refused to add it"}


def test_a_build_without_codes_has_nothing_to_import(save, stored_in_tmp, monkeypatch):
    save.add(make_gun("Guns.Pistol.A"))
    store(build(gear=[gun_entry(balance="Balance.Other", name="Old")]), overwrite=True)  # a version 1 build
    FakeCodes(save, monkeypatch)
    first = loadouts.import_preview({"name": "Test build"})
    assert first["entries"] == [] and first["found"] == 0 and [s["name"] for s in first["skipped"]] == ["Old"]
    with pytest.raises(LoadoutError, match="has no code"):
        loadouts.import_missing({"name": "Test build", "plan_id": first["plan_id"], "entries": [0]})


def test_import_needs_the_previews_id_and_a_fresh_one(save, stored_in_tmp, monkeypatch):
    coded_build(save)
    FakeCodes(save, monkeypatch)
    plan_id = loadouts.import_preview({"name": "Test build"})["plan_id"]
    save._item("Guns.Pistol.A").ExpLevel = 33  # the inventory changed after the preview
    for call in (lambda: loadouts.import_missing({"name": "Test build", "plan_id": plan_id, "entries": [1]}),
                 lambda: loadouts.import_preview({"name": "Test build", "plan_id": plan_id, "start": 1})):
        with pytest.raises(LoadoutError) as caught:
            call()
        assert caught.value.status == 409 and "preview again" in str(caught.value)
    with pytest.raises(LoadoutError, match="tick at least one"):
        loadouts.import_missing({"name": "Test build", "plan_id": plan_id, "entries": []})


# --- routes and the debug dump -------------------------------------------------------------------


def test_routes_are_collected_once_through_the_api_registry():
    gets, posts = api.collect([loadouts])
    assert set(gets) == {"/api/loadouts", "/api/loadouts/get", "/api/loadouts/link", "/api/loadouts/plan",
                         "/api/loadouts/run", "/api/loadouts/import_preview", "/api/loadouts/import_run",
                         "/api/loadouts/export_character", "/api/debug/loadouts"}
    assert set(posts) == {"/api/loadouts/save", "/api/loadouts/import", "/api/loadouts/delete", "/api/loadouts/apply",
                          "/api/loadouts/import_missing"}


def test_the_list_route_works_outside_a_save(monkeypatch, stored_in_tmp):
    store(build(name="Offline"))

    def not_in_save() -> None:
        raise character.CharacterError(409, "not in a save")

    monkeypatch.setattr(character, "read", not_in_save)
    listing = loadouts.list_presets({})
    assert listing["class_key"] is None
    assert [p["name"] for p in listing["presets"]] == ["Offline"]
    assert listing["can_import_missing"] is True and listing["schema_version"] == 2


def test_the_save_route_stores_a_capture_and_answers_with_the_summary(save, stored_in_tmp):
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}))
    answer = loadouts.save({"name": "Snapshot", "include": {"equipped": True}})
    assert answer["preset"]["counts"]["equipped"] == 1
    assert [p["name"] for p in answer["presets"]] == ["Snapshot"]
    with pytest.raises(LoadoutError) as caught:
        loadouts.save({"name": "Snapshot", "include": {"equipped": True}})
    assert caught.value.status == 409


def test_the_debug_dump_writes_a_file_and_does_not_crash_outside_a_save(monkeypatch, stored_in_tmp):
    def not_in_save(*_args: Any) -> None:
        raise ApiError(409, "not in a save")

    monkeypatch.setattr(gear_inventory, "_manager", not_in_save)
    monkeypatch.setattr(character, "read", not_in_save)
    answer = loadouts.debug_dump({})
    assert answer == {"saved_to": str(loadouts.DEBUG_FILE), "items": 0}
    dump = json.loads(loadouts.DEBUG_FILE.read_text(encoding="utf-8"))
    assert dump["capture"]["ok"] is False and dump["items_error"]


def test_a_step_that_wrote_but_read_back_wrong_is_put_back_by_the_rollback(save, stored_in_tmp, monkeypatch):
    request = scenario(save, stored_in_tmp)
    result = preview(request["name"])
    real = save.skills_set_level

    def half(params: dict[str, Any]) -> dict[str, Any]:  # the write lands on 3 instead of 4
        return real({**params, "level": 3 if params["level"] == 4 else params["level"]})

    monkeypatch.setattr(skills, "set_level", half)
    report = loadouts.apply({**request, "plan_id": result["plan_id"]})
    assert statuses(report) == ["rolled_back", "failed", "not_run", "not_run"]
    assert report["steps"][1]["error"].endswith("and that was put back)")
    assert save.skill_tree["GD_Assassin_Skills.Sniping.Headshot"]["level"] == 2
    assert all("_wrote" not in step for step in report["steps"])


def test_a_gear_part_rollback_picks_the_mode_that_offers_the_old_part(save, stored_in_tmp):
    save.offers["legal"]["Barrel"] = ["Barrel.B"]  # the old Barrel.C is only offered in same_slot mode
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.C"}, level=10))
    store(build(gear=[gun_entry(parts={"Barrel": "Barrel.B"}, level=12)]), overwrite=True)
    result = preview("Test build")
    save.explode = lambda name, _params: name == "set_level"
    report = loadouts.apply({"name": "Test build", "plan_id": result["plan_id"]})
    assert statuses(report) == ["rolled_back", "failed"]
    undo = [params for name, params in save.calls if name == "set_part"][-1]
    assert undo["part"] == "Barrel.C" and undo["mode"] == "same_slot"
    assert save._item("Guns.Pistol.A").DefinitionData.BarrelPartDefinition.path == "Barrel.C"


def test_the_whole_character_export_leaves_out_an_unreadable_skill_tree_with_a_warning(save, monkeypatch):
    monkeypatch.setattr(skills, "read", lambda: {"available": False, "reason": "no tree", "skills": []})
    preset, warnings = loadouts.capture(None, {}, whole=True)
    assert "skills" not in preset and "gear" in preset
    assert any("skills left out" in w for w in warnings)
    with pytest.raises(LoadoutError) as caught:
        loadouts.capture("x", {"skills": True})
    assert caught.value.status == 409


# --- review round 1: chunked apply, XP side effect, size limits, partial captures -------------------


def many_guns(save: FakeSave, count: int) -> dict[str, Any]:
    """`count` guns on Barrel A at level 10; the build wants each on Barrel B at level 12: 2 steps per gun."""
    for i in range(count):
        save.add(make_gun(f"Guns.Pistol.{i}", parts={"Barrel": "Barrel.A"}, level=10, name=f"Gun {i}"))
    store(build(gear=[gun_entry(parts={"Barrel": "Barrel.B"}, level=12, name=f"Gun {i}") for i in range(count)]),
          overwrite=True)
    return {"name": "Test build"}


def in_tx(fn: Callable[[], Any]) -> tuple[Any, journal.Transaction]:
    """Runs one request the way the route registry does: inside its own journal transaction."""
    with journal.transaction("/api/loadouts/apply") as tx:
        reply = fn()
    return reply, tx


def test_apply_runs_in_chunks_each_its_own_journal_transaction(save, stored_in_tmp):
    request = many_guns(save, 25)  # 50 steps: chunks of 20, 20, 10
    result = preview(request["name"])
    first, tx1 = in_tx(lambda: loadouts.apply({**request, "plan_id": result["plan_id"]}))
    assert (first["state"], first["cursor"], first["total"], first["chunk"]) == ("running", 20, 50, 1)
    assert first["summary"]["ok"] == 20 and first["summary"]["pending"] == 30
    assert tx1.outcome == "committed" and tx1.label == "Build 'Test build': chunk 1/3"
    replies = [first]
    while replies[-1]["state"] == "running":
        reply, tx = in_tx(lambda: loadouts.apply({"run_id": first["run_id"], "cursor": replies[-1]["cursor"]}))
        assert tx.outcome == "committed"
        replies.append(reply)
    assert [r["cursor"] for r in replies] == [20, 40, 50]
    assert replies[-1]["state"] == "done" and replies[-1]["summary"]["ok"] == 50
    assert [c["status"] for c in replies[-1]["chunks"]] == ["applied"] * 3
    assert all(item.DefinitionData.BarrelPartDefinition.path == "Barrel.B" for item, _ in save.items)


def test_a_chunk_stops_starting_steps_once_its_time_is_up(save, stored_in_tmp, monkeypatch):
    request = many_guns(save, 5)
    result = preview(request["name"])
    ticks = iter(range(0, 10_000))
    monkeypatch.setattr(loadouts, "_clock", lambda: next(ticks) * 0.7)  # every look at the clock is 0.7 s later
    reply = loadouts.apply({**request, "plan_id": result["plan_id"]})
    assert reply["state"] == "running" and 1 <= reply["cursor"] < 4


def test_a_continuation_with_the_wrong_cursor_or_run_is_refused(save, stored_in_tmp):
    request = many_guns(save, 15)
    first = loadouts.apply({**request, "plan_id": preview(request["name"])["plan_id"]})
    with pytest.raises(LoadoutError) as caught:
        loadouts.apply({"run_id": first["run_id"], "cursor": 0})
    assert caught.value.status == 409
    with pytest.raises(LoadoutError) as caught:
        loadouts.apply({"run_id": "nope", "cursor": 20})
    assert caught.value.status == 404
    assert loadouts.run_route({"run_id": first["run_id"]})["cursor"] == 20


def test_a_failure_in_a_later_chunk_rolls_back_only_that_chunk_and_points_to_history(save, stored_in_tmp):
    request = many_guns(save, 15)  # 30 steps: chunk 1 = 1-20, chunk 2 = 21-30
    first, tx1 = in_tx(lambda: loadouts.apply({**request, "plan_id": preview(request["name"])["plan_id"]}))
    calls = len(save.calls)
    save.explode = lambda name, params: name == "set_level" and params["id"] == "Guns.Pistol.12"  # step 26
    second, tx2 = in_tx(lambda: loadouts.apply({"run_id": first["run_id"], "cursor": 20}))
    assert tx1.outcome == "committed" and tx2.outcome == "aborted"
    assert second["state"] == "failed" and second["done"] is True and second["rolled_back"] is True
    steps = second["steps"]
    assert {s["status"] for s in steps[:20]} == {"ok"}
    assert {s["status"] for s in steps[20:25]} == {"rolled_back"} and steps[25]["status"] == "failed"
    assert {s["status"] for s in steps[26:]} == {"not_run"}
    assert [c["status"] for c in second["chunks"]] == ["applied", "rolled_back"]
    assert "chunk 1 (steps 1-20) stay applied" in second["message"] and "History" in second["message"]
    undo = [params for _name, params in save.calls[calls:]][6:]  # 5 forward ok + 1 failed, then the undos
    assert len(undo) == 5  # only chunk 2 is undone
    assert save._item("Guns.Pistol.0").DefinitionData.BarrelPartDefinition.path == "Barrel.B"
    assert save._item("Guns.Pistol.10").DefinitionData.BarrelPartDefinition.path == "Barrel.A"


def test_a_change_between_chunks_stops_the_run_without_writing(save, stored_in_tmp):
    request = many_guns(save, 15)
    first = loadouts.apply({**request, "plan_id": preview(request["name"])["plan_id"]})
    calls = len(save.calls)
    save._item("Guns.Pistol.14").ExpLevel = 5  # the player changed a build gun between two chunks
    second = loadouts.apply({"run_id": first["run_id"], "cursor": 20})
    assert len(save.calls) == calls
    assert second["state"] == "stopped" and second["ok"] is False
    assert second["summary"]["not_run"] == 10 and "game changed" in second["message"]
    with pytest.raises(LoadoutError) as caught:
        loadouts.apply({"run_id": first["run_id"], "cursor": 20})
    assert caught.value.status == 409


def test_a_level_step_shows_its_xp_side_effect_and_xp_is_part_of_the_plan_id(save, stored_in_tmp, monkeypatch):
    monkeypatch.setattr(character, "_owners", lambda: {"pc": object()})
    monkeypatch.setattr(character, "_xp_required", lambda _owners, level: (xp_for(level), "test"))
    store(build(character={"level": 20}), overwrite=True)
    result = preview("Test build")
    (step,) = result["steps"]
    assert step["kind"] == "character.level"
    assert step["side_effect"] == "Experience also changes (now 1,000): it becomes 2,000, the minimum for level 20"
    save.character["xp"] += 5  # only XP moved: the preview no longer matches
    assert preview("Test build")["plan_id"] != result["plan_id"]


def test_the_xp_side_effect_mentions_the_build_xp_step_when_there_is_one(save, stored_in_tmp):
    store(build(character={"level": 20, "xp": 2500}), overwrite=True)
    steps = preview("Test build")["steps"]
    assert [s["kind"] for s in steps] == ["character.level", "character.xp"]
    assert steps[0]["side_effect"].endswith("the next step then sets the build's XP")
    assert steps[1]["side_effect"] is None


def test_a_build_whose_link_would_not_fit_the_post_limit_is_refused(monkeypatch):
    monkeypatch.setattr(loadouts, "LINK_LIMIT", 300)
    with pytest.raises(LoadoutError, match="too big to share"):
        loadouts.validate(build(gear=[gun_entry(name=f"Gun {i}") for i in range(6)]))
    assert len(loadouts.to_link(loadouts.validate(build()))) <= 300


def test_every_stored_build_fits_an_import_link():
    big = build(gear=[gun_entry(name=f"Gun {i}", parts={s: f"GD_Weapons.Part_{s}_{i}" for s in gear_parts.WEAPON_SLOTS})
                      for i in range(loadouts.MAX_GEAR)])
    try:
        preset = loadouts.validate(big)
    except LoadoutError as ex:
        assert "too big to share" in str(ex)
    else:
        assert len(loadouts.to_link(preset)) <= loadouts.LINK_LIMIT < loadouts.IMPORT_LINK_LIMIT


def test_a_partial_save_asks_first_and_is_marked_partial(save, stored_in_tmp):
    save.character["torgue"] = None  # unreadable
    answer = loadouts.save({"name": "Partial", "include": {"currencies": True}})
    assert answer["saved"] is False and answer["partial"] is True
    assert any("Torgue Tokens" in w for w in answer["warnings"])
    assert loadouts.list_presets({})["presets"] == []
    answer = loadouts.save({"name": "Partial", "include": {"currencies": True}, "allow_partial": True})
    assert answer["saved"] is True and answer["preset"]["partial"] is True
    assert any("Torgue Tokens" in note for note in answer["preset"]["left_out"])
    assert "left_out" in loadouts.get_preset({"name": "Partial"})["preset"]


def test_left_out_is_validated():
    with pytest.raises(LoadoutError, match="left_out"):
        loadouts.validate(build(left_out="nope"))
    with pytest.raises(LoadoutError, match="left_out"):
        loadouts.validate(build(left_out=["x"] * 21))
    assert loadouts.validate(build(left_out=["skills left out"]))["left_out"] == ["skills left out"]


# --- review round 2 ------------------------------------------------------------------------------


def test_a_setter_that_writes_and_then_raises_is_put_back_in_both_modes(save, stored_in_tmp, monkeypatch):
    for on_failure in ("rollback", "keep"):
        save.character.update(level=10, xp=xp_for(10), skill_points=3)
        store(build(character={"skill_points": 5, "xp": 4321}), overwrite=True)
        real = save.set_field

        def writes_then_raises(params: dict[str, Any]) -> dict[str, Any]:
            if params["field"] == "xp" and params["value"] == 4321:
                save.character["xp"] = 4321  # the write lands...
                raise character.CharacterError(500, "the pool setter blew up")  # ...and then the setter raises
            return real(params)

        monkeypatch.setattr(character, "set_field", writes_then_raises)
        result = preview("Test build")
        assert [s["kind"] for s in result["steps"]] == ["character.xp", "character.skill_points"]
        report = loadouts.apply({"name": "Test build", "plan_id": result["plan_id"], "on_failure": on_failure})
        assert statuses(report) == ["failed", "not_run"], on_failure
        assert "that was put back" in report["steps"][0]["error"]
        assert save.character["xp"] == xp_for(10)  # the stray write is gone, whatever on_failure said


def test_a_stray_write_that_cannot_be_put_back_is_rollback_failed(save, stored_in_tmp, monkeypatch):
    store(build(character={"xp": 4321}), overwrite=True)
    real = save.set_field

    def stuck(params: dict[str, Any]) -> dict[str, Any]:
        if params["field"] == "xp":
            save.character["xp"] = 4321
            raise character.CharacterError(500, "the pool setter blew up")
        return real(params)

    monkeypatch.setattr(character, "set_field", stuck)
    report = loadouts.apply({"name": "Test build", "plan_id": preview("Test build")["plan_id"]})
    assert statuses(report) == ["rollback_failed"]
    assert report["steps"][0]["error"].endswith("reload the save before playing")


def test_a_target_that_cannot_be_read_back_counts_as_changed(save, stored_in_tmp, monkeypatch):
    store(build(character={"skill_points": 5}), overwrite=True)
    result = preview("Test build")
    save.explode = lambda name, _params: name == "character.set_field"
    monkeypatch.setattr(loadouts, "_read_target", lambda _step, _ids: (_ for _ in ()).throw(RuntimeError("no pri")))
    report = loadouts.apply({"name": "Test build", "plan_id": result["plan_id"]})
    assert statuses(report) == ["rollback_failed"]  # it couldn't be verified, so it is not reported as clean


def test_earlier_chunks_point_to_history_only_when_the_journal_lists_them(save, stored_in_tmp, monkeypatch):
    for listed in (False, True):
        save.items.clear()
        save.explode = None
        request = many_guns(save, 15)
        first = loadouts.apply({**request, "plan_id": preview(request["name"])["plan_id"]})
        entries = [{"label": "Build 'Test build': chunk 1/2", "records": []}] if listed else []
        monkeypatch.setattr(journal, "entries", lambda e=entries: e)
        save.explode = lambda name, params: name == "set_level" and params["id"] == "Guns.Pistol.12"
        second = loadouts.apply({"run_id": first["run_id"], "cursor": 20})
        assert second["history"] is listed
        assert second["chunks"][0]["in_history"] is listed
        if listed:
            assert "History lists them, so you can undo them there" in second["message"]
        else:
            assert "can't find them in History" in second["message"] and "before and after" in second["message"]
        applied = [s for s in second["steps"] if s["status"] == "ok"]
        assert len(applied) == 20 and all(s["before"] is not None and s["after"] is not None for s in applied)


def test_the_stub_journal_never_claims_history(save, stored_in_tmp):
    request = many_guns(save, 15)
    first = loadouts.apply({**request, "plan_id": preview(request["name"])["plan_id"]})
    save._item("Guns.Pistol.14").ExpLevel = 5
    second = loadouts.apply({"run_id": first["run_id"], "cursor": 20})
    assert journal.entries() == [] and second["history"] is False
    assert "can't find them in History" in second["message"]


def test_an_unreadable_state_after_a_chunk_stops_the_run_at_once(save, stored_in_tmp, monkeypatch):
    request = many_guns(save, 15)
    result = preview(request["name"])
    first = loadouts.apply({**request, "plan_id": result["plan_id"]})
    run = loadouts._runs[first["run_id"]]
    assert first["state"] == "running" and isinstance(run.basis, str) and not run.basis.startswith("unreadable")
    monkeypatch.setattr(loadouts, "_basis", lambda _sections: (_ for _ in ()).throw(RuntimeError("chain broke")))
    calls = len(save.calls)
    second = loadouts.apply({"run_id": first["run_id"], "cursor": 20})
    assert second["state"] == "stopped" and len(save.calls) == calls
    assert "couldn't be read" in second["message"]


def test_the_same_read_error_twice_never_lets_a_run_continue(save, stored_in_tmp, monkeypatch):
    request = many_guns(save, 25)
    result = preview(request["name"])
    real = loadouts._basis
    reads = {"n": 0}

    def flaky(sections: list[str]) -> str:
        reads["n"] += 1
        if reads["n"] >= 2:  # the plan's read works; the read after chunk 1 fails
            raise RuntimeError("chain broke")
        return real(sections)

    monkeypatch.setattr(loadouts, "_basis", flaky)
    first = loadouts.apply({**request, "plan_id": result["plan_id"]})
    assert first["state"] == "stopped" and first["summary"]["ok"] == 20 and first["summary"]["not_run"] == 30
    assert "couldn't be read afterwards" in first["message"]


def test_a_run_in_progress_is_never_evicted(save, stored_in_tmp, monkeypatch):
    monkeypatch.setattr(loadouts, "MAX_RUNS", 2)
    request = many_guns(save, 25)  # 50 steps: each new run still has chunks left
    plan_id = preview(request["name"])["plan_id"]
    first = loadouts.apply({**request, "plan_id": plan_id})
    second = loadouts.apply({**request, "plan_id": preview(request["name"])["plan_id"]})
    assert first["state"] == second["state"] == "running"
    with pytest.raises(LoadoutError) as caught:
        loadouts.apply({**request, "plan_id": preview(request["name"])["plan_id"]})
    assert caught.value.status == 409 and "still being applied" in str(caught.value)
    assert loadouts.run_route({"run_id": first["run_id"]})["cursor"] == 20  # both reports survive
    loadouts._runs[first["run_id"]].state = "done"  # once one is finished, its slot can be reused
    third = loadouts.apply({**request, "plan_id": preview(request["name"])["plan_id"]})
    assert third["run_id"] in loadouts._runs and first["run_id"] not in loadouts._runs


def test_expiry_is_enforced_on_lookup(save, stored_in_tmp, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(loadouts, "_clock", lambda: now["t"])
    request = many_guns(save, 15)
    first = loadouts.apply({**request, "plan_id": preview(request["name"])["plan_id"]})
    now["t"] += loadouts.RUN_TTL_SECONDS + 1
    with pytest.raises(LoadoutError) as caught:
        loadouts.run_route({"run_id": first["run_id"]})
    assert caught.value.status == 404 and first["run_id"] not in loadouts._runs


# --- final round: XP around a level step -------------------------------------------------------


def level_scenario(save: FakeSave) -> str:
    """XP 1003 at level 10 (3 above the level's minimum); the build raises the level, the skill points and a gun,
    and the gun's step fails, so the level step has to be rolled back."""
    save.character.update(level=10, xp=1003, skill_points=3)
    save.add(make_gun("Guns.Pistol.A", parts={"Barrel": "Barrel.A"}, level=10))
    store(build(character={"level": 12, "skill_points": 5}, gear=[gun_entry(parts={"Barrel": "Barrel.B"})]),
          overwrite=True)
    save.explode = lambda name, _params: name == "set_part"
    return preview("Test build")["plan_id"]


def test_rolling_back_a_level_step_restores_and_verifies_the_exact_xp(save, stored_in_tmp):
    report = loadouts.apply({"name": "Test build", "plan_id": level_scenario(save)})
    assert statuses(report) == ["rolled_back", "rolled_back", "failed"]
    assert (save.character["level"], save.character["xp"]) == (10, 1003)
    assert report["summary"]["rollback_failed"] == 0


def test_a_level_step_whose_xp_cannot_be_read_first_is_refused_without_writing(save, stored_in_tmp, monkeypatch):
    # The reviewer's reproduction: XP can't be read just before the level write, while the setter itself works.
    plan_id = level_scenario(save)
    real = loadouts._read_xp
    reads = {"n": 0}

    def first_read_fails() -> int:
        reads["n"] += 1
        if reads["n"] == 1:
            raise character.CharacterError(503, "the experience pool is busy")
        return real()

    monkeypatch.setattr(loadouts, "_read_xp", first_read_fails)
    report = loadouts.apply({"name": "Test build", "plan_id": plan_id})
    assert statuses(report) == ["failed", "not_run", "not_run"]
    assert "nothing was written" in report["steps"][0]["error"]
    assert not any(name == "character.set_field" for name, _params in save.calls)
    assert (save.character["level"], save.character["xp"]) == (10, 1003)  # not 1000
    assert report["summary"]["rollback_failed"] == 0


def test_xp_that_cannot_be_verified_after_a_level_rollback_is_rollback_failed(save, stored_in_tmp, monkeypatch):
    plan_id = level_scenario(save)
    real = loadouts._read_xp
    reads = {"n": 0}

    def verify_fails() -> int:
        reads["n"] += 1
        if reads["n"] > 1:  # the read before the write works; the check after the rollback doesn't
            raise character.CharacterError(503, "the experience pool is busy")
        return real()

    monkeypatch.setattr(loadouts, "_read_xp", verify_fails)
    report = loadouts.apply({"name": "Test build", "plan_id": plan_id})
    assert statuses(report) == ["rollback_failed", "rolled_back", "failed"]
    assert report["summary"]["rollback_failed"] == 1
    assert report["steps"][0]["error"].endswith("reload the save before playing")


def test_an_unavailable_skill_tree_fingerprints_a_fixed_marker(save, monkeypatch):
    monkeypatch.setattr(skills, "read", lambda: {"available": False, "reason": "busy at 12:00:01", "skills": []})
    first = loadouts._basis(["skills"])
    monkeypatch.setattr(skills, "read", lambda: {"available": False, "reason": "busy at 12:00:02", "skills": []})
    assert loadouts._basis(["skills"]) == first
