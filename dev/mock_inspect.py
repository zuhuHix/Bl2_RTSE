"""Mock of the inspector API for dev/mock_server.py. Same JSON shapes as rtse/inspector.py, over a small made-up object tree.

Node kinds: {"obj": "ClassName", "f": {field: node}} (an object), {"struct": "Name", "f": {...}}, a list (an array), a bool / int / float / str,
None (an empty reference) and {"enum": ["A", "B"], "v": 1}. The fields are invented; they only make the page look and behave like the real one.
"""

from __future__ import annotations

import re

PAGE = 100
STATUS = ["MS_NotStarted", "MS_Active", "MS_RequiredObjectivesComplete", "MS_ReadyToTurnIn", "MS_Complete", "MS_Failed"]


def enum(options: list[str], v: int) -> dict:
    return {"enum": options, "v": v}


def mission(i: int, name: str, status: int) -> dict:
    return {"struct": "MissionStatusPlayerData", "f": {
        "MissionDef": {"obj": "MissionDefinition", "f": {"MissionName": name, "bPlotCritical": i < 4, "GameStage": 5 + i, "MissionNumber": i + 1, "ObjectiveDefs": [
            {"obj": "MissionObjectiveDefinition", "f": {"ObjectiveCount": 3, "bObjectiveIsOptional": False, "ProgressMessage": "Kill bandits"}}]}},
        "Status": enum(STATUS, status), "bHeardKickoff": True, "bNeedsRewards": False, "GameStage": 5 + i, "ObjectivesProgress": [1, 3, 0],
        "ActiveObjectiveSet": None,
    }}


def build() -> dict:
    names = ["Wakey Wakey", "Rising Action", "Cleaning Up the Berg", "Hunting the Firehawk", "A Dam Fine Rescue"]
    return {
        "pc": {"obj": "WillowPlayerController", "f": {
            "Pawn": {"obj": "WillowPlayerPawn", "f": {"Health": 462.0, "bIsDead": False, "GroundSpeed": 440.0, "JumpZ": 630.0, "Name": "WillowPlayerPawn_0"}},
            "PlayerReplicationInfo": {"obj": "WillowPlayerReplicationInfo", "f": {"PlayerName": "Zero Cool", "Score": 0, "bAdmin": False, "ExpPoints": 1450000, "ExpLevel": 34}},
            "MissionPlaythroughs": [{"struct": "MissionPlaythroughData", "f": {
                "PlayThroughNumber": pt, "ActiveMission": None, "FilteredMissions": [],
                "MissionList": [mission(i, n, 4 if pt == 0 else (4 if i < 3 else 1 if i == 3 else 0)) for i, n in enumerate(names)]}} for pt in range(3)],
            "bShowUndiscoveredMissions": False, "LastMissionSortType": 1, "MissionRestrictionTextDuration": 5.0,
            "PlayerSkillTree": {"obj": "PlayerSkillTree", "f": {"Skills": [{"struct": "PlayerSkillTreeSkillData", "f": {"Grade": g, "Definition": None}} for g in (5, 5, 3, 0)]}},
        }},
        "pawn": None, "pri": None, "skills": None, "world": None, "gri": None, "missions": None,
    }


TREE = build()
LABELS = {"pc": "Player controller", "pawn": "Your character's body", "pri": "Player info", "skills": "Skill tree", "world": "World", "gri": "Game info", "missions": "Mission tracker"}
ALIASES = {"pawn": ("pc", "Pawn"), "pri": ("pc", "PlayerReplicationInfo"), "skills": ("pc", "PlayerSkillTree")}


def root_node(key: str):
    if key in ALIASES:
        base, field = ALIASES[key]
        return TREE[base]["f"][field]
    return TREE.get(key)


def kind_of(node) -> str:
    if node is None:
        return "null"
    if isinstance(node, bool):
        return "bool"
    if isinstance(node, int):
        return "int"
    if isinstance(node, float):
        return "float"
    if isinstance(node, str):
        return "text"
    if isinstance(node, list):
        return "array"
    if "enum" in node:
        return "enum"
    return "object" if "obj" in node else "struct"


def type_of(node, kind: str) -> str:
    if kind == "object":
        return node["obj"]
    if kind == "struct":
        return node["struct"]
    if kind == "array":
        return f"array of {len(node)}"
    return kind


def summary(node, kind: str):
    if kind == "enum":
        return f"{node['enum'][node['v']]} ({node['v']})"
    if kind == "object":
        return f"{node['obj']} (mock)"
    if kind == "struct":
        return node["struct"]
    if kind == "array":
        return f"{len(node)} entries"
    return node


def row(path: str, name: str, node) -> dict:
    kind = kind_of(node)
    return {"name": name, "path": path, "kind": kind, "type": type_of(node, kind), "value": summary(node, kind),
            "editable": kind in ("bool", "int", "float", "text", "enum"), "openable": kind in ("object", "struct") or (kind == "array" and len(node) > 0)}


PATH = re.compile(r"^([a-z]+)((?:\.[A-Za-z][A-Za-z0-9_]*|\[\d{1,6}\])*)$")


def resolve(path: str):
    match = PATH.match(path) if isinstance(path, str) else None
    if not match or match.group(1) not in LABELS:
        raise ValueError("that isn't a valid path")
    node, parent, last = root_node(match.group(1)), None, None
    if node is None:
        raise LookupError(f"{LABELS[match.group(1)]} isn't available right now")
    for m in re.finditer(r"\.([A-Za-z][A-Za-z0-9_]*)|\[(\d+)\]", match.group(2)):
        parent, last = node, (m.group(1), int(m.group(2)) if m.group(2) else None)
        if last[1] is not None:
            if not isinstance(node, list) or last[1] >= len(node):
                raise LookupError("no such entry")
            node = node[last[1]]
        else:
            if not isinstance(node, dict) or "f" not in node or last[0] not in node["f"]:
                raise LookupError(f"no field called {last[0]}")
            node = node["f"][last[0]]
    return node, parent, last


def crumbs(path: str) -> list[dict]:
    root = PATH.match(path).group(1)
    out, built = [{"label": LABELS[root], "path": root}], root
    for m in re.finditer(r"\.([A-Za-z][A-Za-z0-9_]*)|\[(\d+)\]", PATH.match(path).group(2)):
        built += f".{m.group(1)}" if m.group(1) else f"[{m.group(2)}]"
        out.append({"label": m.group(1) or f"[{m.group(2)}]", "path": built})
    return out


def look(params: dict) -> dict:
    path = params.get("path")
    if not path:
        return {"path": "", "kind": "roots", "title": "Live game data", "crumbs": [], "total": len(LABELS), "offset": 0,
                "rows": [{"name": label, "path": key, "kind": "object", "type": "start", "value": "mock object" if root_node(key) is not None else "not available right now",
                          "editable": False, "openable": root_node(key) is not None} for key, label in LABELS.items()]}
    node, _parent, _last = resolve(path)
    kind = kind_of(node)
    try:
        offset = max(0, int(params.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    reply = {"path": path, "kind": kind, "type": type_of(node, kind), "crumbs": crumbs(path), "offset": 0, "total": 0, "rows": []}
    if kind in ("object", "struct"):
        names = sorted(node["f"], key=str.lower)
        reply["total"] = len(names)
        reply["rows"] = [row(f"{path}.{n}", n, node["f"][n]) for n in names]
    elif kind == "array":
        reply.update({"total": len(node), "offset": offset})
        reply["rows"] = [row(f"{path}[{i}]", f"[{i}]", node[i]) for i in range(offset, min(len(node), offset + PAGE))]
    else:
        reply["rows"] = [row(path, path.rsplit(".", 1)[-1], node)]
    return reply


def set_value(params: dict) -> dict:
    if params.get("confirm") is not True:
        raise ValueError('writing needs {"confirm": true}')
    path = params.get("path")
    node, parent, last = resolve(path)
    if last is None or parent is None:
        raise ValueError("pick a single field or list entry")
    kind, raw = kind_of(node), params.get("value")
    if kind == "bool":
        new = raw if isinstance(raw, bool) else {"true": True, "false": False}.get(str(raw).lower())
        if new is None:
            raise ValueError("that field takes true or false")
    elif kind in ("int", "enum"):
        if isinstance(raw, bool):
            raise ValueError("that field takes a whole number")
        try:
            new = int(raw)
        except (TypeError, ValueError):
            if kind == "enum" and raw in node["enum"]:
                new = node["enum"].index(raw)
            else:
                raise ValueError("that field takes a whole number") from None
        if kind == "enum" and not 0 <= new < len(node["enum"]):
            raise ValueError(f"{new} isn't one of this field's values")
    elif kind == "float":
        try:
            new = float(raw)
        except (TypeError, ValueError):
            raise ValueError("that field takes a number") from None
    elif kind == "text":
        if not isinstance(raw, str):
            raise ValueError("that field takes text up to 2000 characters")
        new = raw
    else:
        raise ValueError(f"a {kind} can't be edited here")
    before = summary(node, kind)
    name, index = last
    if kind == "enum":
        node["v"] = new
        stored = node
    elif index is not None:
        parent[index] = new
        stored = new
    else:
        parent["f"][name] = new
        stored = new
    after = summary(stored, kind)
    applied = {"path": path, "kind": kind, "before": before, "requested": after, "after": after}
    parent_path = path[: path.rindex("[")] if index is not None else path.rsplit(".", 1)[0]
    return {**look({"path": parent_path}), "applied": applied}


def handle(path: str, params: dict, shared: dict) -> dict | None:
    if path == "/api/inspect":
        return look(params)
    if path == "/api/inspect/set":
        return set_value(params)
    return None
