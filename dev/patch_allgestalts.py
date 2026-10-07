from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


api = Path("rtse/api.py")
a = api.read_text(encoding="utf-8")

a = replace_once(
    a,
    'DEBUG_GESTALT_FILE = Path(__file__).parent / "debug_gestalt.json"',
    'DEBUG_GESTALT_FILE = Path(__file__).parent / "debug_gestalt.json"\nGESTALTS_FILE = Path(__file__).parent / "gestalts.json"',
)

a = replace_once(
    a,
    "def rescan_parts(",
    '''def export_gestalts(_params: Params) -> dict[str, Any]:
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
        out[definition.Name] = entry
    GESTALTS_FILE.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return {"saved_to": str(GESTALTS_FILE), "gestalts": sorted(out)}


def rescan_parts(''',
)
a = replace_once(a, '    "/api/debug/gestalt": debug_gestalt,\n', '    "/api/debug/gestalt": debug_gestalt,\n    "/api/debug/gestalts": export_gestalts,\n')
api.write_text(a, encoding="utf-8")

js = Path("rtse/web/app.js")
s = js.read_text(encoding="utf-8")
s = replace_once(
    s,
    '      h("button", { class: "btn sm", onclick: () => dump("/api/debug/inventory") }, "Save inventory dump"),',
    '      h("button", { class: "btn sm", onclick: () => dump("/api/debug/inventory") }, "Save inventory dump"),\n      h("button", { class: "btn sm", onclick: () => dump("/api/debug/gestalts") }, "Save ALL gestalt tables"),',
)
js.write_text(s, encoding="utf-8")
print("patched")
