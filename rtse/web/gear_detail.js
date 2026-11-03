// The gear workbench: the item banner and level, the 3D or blueprint view, legend, stats, info, and the
// part picker (search, filters, preview, bolt-on).
import {
  ELEMENTS, MANUFACTURERS, STAT_GROUPS, classLabel, cleanItemName, describeEffect, formatNumber,
  dlcOwnership, itemManufacturer, manufacturerColor, PART_STATUS, parsePart, rarityColor, slotLabel, statusText, statusTitle,
} from "./meta.js";
import { SLOT_REGION, gunFor, gunSvg } from "./guns.js";
import { itemClassSvg } from "./icons.js";
import {
  $, api, dropdown, dump, emblem, ensureViewerModule, fill, guarded, h, icon, shell, state, toast, unverifiedBadge, v3d,
} from "./core.js";
import {
  STEEL, entryFor, hostType, isWeapon, modelKeyFor, partPlate, pieceColor, piecesFor, piecesOf, weaponTypeFrom,
} from "./gear_model.js";

const MODES = [
  { id: "legal", label: "Legal", hint: "Only parts this gun could roll" },
  { id: "same_slot", label: "Any gun", hint: "Every part used in this slot by any weapon" },
  { id: "everything", label: "Everything", hint: "Any part, in any slot", chaos: true },
];
const PAGE = 90;

const stage = { host: null, viewer: null }; // the main 3D view; its canvas survives re-renders
let hover3d = null;
let lastKept = null; // what the game kept after the last bolt-on or level change: { id, what, kept, removing }

// ---------- which gun a part comes from ----------

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
  return `${option.id}\n${owned[0] > owned.length - 1 ? `On ${owned[0]} guns, e.g. ${names}` : `On: ${names}`}`;
}

/** A small coloured chip with the status's short label (an unknown part says what is unknown); nothing when the option has no status. */
function statusChip(option) {
  const meta = option && PART_STATUS[option.status];
  if (!meta) return null;
  const text = option.status === "unknown" ? statusText(option) : meta.short;
  return h("span", { class: "pstat", vars: { "--c": meta.color }, title: statusTitle(option) }, h("i", { class: "pdot" }), text);
}

/** A small plate for the card: the SVG sketch at first, replaced by a render of the real part once it is on screen. */
function partThumb(option, slotName, detail) {
  const { el } = partPlate(option, slotName, detail, "thumb");
  const p = state.picker;
  if (p && p.watcher && option.meshes && option.meshes.length && !v3d.failed) {
    p.lazy.set(el, async () => {
      const module = await ensureViewerModule();
      if (!module || !module.partThumbnail) return;
      const url = await module.partThumbnail(option.meshes, pieceColor(option, slotName), 120, 76);
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

/** Region fills for the hero diagram: each part is coloured by its own manufacturer (or element). */
function regionFills(detail) {
  const fills = {};
  for (const slot of detail.slots) {
    if (!slot.current) continue;
    const info = parsePart(slot.current);
    const color = info.element ? ELEMENTS[info.element].color : info.manufacturer ? manufacturerColor(info.manufacturer) : STEEL;
    fills[SLOT_REGION[slot.slot]] = color;
  }
  return fills;
}

/** Hover text for each region: which slot it is and what is in it. */
function regionTitles(detail) {
  const titles = {};
  for (const slot of detail.slots) {
    const part = slot.current ? parsePart(slot.current).title : "empty";
    titles[SLOT_REGION[slot.slot]] = `${slotLabel(slot.slot)}: ${part}`.replace(/[<>&]/g, "");
  }
  return titles;
}

export async function loadDetail() {
  const detail = await guarded(() => api("GET", `/api/item?id=${encodeURIComponent(state.selected)}&mode=${state.mode}`));
  if (detail) setDetail(detail);
}

/** Keeps what the game kept after an edit, for the panel on the gear page (none from an older backend). */
function keepResult(result, removing = false) {
  const kept = result.applied && result.applied.kept;
  lastKept = kept ? { id: result.id, what: result.applied.what, kept, removing } : null;
}

function setDetail(detail) {
  state.detail = detail;
  state.selected = detail.id; // the game can change an item's id when it rebuilds it
  state.mode = detail.mode;
  shell.renderWorkspace();
  if (state.picker) renderPicker();
}

export function renderGear(root) {
  const detail = state.detail;
  if (!detail) {
    fill(root, h("div", { class: "empty" }, state.selected
      ? h("div", {}, h("h2", { text: "Loading..." }))
      : h("div", {}, h("h2", { text: "Pick your gear" }), h("p", { text: "Choose a weapon or item from the loadout on the left to start tinkering." }))));
    return;
  }

  const entry = entryFor(detail.id) || { class: detail.class, name: detail.name, type_path: null, rarity: null };
  const weapon = isWeapon(detail);
  const mfr = itemManufacturer(detail.name);
  const type = weaponTypeFrom(entry.type_path);
  // The level range comes from the game (limits.min/max); gear level = character cap + Overpower levels.
  const { min: minLevel, max: maxLevel, character_max: characterMax, sources: capSources = {} } = detail.limits;
  const overpower = detail.overpower || 0;
  const capFallback = Object.values(capSources).some((from) => String(from).includes("fallback"));
  const levelInput = h("input", { type: "number", min: minLevel, max: maxLevel, value: detail.level, onkeydown: (e) => e.key === "Enter" && commitLevel() });
  const commitLevel = () => applyLevel(parseInt(levelInput.value, 10));
  const nudge = (by) => () => { levelInput.value = Math.max(minLevel, Math.min(maxLevel, (parseInt(levelInput.value, 10) || minLevel) + by)); };

  const banner = h(
    "div",
    { class: "banner", vars: { "--rar": rarityColor(entry.rarity) } },
    h("div", {},
      h("span", { class: `stamp ${detail.location.toLowerCase()}`, text: detail.location }),
      h("h1", { text: cleanItemName(detail.name), title: detail.name }),
      h("div", { class: "facts" },
        mfr && h("span", {}, emblem(mfr, 18), h("b", { text: MANUFACTURERS[mfr].label })),
        h("span", { text: type || classLabel(detail.class) }),
        entry.rarity != null && h("span", { text: `Rarity ${entry.rarity}` }),
        overpower > 0 && h("span", { title: `Gear level ${detail.level}: character level ${characterMax} plus ${overpower} Overpower levels`, text: `${characterMax} OP${overpower}` })),
      capFallback && h("div", { class: "unv-note", title: "The level cap came from a fallback value, not from the game", text: "level cap not read from the game (unverified)" })),
    h("div", { class: "level" },
      h("span", { class: "big", text: "LVL" }),
      h("div", { class: "stepper" },
        h("button", { title: "Down", "aria-label": "Level down", onclick: nudge(-1) }, icon("minus", 15)),
        levelInput,
        h("button", { title: "Up", "aria-label": "Level up", onclick: nudge(1) }, icon("plus", 15))),
      h("button", { class: "btn primary", onclick: commitLevel }, icon("check", 15), "Set"),
      h("button", { class: "btn ghost", title: "Re-read values from the game", "aria-label": "Refresh", onclick: () => guarded(loadDetail) }, icon("refresh", 15))),
  );

  const overridden = detail.stats.filter((stat) => stat.override !== null).length;
  fill(
    root,
    h("div", { class: "bench" },
      banner,
      h("div", { class: "bench-body" },
        state.mode !== "legal" && detail.slots.length > 0
          && h("div", { class: "warn-banner" }, icon("alert", 18), "Illegal part mode is on. Odd combinations can break stats or crash the game. Save first."),
        keptPanel(detail),
        renderDiagram(detail, entry, weapon),
        detail.slots.length > 0 && h("ol", { class: "legend" }, detail.slots.map((slot, index) => legendRow(slot, index, detail))))),
    weapon && detail.stats.length > 0
      && dropdown("stats", "Stats bench", overridden ? `${overridden} tuned` : "", renderStats(detail)),
    dropdown("info", "Item info & dev tools", "", renderInfo(detail)),
  );
}

function renderDiagram(detail, entry, weapon) {
  const key = modelKeyFor(detail);
  const pieces = piecesFor(detail);
  const can3d = Boolean(key && pieces.length && !v3d.failed);
  if (state.view3d && can3d) return renderStage(detail, key, pieces);
  return renderBlueprint(detail, entry, weapon, can3d);
}

function setView3d(on) {
  state.view3d = on;
  localStorage.setItem("rtse.view3d", on ? "on" : "off");
  shell.renderWorkspace();
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
    if (!module) return shell.renderWorkspace();
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
      v3d.failed = true;
      shell.renderWorkspace();
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

/** Highlights a slot in both the legend and the diagram. */
function setHot(slotName, on) {
  const leg = document.querySelector(`.leg[data-slot="${slotName}"]`);
  if (leg) leg.classList.toggle("hot", on);
  const region = document.querySelector(`.gun [data-region="${SLOT_REGION[slotName]}"].r`);
  if (region) region.classList.toggle("hot", on);
  if (stage.viewer) stage.viewer.highlight(on ? slotName : null);
}

function legendRow(slot, index, detail) {
  const current = slot.current;
  const info = current ? parsePart(current) : null;
  const type = hostType(detail);
  const foreign = info && isWeapon(detail) && type && info.weaponType && info.weaponType !== type;
  return h(
    "li",
    { style: { display: "contents" } },
    h(
      "button",
      {
        class: `leg${current ? "" : " empty-slot"}`,
        "data-slot": slot.slot,
        onclick: () => openPicker(slot.slot),
        onmouseenter: () => setHot(slot.slot, true),
        onmouseleave: () => setHot(slot.slot, false),
      },
      h("span", { class: "num", text: String(index + 1) }),
      h("div", {},
        h("div", { class: "k", text: slotLabel(slot.slot) }),
        h("div", { class: "n", text: current ? info.title : "Empty" }),
        current && h("div", { class: "tags" },
          info.manufacturer && h("span", {}, emblem(info.manufacturer, 14), MANUFACTURERS[info.manufacturer].label),
          info.element && h("span", { class: "elem", vars: { "--c": ELEMENTS[info.element].color }, text: ELEMENTS[info.element].label }),
          info.rarity && h("span", { text: info.rarity }),
          current.status !== "natural" && statusChip(current),
          foreign && h("span", { class: "foreign" }, icon("alert", 13), `from ${info.weaponType}`))),
      h("span", { class: "swap" }, icon("swap", 18)),
    ),
  );
}


function renderStats(detail) {
  const byName = new Map(detail.stats.map((s) => [s.name, s]));
  const groups = STAT_GROUPS.map((g) => ({ title: g.title, stats: g.stats.map((n) => byName.get(n)).filter(Boolean) }));
  const other = detail.stats.filter((s) => !STAT_GROUPS.some((g) => g.stats.includes(s.name)));
  if (other.length) groups.push({ title: "Other", stats: other });
  return h(
    "div",
    {},
    h("p", { class: "note", text: "Tuned values are saved in rtse/overrides.json and re-applied whenever the game rebuilds this weapon." }),
    detail.override_scope === "legacy" && h("p", { class: "note", text: "These overrides were saved before RTSE could tell identical guns apart; they apply to every identical copy. Editing a stat here gives this gun its own copy." }),
    groups.filter((g) => g.stats.length).map((g) =>
      h("div", { class: "stat-group" }, h("h4", { text: g.title }), g.stats.map(statRow))),
  );
}

function statRow(stat) {
  const overridden = stat.override !== null;
  const input = h("input", { class: "field", type: "number", step: "any", value: formatNumber(overridden ? stat.override : stat.value), "aria-label": `${stat.label} value` });
  const commit = (value) => applyStat(stat, value);
  const scale = (factor) => () => commit(+(stat.value * factor).toPrecision(6));
  return h(
    "div",
    { class: `srow${overridden ? " overridden" : ""}` },
    h("div", {}, h("div", { class: "lbl", text: stat.label.replace(/\s*[-(].*$/, "") }),
      h("div", { class: "id", text: `${stat.name} / base ${formatNumber(stat.base)}` })),
    h("div", { class: "val", text: formatNumber(stat.value) }),
    h("div", { class: "ctrl" },
      input,
      h("button", { class: "btn sm primary", onclick: () => { const v = parseFloat(input.value); Number.isNaN(v) ? toast("Enter a number", true) : commit(v); } }, "Set"),
      overridden && h("button", { class: "btn sm danger", title: "Back to the game's value", onclick: () => commit(null) }, icon("x", 13), "Reset"),
      h("button", { class: "btn sm", title: "Halve", onclick: scale(0.5) }, "/2"),
      h("button", { class: "btn sm", onclick: scale(2) }, "x2"),
      h("button", { class: "btn sm", onclick: scale(10) }, "x10")),
  );
}

function renderInfo(detail) {
  const rows = [
    ["Item id", detail.id], ["Class", detail.class], ["Location", detail.location],
    ["Level requirement", detail.level], ["Manufacturer grade", detail.grade], ["Game stage", detail.game_stage],
  ];
  if (detail.dlc) rows.push(["DLC", h("span", {}, detail.dlc.name, unverifiedBadge("The DLC comes from the game's balance data; not confirmed in game"))]);
  return h(
    "div",
    {},
    h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)])),
    h("div", { class: "tools" },
      h("button", { class: "btn sm", onclick: () => dump(`/api/debug/item?id=${encodeURIComponent(detail.id)}`) }, "Save item dump"),
      h("button", { class: "btn sm", onclick: () => dump(`/api/debug/validation?id=${encodeURIComponent(detail.id)}`) }, "Save validation dump"),
      h("button", { class: "btn sm", onclick: () => dump("/api/debug/inventory") }, "Save inventory dump"),
      h("button", { class: "btn sm", onclick: () => dump("/api/debug/gestalts") }, "Save ALL gestalt tables"),
      h("button", { class: "btn sm", onclick: () => dump("/api/debug/coverage") }, "Save mesh coverage report"),
      detail.class === "WillowWeapon" && h("button", { class: "btn sm", onclick: () => dump(`/api/debug/meshes?id=${encodeURIComponent(detail.id)}`) }, "Save mesh probe"),
      detail.class === "WillowWeapon" && h("button", { class: "btn sm", onclick: () => dump(`/api/debug/gestalt?id=${encodeURIComponent(detail.id)}`) }, "Save gestalt probe"),
      h("button", { class: "btn sm", onclick: () => guarded(async () => { await api("POST", "/api/parts/rescan", {}); state.owners = {}; toast("Part list cleared"); }) }, icon("refresh", 13), "Rescan parts")),
  );
}

// ---------- what the game kept ----------

/** After a bolt-on or a level change: what the game kept of it. Shown only on the item it was for; the x clears it. */
function keptPanel(detail) {
  if (!lastKept || lastKept.id !== detail.id) return null;
  const { what, kept, removing } = lastKept;
  const head = h("div", { class: "kept-head" },
    h("h3", { text: "What the game kept" }),
    what && h("span", { class: "kept-what", text: what }),
    h("button", { class: "btn sm ghost", title: "Dismiss", "aria-label": "Dismiss what the game kept", onclick: () => { lastKept = null; shell.renderWorkspace(); } }, icon("x", 13)));
  if (kept.error) return h("div", { class: "kept" }, head, h("div", { class: "kept-line bad" }, icon("alert", 15), h("span", { text: kept.error })));

  const name = kept.name && (kept.name.before === kept.name.after ? "unchanged" : `${cleanItemName(kept.name.before)} -> ${cleanItemName(kept.name.after)}`);
  const rows = (kept.parts || []).filter((part) => part.changed || part.edited);
  const stats = (kept.stats || []).map((s) => `${s.label.replace(/\s*[-(].*$/, "")}: ${formatNumber(s.before)} -> ${formatNumber(s.after)}`);
  return h("div", { class: "kept" }, head,
    name && h("div", { class: "kept-line" }, h("b", { text: "Name" }), h("span", { text: name })),
    rows.length > 0 && h("div", { class: "kept-table" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { text: "Slot" }), h("th", { text: "Before" }), h("th", { text: "After" }), h("th", { text: "Edit" }))),
      h("tbody", {}, rows.map((part) => keptRow(part, removing))))),
    stats.length > 0 && h("div", { class: "kept-line" }, h("b", { text: "Stats" }), h("span", { text: stats.join("  /  ") })),
    keptChecks(kept),
    h("div", { class: "chk-note", text: SAVE_PLACEHOLDER }));
}

function keptRow(part, removing) {
  const title = (name) => (name ? parsePart(name).title : "empty");
  let edit = null;
  if (part.edited) {
    const kept = removing ? part.after === null : part.after !== null;
    edit = kept
      ? h("span", { class: "tick" }, icon("check", 14), "kept")
      : h("span", { class: "warn" }, icon("alert", 14), "the game did not keep it");
  }
  return h("tr", {}, h("td", { text: slotLabel(part.slot) }), h("td", { text: title(part.before) }), h("td", { text: title(part.after) }), h("td", {}, edit));
}

async function applyLevel(level) {
  if (!Number.isInteger(level)) return toast("Enter a whole number", true);
  const result = await guarded(() => api("POST", "/api/item/set_level", { id: state.selected, mode: state.mode, level }));
  if (result) { keepResult(result); setDetail(result); toast(`Level ${result.level}`); if (result.warnings?.length) result.warnings.forEach((w) => toast(w, true)); }
}

async function applyStat(stat, value) {
  const result = await guarded(() => api("POST", "/api/item/set_stat", { id: state.selected, mode: state.mode, stat: stat.name, value }));
  if (!result) return;
  lastKept = null;
  setDetail(result);
  const now = result.stats.find((s) => s.name === stat.name);
  toast(value === null ? `${stat.name} reset` : `${stat.name} ${formatNumber(now && now.value)}`);
  if (result.warnings?.length) result.warnings.forEach((w) => toast(w, true));
}

async function applyPart(slotName, option) {
  const result = await guarded(() => api("POST", "/api/item/set_part", { id: state.selected, mode: state.mode, slot: slotName, part: option ? option.id : null }));
  if (!result) return;
  keepResult(result, !option);
  closePicker();
  setDetail(result);
  toast(option ? `${slotLabel(slotName)}: ${parsePart(option).title}` : `${slotLabel(slotName)} removed`);
  if (result.warnings?.length) result.warnings.forEach((w) => toast(w, true));
}

// ---------- part picker ----------

function openPicker(slotName) {
  state.picker = { slot: slotName, query: "", mfr: null, type: null, status: null, shown: PAGE, chosen: null, loading: false, stageHost: null, viewer: null, timer: null, watcher: null, lazy: new WeakMap() };
  renderPicker();
}

export function closePicker() {
  const p = state.picker;
  if (p) {
    clearTimeout(p.timer);
    if (p.watcher) p.watcher.disconnect();
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
    if (!state.view3d || v3d.failed || !key || !pieces.length || state.picker !== p) return;
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
  if (!state.view3d || v3d.failed || !key || !piecesFor(detail).length) return null;
  p.stageHost ||= h("div", { class: "stage stage-mini" });
  return p.stageHost;
}

const parsedCache = new WeakMap();

function parsedOptions(slot) {
  let parsed = parsedCache.get(slot);
  if (!parsed) {
    parsed = slot.options.map((option) => {
      const info = parsePart(option);
      return { option, info, hay: `${info.title} ${option.name} ${option.group} ${info.manufacturer || ""} ${info.weaponType || ""} ${info.element || ""}`.toLowerCase() };
    });
    parsedCache.set(slot, parsed);
  }
  return parsed;
}

function ownerHay(option) {
  const owned = ownerEntry(option, state.detail);
  return owned ? owned.slice(1).join(" ").toLowerCase() : "";
}

function filteredOptions(slot) {
  const p = state.picker;
  const words = p.query.toLowerCase().split(/\s+/).filter(Boolean);
  return parsedOptions(slot).filter((entry) =>
    (!p.mfr || entry.info.manufacturer === p.mfr)
    && (!p.type || entry.info.weaponType === p.type)
    && (!p.status || entry.option.status === p.status)
    && words.every((w) => entry.hay.includes(w) || ownerHay(entry.option).includes(w)));
}

function renderPicker() {
  const p = state.picker;
  const detail = state.detail;
  const slot = detail && detail.slots.find((s) => s.slot === p.slot);
  if (!slot) return closePicker();

  const searchBox = h("input", {
    class: "field", style: { width: "100%" }, placeholder: "Search by name, gun, maker or element...", value: p.query,
    oninput: (e) => { p.query = e.target.value; p.shown = PAGE; renderResults(); },
  });
  const modeBar = h("div", { class: "seg" }, MODES.map((m) =>
    h("button", { class: `${m.id === state.mode ? "on" : ""}${m.chaos ? " chaos" : ""}`, title: m.hint, text: m.label, onclick: () => switchMode(m.id) })));

  const results = h("div", { class: "results", id: "results", onscroll: (e) => {
    const el = e.target;
    if (el.scrollTop + el.clientHeight > el.scrollHeight - 500 && p.shown < filteredOptions(slot).length) { p.shown += PAGE; renderResults(true); }
  } });
  const current = slot.current ? parsePart(slot.current) : null;
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
    $("#picker-root"),
    h("div", { class: "scrim", onclick: closePicker }),
    h("section", { class: "picker", role: "dialog", "aria-label": `Swap ${slotLabel(slot.slot)}` },
      h("header", {},
        h("div", {}, h("h2", {}, "Swap ", h("span", { text: slotLabel(slot.slot) })),
          h("small", { text: current ? `On the gun now: ${current.title}` : "Nothing in this slot right now" })),
        h("div", { class: "spacer" }),
        h("button", { class: "btn", onclick: closePicker }, icon("x", 15), "Close")),
      h("div", { class: "toolbar" },
        h("div", { style: { display: "flex", gap: "14px", alignItems: "center", flexWrap: "wrap" } }, modeBar,
          h("span", { style: { color: "var(--muted)", fontWeight: 600 }, text: MODES.find((m) => m.id === state.mode).hint })),
        cacheHint(detail),
        searchBox,
        h("div", { class: "filters", id: "filters" })),
      results,
      h("aside", { class: "preview", id: "preview" }),
    ),
  );
  renderFilters();
  renderResults();
  renderPreview();
  syncPreviewViewer(p.chosen);
  searchBox.focus();
}

async function switchMode(mode) {
  if (mode === state.mode) return;
  state.mode = mode;
  localStorage.setItem("rtse.mode", mode);
  await fetchDetail(mode);
}

/** Reads the item in `mode` and shows it. The picker stays open and shows the scan while the parts load. */
async function fetchDetail(mode) {
  const p = state.picker;
  if (p) { p.loading = true; p.chosen = null; renderResults(); renderPreview(); }
  const detail = await guarded(() => api("GET", `/api/item?id=${encodeURIComponent(state.selected)}&mode=${mode}`));
  if (p) p.loading = false;
  if (detail) setDetail(detail);
  else if (p) renderResults();
}

/** Rescans the part list in the game (after loading DLC or changing maps), then reads the item again in the current mode. */
async function rescanParts() {
  await api("POST", "/api/parts/rescan", {});
  state.owners = {};
  await fetchDetail(state.mode);
}

/** "just now", "40 s ago", "2 min ago", "3 h ago". */
function scannedAgo(seconds) {
  if (seconds == null) return "some time ago";
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${Math.round(seconds)} s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  return `${Math.floor(seconds / 3600)} h ago`;
}

/** The part list's scan age, with a Rescan button. Shown only in the modes that scan every part. */
function cacheHint(detail) {
  const cache = detail.parts_cache;
  if (state.mode === "legal" || !cache || !cache.scanned) return null;
  const where = cache.map ? ` on ${cache.map}` : "";
  return h("div", { class: "scan-hint" },
    h("span", { text: `Part list scanned ${scannedAgo(cache.age_s)}${where}. Rescan after loading DLC or changing maps.` }),
    h("button", { class: "btn sm ghost", onclick: () => guarded(rescanParts) }, icon("refresh", 13), "Rescan"));
}

/** The status filter chips: each status that occurs in this slot, with how many parts have it. */
function statusCounts(slot) {
  const counts = new Map();
  for (const { option } of parsedOptions(slot)) {
    if (option.status) counts.set(option.status, (counts.get(option.status) || 0) + 1);
  }
  return Object.keys(PART_STATUS).filter((key) => counts.has(key)).map((key) => [key, counts.get(key)]);
}

function renderFilters() {
  const p = state.picker;
  const slot = state.detail.slots.find((s) => s.slot === p.slot);
  const holder = $("#filters");
  if (!holder) return;
  const count = (selector) => {
    const map = new Map();
    for (const entry of parsedOptions(slot)) {
      const key = selector(entry.info);
      if (key) map.set(key, (map.get(key) || 0) + 1);
    }
    return [...map.entries()].sort((a, b) => b[1] - a[1]);
  };
  const toggle = (field, key) => () => { p[field] = p[field] === key ? null : key; p.shown = PAGE; renderFilters(); renderResults(); };
  fill(
    holder,
    count((i) => i.manufacturer).map(([key, n]) =>
      h("button", { class: `fchip${p.mfr === key ? " on" : ""}`, onclick: toggle("mfr", key) }, emblem(key, 15), `${MANUFACTURERS[key].label} ${n}`)),
    count((i) => i.weaponType).map(([key, n]) =>
      h("button", { class: `fchip${p.type === key ? " on" : ""}`, onclick: toggle("type", key), text: `${key} ${n}` })),
    statusCounts(slot).map(([key, n]) =>
      h("button", { class: `fchip${p.status === key ? " on" : ""}`, title: PART_STATUS[key].label, onclick: toggle("status", key) },
        h("i", { class: "pdot", vars: { "--c": PART_STATUS[key].color } }), `${PART_STATUS[key].label} ${n}`)),
  );
}

function renderResults(append = false) {
  const p = state.picker;
  const holder = $("#results");
  if (!p || !holder) return;
  if (p.loading) {
    fill(holder, h("div", { class: "count", text: "Scanning every weapon for parts. The first time can take a few seconds..." }));
    return;
  }
  const detail = state.detail;
  const slot = detail.slots.find((s) => s.slot === p.slot);
  const list = filteredOptions(slot);
  const currentId = slot.current ? slot.current.id : null;
  const type = hostType(detail);
  const cards = list.slice(0, p.shown).map(({ option, info }) => {
    const foreign = isWeapon(detail) && type && info.weaponType && info.weaponType !== type;
    return h(
      "button",
      {
        class: `card${p.chosen && p.chosen.id === option.id ? " sel" : ""}${option.id === currentId ? " cur" : ""}`,
        title: foreign ? `${ownerTooltip(option, detail)}\n(from a ${info.weaponType}, not a ${type})` : ownerTooltip(option, detail),
        onclick: () => choose(option),
        ondblclick: () => applyPart(slot.slot, option),
        onmouseenter: () => syncPreviewViewer(option),
        onmouseleave: () => syncPreviewViewer(p.chosen),
      },
      partThumb(option, slot.slot, detail),
      h("div", { style: { minWidth: 0 } },
        h("div", { class: "n", text: gunName(option, info, detail) }),
        h("div", { class: "t" },
          info.manufacturer && emblem(info.manufacturer, 12),
          h("span", { text: option.name }),
          statusChip(option),
          foreign && h("span", { class: "flag", title: "From a different gun type" }, icon("alert", 13)))),
    );
  });
  const scrollTop = append ? holder.scrollTop : 0;
  fill(
    holder,
    h("div", { class: "count", text: `${list.length} of ${slot.options.length} parts${p.shown < list.length ? `  /  showing ${p.shown}` : ""}` }),
    list.length ? h("div", { class: "grid" }, cards) : h("div", { class: "empty-note", text: "No parts match that search." }),
  );
  holder.scrollTop = scrollTop;
}

// ---------- check before apply ----------

const STATUS_REASON = "Read from the game's balance data; not confirmed in game";
const QUICK_REASON = "A game query; what it means is not confirmed in game";
// The save check is not built yet (it waits for the offline serial decoder): the picker and the kept panel say so.
const SAVE_PLACEHOLDER = "Save/reload check: coming back as an offline round-trip (no game calls) once the serial decoder lands.";

const yesNo = (ok) => (ok === true ? "yes" : ok === false ? "no" : "n/a");

/** The CHECK BEFORE APPLY block: the status and DLC line, then the save check placeholder. Advisory: Apply is never blocked. */
function checkSection(option) {
  const meta = PART_STATUS[option.status];
  return h("div", { class: "check" },
    h("div", { class: "label", text: "CHECK BEFORE APPLY" }),
    meta && h("div", { class: "chk-status", vars: { "--c": meta.color } },
      h("i", { class: "pdot" }), h("b", { text: statusText(option) }), unverifiedBadge(STATUS_REASON),
      option.status_reason && h("span", { class: "chk-reason", text: option.status_reason })),
    option.dlc && h("div", { class: "chk-dlc", text: `DLC: ${option.dlc.name} (${dlcOwnership(option.dlc)})` }),
    h("div", { class: "chk-note", text: SAVE_PLACEHOLDER }));
}

/** The game's quick queries after an edit (weapons: CanBeSaved). Each one is a game query, unverified. */
function keptChecks(kept) {
  const checks = kept.quick_checks || [];
  if (checks.length === 0) return null;
  return h("div", { class: "kept-line" }, h("b", { text: "Checks" }),
    h("ul", { class: "chk-list kept-qc" }, checks.map((c) => h("li", {},
      h("span", { text: c.name }), h("b", { class: `yn ${yesNo(c.ok)}`, text: yesNo(c.ok) }), h("small", { text: c.detail }), unverifiedBadge(QUICK_REASON)))));
}

async function choose(option) {
  const p = state.picker;
  p.chosen = option;
  renderResults(true);
  renderPreview();
  if (!state.effects.has(option.id)) {
    state.effects.set(option.id, { loading: true });
    try {
      state.effects.set(option.id, await api("GET", `/api/part?id=${encodeURIComponent(option.id)}&cls=${encodeURIComponent(option.class)}`));
    } catch (error) {
      state.effects.set(option.id, { error: error.message });
    }
    if (state.picker && state.picker.chosen && state.picker.chosen.id === option.id) renderPreview();
  }
}

function renderPreview() {
  const p = state.picker;
  const holder = $("#preview");
  if (!p || !holder) return;
  const detail = state.detail;
  const slot = detail.slots.find((s) => s.slot === p.slot);
  const option = p.chosen;
  const stageEl = previewStage(p);
  if (!option) {
    fill(
      holder,
      stageEl,
      h("h3", { text: "Pick a part" }),
      h("div", { class: "hint", text: "Click a part to see what it does. Double-click to bolt it on straight away." }),
      slot.current && h("div", { class: "actions" }, h("button", { class: "btn danger", onclick: () => applyPart(slot.slot, null) }, icon("trash", 15), "Remove current part")),
    );
    return;
  }

  const { info, el } = partPlate(option, slot.slot, detail, "lg");
  syncPreviewViewer(option);
  const labels = Object.fromEntries(detail.stats.map((s) => [s.name, s.label]));
  const fx = state.effects.get(option.id) || { loading: true };
  let effects;
  if (fx.loading) effects = h("div", { class: "hint", text: "Reading what this part does..." });
  else if (fx.error) effects = h("div", { class: "hint", text: `Could not read effects: ${fx.error}` });
  else if (!fx.effects.length) effects = h("div", { class: "hint", text: "No stat modifiers found. It may be cosmetic, or its effects aren't readable yet." });
  else effects = h("div", { class: "fx" }, fx.effects.map((effect) => {
    const d = describeEffect(effect, labels);
    return h("div", { class: d.tone }, d.tone && icon(d.tone === "up" ? "arrowUp" : "arrowDown", 16), h("span", { text: d.text }), h("b", { text: d.value }), unverifiedBadge(d.reason));
  }));

  const isCurrent = slot.current && slot.current.id === option.id;
  fill(
    holder,
    stageEl || el,
    h("div", {}, h("h3", { text: gunName(option, info, detail) }),
      h("div", { class: "path", style: { marginTop: "4px" }, text: option.name }),
      h("div", { class: "tags", style: { marginTop: "8px" } },
        info.manufacturer && h("span", {}, emblem(info.manufacturer, 15), MANUFACTURERS[info.manufacturer].label),
        info.weaponType && h("span", { text: info.weaponType }),
        info.element && h("span", { class: "elem", vars: { "--c": ELEMENTS[info.element].color }, text: ELEMENTS[info.element].label }),
        info.rarity && h("span", { text: info.rarity }))),
    h("div", {}, h("div", { class: "label", text: "WHAT IT DOES" }), effects),
    h("div", { class: "path", text: option.id }),
    checkSection(option),
    h("div", { class: "actions" },
      h("button", { class: "btn primary", disabled: isCurrent || null, onclick: () => applyPart(slot.slot, option) },
        icon("check", 16), isCurrent ? "Already on the gun" : `Bolt on this ${slotLabel(slot.slot).toLowerCase()}`),
      slot.current && h("button", { class: "btn danger", onclick: () => applyPart(slot.slot, null) }, icon("trash", 15), "Remove current part"),
      h("button", { class: "btn ghost sm", onclick: () => dump(`/api/debug/part?id=${encodeURIComponent(option.id)}&cls=${encodeURIComponent(option.class)}`) }, "Save part dump"),
      h("button", { class: "btn ghost sm", onclick: () => dump(`/api/debug/validation?id=${encodeURIComponent(state.selected)}&mode=${state.mode}`) }, "Save validation dump")),
  );
}
