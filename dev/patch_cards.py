from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:80]!r} (found {text.count(old)})"
    return text.replace(old, new)


js = Path("rtse/web/app.js")
s = js.read_text(encoding="utf-8")

s = replace_once(s, "  effects: new Map(),\n", "  effects: new Map(),\n  owners: {}, // \"weapon\" | \"item\" -> Map(part id -> [how many guns roll it, first few gun names])\n")

# --- gun name + thumbnails, right after partPlate ---
s = replace_once(
    s,
    "/** Region fills for the hero diagram",
    '''// ---------- which gun a part comes from ----------

const SPECIFIC_MAX = 3; // a part only this many guns can roll counts as theirs; more than that is a shared generic part

function loadOwners(detail) {
  const kind = isWeapon(detail) ? "weapon" : "item";
  if (state.owners[kind]) return;
  state.owners[kind] = new Map();
  api("GET", `/api/part_owners?kind=${kind}`).then((data) => {
    state.owners[kind] = new Map(Object.entries(data.owners));
    if (state.picker) { renderResults(true); renderPreview(); }
  }).catch((error) => console.warn("Could not read which guns own each part:", error));
}

function ownerEntry(option, detail) {
  const map = state.owners[isWeapon(detail) ? "weapon" : "item"];
  return map ? map.get(option.id) : undefined;
}

/** The card's title: the gun this part comes with. Shared parts get their maker and gun type instead. */
function gunName(option, info, detail) {
  const owned = ownerEntry(option, detail);
  if (owned && owned[0] <= SPECIFIC_MAX) return owned[0] > 1 ? `${owned[1]} +${owned[0] - 1}` : owned[1];
  const maker = info.manufacturer && MANUFACTURERS[info.manufacturer].label;
  const kind = isWeapon(detail) ? info.weaponType : classLabel(detail.class);
  return maker && kind ? `${maker} ${kind}` : info.title;
}

function ownerTooltip(option, detail) {
  const owned = ownerEntry(option, detail);
  if (!owned) return option.id;
  const names = owned.slice(1).join(", ");
  return `${option.id}\\n${owned[0] > owned.length - 1 ? `On ${owned[0]} guns, e.g. ${names}` : `On: ${names}`}`;
}

/** A small plate for the card: the SVG sketch at first, replaced by a render of the real part once it is on screen. */
function partThumb(option, slotName, detail) {
  const { el } = partPlate(option, slotName, detail, "thumb");
  const p = state.picker;
  if (p && p.watcher && option.meshes && option.meshes.length && !v3dFailed) {
    p.lazy.set(el, async () => {
      const module = await ensureViewerModule();
      if (!module || !module.partThumbnail) return;
      const url = await module.partThumbnail(option.meshes, pieceColor(option, slotName), 104, 66);
      if (!url) return;
      const img = new Image();
      img.alt = "";
      img.src = url;
      el.classList.add("lit");
      el.replaceChildren(img);
    });
    p.watcher.observe(el);
  }
  return el;
}

/** Region fills for the hero diagram''',
)

# --- picker state ---
s = replace_once(
    s,
    "loading: false, stageHost: null, viewer: null, timer: null };",
    "loading: false, stageHost: null, viewer: null, timer: null, watcher: null, lazy: new WeakMap() };",
)
s = replace_once(
    s,
    "    clearTimeout(p.timer);\n    if (p.viewer) p.viewer.dispose();",
    "    clearTimeout(p.timer);\n    if (p.watcher) p.watcher.disconnect();\n    if (p.viewer) p.viewer.dispose();",
)

# --- search by gun name too ---
s = replace_once(
    s,
    "    && words.every((w) => entry.hay.includes(w)));",
    "    && words.every((w) => entry.hay.includes(w) || ownerHay(entry.option).includes(w)));",
)
s = replace_once(
    s,
    "function filteredOptions(slot) {",
    '''function ownerHay(option) {
  const owned = ownerEntry(option, state.detail);
  return owned ? owned.slice(1).join(" ").toLowerCase() : "";
}

function filteredOptions(slot) {''',
)

# --- observer + owners when the picker opens ---
s = replace_once(
    s,
    "  const current = slot.current ? parsePart(slot.current.name, slot.current.group) : null;\n\n  fill(\n    $(\"#picker-root\"),",
    '''  const current = slot.current ? parsePart(slot.current.name, slot.current.group) : null;
  if (p.watcher) p.watcher.disconnect();
  p.watcher = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      p.watcher.unobserve(entry.target);
      const load = p.lazy.get(entry.target);
      if (load) load();
    }
  }, { root: results, rootMargin: "300px" });
  loadOwners(detail);

  fill(
    $("#picker-root"),''',
)

# --- the card ---
s = replace_once(
    s,
    '''        title: foreign ? `${option.id}\\n(from a ${info.weaponType}, not a ${type})` : option.id,''',
    '''        title: foreign ? `${ownerTooltip(option, detail)}\\n(from a ${info.weaponType}, not a ${type})` : ownerTooltip(option, detail),''',
)
s = replace_once(
    s,
    '''      partPlate(option, slot.slot, detail).el,
      h("div", { style: { minWidth: 0 } },
        h("div", { class: "n", text: info.title }),
        h("div", { class: "m" },
          info.manufacturer && emblem(info.manufacturer, 13),
          h("span", { text: [info.manufacturer && MANUFACTURERS[info.manufacturer].label, info.weaponType].filter(Boolean).join(" / ") || option.group }),
          foreign && h("span", { class: "flag", title: "From a different gun type" }, icon("alert", 14)))),''',
    '''      partThumb(option, slot.slot, detail),
      h("div", { style: { minWidth: 0 } },
        h("div", { class: "n", text: gunName(option, info, detail) }),
        h("div", { class: "t" },
          info.manufacturer && emblem(info.manufacturer, 12),
          h("span", { text: option.name }),
          foreign && h("span", { class: "flag", title: "From a different gun type" }, icon("alert", 13)))),''',
)

# --- preview pane header: same naming ---
s = replace_once(
    s,
    '''    h("div", {}, h("h3", { text: info.title }),
      h("div", { class: "tags", style: { marginTop: "8px" } },''',
    '''    h("div", {}, h("h3", { text: gunName(option, info, detail) }),
      h("div", { class: "path", style: { marginTop: "4px" }, text: option.name }),
      h("div", { class: "tags", style: { marginTop: "8px" } },''',
)

# --- rescan also forgets who owns what ---
s = replace_once(
    s,
    'await api("POST", "/api/parts/rescan", {}); toast("Part list cleared");',
    'await api("POST", "/api/parts/rescan", {}); state.owners = {}; toast("Part list cleared");',
)
js.write_text(s, encoding="utf-8")

css = Path("rtse/web/app.css")
c = css.read_text(encoding="utf-8")
c = replace_once(c, "repeat(auto-fill, minmax(236px, 1fr))", "repeat(auto-fill, minmax(280px, 1fr))")
c = replace_once(c, "grid-template-columns: 76px 1fr; gap: 10px; align-items: center; padding: 7px 10px 7px 7px;", "grid-template-columns: 104px 1fr; gap: 11px; align-items: center; padding: 7px 10px 7px 7px;")
c = replace_once(
    c,
    ".plate.md { width: 98px; height: 48px; }",
    ".plate.md { width: 98px; height: 48px; }\n.plate.thumb { width: 104px; height: 66px; background: radial-gradient(circle at 50% 45%, #2b2619, #15130f 75%); }\n.plate.thumb.lit img { object-fit: cover; }",
)
c = replace_once(
    c,
    ".card .flag {",
    ".card .t { font: 11.5px Consolas, monospace; color: var(--muted); margin-top: 4px; display: flex; gap: 6px; align-items: center; min-width: 0; }\n.card .t span:not(.flag) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }\n.card .flag {",
)
css.write_text(c, encoding="utf-8")
print("patched")
