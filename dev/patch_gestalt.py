from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


api = Path("rtse/api.py")
a = api.read_text(encoding="utf-8")

a = replace_once(
    a,
    'DEBUG_MESH_FILE = Path(__file__).parent / "debug_meshes.json"',
    'DEBUG_MESH_FILE = Path(__file__).parent / "debug_meshes.json"\nDEBUG_GESTALT_FILE = Path(__file__).parent / "debug_gestalt.json"',
)

a = replace_once(
    a,
    "def rescan_parts(",
    '''MAX_ARRAY_ITEMS = 600


def _value_summary(value: Any, depth: int = 0) -> Any:
    """Any property value as JSON: scalars, object paths, structs and (bounded) arrays."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, unreal.UObject):
        return value._path_name()  # noqa: SLF001
    if isinstance(value, Enum):
        return value.name
    if hasattr(value, "_type"):
        return _struct_summary(value) if depth < 3 else "<struct>"
    try:
        items = list(value)
    except TypeError:
        return f"<{type(value).__name__}>"
    return {
        "length": len(items),
        "items": [_value_summary(v, depth + 1) for v in items[:MAX_ARRAY_ITEMS]],
    }


def _deep_summary(obj: Any) -> dict[str, Any]:
    """Every property of an object, arrays included (which _object_summary skips)."""
    out: dict[str, Any] = {}
    for prop in obj.Class._fields():  # noqa: SLF001
        try:
            out[prop.Name] = _value_summary(getattr(obj, prop.Name))
        except Exception as ex:  # noqa: BLE001
            out[prop.Name] = f"<error {ex!r}>"
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


def rescan_parts(''',
)
a = replace_once(a, '    "/api/debug/meshes": debug_meshes,\n', '    "/api/debug/meshes": debug_meshes,\n    "/api/debug/gestalt": debug_gestalt,\n')
api.write_text(a, encoding="utf-8")

js = Path("rtse/web/app.js")
s = js.read_text(encoding="utf-8")
s = replace_once(
    s,
    '"Save mesh probe"),',
    '"Save mesh probe"),\n      detail.class === "WillowWeapon" && h("button", { class: "btn sm", onclick: () => dump(`/api/debug/gestalt?id=${encodeURIComponent(detail.id)}`) }, "Save gestalt probe"),',
)
js.write_text(s, encoding="utf-8")
print("patched")
