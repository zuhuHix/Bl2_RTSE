"""Mock of the skill tree routes, for dev/mock_server.py (auto-loaded as dev/mock_skills.py).

Serves the REAL skill trees (names, tiers, cells, descriptions, max ranks) that dev/extract_skills.py pulled out
of the installed game into rtse/skilltrees.json, with invented points. Only the live grades are made up. The
class is Zer0 / Assassin unless /api/skills?class=siren (soldier, mercenary, mechromancer, psycho) switches it;
the choice sticks. Tiers lock like the game's menu does, but setting a level here ignores that, like the real
route is meant to. /api/skills?unavailable=1 shows the "tree can't be read" state.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

_spec = importlib.util.spec_from_file_location("skilltree_data", Path(__file__).resolve().parent.parent / "rtse" / "skilltree_data.py")
data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(data)

active = os.environ.get("MOCK_SKILLS_CLASS", "assassin")
LEVELS: dict[str, dict[str, int]] = {}  # class -> skill path -> points


def _paths(tree: dict) -> list[str]:
    """Every skill the live list would hold: the action skill, each tier's skills, then the hidden helpers."""
    out = [tree["action"]] if tree["action"] else []
    for branch in tree["branches"]:
        for tier in branch["tiers"]:
            out += [c for c in tier["cells"] if c]
    for branch in tree["branches"]:
        for tier in branch["tiers"]:
            out += tier.get("hidden", [])
    return out


def _levels(key: str) -> dict[str, int]:
    """A plausible build: the first two tiers of the first branch full, a few points elsewhere, so some tiers are locked."""
    if key not in LEVELS:
        tree = data.trees()[key]
        levels = {p: 0 for p in _paths(tree)}
        if tree["action"]:
            levels[tree["action"]] = 1
        for b, branch in enumerate(tree["branches"]):
            for t, tier in enumerate(branch["tiers"]):
                for path in (c for c in tier["cells"] if c):
                    top = tree["skills"][path]["max"]
                    if b == 0 and t < 3:
                        levels[path] = top
                    elif b == 1 and t < 2:
                        levels[path] = min(top, 3)
                    elif b == 2 and t == 0:
                        levels[path] = min(top, 2)
        LEVELS[key] = levels
    return LEVELS[key]


def _state(shared: dict, unavailable: bool = False) -> dict:
    tree = data.trees()[active]
    base = {
        "class_key": active, "skill_points": shared["CHAR"]["skill_points"],
        "limits": {"skill_points": 999, "tier_unlock": 5},
        "sources": {"tree": "pc.PlayerSkillTree", "list": "Skills", "skill_points": "pri.GeneralSkillPoints"},
    }
    if unavailable:
        return {**base, "available": False, "reason": "found pc.PlayerSkillTree but couldn't find a list of skills in it - run the skills dump",
                "skills": [], "branches": [], "spent": 0, "sources": {**base["sources"], "tree": None, "list": None}}
    levels = _levels(active)
    skills = [
        {"id": i, "path": path, "name": tree["skills"][path]["name"], "level": levels[path], "max": tree["skills"][path]["max"], "grade_via": "field:Grade"}
        for i, path in enumerate(_paths(tree))
    ]
    for skill in skills:
        skill.update(description=None, icon=None, col=None, action=False, hidden=False, row=None, requires=None, locked=None)
    layout = data.apply(tree, skills)
    branches = [{"key": b["key"], "label": b["label"], "points": b["points"], "rows": len(b["tiers"])} for b in layout["branches"]]
    return {**base, "available": True, "reason": None, "skills": skills, "tree": layout, "branches": branches, "spent": sum(levels.values()) - (1 if tree["action"] else 0)}


def handle(path: str, params: dict, shared: dict) -> dict | None:
    global active
    if path == "/api/skills":
        if params.get("class") in data.trees():
            active = params["class"]
        return _state(shared, params.get("unavailable") == "1")
    if path == "/api/skills/set":
        tree = data.trees()[active]
        paths = _paths(tree)
        try:
            skill_path = paths[int(params["id"])]
            level = int(params["level"])
        except (KeyError, TypeError, IndexError):
            raise ValueError("unknown skill") from None
        info = tree["skills"][skill_path]
        if not 0 <= level <= info["max"]:
            raise ValueError(f"{info['name']} must be 0-{info['max']}")
        levels = _levels(active)
        before = levels[skill_path]
        levels[skill_path] = level  # no tier check, on purpose
        applied = {"name": info["name"], "requested": level, "via": "tree.SetSkillGrade", "notified": None, "before": before, "after": level}
        return {**_state(shared), "applied": applied}
    if path == "/api/skills/reset":
        if params.get("confirm") is not True:
            raise ValueError('resetting the skill tree needs {"confirm": true}')
        tree = data.trees()[active]
        levels = _levels(active)
        spent = sum(v for p, v in levels.items() if p != tree["action"])
        before_points = shared["CHAR"]["skill_points"]
        for key in levels:
            levels[key] = 1 if key == tree["action"] else 0
        shared["CHAR"]["skill_points"] = min(before_points + spent, 999)
        applied = {"via": "pc.ResetSkillTree", "before": spent, "after": 0, "refunded": spent, "refund_via": "pri.GeneralSkillPoints", "skill_points": shared["CHAR"]["skill_points"]}
        return {**_state(shared), "applied": applied}
    if path == "/api/debug/skills":
        return {"saved_to": "(mock)", "found": "pc.PlayerSkillTree", "skills": len(_paths(data.trees()[active])), "reason": None}
    return None
