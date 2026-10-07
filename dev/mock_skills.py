"""Mock of the skill tree routes, for dev/mock_server.py (auto-loaded as dev/mock_skills.py).

A made-up tree for one class (Zer0 / Assassin: Sniping, Cunning, Bloodshed). The skill names, tiers and
maximum ranks are from memory and only roughly right; they exist to give the page realistic data. Tiers
unlock at 5 points per row in the tiers below, like the game's menu, but setting a level here ignores
that, like the real route is meant to. Open /api/skills?unavailable=1 to see the "tree can't be read" state.
"""

from __future__ import annotations

TIER_UNLOCK = 5

# branch -> label -> tiers, each a list of (skill, max rank)
TREE = {
    "sniping": ("Sniping", [
        [("Fast Hands", 5), ("Optics", 5)],
        [("Headsplosion", 5), ("Velocity", 5)],
        [("Precision", 5), ("Ambush", 5), ("Bore", 1)],
        [("Rising Shot", 5), ("Sniper's Mark", 5)],
        [("Kill Confirmed", 5), ("Counter Strike", 5)],
        [("One Shot One Kill", 1)],
    ]),
    "cunning": ("Cunning", [
        [("Fast Hands II", 5), ("Followthrough", 5)],
        [("Backstab", 5), ("Pot Shot", 5)],
        [("Unseen", 5), ("Innervate", 5)],
        [("Fearless", 5), ("Death Mark", 1)],
        [("Rising Shot II", 5), ("Silence", 5)],
        [("Last Gasp", 1)],
    ]),
    "bloodshed": ("Bloodshed", [
        [("Bloodlust", 5), ("Maim", 5)],
        [("Execute", 5), ("Be Like Water", 5)],
        [("Many Must Fall", 5), ("Unforeseen", 5)],
        [("Death Blossom", 5), ("Shadow Strike", 5)],
        [("Iron Hand", 5), ("Followthrough II", 5)],
        [("Ghost", 1)],
    ]),
}

SKILLS: list[dict] = []
for _key, (_label, _tiers) in TREE.items():
    for _row, _tier in enumerate(_tiers):
        for _name, _max in _tier:
            SKILLS.append({"id": len(SKILLS), "branch": _key, "branch_label": _label, "row": _row, "name": _name, "max": _max,
                           "path": f"GD_Assassin_Skills.{_label}.{_name.replace(' ', '').replace(chr(39), '')}"})

# a plausible build: points per skill id, so a couple of tiers are locked
LEVELS: dict[int, int] = {}
for _skill in SKILLS:
    LEVELS[_skill["id"]] = 0
for _name, _points in (
    ("Fast Hands", 5), ("Optics", 5), ("Headsplosion", 5), ("Velocity", 4), ("Precision", 5), ("Bore", 1),
    ("Fast Hands II", 3), ("Followthrough", 2), ("Backstab", 1),
    ("Bloodlust", 5), ("Maim", 3), ("Execute", 0),
):
    LEVELS[next(s["id"] for s in SKILLS if s["name"] == _name)] = _points


def _skills() -> list[dict]:
    out = []
    for skill in SKILLS:
        below = sum(LEVELS[s["id"]] for s in SKILLS if s["branch"] == skill["branch"] and s["row"] < skill["row"])
        requires = skill["row"] * TIER_UNLOCK
        out.append({**skill, "level": LEVELS[skill["id"]], "requires": requires, "locked": below < requires, "grade_via": "field:Grade"})
    return out


def _state(shared: dict, unavailable: bool = False) -> dict:
    base = {
        "class_key": "assassin", "skill_points": shared["CHAR"]["skill_points"],
        "limits": {"skill_points": 999, "tier_unlock": TIER_UNLOCK},
        "sources": {"tree": "pc.PlayerSkillTree", "list": "Skills", "skill_points": "pri.GeneralSkillPoints"},
    }
    if unavailable:
        return {**base, "available": False, "reason": "found pc.PlayerSkillTree but couldn't find a list of skills in it - run the skills dump",
                "skills": [], "branches": [], "spent": 0, "sources": {**base["sources"], "tree": None, "list": None}}
    skills = _skills()
    branches = [
        {"key": key, "label": label, "points": sum(s["level"] for s in skills if s["branch"] == key), "rows": len(tiers)}
        for key, (label, tiers) in TREE.items()
    ]
    return {**base, "available": True, "reason": None, "skills": skills, "branches": branches, "spent": sum(LEVELS.values())}


def handle(path: str, params: dict, shared: dict) -> dict | None:
    if path == "/api/skills":
        return _state(shared, params.get("unavailable") == "1")
    if path == "/api/skills/set":
        try:
            skill = SKILLS[int(params["id"])]
            level = int(params["level"])
        except (KeyError, TypeError, IndexError):
            raise ValueError("unknown skill") from None
        if not 0 <= level <= skill["max"]:
            raise ValueError(f"{skill['name']} must be 0-{skill['max']}")
        before = LEVELS[skill["id"]]
        LEVELS[skill["id"]] = level  # no tier check, on purpose
        applied = {"name": skill["name"], "requested": level, "via": "entry.Grade", "notified": "pc.NotifySkillRankChanged", "before": before, "after": level}
        return {**_state(shared), "applied": applied}
    if path == "/api/skills/reset":
        if params.get("confirm") is not True:
            raise ValueError('resetting the skill tree needs {"confirm": true}')
        spent = sum(LEVELS.values())
        before_points = shared["CHAR"]["skill_points"]
        for key in LEVELS:
            LEVELS[key] = 0
        shared["CHAR"]["skill_points"] = min(before_points + spent, 999)
        applied = {"via": "pc.ResetSkillTree", "before": spent, "after": 0, "refunded": spent, "refund_via": "pri.GeneralSkillPoints", "skill_points": shared["CHAR"]["skill_points"]}
        return {**_state(shared), "applied": applied}
    if path == "/api/debug/skills":
        return {"saved_to": "(mock)", "found": "pc.PlayerSkillTree", "skills": len(SKILLS), "reason": None}
    return None
