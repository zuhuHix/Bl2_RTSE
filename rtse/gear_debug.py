"""Gear debug dumps: write raw game data to JSON files next to this module, to find real field names.

Nothing here changes the game.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from unrealsdk import find_all, logging, unreal

from . import stats
from .gear_inventory import _find_item, _item_id, _manager, _walk_chain
from .gear_parts import PartRef, _part_effects, _part_object, _part_universe, _struct_summary, _value_summary
from .routing import ApiError, Params, Route

DEBUG_DUMP_FILE = Path(__file__).parent / "debug_item.json"
DEBUG_INVENTORY_FILE = Path(__file__).parent / "debug_inventory.json"
DEBUG_PART_FILE = Path(__file__).parent / "debug_part.json"
DEBUG_MESH_FILE = Path(__file__).parent / "debug_meshes.json"
DEBUG_GESTALT_FILE = Path(__file__).parent / "debug_gestalt.json"
GESTALTS_FILE = Path(__file__).parent / "gestalts.json"
COVERAGE_FILE = Path(__file__).parent / "debug_coverage.json"
MODELS_MANIFEST = Path(__file__).parent / "web" / "models" / "parts.json"

def debug_part(params: Params) -> dict[str, Any]:
    """Dumps a part's simple fields and raw effects, to refine how effects are read."""
    part = _part_object(params)
    simple: dict[str, Any] = {}
    for prop in part.Class._fields():  # noqa: SLF001
        try:
            value = getattr(part, prop.Name)
        except Exception:  # noqa: BLE001, S112
            continue
        if isinstance(value, unreal.UObject):
            simple[prop.Name] = value._path_name()  # noqa: SLF001
        elif value is None or isinstance(value, (bool, int, float, str)):
            simple[prop.Name] = value
        elif isinstance(value, Enum):
            simple[prop.Name] = value.name
        else:
            simple[prop.Name] = f"<{type(value).__name__}>"
    out = {"id": params["id"], "fields": simple, "effects": _part_effects(part)}
    DEBUG_PART_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_PART_FILE), "effects": len(out["effects"])}



MAX_SOCKETS = 80


def _object_summary(obj: Any) -> dict[str, Any]:
    """Simple fields of an object, with struct fields flattened one level (e.g. a socket's location)."""
    out: dict[str, Any] = {}
    for prop in obj.Class._fields():  # noqa: SLF001
        try:
            value = getattr(obj, prop.Name)
        except Exception:  # noqa: BLE001, S112
            continue
        if isinstance(value, unreal.UObject):
            out[prop.Name] = value._path_name()  # noqa: SLF001
        elif isinstance(value, Enum):
            out[prop.Name] = value.name
        elif value is None or isinstance(value, (bool, int, float, str)):
            out[prop.Name] = value
        elif hasattr(value, "_type"):
            try:
                out[prop.Name] = _struct_summary(value)
            except Exception:  # noqa: BLE001, S110
                pass
    return out


def _mesh_report(component: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": component._path_name(), "fields": _object_summary(component)}  # noqa: SLF001
    mesh = getattr(component, "SkeletalMesh", None)
    if mesh is None:
        return entry
    entry["skeletal_mesh"] = mesh._path_name()  # noqa: SLF001
    entry["mesh_fields"] = _object_summary(mesh)
    try:
        entry["sockets"] = [_object_summary(s) for s in list(mesh.Sockets)[:MAX_SOCKETS]]
    except Exception as ex:  # noqa: BLE001
        entry["sockets"] = f"<error {ex!r}>"
    try:
        entry["bones"] = [_struct_summary(b) for b in list(mesh.RefSkeleton)[:MAX_SOCKETS]]
    except Exception as ex:  # noqa: BLE001
        entry["bones"] = f"<error {ex!r}>"
    return entry


def debug_meshes(params: Params) -> dict[str, Any]:
    """For one weapon: which mesh each part uses, plus the built weapon mesh with its sockets and bones."""
    item, _location = _find_item(params)
    out: dict[str, Any] = {"item": item.GetHumanReadableName(), "class": item.Class.Name, "components": {}, "parts": {}}

    for name in ("FirstPersonMesh", "ThirdPersonMesh"):
        try:
            component = getattr(item, name)
            out["components"][name] = None if component is None else _mesh_report(component)
        except Exception as ex:  # noqa: BLE001
            out["components"][name] = f"<error {ex!r}>"

    data = item.DefinitionData
    for prop in data._type._fields():  # noqa: SLF001
        try:
            part = getattr(data, prop.Name)
        except Exception:  # noqa: BLE001, S112
            continue
        if not isinstance(part, unreal.UObject):
            continue
        summary = _object_summary(part)
        mesh_refs = {k: v for k, v in summary.items() if "Mesh" in k and isinstance(v, str)}
        out["parts"][prop.Name] = {"path": part._path_name(), "class": part.Class.Name, "mesh_refs": mesh_refs, "fields": summary}  # noqa: SLF001

    DEBUG_MESH_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_MESH_FILE), "parts": len(out["parts"])}



def _deep_summary(obj: Any) -> dict[str, Any]:
    """Every data property of an object, arrays included, without the built-in functions."""
    out: dict[str, Any] = {}
    for prop in obj.Class._fields():  # noqa: SLF001
        try:
            value = _value_summary(getattr(obj, prop.Name))
        except Exception as ex:  # noqa: BLE001
            value = f"<error {ex!r}>"
        if isinstance(value, str) and value.startswith(("<BoundFunction", "<UnrealEnum", "<WrappedMultiMap")):
            continue
        out[prop.Name] = value
    return out


def debug_gestalt(params: Params) -> dict[str, Any]:
    """Dumps the weapon type's gestalt definition: how part names map onto the shared gestalt mesh."""
    item, _location = _find_item(params)
    weapon_type = item.DefinitionData.WeaponTypeDefinition
    if weapon_type is None:
        raise ApiError(400, "this weapon has no weapon type")
    gestalt = weapon_type.GestaltMesh
    if gestalt is None:
        raise ApiError(404, "this weapon type has no gestalt mesh")

    out: dict[str, Any] = {
        "weapon_type": weapon_type._path_name(),  # noqa: SLF001
        "gestalt": gestalt._path_name(),  # noqa: SLF001
        "gestalt_class": gestalt.Class.Name,
        "definition": _deep_summary(gestalt),
    }
    for name in ("SkeletalMesh", "GestaltSkeletalMesh"):
        mesh = getattr(gestalt, name, None)
        if mesh is not None:
            out[f"{name}_object"] = _deep_summary(mesh)
    DEBUG_GESTALT_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_GESTALT_FILE), "fields": len(out["definition"])}


def export_gestalts(_params: Params) -> dict[str, Any]:
    """Writes every gestalt mesh definition: part name -> triangle slice, sockets and part bounds.

    A gestalt is one shared mesh per gun type holding every part; this table says which slice of its
    triangle list is which part, which is what a 3D builder needs.
    """
    out: dict[str, Any] = {}
    for definition in find_all("GestaltSkeletalMeshDefinition"):
        if definition.Name.startswith("Default__"):
            continue
        entry: dict[str, Any] = {"path": definition._path_name()}  # noqa: SLF001
        for field in ("GestaltInfos", "GestaltSocketMappings", "GestaltPartBounds", "GestaltAccessoryNames"):
            try:
                entry[field] = _value_summary(getattr(definition, field))
            except Exception as ex:  # noqa: BLE001
                entry[field] = f"<error {ex!r}>"
        mesh = getattr(definition, "GestaltSkeletalMesh", None)
        if mesh is not None:
            entry["mesh"] = mesh._path_name()  # noqa: SLF001
            try:
                entry["mesh_sockets"] = [_object_summary(s) for s in list(mesh.Sockets)[:200]]
            except Exception as ex:  # noqa: BLE001
                entry["mesh_sockets"] = f"<error {ex!r}>"
        # Keyed by full path: the game holds more than one version (e.g. Weap_Pistol and
        # Remaster_Weap_Pistol) with the same short name and different geometry.
        out[definition._path_name()] = entry  # noqa: SLF001
    GESTALTS_FILE.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return {"saved_to": str(GESTALTS_FILE), "gestalts": sorted(out)}


def export_coverage(_params: Params) -> dict[str, Any]:
    """Compares every part the game knows about with the meshes we have models for."""
    manifest = json.loads(MODELS_MANIFEST.read_text(encoding="utf-8"))
    known = {name for model in manifest.values() for name in model["parts"]}

    report: dict[str, Any] = {"models": {key: len(model["parts"]) for key, model in manifest.items()}}
    for kind, weapon in (("weapon", True), ("item", False)):
        parts: dict[str, PartRef] = {}
        for refs in _part_universe(weapon=weapon).values():
            parts.update(refs)
        with_meshes = [ref for ref in parts.values() if ref.meshes]
        uncovered = [ref for ref in with_meshes if any(m not in known for m in ref.meshes)]
        report[kind] = {
            "parts_total": len(parts),
            "parts_naming_a_mesh": len(with_meshes),
            "parts_without_mesh_name": len(parts) - len(with_meshes),
            "parts_whose_mesh_we_lack": len(uncovered),
            "meshes_we_lack": sorted({m for ref in uncovered for m in ref.meshes if m not in known}),
            "examples": [{"part": ref.id, "meshes": list(ref.meshes)} for ref in uncovered[:120]],
        }
    COVERAGE_FILE.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return {
        "saved_to": str(COVERAGE_FILE),
        "weapon_parts": report["weapon"]["parts_total"],
        "weapon_missing": report["weapon"]["parts_whose_mesh_we_lack"],
        "item_parts": report["item"]["parts_total"],
        "item_missing": report["item"]["parts_whose_mesh_we_lack"],
    }



def debug_item(params: Params) -> dict[str, Any]:
    """Dumps every readable scalar field of an item and its DefinitionData, to find real field names."""
    item, _location = _find_item(params)

    def dump(owner_type: Any, obj: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for prop in owner_type._fields():  # noqa: SLF001
            name = prop.Name
            try:
                value = getattr(obj, name)
            except Exception as ex:  # noqa: BLE001
                out[name] = f"<error {ex!r}>"
                continue
            if value is None or isinstance(value, (bool, int, float, str)):
                out[name] = value
            elif isinstance(value, unreal.UObject):
                out[name] = value._path_name()  # noqa: SLF001
            else:
                out[name] = f"<{type(value).__name__}>"
        return out

    data = item.DefinitionData
    # UniqueId is what per-gun stat overrides are keyed on. Whether it survives a save and reload is unverified.
    unique_id = stats.unique_id(item)
    result = {
        "name": item.GetHumanReadableName(),
        "unique_id": unique_id,  # 0 when unreadable
        "identity": stats.identity(item),
        "item": dump(item.Class, item),
        "definition_data": dump(data._type, data),  # noqa: SLF001
    }
    logging.info(f"RTSE: debug_item {result['name']!r} UniqueId={unique_id} identity={result['identity']}")
    DEBUG_DUMP_FILE.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return {
        "saved_to": str(DEBUG_DUMP_FILE),
        "fields": len(result["item"]) + len(result["definition_data"]),
        "unique_id": unique_id,
    }


def debug_inventory(_params: Params) -> dict[str, Any]:
    """Dumps the inventory manager's fields and chains, to verify where equipped gear lives."""
    manager = _manager()
    out: dict[str, Any] = {"fields": {}, "chains": {}}
    for prop in manager.Class._fields():  # noqa: SLF001
        name = prop.Name
        try:
            value = getattr(manager, name)
        except Exception as ex:  # noqa: BLE001
            out["fields"][name] = f"<error {ex!r}>"
            continue
        if value is None or isinstance(value, (bool, int, float, str)):
            out["fields"][name] = value
        elif isinstance(value, unreal.UObject):
            out["fields"][name] = value._path_name()  # noqa: SLF001
        else:
            out["fields"][name] = f"<{type(value).__name__}>"

    for chain_name in ("InventoryChain", "ItemChain"):
        try:
            out["chains"][chain_name] = [
                {"class": i.Class.Name, "path": _item_id(i), "name": i.GetHumanReadableName()}
                for i in _walk_chain(getattr(manager, chain_name))
            ]
        except Exception as ex:  # noqa: BLE001
            out["chains"][chain_name] = f"<error {ex!r}>"
    out["backpack_count"] = len([i for i in manager.Backpack if i is not None])

    DEBUG_INVENTORY_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_INVENTORY_FILE), "chains": {k: len(v) if isinstance(v, list) else v for k, v in out["chains"].items()}}


# --- Routes (paths are the API; api.py collects these) ---

ROUTE_ERROR = ApiError

GET_ROUTES: dict[str, Route] = {
    "/api/debug/item": debug_item,
    "/api/debug/inventory": debug_inventory,
    "/api/debug/part": debug_part,
    "/api/debug/meshes": debug_meshes,
    "/api/debug/gestalt": debug_gestalt,
    "/api/debug/gestalts": export_gestalts,
    "/api/debug/coverage": export_coverage,
}
POST_ROUTES: dict[str, Route] = {}
