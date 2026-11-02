"""dev/mock_lab.py: the Weapon lab mock. Hits come from a fake clock, so these run the same every time."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[1]


@pytest.fixture
def lab(monkeypatch):
    """A fresh copy of the mock module, with its clock replaced by a number the test moves by hand."""
    spec = importlib.util.spec_from_file_location("mock_lab_under_test", DEV / "mock_lab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = {"now": 100.0}
    monkeypatch.setattr(module, "_clock", lambda: clock["now"])
    return module, clock


def test_start_then_hits_appear_as_the_clock_advances(lab):
    mod, clock = lab
    state = mod.handle("/api/lab/session/start", {"cap": 2000}, {})
    assert state["session"]["active"] is True
    clock["now"] += 2.5
    reply = mod.handle("/api/lab/hits", {"after": "-1"}, {})
    assert reply["session"] == 1
    assert reply["hits"], "2.5 s at 8 shots a second should land some hits"
    seqs = [hit["seq"] for hit in reply["hits"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert reply["last"] == seqs[-1]
    state = mod.handle("/api/lab", {}, {})
    assert state["session"]["shots"] == 21  # shots at 0, 1/8, ... 2.5 s: int(2.5 * 8) + 1
    assert state["summary"]["hits"] == len(reply["hits"])
    assert state["summary"]["targets"], "the target list drives the filter in the page"


def test_hits_after_a_seq_only_return_newer_ones(lab):
    mod, clock = lab
    mod.handle("/api/lab/session/start", {}, {})
    clock["now"] += 3.0
    everything = mod.handle("/api/lab/hits", {}, {})["hits"]
    newer = mod.handle("/api/lab/hits", {"after": str(everything[2]["seq"])}, {})["hits"]
    assert [h["seq"] for h in newer] == [h["seq"] for h in everything[3:]]


def test_stop_freezes_the_session(lab):
    mod, clock = lab
    mod.handle("/api/lab/session/start", {}, {})
    clock["now"] += 1.0
    stopped = mod.handle("/api/lab/session/stop", {}, {})
    assert stopped["session"]["active"] is False
    recorded = stopped["session"]["recorded"]
    clock["now"] += 10.0
    later = mod.handle("/api/lab", {}, {})
    assert later["session"]["recorded"] == recorded
    assert later["session"]["shots"] == stopped["session"]["shots"]


def test_cap_keeps_only_the_newest_hits(lab):
    mod, clock = lab
    mod.handle("/api/lab/session/start", {"cap": 10}, {})
    clock["now"] += 10.0
    state = mod.handle("/api/lab", {}, {})
    session = state["session"]
    assert session["kept"] == 10
    assert session["dropped"] == session["recorded"] - 10
    newest = mod.handle("/api/lab/hits", {"after": "-1", "limit": "2000"}, {})["hits"]
    assert len(newest) == 10 and newest[-1]["seq"] == session["recorded"] - 1


def test_start_rejects_a_cap_out_of_range(lab):
    mod, _ = lab
    with pytest.raises(ValueError):
        mod.handle("/api/lab/session/start", {"cap": 5}, {})


def test_compare_without_snapshot_a_is_a_lookup_error(lab):
    mod, _ = lab
    with pytest.raises(LookupError):
        mod.handle("/api/lab/compare", {}, {})


def test_compare_reports_changed_rows_and_parts(lab, monkeypatch):
    mod, _ = lab
    item = copy.deepcopy(mod.FALLBACK)
    monkeypatch.setattr(mod, "_detail", lambda _id: copy.deepcopy(item))
    mod.handle("/api/lab/snapshot", {"id": item["id"], "slot": "a"}, {})
    item["stats"][0]["value"] = 90.0  # InstantHitDamage, up from 76.9
    item["slots"][1]["current"] = {"id": "GD_Fallback.Grip.Grip_B", "name": "Grip_B"}
    compare = mod.handle("/api/lab/compare", {"live": "1"}, {})
    assert compare["b_live"] is True
    rows = {row["name"]: row for row in compare["rows"]}
    assert rows["InstantHitDamage"]["changed"] is True
    assert rows["InstantHitDamage"]["delta"] == pytest.approx(13.1)
    assert rows["ClipSize"]["changed"] is False
    assert rows["est_shot_damage"]["estimated"] is True
    assert rows["est_card_dps"]["a"] == pytest.approx(76.9 * 1 / 0.5)
    assert any(p["slot"] == "Grip" and p["changed"] for p in compare["parts"])


def test_compare_with_snapshot_b_uses_b(lab):
    mod, _ = lab
    mod.handle("/api/lab/snapshot", {"id": mod.FALLBACK["id"], "slot": "a"}, {})
    state = mod.handle("/api/lab/snapshot", {"id": mod.FALLBACK["id"], "slot": "b"}, {})
    assert state["snapshots"]["b"]["slot"] == "b"
    assert mod.handle("/api/lab/compare", {}, {})["b_live"] is False


def test_unknown_paths_are_not_ours_and_unknown_lab_paths_are_404(lab):
    mod, _ = lab
    assert mod.handle("/api/items", {}, {}) is None
    assert mod.handle("/api/sdu", {}, {}) is None
    assert mod.handle("/api/debug/lab", {}, {}) == {"saved_to": "(mock) rtse/debug_lab.json"}
    with pytest.raises(LookupError):
        mod.handle("/api/lab/nope", {}, {})
