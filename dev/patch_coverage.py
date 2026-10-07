from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:80]!r} (found {text.count(old)})"
    return text.replace(old, new)


api = Path("rtse/api.py")
a = api.read_text(encoding="utf-8")

a = replace_once(
    a,
    'GESTALTS_FILE = Path(__file__).parent / "gestalts.json"',
    'GESTALTS_FILE = Path(__file__).parent / "gestalts.json"\nCOVERAGE_FILE = Path(__file__).parent / "debug_coverage.json"\nMODELS_MANIFEST = Path(__file__).parent / "web" / "models" / "parts.json"',
)

a = replace_once(
    a,
    "def rescan_parts(",
    '''def export_coverage(_params: Params) -> dict[str, Any]:
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


def rescan_parts(''',
)
a = replace_once(a, '    "/api/debug/gestalts": export_gestalts,\n', '    "/api/debug/gestalts": export_gestalts,\n    "/api/debug/coverage": export_coverage,\n')
api.write_text(a, encoding="utf-8")

js = Path("rtse/web/app.js")
s = js.read_text(encoding="utf-8")
s = replace_once(
    s,
    '      h("button", { class: "btn sm", onclick: () => dump("/api/debug/gestalts") }, "Save ALL gestalt tables"),',
    '      h("button", { class: "btn sm", onclick: () => dump("/api/debug/gestalts") }, "Save ALL gestalt tables"),\n      h("button", { class: "btn sm", onclick: () => dump("/api/debug/coverage") }, "Save mesh coverage report"),',
)
js.write_text(s, encoding="utf-8")
print("patched")
