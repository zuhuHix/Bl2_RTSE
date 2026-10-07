from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:80]!r} (found {text.count(old)})"
    return text.replace(old, new)


web = Path("rtse/web")
js = (web / "app.js").read_text(encoding="utf-8")

# ---- state + lazy viewer module ----
js = replace_once(
    js,
    "  effects: new Map(),\n};\n",
    '''  effects: new Map(),
  view3d: localStorage.getItem("rtse.view3d") !== "off", // 3D gun view (falls back to the blueprint)
};

// viewer3d.js (and three.js) load on demand; if WebGL or the models are unavailable we fall back.
let v3dModule = null;
let v3dFailed = false;
const stage = { host: null, viewer: null }; // the main 3D view; its canvas survives re-renders
let hover3d = null;

async function ensureViewerModule() {
  if (v3dModule || v3dFailed) return v3dModule;
  try {
    v3dModule = await import("./viewer3d.js");
  } catch (error) {
    console.warn("3D viewer unavailable:", error);
    v3dFailed = true;
  }
  return v3dModule;
}
''',
)

# ---- helpers: model key, piece colours ----
js = replace_once(
    js,
    "function rowPlate(item) {",
    '''/** Which gestalt model draws this item, or null if it has none. */
function modelKeyFor(detail) {
  if (isWeapon(detail)) return weaponTypeFrom((entryFor(detail.id) || {}).type_path);
  return { WillowShield: "Shield", WillowGrenadeMod: "Grenade", WillowArtifact: "Relic" }[detail.class] || null;
}

function mixHex(a, b, t) {
  const pa = parseInt(a.slice(1), 16);
  const pb = parseInt(b.slice(1), 16);
  const channel = (shift) => Math.round(((pa >> shift) & 255) * (1 - t) + ((pb >> shift) & 255) * t);
  return `#${((1 << 24) | (channel(16) << 16) | (channel(8) << 8) | channel(0)).toString(16).slice(1)}`;
}

/** Paint colour for a part in 3D: its maker's colour, nudged by what kind of part it is. */
function pieceColor(part, slotName) {
  const info = parsePart(part.name, part.group);
  let base = info.manufacturer ? manufacturerColor(info.manufacturer) : STEEL;
  if (info.element) base = ELEMENTS[info.element].color;
  if (slotName === "Barrel") return mixHex(base, "#aab2bc", 0.5);
  if (slotName === "Grip") return mixHex(base, "#7a5230", 0.55);
  if (slotName === "Stock") return mixHex(base, "#7a5230", 0.4);
  if (slotName === "Sight") return mixHex(base, "#7fb4e6", 0.4);
  if (slotName.startsWith("Accessory")) return mixHex(base, "#f3ecd9", 0.3);
  return base;
}

function piecesOf(part, slotName) {
  return (part.meshes || []).map((mesh) => ({ slot: slotName, mesh, color: pieceColor(part, slotName) }));
}

/** Everything currently on the item, as 3D pieces. */
function piecesFor(detail) {
  return detail.slots.flatMap((slot) => (slot.current ? piecesOf(slot.current, slot.slot) : []));
}

function rowPlate(item) {''',
)

# ---- hero diagram: 3D stage or blueprint ----
start = js.index("function renderDiagram(detail, entry, weapon) {")
end = js.index("/** Highlights a slot in both the legend and the diagram. */")
js = js[:start] + '''function renderDiagram(detail, entry, weapon) {
  const key = modelKeyFor(detail);
  const pieces = piecesFor(detail);
  const can3d = Boolean(key && pieces.length && !v3dFailed);
  if (state.view3d && can3d) return renderStage(detail, key, pieces);
  return renderBlueprint(detail, entry, weapon, can3d);
}

function setView3d(on) {
  state.view3d = on;
  localStorage.setItem("rtse.view3d", on ? "on" : "off");
  renderWorkspace();
}

function renderStage(detail, key, pieces) {
  if (!stage.host) stage.host = h("div", { class: "stage" });
  const view = (name, label) => h("button", { class: "btn sm", onclick: () => stage.viewer && stage.viewer.setView(name) }, label);
  const box = h("div", { class: "diagram" },
    stage.host,
    h("div", { class: "stage-tools" }, view("side", "Side"), view("angle", "Angle"), view("top", "Top"),
      h("button", { class: "btn sm ghost", onclick: () => setView3d(false) }, "Blueprint")),
    h("div", { class: "cap" }, h("i", { text: "Drag to orbit" }), "  /  click a part to swap it  /  colours show who made each part"));

  ensureViewerModule().then(async (module) => {
    if (!module) return renderWorkspace();
    try {
      stage.viewer ||= module.createViewer(stage.host, {
        onPick: (slot) => openPicker(slot),
        onHover: (slot) => {
          if (hover3d === slot) return;
          if (hover3d) setHot(hover3d, false);
          hover3d = slot;
          if (slot) setHot(slot, true);
        },
      });
      await stage.viewer.setBuild(key, pieces, detail.hide_bones || []);
    } catch (error) {
      console.warn("3D build failed:", error);
      v3dFailed = true;
      renderWorkspace();
    }
  });
  return box;
}

function renderBlueprint(detail, entry, weapon, can3d) {
  const toggle = can3d && h("button", { class: "btn sm", onclick: () => setView3d(true) }, "3D view");
  if (!weapon) {
    const mfr = itemManufacturer(detail.name);
    return h("div", { class: "diagram" },
      h("div", { class: "item-big", vars: { "--c": manufacturerColor(mfr), "--cut": "#0b0a09" }, html: itemClassSvg(detail.class) }),
      toggle && h("div", { class: "stage-tools" }, toggle),
      h("div", { class: "cap" }, classLabel(detail.class)));
  }
  const markers = detail.slots.map((slot, index) => ({ region: SLOT_REGION[slot.slot], label: String(index + 1) }));
  const type = gunFor(weaponTypeFrom(entry.type_path));
  const box = h("div", { class: "diagram" },
    h("div", { class: "gunwrap", html: gunSvg(type, { fills: regionFills(detail), markers, className: "interactive", titles: regionTitles(detail) }) }),
    toggle && h("div", { class: "stage-tools" }, toggle),
    h("div", { class: "cap" }, h("i", { text: "Click a part" }), " to swap it  /  colours show who made each part"));

  const slotOf = (region) => Object.keys(SLOT_REGION).find((name) => SLOT_REGION[name] === region);
  const svg = box.querySelector("svg");
  svg.addEventListener("click", (event) => {
    const target = event.target.closest("[data-region]");
    if (target) openPicker(slotOf(target.dataset.region));
  });
  svg.addEventListener("mouseover", (event) => {
    const target = event.target.closest("[data-region]");
    if (target) setHot(slotOf(target.dataset.region), true);
  });
  svg.addEventListener("mouseout", (event) => {
    const target = event.target.closest("[data-region]");
    if (target) setHot(slotOf(target.dataset.region), false);
  });
  return box;
}

''' + js[end:]

# setHot also lights the 3D piece
js = replace_once(
    js,
    "  const region = document.querySelector(`.gun [data-region=\"${SLOT_REGION[slotName]}\"].r`);\n  if (region) region.classList.toggle(\"hot\", on);\n}",
    "  const region = document.querySelector(`.gun [data-region=\"${SLOT_REGION[slotName]}\"].r`);\n  if (region) region.classList.toggle(\"hot\", on);\n  if (stage.viewer) stage.viewer.highlight(on ? slotName : null);\n}",
)

# ---- picker: own small 3D view that previews the hovered/chosen part on the gun ----
js = replace_once(
    js,
    "  state.picker = { slot: slotName, query: \"\", mfr: null, type: null, shown: PAGE, chosen: null, loading: false };\n  renderPicker();\n}",
    "  state.picker = { slot: slotName, query: \"\", mfr: null, type: null, shown: PAGE, chosen: null, loading: false, stageHost: null, viewer: null, timer: null };\n  renderPicker();\n}",
)
js = replace_once(
    js,
    "function closePicker() {\n  state.picker = null;\n  fill($(\"#picker-root\"));\n}",
    '''function closePicker() {
  const p = state.picker;
  if (p) {
    clearTimeout(p.timer);
    if (p.viewer) p.viewer.dispose();
  }
  state.picker = null;
  fill($("#picker-root"));
}

/** Shows the gun in the picker with `option` (or nothing) swapped into the slot being edited. */
function syncPreviewViewer(option) {
  const p = state.picker;
  if (!p) return;
  clearTimeout(p.timer);
  p.timer = setTimeout(async () => {
    const detail = state.detail;
    const key = detail && modelKeyFor(detail);
    const pieces = detail ? piecesFor(detail) : [];
    if (!state.view3d || v3dFailed || !key || !pieces.length || state.picker !== p) return;
    const module = await ensureViewerModule();
    if (!module || state.picker !== p) return;
    try {
      p.viewer ||= module.createViewer(p.stageHost, {});
      await p.viewer.setBuild(key, pieces, detail.hide_bones || []);
      const swapped = option && option.meshes && option.meshes.length ? piecesOf(option, p.slot) : null;
      await p.viewer.setPreview(p.slot, swapped);
    } catch (error) {
      console.warn("3D preview failed:", error);
    }
  }, 60);
}

function previewStage(p) {
  const detail = state.detail;
  const key = detail && modelKeyFor(detail);
  if (!state.view3d || v3dFailed || !key || !piecesFor(detail).length) return null;
  p.stageHost ||= h("div", { class: "stage stage-mini" });
  return p.stageHost;
}''',
)

# hovering a card previews it on the gun; leaving restores the chosen part
js = replace_once(
    js,
    "        onclick: () => choose(option),\n        ondblclick: () => applyPart(slot.slot, option),\n      },",
    "        onclick: () => choose(option),\n        ondblclick: () => applyPart(slot.slot, option),\n        onmouseenter: () => syncPreviewViewer(option),\n        onmouseleave: () => syncPreviewViewer(p.chosen),\n      },",
)

# preview panel: 3D view on top (when available), otherwise the drawn plate
js = replace_once(
    js,
    '''  if (!option) {
    fill(
      holder,
      h("h3", { text: "Pick a part" }),''',
    '''  const stageEl = previewStage(p);
  if (!option) {
    fill(
      holder,
      stageEl,
      h("h3", { text: "Pick a part" }),''',
)
js = replace_once(js, "  const { info, el } = partPlate(option, slot.slot, detail, \"lg\");\n", "  const { info, el } = partPlate(option, slot.slot, detail, \"lg\");\n  syncPreviewViewer(option);\n")
js = replace_once(js, "  fill(\n    holder,\n    el,\n    h(\"div\", {}, h(\"h3\", { text: info.title }),", "  fill(\n    holder,\n    stageEl || el,\n    h(\"div\", {}, h(\"h3\", { text: info.title }),")

# open the picker with the current gun already shown
js = replace_once(js, "  renderFilters();\n  renderResults();\n  renderPreview();\n  searchBox.focus();\n}", "  renderFilters();\n  renderResults();\n  renderPreview();\n  syncPreviewViewer(p.chosen);\n  searchBox.focus();\n}")
(web / "app.js").write_text(js, encoding="utf-8")

# ---- index.html: import map so three.js resolves locally ----
html = (web / "index.html").read_text(encoding="utf-8")
html = replace_once(
    html,
    '  <script type="module" src="app.js"></script>',
    '  <script type="importmap">{ "imports": { "three": "./vendor/three/three.module.js", "three/addons/": "./vendor/three/" } }</script>\n  <script type="module" src="app.js"></script>',
)
(web / "index.html").write_text(html, encoding="utf-8")

# ---- css ----
css = (web / "app.css").read_text(encoding="utf-8")
css = replace_once(
    css,
    ".item-art { width: 100%; height: 100%; }",
    '''.stage { position: relative; width: 100%; height: 420px; }
.stage-canvas { position: absolute; inset: 0; width: 100%; height: 100%; display: block; cursor: grab; touch-action: none; }
.stage-canvas:active { cursor: grabbing; }
.stage-mini { height: 210px; border: 3px solid #000; background: radial-gradient(circle at 50% 45%, #2b2619, #15130f 70%); box-shadow: inset 0 0 0 1px var(--line); flex: none; }
.stage-tools { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; }
.item-art { width: 100%; height: 100%; }''',
)
(web / "app.css").write_text(css, encoding="utf-8")
print("patched")
