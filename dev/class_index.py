"""Index of the game's real classes, read from its code packages (read-only): fields with their types and functions with
their parameters, including everything inherited. Used to check RTSE's guessed names without running the game.

    python -I dev/class_index.py                       builds dev/out/classes.json
    python -I dev/class_index.py WillowPlayerController [substring]   lists a class's members (optionally filtered)

Only proves what EXISTS (names, types, parameters). It says nothing about what a call does at runtime.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import upk  # noqa: E402

GAME = Path("C:/Program Files (x86)/Steam/steamapps/common/Borderlands 2/WillowGame/CookedPCConsole")
PACKAGES = ("Core", "Engine", "GameFramework", "GFxUI", "GearboxFramework", "WillowGame", "WillowGameContent", "OnlineSubsystemSteamworks", "IpDrv", "AkAudio")
CACHE = Path(__file__).resolve().parent / "out" / "classes.json"
STRUCT_KINDS = {"ScriptStruct"}


def _kind(pkg: upk.Package, export: dict[str, Any]) -> str:
    return pkg.class_name(export).split(".")[-1]


def _param(pkg: upk.Package, export: dict[str, Any]) -> str:
    return f"{_kind(pkg, export).replace('Property', '')} {export['name']}"


def _describe_members(pkg: upk.Package, kids: dict[int, list[int]], owner: int) -> tuple[dict[str, str], dict[str, list[str]]]:
    fields: dict[str, str] = {}
    functions: dict[str, list[str]] = {}
    for k in kids[owner]:
        e = pkg.exports[k - 1]
        kind = _kind(pkg, e)
        if kind in ("Enum", "Const", "ScriptStruct", "State"):
            continue
        if kind == "Function":
            # children are listed in reverse declaration order; the return value comes first
            params = [_param(pkg, pkg.exports[c - 1]) for c in kids[k]]
            functions[e["name"]] = list(reversed(params))
        elif kind.endswith("Property"):
            fields[e["name"]] = kind.replace("Property", "")
    return fields, functions


def build() -> dict[str, Any]:
    classes: dict[str, Any] = {}
    structs: dict[str, Any] = {}
    for name in PACKAGES:
        path = GAME / f"{name}.upk"
        if not path.is_file():
            continue
        pkg = upk.load(path)
        kids: dict[int, list[int]] = collections.defaultdict(list)
        for i, e in enumerate(pkg.exports, 1):
            kids[e["outer"]].append(i)
        for i, e in enumerate(pkg.exports, 1):
            kind = pkg.class_name(e)
            if kind == "Class":
                fields, functions = _describe_members(pkg, kids, i)
                sup = pkg.object_name(e["super"]).split(".")[-1] if e["super"] else None
                classes[e["name"]] = {"package": name, "super": sup, "fields": fields, "functions": functions}
                for k in kids[i]:
                    if _kind(pkg, pkg.exports[k - 1]) == "ScriptStruct":
                        s_fields = {pkg.exports[c - 1]["name"]: _kind(pkg, pkg.exports[c - 1]).replace("Property", "") for c in kids[k] if _kind(pkg, pkg.exports[c - 1]).endswith("Property")}
                        structs[f"{e['name']}.{pkg.exports[k - 1]['name']}"] = s_fields
            elif kind == "ScriptStruct" and e["outer"] and pkg.exports[e["outer"] - 1]["name"] not in classes:
                pass
        print(name, len(classes), "classes so far")
    return {"classes": classes, "structs": structs}


def load() -> dict[str, Any]:
    if not CACHE.is_file():
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps(build()), encoding="utf-8")
    return json.loads(CACHE.read_text(encoding="utf-8"))


class Index:
    def __init__(self) -> None:
        data = load()
        self.classes: dict[str, Any] = data["classes"]
        self.structs: dict[str, Any] = data["structs"]

    def chain(self, cls: str) -> list[str]:
        out = []
        while cls and cls in self.classes:
            out.append(cls)
            cls = self.classes[cls]["super"]
        return out

    def field(self, cls: str, name: str) -> tuple[str, str] | None:
        """(type, declaring class) of a field, searching superclasses."""
        for c in self.chain(cls):
            if name in self.classes[c]["fields"]:
                return self.classes[c]["fields"][name], c
        return None

    def function(self, cls: str, name: str) -> tuple[list[str], str] | None:
        for c in self.chain(cls):
            if name in self.classes[c]["functions"]:
                return self.classes[c]["functions"][name], c
        return None

    def search(self, cls: str, needle: str) -> list[str]:
        needle = needle.lower()
        out = []
        for c in self.chain(cls):
            info = self.classes[c]
            out += [f"{c}.{n}: {t}" for n, t in info["fields"].items() if needle in n.lower()]
            out += [f"{c}.{n}({', '.join(p)})" for n, p in info["functions"].items() if needle in n.lower()]
        return out


if __name__ == "__main__":
    if len(sys.argv) == 1:
        CACHE.unlink(missing_ok=True)
        print(len(load()["classes"]), "classes cached at", CACHE)
    else:
        index = Index()
        for line in index.search(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""):
            print(line)
