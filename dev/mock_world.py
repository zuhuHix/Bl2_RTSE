"""Mock of the World & progress API for dev/mock_server.py. Same JSON shapes as rtse/world.py.

All names below (missions, challenges, fast-travel stations) are written from memory of Borderlands 2 and
are only roughly right: they exist to give the page realistic-looking, scroll-worthy data, not to be a
reference. Set MOCK_WORLD_FAIL=challenges,stations (any of the four section names) to see "unavailable".
"""

from __future__ import annotations

import os
import re

STATUS_LABELS = {
    "not_started": "Not started", "active": "Active", "objectives_done": "Objectives done",
    "ready": "Ready to turn in", "complete": "Complete", "failed": "Failed", "unknown": "Unknown",
}
WRITABLE = ("not_started", "active", "ready", "complete")
PLAYTHROUGHS = (("Normal", "Normal"), ("TVHM", "True Vault Hunter Mode"), ("UVHM", "Ultimate Vault Hunter Mode"))
FAIL = {s for s in os.environ.get("MOCK_WORLD_FAIL", "").split(",") if s}


def camel(name: str) -> str:
    return "".join(w.capitalize() for w in re.sub(r"[^A-Za-z0-9 ]", "", name).split())


MAIN = [
    "Wakey Wakey", "Rising Action", "Cleaning Up the Berg", "Hunting the Firehawk", "A Dam Fine Rescue", "Splinter Group",
    "Where Angels Fear to Tread", "Positive Self Image", "Shielded Favors", "Best Mother's Day Ever", "Bright Lights, Flying City",
    "The Man Who Would Be Jack", "Digging Up the Dirt", "Out of Body Experience", "Ice Cold", "Rising Tide", "The Final Piece",
    "Hunting the Firehawk II", "Won't Get Fooled Again", "The Beginning of the End", "Blood of the Ancients", "Data Mining",
]
SIDE = [
    "Best Minion Ever", "Rock, Paper, Genocide", "Claptrap's Secret Stash", "Mine, All Mine", "This Town Ain't Big Enough",
    "Gluttony", "Hidden Journals", "Do No Harm", "Medical Mystery", "The Cleansing", "Animal Rights", "Neither Rain nor Snow nor Skags",
    "Tec Support", "Rakkaholic", "Swatting Flies", "Skag Hunter", "Eridium Hunter", "Dr. Zed's Pharmacy", "Pickin' Up the Pieces",
    "Troubleshooting", "Ellie's Garage", "Bandit Slaughter: Wasteland", "Cult Following", "Gun Cleaning", "Bad Hair Day",
    "Hyperion Contract #873", "A Real Boy", "Short Fuse", "Stalker Hunt", "Sheriff of Lynchwood", "Pain in the Butt", "Chief Wanted",
]
DLC = {
    "Tiny Tina's Assault on Dragon Keep": ("gd_aster", [
        "Dragon Keep", "The Beard's Gambit", "Sorcerer's Apprentice", "Mimic of Doom", "Wedding Day Massacre", "Tina's Boss Fight"]),
    "Hammerlock's Big Game Hunt": ("gd_sage", [
        "Big Game Hunt", "Scalywag Trouble", "Savage Lands", "Hunting the Hunter", "Specimen Collection", "Pickle Nuts"]),
    "Mr. Torgue's Campaign of Carnage": ("gd_iris", [
        "Campaign of Carnage", "Flame Cleared", "Arena Warm-Up", "Round One", "Round Two", "The Final Round"]),
    "Captain Scarlett's Pirate Booty": ("gd_orchid", [
        "Pirate's Booty", "Sink or Swim", "Booty Call", "Seas of Sand", "Buried Treasure"]),
}


def build_missions() -> dict[int, list[dict]]:
    base: list[tuple[str, str, str, str]] = []
    for i, name in enumerate(MAIN):
        base.append((f"GD_Episode{i // 8 + 1:02d}.M_Ep{i + 1}_{camel(name)}", name, "main", "Main story"))
    for name in SIDE:
        base.append((f"GD_Z1_Missions.M_{camel(name)}", name, "side", "Side missions"))
    for group, (prefix, names) in DLC.items():
        for name in names:
            base.append((f"GD_{prefix[3:].title()}.M_{camel(name)}", name, "dlc", group))
    out: dict[int, list[dict]] = {}
    for pt in (1, 2, 3):
        rows = []
        for i, (mid, name, kind, group) in enumerate(base):
            if pt == 1:  # finished the first run
                status = "failed" if kind == "side" and i % 11 == 0 else "complete"
            elif pt == 2:  # partway through TVHM
                status = ("complete" if i < 9 else "active" if i == 9 else "ready" if i == 10 else "not_started") if kind == "main" else (
                    "complete" if i % 3 == 0 else "active" if i % 7 == 0 else "not_started")
            else:
                status = "not_started"
            rows.append({"id": mid, "name": name, "type": kind, "group": group, "status": status})
        out[pt] = rows
    return out


CHALLENGE_DATA = {
    "Combat": [
        ("Skag Slayer", "Kill skags", 500), ("Bandit Hunter", "Kill bandits", 500), ("Crit Happy", "Land critical hits", 1000),
        ("Pistol Whipped", "Kill enemies with pistols", 250), ("Shotgun Surgeon", "Kill enemies with shotguns", 250),
        ("Sniper Elite", "Kill enemies with sniper rifles", 250), ("Rocket Man", "Kill enemies with launchers", 100),
        ("Elemental Overload", "Kill enemies with elemental damage", 300), ("Melee Master", "Kill enemies with melee", 100),
        ("Grenadier", "Kill enemies with grenades", 150),
    ],
    "Exploration": [
        ("Cartographer", "Discover locations", 40), ("Vault Hunter", "Find hidden vaults", 10), ("Dust Collector", "Search dust piles", 50),
        ("Cache Raider", "Open red chests", 40), ("Eridium Seeker", "Pick up eridium", 250), ("Echo Collector", "Find ECHO recorders", 20),
    ],
    "Collection": [
        ("Pack Rat", "Pick up weapons", 1000), ("Cash Money", "Collect cash (thousands)", 500), ("Rarity Check", "Find rare items", 50),
        ("Cosmetic Hoarder", "Find skins and heads", 10),
    ],
    "Vehicles": [
        ("Road Warrior", "Kill enemies with vehicles", 100), ("Grease Monkey", "Drive distance (km)", 500), ("Hot Wheels", "Perform vehicle jumps", 25),
    ],
    "Misc": [
        ("Second Wind", "Get Second Wind", 100), ("Friendly Fire", "Revive teammates", 25), ("Big Spender", "Spend cash (thousands)", 500),
        ("Level Up", "Gain levels", 80),
    ],
}


def build_challenges() -> list[dict]:
    rows = []
    for g, (group, items) in enumerate(CHALLENGE_DATA.items()):
        for i, (name, desc, goal) in enumerate(items):
            seed = (g * 7 + i * 5) % 11
            progress = goal if seed in (0, 3, 8) else goal * seed // 12
            rows.append({"id": f"GD_Challenges.Ch_{camel(name)}", "name": name, "desc": desc, "group": group, "goal": goal, "progress": progress})
    return rows


STATIONS = [  # (name, level package)
    ("Sanctuary", "SanctuaryAir_P"), ("Fyrestone", "Grass_Cliffs_P"), ("Three Horns - Valley", "Grass_Lands_P"), ("Three Horns - Divide", "Grass_Lands_P"),
    ("Southern Shelf", "SouthernShelf_P"), ("Southern Shelf - Bay", "SouthernShelf_P"), ("Frostburn Canyon", "Ice_P"), ("Bloodshot Stronghold", "Ice_P"),
    ("Bloodshot Ramparts", "Ice_P"), ("The Highlands", "Interlude_P"), ("Highlands - Outwash", "Interlude_P"), ("Wildlife Exploitation Preserve", "Interlude_P"),
    ("Thousand Cuts", "Cliffs_P"), ("Caustic Caverns", "CraterLake_P"), ("Sawtooth Cauldron", "Cauldron_P"), ("Tundra Express", "Tundra_Express_P"),
    ("Opportunity", "Lynchwood_P"), ("Lynchwood", "Lynchwood_P"), ("Hero's Pass", "Mountain_P"), ("Eridium Blight", "Blight_P"),
    ("Arid Nexus - Badlands", "Fridge_P"), ("Arid Nexus - Boneyard", "Fridge_P"), ("Dust", "Dust_P"), ("Control Core Angel", "Core_P"),
    ("Washburne Refinery", "Refinery_P"), ("Oasis", "Oasis_P"), ("Rust Commons East", "Rust_P"), ("Rust Commons West", "Rust_P"),
    ("Natural Selection Annex", "Annex_P"), ("Friendship Gulag", "Gulag_P"), ("Hunter's Grotto", "Grotto_P"), ("Terramorphous Peak", "Peak_P"),
    ("Magnys Lighthouse", "Lighthouse_P"), ("Hayter's Folly", "Folly_P"), ("The Underground", "Underground_P"), ("Candlerakk's Crag", "Crag_P"),
    ("Dragon Keep", "DragonKeep_P"), ("Hunter's Grotto - Docks", "Grotto_P"), ("Leviathan's Lair", "Lair_P"), ("Sir Hammerlock's Safari", "Safari_P"),
]

MISSIONS = build_missions()
CHALLENGES = build_challenges()
VISITED = {f"GD_FastTravel.FT_{camel(name)}" for name, _lvl in STATIONS[:18]}
PLAYTHROUGH = {"current": 2}


def station_rows() -> list[dict]:
    rows = [{"id": f"GD_FastTravel.FT_{camel(n)}", "name": n, "level": lvl, "visited": f"GD_FastTravel.FT_{camel(n)}" in VISITED} for n, lvl in STATIONS]
    return sorted(rows, key=lambda r: r["name"].lower())


def missions_section() -> dict:
    pts = []
    for pt, rows in MISSIONS.items():
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        pts.append({"number": pt, "rows": rows, "counts": counts})
    return {"available": True, "error": None, "playthroughs": pts, "current": PLAYTHROUGH["current"], "source": "pc.MissionPlaythroughs.MissionList (mock)", "statuses": STATUS_LABELS}


def challenge_row(c: dict) -> dict:
    return {**c, "done": c["progress"] >= c["goal"]}


def section(name: str) -> dict:
    if name in FAIL:
        return {"available": False, "error": f"this game build doesn't expose the {name} data (mock failure)"}
    if name == "missions":
        return missions_section()
    if name == "challenges":
        return {"available": True, "error": None, "rows": [challenge_row(c) for c in CHALLENGES], "source": "challenges.ChallengeList (mock)"}
    if name == "stations":
        return {"available": True, "error": None, "rows": station_rows(), "warning": None}
    cur = PLAYTHROUGH["current"]
    return {
        "available": True, "error": None, "current": cur, "raw": cur - 1, "source": "pc.GetCurrentPlaythrough() (mock)",
        "options": [{"number": i + 1, "short": s, "name": n} for i, (s, n) in enumerate(PLAYTHROUGHS)],
    }


def need_confirm(params: dict, what: str) -> None:
    if params.get("confirm") is not True:
        raise ValueError(f"{what} needs confirmation - send {{\"confirm\": true}}")


def ids_of(params: dict) -> list[str]:
    raw = params.get("ids")
    if raw is None and params.get("id") is not None:
        raw = [params["id"]]
    if not isinstance(raw, list) or not raw or not all(isinstance(i, str) and i for i in raw):
        raise ValueError("send 'id' (a string) or 'ids' (a non-empty list of strings)")
    return list(dict.fromkeys(raw))


def counts_of(rows: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return out


def apply_missions(params: dict, wanted: list[str] | None, key: str) -> dict:
    if "missions" in FAIL:
        raise ValueError("this game build doesn't expose the mission list - run the world dump")
    pt = params.get("playthrough", PLAYTHROUGH["current"])
    if not isinstance(pt, int) or not 1 <= pt <= 3:
        raise ValueError("playthrough must be 1-3")
    pool = MISSIONS[pt]
    by_id = {r["id"]: r for r in pool}
    if wanted is None:
        targets = pool
    else:
        unknown = [i for i in wanted if i not in by_id]
        if unknown:
            raise LookupError(f"unknown mission: {unknown[0]}")
        targets = [by_id[i] for i in wanted]
    before = counts_of(targets)
    single_before = targets[0]["status"]
    changed = 0
    for row in targets:
        if row["status"] != key:
            row["status"] = key
            changed += 1
            OBJECTIVES.pop((pt, row["id"]), None)  # the objectives follow the new state
    one = len(targets) == 1
    applied = {
        "what": "mission" if one else "missions", "playthrough": pt, "requested": key, "count": len(targets), "changed": changed, "failed": [],
        "via": ["entry.Status"], "before": single_before if one else before, "after": key if one else counts_of(targets),
    }
    if one:
        applied["name"] = targets[0]["name"]
    return {"missions": missions_section(), "applied": applied}



OBJECTIVE_NAMES = [
    ("Kill the bandits", (6, 12)), ("Collect the parts", (3, 8)), ("Find the key", (1, 1)), ("Destroy the generators", (2, 4)),
    ("Reach the vault door", (1, 1)), ("Loot the chests", (2, 5)), ("Return to the quest giver", (1, 1)),
]
OBJECTIVES: dict[tuple[int, str], dict] = {}
TRACKED: dict[int, str] = {}


def objective_state(pt: int, row: dict) -> dict:
    key = (pt, row["id"])
    if key not in OBJECTIVES:
        seed = sum(ord(c) for c in row["id"])
        count = 3 + seed % 2
        defs = []
        for i in range(count):
            name, (low, high) = OBJECTIVE_NAMES[(seed + i * 3) % len(OBJECTIVE_NAMES)]
            defs.append({"id": f"{row['id']}.Obj{i + 1}", "name": name, "target": low + (seed + i) % (high - low + 1), "optional": i == count - 1 and seed % 3 == 0})
        split = (count + 1) // 2
        sets = [
            {"id": f"{row['id']}.Set1", "name": "Set1", "objectives": list(range(split))},
            {"id": f"{row['id']}.Set2", "name": "Set2", "objectives": list(range(split, count))},
        ]
        status = row["status"]
        if status in ("complete", "ready", "objectives_done"):
            progress = [d["target"] for d in defs]
            active = sets[-1]["id"]
        elif status == "active":
            progress = [d["target"] if i < split - 1 else d["target"] // 2 for i, d in enumerate(defs)]
            active = sets[0]["id"]
        else:
            progress = [0] * count
            active = None
        OBJECTIVES[key] = {"defs": defs, "sets": sets, "progress": progress, "active": active}
    return OBJECTIVES[key]


def objectives_view(pt: int, row: dict) -> dict:
    state = objective_state(pt, row)
    return {
        "mission": {"id": row["id"], "name": row["name"], "playthrough": pt, "status": row["status"]},
        "objectives": [{"index": i, **d, "progress": state["progress"][i]} for i, d in enumerate(state["defs"])],
        "sets": [{**st, "active": st["id"] == state["active"]} for st in state["sets"]],
        "active_set": state["active"], "tracked": TRACKED.get(pt) == row["id"], "progress_len": len(state["progress"]), "note": None,
    }


def find_row(params: dict) -> tuple[int, dict]:
    if "missions" in FAIL:
        raise ValueError("this game build doesn't expose the mission list - run the world dump")
    pt = params.get("playthrough", PLAYTHROUGH["current"])
    pt = int(pt) if isinstance(pt, str) and pt.isdigit() else pt
    if not isinstance(pt, int) or not 1 <= pt <= 3:
        raise ValueError("playthrough must be 1-3")
    row = next((r for r in MISSIONS[pt] if r["id"] == params.get("id")), None)
    if row is None:
        raise LookupError(f"unknown mission: {params.get('id')}")
    return pt, row


def handle(path: str, params: dict, shared: dict) -> dict | None:
    if path == "/api/world":
        only = params.get("section")
        names = ["playthrough", "missions", "challenges", "stations"]
        if only is not None and only not in names:
            raise ValueError(f"unknown section (use {', '.join(names)})")
        return {n: section(n) for n in names if only in (None, n)}
    if path == "/api/debug/world":
        return {"saved_to": "(mock) rtse/debug_world.json", "sections": {n: n not in FAIL for n in ("playthrough", "missions", "challenges", "stations")}}

    if path == "/api/world/objectives":
        pt, row = find_row(params)
        return objectives_view(pt, row)
    if path == "/api/world/objective":
        pt, row = find_row(params)
        state = objective_state(pt, row)
        index, value = params.get("index"), params.get("progress")
        if not isinstance(index, int) or not 0 <= index < len(state["defs"]):
            raise LookupError(f"this mission has {len(state['defs'])} objectives")
        if not isinstance(value, int) or not 0 <= value <= 9999:
            raise ValueError("progress must be 0-9999")
        before = state["progress"][index]
        state["progress"][index] = value
        applied = {"what": "objective", "mission": row["name"], "objective": state["defs"][index]["name"], "index": index, "requested": value,
                   "before": before, "after": value, "via": f"entry.ObjectivesProgress[{index}]"}
        return {**objectives_view(pt, row), "applied": applied}
    if path == "/api/world/objective_set":
        pt, row = find_row(params)
        state = objective_state(pt, row)
        if params.get("set") not in {st["id"] for st in state["sets"]}:
            raise LookupError("this mission has no objective set like that")
        before, state["active"] = state["active"], params["set"]
        applied = {"what": "objective set", "mission": row["name"], "requested": params["set"], "before": before, "after": params["set"], "via": "entry.ActiveObjectiveSet"}
        return {**objectives_view(pt, row), "applied": applied}
    if path == "/api/world/track":
        pt, row = find_row(params)
        if pt != PLAYTHROUGH["current"]:
            raise ValueError("only a mission of the playthrough you're in can be tracked")
        before = TRACKED.get(pt) == row["id"]
        TRACKED[pt] = row["id"]
        applied = {"what": "tracked mission", "mission": row["name"], "requested": True, "before": before, "after": True, "via": "missions.SetActiveMission()"}
        return {**objectives_view(pt, row), "applied": applied}

    if path == "/api/world/mission":
        need_confirm(params, "changing mission state")
        if params.get("status") not in WRITABLE:
            raise ValueError(f"status must be one of {', '.join(WRITABLE)}")
        return apply_missions(params, ids_of(params), params["status"])
    if path == "/api/world/missions/reset":
        need_confirm(params, "resetting missions")
        return apply_missions(params, None if params.get("all") is True else ids_of(params), "not_started")

    if path == "/api/world/challenge":
        action = params.get("action")
        if action not in ("complete", "reset"):
            raise ValueError("action must be 'complete' or 'reset'")
        if action == "reset":
            need_confirm(params, "resetting challenges")
        if "challenges" in FAIL:
            raise ValueError("this game build doesn't expose the challenge list - run the world dump")
        wanted = ids_of(params)
        by_id = {c["id"]: c for c in CHALLENGES}
        unknown = [i for i in wanted if i not in by_id]
        if unknown:
            raise LookupError(f"unknown challenge: {unknown[0]}")
        before = [challenge_row(by_id[i]) for i in wanted]
        for i in wanted:
            by_id[i]["progress"] = by_id[i]["goal"] if action == "complete" else 0
        after = [challenge_row(by_id[i]) for i in wanted]
        one = len(wanted) == 1
        applied = {
            "what": "challenge" if one else "challenges", "requested": action, "count": len(wanted), "failed": [], "via": ["entry.Progress", "entry.bCompleted"],
            "before": [before[0]["done"], before[0]["progress"]] if one else sum(1 for r in before if r["done"]),
            "after": [after[0]["done"], after[0]["progress"]] if one else sum(1 for r in after if r["done"]),
        }
        if one:
            applied["name"] = after[0]["name"]
        return {"challenges": section("challenges"), "applied": applied}

    if path == "/api/world/stations":
        visited = params.get("visited", True)
        if not isinstance(visited, bool):
            raise ValueError("'visited' must be true or false")
        if not visited:
            need_confirm(params, "locking stations again")
        if "stations" in FAIL:
            raise ValueError("no fast-travel station definitions were found in this game build - run the world dump")
        rows = {r["id"]: r for r in station_rows()}
        if params.get("all") is True:
            wanted = list(rows)
        else:
            wanted = ids_of(params)
            unknown = [i for i in wanted if i not in rows]
            if unknown:
                raise LookupError(f"unknown station: {unknown[0]}")
        before = sum(1 for i in wanted if rows[i]["visited"])
        for i in wanted:
            (VISITED.add if visited else VISITED.discard)(i)
        after = sum(1 for i in wanted if i in VISITED)
        applied = {
            "what": "stations", "requested": "unlock" if visited else "lock", "count": len(wanted), "failed": [], "via": ["pc.VisitedTeleporters"],
            "before": before, "after": after,
        }
        if len(wanted) == 1:
            applied["name"] = rows[wanted[0]]["name"]
        return {"stations": section("stations"), "applied": applied}

    if path == "/api/world/playthrough":
        need_confirm(params, "switching playthrough")
        target = params.get("playthrough")
        if not isinstance(target, int) or isinstance(target, bool) or not 1 <= target <= 3:
            raise ValueError("playthrough must be 1-3")
        if "playthrough" in FAIL:
            raise ValueError("this game build has no writable playthrough field - run the world dump")
        before = PLAYTHROUGH["current"]
        PLAYTHROUGH["current"] = target
        applied = {"what": "playthrough", "requested": target, "via": "pc.CurrentPlaythrough (mock)", "before": before, "after": target}
        return {"playthrough": section("playthrough"), "applied": applied}
    return None
