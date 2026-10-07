from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:80]!r} (found {text.count(old)})"
    return text.replace(old, new)


# ---------------- viewer3d.js: render any set of coloured pieces ----------------
v = Path("rtse/web/viewer3d.js")
t = v.read_text(encoding="utf-8")

t = replace_once(
    t,
    "function renderThumb(model, meshes, color, width, height) {\n  const geometries = meshes.map((m) => model.entry.parts[m] && sliceGeometry(model, model.entry.parts[m], new Set())).filter(Boolean);\n  if (!geometries.length) return null;\n",
    "function renderThumb(model, pieces, hidden, view, width, height) {\n  const geometries = pieces\n    .map((p) => model.entry.parts[p.mesh] && { ...sliceGeometry(model, model.entry.parts[p.mesh], hidden), color: p.color })\n    .filter((g) => g && g.geometry);\n  if (!geometries.length) return null;\n",
)
t = replace_once(
    t,
    "  for (const { geometry } of geometries) {\n    const material = new THREE.MeshToonMaterial({ color, gradientMap: gradient, side: THREE.DoubleSide });",
    "  for (const { geometry, color } of geometries) {\n    const material = new THREE.MeshToonMaterial({ color, gradientMap: gradient, side: THREE.DoubleSide });",
)
t = replace_once(t, "const dir = new THREE.Vector3(...VIEWS.angle).normalize();\n  const corners", "const dir = new THREE.Vector3(...(VIEWS[view] || VIEWS.angle)).normalize();\n  const corners")

start = t.index("/** A PNG data URL of one part")
t = t[:start] + '''function queueThumb(key, build) {
  if (!thumbCache.has(key)) {
    const job = thumbQueue.then(async () => {
      try {
        return await build();
      } catch (error) {
        console.warn("thumbnail failed:", error);
        return null;
      }
    });
    thumbQueue = job.then(() => new Promise((r) => requestAnimationFrame(() => r()))); // yield between renders
    thumbCache.set(key, job);
  }
  return thumbCache.get(key);
}

/** A PNG data URL of one part (given the mesh names it draws), or null if we have no model for it. */
export function partThumbnail(meshes, color = "#c9a25a", width = 128, height = 80) {
  return queueThumb(`part|${meshes.join(",")}|${color}|${width}x${height}`, async () => {
    const type = await modelKeyOfMesh(meshes[0]);
    if (!type) return null;
    return renderThumb(await loadModel(type), meshes.map((mesh) => ({ mesh, color })), new Set(), "angle", width, height);
  });
}

/** A PNG data URL of a whole gun: pieces are [{ mesh, color }], hideBones as in setBuild. */
export function buildThumbnail(typeKey, pieces, hideBones = [], view = "side", width = 96, height = 56) {
  const signature = pieces.map((p) => `${p.mesh}:${p.color}`).join(",");
  return queueThumb(`build|${typeKey}|${signature}|${hideBones.join(",")}|${view}|${width}x${height}`, async () => {
    if (!(await hasModel(typeKey))) return null;
    return renderThumb(await loadModel(typeKey), pieces, new Set(hideBones), view, width, height);
  });
}
'''
v.write_text(t, encoding="utf-8")

# ---------------- api.py: the build on each inventory row ----------------
a = Path("rtse/api.py")
s = a.read_text(encoding="utf-8")
s = replace_once(
    s,
    "def items(_params: Params) -> dict[str, Any]:",
    '''def _current_build(item: unreal.UObject) -> list[dict[str, Any]]:
    """The parts on an item right now (name and meshes only), so the list can draw a picture of it."""
    if _is_partless(item):
        return []
    weapon = _is_weapon(item)
    suffix = "PartDefinition" if weapon else "ItemPartDefinition"
    build: list[dict[str, Any]] = []
    for name in WEAPON_SLOTS if weapon else ITEM_SLOTS:
        try:
            part = getattr(item.DefinitionData, f"{name}{suffix}")
        except Exception:  # noqa: BLE001, S112
            continue
        if part is not None:
            ref = _ref_dict(_ref(part))
            build.append({"slot": name, "name": ref["name"], "group": ref["group"], "meshes": ref["meshes"]})
    return build


def items(_params: Params) -> dict[str, Any]:''',
)
s = replace_once(
    s,
    '                "type_path": _weapon_type_path(item),\n            }\n            for item, location in found',
    '                "type_path": _weapon_type_path(item),\n                "build": _current_build(item),\n                "hide_bones": _hidden_bones(item),\n            }\n            for item, location in found',
)
a.write_text(s, encoding="utf-8")

# ---------------- app.js ----------------
js = Path("rtse/web/app.js")
j = js.read_text(encoding="utf-8")
j = replace_once(
    j,
    "function rowPlate(item) {",
    '''const rowThumbs = new Map(); // picture signature -> data URL, so a re-render never flashes back to the sketch

/** Swaps a plate's sketch for a render of the real gun once one is ready. */
function showRender(plate, item) {
  const key = modelKeyFor(item);
  const pieces = (item.build || []).flatMap((part) => piecesOf(part, part.slot));
  if (!key || !pieces.length || v3dFailed) return;
  const signature = `${key}|${pieces.map((p) => `${p.mesh}:${p.color}`).join(",")}|${(item.hide_bones || []).join(",")}`;
  const put = (url) => {
    const img = new Image();
    img.alt = "";
    img.src = url;
    plate.classList.add("lit");
    plate.replaceChildren(img);
  };
  if (rowThumbs.has(signature)) return put(rowThumbs.get(signature));
  ensureViewerModule().then(async (module) => {
    if (!module || !module.buildThumbnail) return;
    const url = await module.buildThumbnail(key, pieces, item.hide_bones || [], "side", 96, 56);
    if (!url) return;
    rowThumbs.set(signature, url);
    if (plate.isConnected) put(url);
  });
}

function rowPlate(item) {
  const plate = rowPlateSketch(item);
  showRender(plate, item);
  return plate;
}

function rowPlateSketch(item) {''',
)
js.write_text(j, encoding="utf-8")

# ---------------- css ----------------
css = Path("rtse/web/app.css")
c = css.read_text(encoding="utf-8")
c = replace_once(c, ".row { display: grid; grid-template-columns: 90px 1fr auto;", ".row { display: grid; grid-template-columns: 112px 1fr auto;")
c = replace_once(
    c,
    ".row .plate { margin-left: 14px; }",
    ".row .plate { margin-left: 14px; width: 96px; height: 56px; }\n.row .plate.lit { background: radial-gradient(circle at 50% 45%, #2b2619, #15130f 75%); }",
)
css.write_text(c, encoding="utf-8")
print("patched")
