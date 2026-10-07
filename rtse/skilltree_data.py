"""The real skill trees, as read out of the installed game's packages by dev/extract_skills.py (rtse/skilltrees.json).

No game imports here, so the mock server can load this file too. The file holds, per class: the action skill, three
branches of six tiers with up to three skills per tier (the cell each one sits in), the points needed to open each
tier, and every skill's name, description, maximum rank and icon id. Live grades come from the game; this supplies
everything around them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TREES_FILE = Path(__file__).parent / "skilltrees.json"
MIN_OVERLAP = 5  # a live skill list must share this many skills with a class's tree to count as that class

_cache: dict[str, Any] | None = None


def trees() -> dict[str, Any]:
    """Every class's tree by key (assassin, siren, ...), or {} if the data file is missing or unreadable."""
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(TREES_FILE.read_text(encoding="utf-8")).get("trees", {})
        except (OSError, ValueError):
            _cache = {}
    return _cache


def match(paths: set[str]) -> tuple[str | None, dict[str, Any] | None]:
    """The class whose tree shares the most skills with this set of skill paths (GD_Assassin_Skills.Sniping.HeadShot, ...)."""
    best_key, best = None, 0
    for key, tree in trees().items():
        overlap = len(paths & set(tree["skills"]))
        if overlap > best:
            best_key, best = key, overlap
    if best_key is None or best < MIN_OVERLAP:
        return None, None
    return best_key, trees()[best_key]


def apply(tree: dict[str, Any], skills: list[dict[str, Any]]) -> dict[str, Any]:
    """Merges a tree with the live skills (dicts with id, path, level, max, name). Fills in each skill's description, icon,
    branch, row, cell and lock state, and returns the layout: the action skill, branches > tiers > three cells."""
    by_path = {s["path"]: s for s in skills if s.get("path")}
    placed: set[str] = set()

    def level(path: str | None) -> int:
        skill = by_path.get(path) if path else None
        return (skill["level"] or 0) if skill else 0

    def annotate(path: str, **extra: Any) -> None:
        skill, info = by_path.get(path), tree["skills"][path]
        if skill is None:
            return
        placed.add(path)
        skill["description"] = info["description"]
        skill["icon"] = info["icon"]
        skill["game_name"] = info["name"]
        if skill["max"] is None:  # the live definition didn't give one; the game files do
            skill["max"] = info["max"]
            skill["max_from"] = "game files"
        skill.update(extra)

    action = tree.get("action")
    if action:
        annotate(action, branch="action", branch_label="Action skill", row=None, col=None, locked=False, requires=0, action=True)

    branches = []
    for branch in tree["branches"]:
        spent, need, tiers = 0, 0, []
        for number, tier in enumerate(branch["tiers"], start=1):
            locked = spent < need
            cells = []
            for col, path in enumerate(tier["cells"]):
                if path:
                    annotate(path, branch=branch["name"].lower(), branch_label=branch["name"], row=number - 1, col=col, locked=locked, requires=need, action=False)
                cells.append(by_path[path]["id"] if path in by_path else None)
            hidden = [path for path in tier.get("hidden", []) if path in by_path]
            for path in hidden:
                annotate(path, branch=branch["name"].lower(), branch_label=branch["name"], row=number - 1, col=None, locked=locked, requires=need, action=False, hidden=True)
            tier_points = sum(level(path) for path in [*tier["cells"], *tier.get("hidden", [])])
            tiers.append({"number": number, "need": need, "locked": locked, "points": tier_points, "cells": cells})
            spent += tier_points
            need += tier["unlock"]
        branches.append({"key": branch["name"].lower(), "label": branch["name"], "points": spent, "tiers": tiers})

    return {
        "class_key": tree["class"],
        "action": by_path[action]["id"] if action in by_path else None,
        "branches": branches,
        "unplaced": [s["id"] for s in skills if s.get("path") and s["path"] not in placed],  # live skills the tree data doesn't know
        "missing": [p for p in tree["skills"] if p not in by_path],  # tree skills the game's list doesn't have
    }
