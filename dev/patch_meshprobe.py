from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


api = Path("rtse/api.py")
a = api.read_text(encoding="utf-8")

a = replace_once(
    a,
    'DEBUG_PART_FILE = Path(__file__).parent / "debug_part.json"',
    'DEBUG_PART_FILE = Path(__file__).parent / "debug_part.json"\nDEBUG_MESH_FILE = Path(__file__).parent / "debug_meshes.json"',
)

a = replace_once(
    a,
    "def rescan_parts(",
    '''MAX_SOCKETS = 80


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


def rescan_parts(''',
)
a = replace_once(a, '    "/api/debug/part": debug_part,\n', '    "/api/debug/part": debug_part,\n    "/api/debug/meshes": debug_meshes,\n')
api.write_text(a, encoding="utf-8")

js = Path("rtse/web/app.js")
s = js.read_text(encoding="utf-8")
s = replace_once(
    s,
    '      h("button", { class: "btn sm", onclick: () => dump("/api/debug/inventory") }, "Save inventory dump"),',
    '      h("button", { class: "btn sm", onclick: () => dump("/api/debug/inventory") }, "Save inventory dump"),\n      detail.class === "WillowWeapon" && h("button", { class: "btn sm", onclick: () => dump(`/api/debug/meshes?id=${encodeURIComponent(detail.id)}`) }, "Save mesh probe"),',
)
js.write_text(s, encoding="utf-8")
print("patched")
