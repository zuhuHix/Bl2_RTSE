"""The inspector: browse the live game's own objects and change plain numbers, switches and text in them.

It starts from a few well-known objects reached from the player controller (ROOTS) and follows a path such as
"pc.MissionPlaythroughs[1].MissionList[3].Status". Every field the game defines is listed with its real value; objects, structs and
arrays can be opened. Only whole numbers, decimals, true/false, text and enum values can be written. Object references, structs and
arrays are read-only, and no game function is ever called from here.

Nothing checks that a change makes sense to the game (that is the point of the page), so a wrong value can confuse it or crash it.
Every write is read back and reported. What a given field means is not known to this code: the page only shows what the game holds.

Paths are validated before anything touches the game: a root key, then ".Name" (a letter first, so no private or dunder names) or
"[index]" parts only.
"""

from __future__ import annotations

import math
import re
from typing import Any

from mods_base import get_pc
from unrealsdk import logging, unreal

PAGE = 100  # most array entries one answer lists
MAX_PATH = 300
MAX_TEXT = 2000  # longest text value accepted

# key -> (label, path from the player controller)
ROOTS: dict[str, tuple[str, str]] = {
    "pc": ("Player controller", ""),
    "pawn": ("Your character's body", "Pawn"),
    "pri": ("Player info", "PlayerReplicationInfo"),
    "skills": ("Skill tree", "PlayerSkillTree"),
    "world": ("World", "WorldInfo"),
    "gri": ("Game info", "WorldInfo.GRI"),
    "missions": ("Mission tracker", "WorldInfo.GRI.MissionTracker"),
}

_PATH = re.compile(r"^([a-z]+)((?:\.[A-Za-z][A-Za-z0-9_]*|\[\d{1,6}\])*)$")
_TOKEN = re.compile(r"\.([A-Za-z][A-Za-z0-9_]*)|\[(\d+)\]")


class InspectError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# ---------- reading values ----------


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(getattr(value, "name", None), str) and hasattr(value, "__int__") and not isinstance(value, (bool, str)):
        return "enum"  # first: the game's enums may also be ints
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "text"
    if isinstance(value, unreal.UObject):
        return "object"
    name = type(value).__name__
    if name == "WrappedStruct":
        return "struct"
    if name == "WrappedArray":
        return "array"
    if callable(value):
        return "function"
    if hasattr(value, "__len__") and hasattr(value, "__getitem__"):
        return "array"
    return "other"


def _path_name(obj: Any) -> str:
    try:
        return obj._path_name()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return str(obj)


def _type_name(value: Any, kind: str) -> str:
    try:
        if kind == "object":
            return str(value.Class.Name)
        if kind == "struct":
            return str(value._type.Name)  # noqa: SLF001
        if kind == "enum":
            return type(value).__name__
        if kind == "array":
            return f"array of {len(value)}"
    except Exception:  # noqa: BLE001, S110
        pass
    return kind


def _summary(value: Any, kind: str) -> Any:
    """What the list shows: the real value for plain kinds, a short description for the rest."""
    if kind in ("bool", "int", "text"):
        return value
    if kind == "float":
        return value if math.isfinite(value) else str(value)
    if kind == "null":
        return None
    if kind == "enum":
        try:
            return f"{value.name} ({int(value)})"
        except Exception:  # noqa: BLE001
            return str(value)
    if kind == "object":
        return _path_name(value)
    if kind == "array":
        try:
            return f"{len(value)} entries"
        except Exception:  # noqa: BLE001
            return "array"
    if kind == "struct":
        return _type_name(value, kind)
    return str(value)[:200]


def _row(path: str, name: str, value: Any) -> dict[str, Any]:
    kind = _kind(value)
    editable = kind in ("bool", "int", "text", "enum") or (kind == "float" and math.isfinite(value))
    openable = kind in ("object", "struct", "array")
    return {
        "name": name, "path": path, "kind": kind, "type": _type_name(value, kind), "value": _summary(value, kind),
        "editable": editable, "openable": openable and (kind != "array" or len(value) > 0),
    }


def _field_names(obj: Any) -> list[str]:
    """Every field the game defines for this object or struct, own and inherited, without repeats."""
    try:
        cls = obj.Class if isinstance(obj, unreal.UObject) else obj._type  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return []
    names: list[str] = []
    seen: set[str] = set()
    for _ in range(40):
        if cls is None:
            break
        try:
            for prop in cls._fields():  # noqa: SLF001
                name = getattr(prop, "Name", None)
                if name and str(name) not in seen:
                    seen.add(str(name))
                    names.append(str(name))
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            cls = cls.SuperField
        except Exception:  # noqa: BLE001
            break
    return names


# ---------- paths ----------


def _roots() -> dict[str, Any]:
    pc = get_pc(possibly_loading=True)
    if pc is None:
        raise InspectError(409, "not in a save")
    found: dict[str, Any] = {}
    for key, (_label, path) in ROOTS.items():
        target: Any = pc
        for part in filter(None, path.split(".")):
            try:
                target = getattr(target, part)
            except Exception:  # noqa: BLE001
                target = None
            if target is None:
                break
        if target is not None:
            found[key] = target
    return found


def _tokens(rest: str) -> list[tuple[str, int | None]]:
    return [(m.group(1), None) if m.group(1) else ("", int(m.group(2))) for m in _TOKEN.finditer(rest)]


def _step(value: Any, name: str, index: int | None) -> Any:
    try:
        if index is not None:
            if _kind(value) != "array":
                raise InspectError(400, "that isn't a list")
            if not 0 <= index < len(value):
                raise InspectError(404, f"there are only {len(value)} entries")
            return value[index]
        if _kind(value) not in ("object", "struct"):
            raise InspectError(400, "only objects and structs have fields")
        if name not in _field_names(value):
            raise InspectError(404, f"no field called {name}")
        return getattr(value, name)
    except InspectError:
        raise
    except Exception as ex:  # noqa: BLE001
        raise InspectError(404, f"can't read that: {type(ex).__name__}: {ex}") from None


def _parse(path: Any) -> tuple[str, list[tuple[str, int | None]]]:
    if not isinstance(path, str) or not path or len(path) > MAX_PATH or not (match := _PATH.match(path)):
        raise InspectError(400, "that isn't a valid path")
    return match.group(1), _tokens(match.group(2))


def _resolve(path: str) -> tuple[Any, Any, tuple[str, int | None] | None]:
    """(value, its parent, the last step) for a path."""
    root, steps = _parse(path)
    roots = _roots()
    if root not in ROOTS:
        raise InspectError(404, "unknown starting point")
    if root not in roots:
        raise InspectError(404, f"{ROOTS[root][0]} isn't available right now")
    value, parent = roots[root], None
    for name, index in steps:
        parent, value = value, _step(value, name, index)
    return value, parent, steps[-1] if steps else None


def _crumbs(path: str) -> list[dict[str, str]]:
    root, steps = _parse(path)
    out = [{"label": ROOTS[root][0] if root in ROOTS else root, "path": root}]
    built = root
    for name, index in steps:
        built += f".{name}" if index is None else f"[{index}]"
        out.append({"label": name if index is None else f"[{index}]", "path": built})
    return out


# ---------- the two requests ----------


def look(params: dict[str, Any]) -> dict[str, Any]:
    """With no path: the starting points. With one: what is there (an object's fields, an array's entries, or one value)."""
    path = params.get("path")
    if not path:
        roots = _roots()
        return {
            "path": "", "kind": "roots", "title": "Live game data", "crumbs": [], "total": len(ROOTS), "offset": 0,
            "rows": [{"name": label, "path": key, "kind": "object", "type": "start", "value": _path_name(roots[key]) if key in roots else "not available right now",
                      "editable": False, "openable": key in roots} for key, (label, _p) in ROOTS.items()],
        }
    value, _parent, _last = _resolve(path)
    kind = _kind(value)
    try:
        offset = max(0, int(params.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    reply: dict[str, Any] = {"path": path, "kind": kind, "type": _type_name(value, kind), "crumbs": _crumbs(path), "offset": 0, "total": 0, "rows": []}
    if kind in ("object", "struct"):
        names = sorted(_field_names(value), key=str.lower)
        reply["total"] = len(names)
        for name in names:
            try:
                reply["rows"].append(_row(f"{path}.{name}", name, getattr(value, name)))
            except Exception as ex:  # noqa: BLE001
                reply["rows"].append({"name": name, "path": f"{path}.{name}", "kind": "error", "type": "unreadable", "value": f"{type(ex).__name__}: {ex}", "editable": False, "openable": False})
    elif kind == "array":
        total = len(value)
        reply.update({"total": total, "offset": offset})
        for i in range(offset, min(total, offset + PAGE)):
            try:
                reply["rows"].append(_row(f"{path}[{i}]", f"[{i}]", value[i]))
            except Exception as ex:  # noqa: BLE001
                reply["rows"].append({"name": f"[{i}]", "path": f"{path}[{i}]", "kind": "error", "type": "unreadable", "value": f"{type(ex).__name__}: {ex}", "editable": False, "openable": False})
    else:
        reply["rows"].append(_row(path, path.rsplit(".", 1)[-1], value))
    return reply


def _convert(current: Any, kind: str, raw: Any) -> Any:
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        raise InspectError(400, "that field takes true or false")
    if kind in ("int", "enum"):
        if isinstance(raw, bool):
            raise InspectError(400, "that field takes a whole number")
        try:
            number = int(raw) if not isinstance(raw, float) or raw == int(raw) else None
            if number is None:
                raise ValueError
        except (TypeError, ValueError):
            if kind == "enum" and isinstance(raw, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", raw):
                try:
                    return getattr(type(current), raw)
                except Exception:  # noqa: BLE001
                    raise InspectError(400, f"{raw} isn't one of this field's values") from None
            raise InspectError(400, "that field takes a whole number") from None
        if kind == "enum":
            try:
                return type(current)(number)
            except Exception:  # noqa: BLE001
                raise InspectError(400, f"{number} isn't one of this field's values") from None
        return number
    if kind == "float":
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise InspectError(400, "that field takes a number") from None
        if not math.isfinite(number) or isinstance(raw, bool):
            raise InspectError(400, "that field takes an ordinary number")
        return number
    if kind == "text":
        if not isinstance(raw, str) or len(raw) > MAX_TEXT:
            raise InspectError(400, f"that field takes text up to {MAX_TEXT} characters")
        return raw
    raise InspectError(400, f"a {kind} can't be edited here")


def set_value(params: dict[str, Any]) -> dict[str, Any]:
    if params.get("confirm") is not True:
        raise InspectError(400, 'writing needs {"confirm": true}')
    path = params.get("path")
    current, parent, last = _resolve(path) if isinstance(path, str) else (None, None, None)
    if last is None or parent is None:
        raise InspectError(400, "pick a single field or list entry")
    kind = _kind(current)
    new = _convert(current, kind, params.get("value"))
    name, index = last
    try:
        if index is not None:
            parent[index] = new
        else:
            setattr(parent, name, new)
    except Exception as ex:  # noqa: BLE001
        raise InspectError(501, f"the game refused the write ({type(ex).__name__}: {ex})") from None
    after = _resolve(path)[0]
    applied = {"path": path, "kind": kind, "before": _summary(current, kind), "requested": _summary(new, _kind(new)), "after": _summary(after, _kind(after))}
    logging.info(f"RTSE: inspector {applied}")
    parent_path = path[: path.rindex("[")] if index is not None else path.rsplit(".", 1)[0]
    return {**look({"path": parent_path, "offset": params.get("offset") or 0}), "applied": applied}
