"""Pulls the real skill trees out of the installed game (read-only) into rtse/skilltrees.json.

    python -I dev/extract_skills.py

Reads each class's GD_<Class>_Streaming_SF.upk: the SkillTreeDefinition (root action skill + three branches),
each branch's tiers (skills + points needed to open the next tier), its layout (which of 3 cells are used) and
each SkillDefinition's name, description, max rank and icon.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import upk  # noqa: E402

GAME = Path("C:/Program Files (x86)/Steam/steamapps/common/Borderlands 2")
OUT = Path(__file__).resolve().parent.parent / "rtse" / "skilltrees.json"
CLASSES = {  # key -> package, relative to the game folder
    "assassin": "WillowGame/CookedPCConsole/GD_Assassin_Streaming_SF.upk",
    "siren": "WillowGame/CookedPCConsole/GD_Siren_Streaming_SF.upk",
    "soldier": "WillowGame/CookedPCConsole/GD_Soldier_Streaming_SF.upk",
    "mercenary": "WillowGame/CookedPCConsole/GD_Mercenary_Streaming_SF.upk",
    "mechromancer": "DLC/Tulip/Compat/Content/GD_Tulip_Mechro_Streaming_SF.upk",
    "psycho": "DLC/Lilac/Compat/Content/GD_Lilac_Psycho_Streaming_SF.upk",
}
DEFAULT_MAX_GRADE = 1  # SkillDefinitions that don't store MaxGrade; the class default is not in these packages (assumed)


def clean(text: str) -> str:
    """Drops the [skill]..[-skill] markup, turns <StringAliasMap:Action.ActionSkill> into [ActionSkill], tidies spaces."""
    text = re.sub(r"\[/?-?\w+\]", "", text)
    text = re.sub(r"<StringAliasMap:(?:[\w]+\.)*(\w+)>", r"[\1 key]", text)
    return re.sub(r"\s+", " ", text).strip()


def refs(pkg: upk.Package, value: Any) -> list[str]:
    ints = value.get("__ints", []) if isinstance(value, dict) else []
    return [pkg.object_name(i) for i in ints]


def extract(key: str, path: str) -> dict[str, Any]:
    pkg = upk.load(GAME / path)
    by_name = {pkg.object_name(i): e for i, e in enumerate(pkg.exports, start=1)}
    by_class: dict[str, list[str]] = {}
    for name, e in by_name.items():
        by_class.setdefault(pkg.class_name(e).split(".")[-1], []).append(name)

    tree_name = by_class["SkillTreeDefinition"][0]
    root_name = pkg.properties(by_name[tree_name])["Root"]
    skills: dict[str, dict[str, Any]] = {}

    def skill(name: str) -> str:
        if name not in skills:
            p = pkg.properties(by_name[name]) if name in by_name else {}
            skills[name] = {
                "id": name,
                "name": p.get("SkillName", name.rsplit(".", 1)[-1]),
                "description": clean(p.get("SkillDescription", "")),
                "max": p.get("MaxGrade", DEFAULT_MAX_GRADE),
                "max_is_default": "MaxGrade" not in p,
                "icon": p.get("SkillIcon"),
            }
        return name

    def branch(name: str) -> dict[str, Any]:
        p = pkg.properties(by_name[name])
        layout = by_name.get(p["Layout"]) if "Layout" in p else None
        cells = [t["bCellIsOccupied"]["__bytes"] for t in pkg.properties(layout)["Tiers"]] if layout else []
        tiers = []
        for index, tier in enumerate(p.get("Tiers", [])):
            names = [n for n in refs(pkg, tier.get("Skills")) if n != "None"]
            row = cells[index] if index < len(cells) else [1] * len(names)
            # skills fill the occupied cells left to right; any beyond that are hidden helper skills (status detectors etc.)
            placed, it = [None] * len(row), iter(names)
            for col, used in enumerate(row):
                if used:
                    placed[col] = skill(next(it, "None")) if names else None
            hidden = [skill(n) for n in it]
            tiers.append({"unlock": tier.get("PointsToUnlockNextTier", 0), "cells": placed, "hidden": hidden})
        return {"id": name, "name": p.get("BranchName", name.rsplit(".", 1)[-1]), "tiers": tiers, "children": refs(pkg, p.get("Children"))}

    root = branch(root_name)
    branches = [branch(child) for child in root["children"]]
    return {
        "class": key,
        "tree": tree_name,
        "action": next((c for c in root["tiers"][0]["cells"] if c), None) if root["tiers"] else None,
        "branches": branches,
        "skills": skills,
        "source": path,
    }


def main() -> None:
    trees = {}
    for key, path in CLASSES.items():
        trees[key] = extract(key, path)
        t = trees[key]
        print(key, [(b["name"], sum(len([c for c in tier["cells"] if c]) for tier in b["tiers"])) for b in t["branches"]], "action:", t["action"])
    OUT.write_text(json.dumps({"generated_from": "installed Borderlands 2 packages", "trees": trees}, indent=1), encoding="utf-8")
    print("wrote", OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
