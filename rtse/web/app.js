import {
  ELEMENTS, MANUFACTURERS, STAT_GROUPS, classLabel, cleanItemName, describeEffect, formatNumber,
  itemManufacturer, manufacturerColor, parsePart, rarityColor, slotLabel,
} from "./meta.js";
import { SLOT_REGION, gunFor, gunSvg, plateSvg } from "./guns.js";
import { CLASS_INFO, classFromText, portraitUrl } from "./portraits.js";
import { emblemSvg, itemClassSvg, letterSvg, plateEl, ui } from "./icons.js";
import * as sduPage from "./page_sdu.js";
import * as skillsPage from "./page_skills.js";
import * as worldPage from "./page_world.js";

const token = new URLSearchParams(location.search).get("t") || "";
const MODES = [
  { id: "legal", label: "Legal", hint: "Only parts this gun could roll" },
  { id: "same_slot", label: "Any gun", hint: "Every part used in this slot by any weapon" },
  { id: "everything", label: "Everything", hint: "Any part, in any slot", chaos: true },
];
const PAGE = 90;
const STEEL = "#8a8577"; // parts with no recognisable manufacturer

const state = {
  items: [],
  itemsKey: "",
  search: "",
  selected: null,
  detail: null,
  open: { stats: false, info: false, charinfo: false, ammoinfo: false }, // which dropdown sections are expanded
  mode: localStorage.getItem("rtse.mode") || "legal",
  picker: null,
  page: "gear", // "gear" (item editor) or "character"
  character: null,
  ammo: null,
  effects: new Map(),
  owners: {}, // "weapon" | "item" -> Map(part id -> [how many guns roll it, first few gun names])
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

// ---------- helpers ----------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "html") el.innerHTML = value;
    else if (key === "style") Object.assign(el.style, value);
    else if (key === "vars") for (const [k, v] of Object.entries(value)) el.style.setProperty(k, v);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

const $ = (selector) => document.querySelector(selector);

/** Replaces a parent's children, flattening arrays and dropping null/false (which the DOM would print). */
function fill(parent, ...nodes) {
  parent.replaceChildren(...nodes.flat(Infinity).filter((node) => node !== null && node !== undefined && node !== false));
}

const icon = (name, size = 16) => h("span", { class: "icon", style: { display: "inline-flex" }, html: ui(name, size) });
const emblem = (key, size = 16) => h("span", { style: { display: "inline-flex" }, html: emblemSvg(key, manufacturerColor(key), size) });

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { "X-RTSE-Token": token, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function toast(message, isError = false) {
  const el = h("div", { class: `toast${isError ? " err" : ""}`, text: message });
  $("#toasts").append(el);
  setTimeout(() => el.remove(), isError ? 7000 : 3200);
}

async function guarded(action) {
  try {
    return await action();
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

function weaponTypeFrom(path) {
  const p = (path || "").toLowerCase();
  if (p.includes("pistol")) return "Pistol";
  if (p.includes("smg")) return "SMG";
  if (p.includes("sniper")) return "Sniper Rifle";
  if (p.includes("shotgun")) return "Shotgun";
  if (p.includes("launcher")) return "Launcher";
  if (p.includes("assault") || p.includes("_ar") || p.includes("rifle")) return "Assault Rifle";
  return null;
}

const entryFor = (id) => state.items.find((item) => item.id === id) || null;
const hostType = (detail) => weaponTypeFrom((entryFor(detail.id) || {}).type_path);
const isWeapon = (detail) => detail.class === "WillowWeapon";

// ---------- art ----------

/** A small drawn plate for a part in a slot. */
function partPlate(part, slotName, detail, cls = "") {
  const info = parsePart(part.name, part.group);
  if (!isWeapon(detail)) {
    return { info, el: plateEl({ html: letterSvg(slotName), color: "var(--bone)", cls: `${cls} item`, imageName: part.name }) };
  }
  const type = gunFor(info.weaponType || hostType(detail));
  const color = info.element ? ELEMENTS[info.element].color : info.manufacturer ? manufacturerColor(info.manufacturer) : STEEL;
  return { info, el: plateEl({ html: plateSvg(type, slotName, color), color, cls, imageName: part.name }) };
}

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

/** A small plate for the card: the SVG sketch at first, replaced by a render of the real part once it is on screen. */
function partThumb(option, slotName, detail) {
  const { el } = partPlate(option, slotName, detail, "thumb");
  const p = state.picker;
  if (p && p.watcher && option.meshes && option.meshes.length && !v3dFailed) {
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
    const info = parsePart(slot.current.name, slot.current.group);
    const color = info.element ? ELEMENTS[info.element].color : info.manufacturer ? manufacturerColor(info.manufacturer) : STEEL;
    fills[SLOT_REGION[slot.slot]] = color;
  }
  return fills;
}

/** Hover text for each region: which slot it is and what is in it. */
function regionTitles(detail) {
  const titles = {};
  for (const slot of detail.slots) {
    const part = slot.current ? parsePart(slot.current.name, slot.current.group).title : "empty";
    titles[SLOT_REGION[slot.slot]] = `${slotLabel(slot.slot)}: ${part}`.replace(/[<>&]/g, "");
  }
  return titles;
}

/** Which gestalt model draws this item, or null if it has none. */
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

const rowThumbs = new Map(); // picture signature -> data URL, so a re-render never flashes back to the sketch

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

function rowPlateSketch(item) {
  const mfr = itemManufacturer(item.name);
  const color = manufacturerColor(mfr);
  if (item.class === "WillowWeapon") {
    const type = gunFor(weaponTypeFrom(item.type_path));
    const fills = Object.fromEntries(["stock", "accessory1", "grip", "body", "accessory2", "barrel", "sight"].map((r) => [r, color]));
    return plateEl({ html: gunSvg(type, { fills, solid: true, className: "plate-art" }), color });
  }
  return plateEl({ html: itemClassSvg(item.class), color, cls: "item" });
}

// ---------- loadout (sidebar) ----------

function renderInventory() {
  const query = state.search.trim().toLowerCase();
  const visible = state.items.filter((item) => !query || `${item.name} ${classLabel(item.class)}`.toLowerCase().includes(query));
  fill($("#inventory"), ["Equipped", "Backpack"].map((location) => {
    const group = visible.filter((item) => item.location === location);
    return [
      h("div", { class: `group-title ${location.toLowerCase()}` }, h("span", { text: location }), h("em", { text: String(group.length) })),
      group.length ? group.map(gearRow) : h("div", { class: "empty-note", text: query ? "No matches." : "Nothing here." }),
    ];
  }));
}

function gearRow(item) {
  return h(
    "div",
    {
      class: `row${item.id === state.selected ? " selected" : ""}`,
      vars: { "--rar": rarityColor(item.rarity) },
      title: item.name,
      onclick: () => selectItem(item.id),
    },
    rowPlate(item),
    h("div", {}, h("div", { class: "t", text: cleanItemName(item.name) }),
      h("div", { class: "s", text: weaponTypeFrom(item.type_path) || classLabel(item.class) })),
    item.level != null && h("div", { class: "lvl" }, h("small", { text: "LV" }), String(item.level)),
  );
}

async function pollInventory() {
  const pill = $("#conn");
  try {
    const [ping, inventory] = await Promise.all([api("GET", "/api/ping"), api("GET", "/api/items")]);
    pill.className = "pill ok";
    pill.lastChild.textContent = ping.in_game ? "Connected" : "Connected - no save loaded";
    const key = JSON.stringify(inventory.items);
    if (key !== state.itemsKey) {
      state.itemsKey = key;
      state.items = inventory.items;
      renderInventory();
    }
    if (inventory.warnings.length) pill.lastChild.textContent += ` / ${inventory.warnings.join("; ")}`;
  } catch (error) {
    pill.className = "pill bad";
    pill.lastChild.textContent = `Disconnected (${error.message})`;
  }
}

// ---------- workbench ----------

async function selectItem(id) {
  state.page = "gear";
  renderPinned();
  state.selected = id;
  state.detail = null;
  closePicker();
  renderInventory();
  renderWorkspace();
  await loadDetail();
}

async function loadDetail() {
  const detail = await guarded(() => api("GET", `/api/item?id=${encodeURIComponent(state.selected)}&mode=${state.mode}`));
  if (detail) setDetail(detail);
}

function setDetail(detail) {
  state.detail = detail;
  state.selected = detail.id; // the game can change an item's id when it rebuilds it
  state.mode = detail.mode;
  renderWorkspace();
  if (state.picker) renderPicker();
}

function renderWorkspace() {
  const root = $("#workspace");
  if (state.page === "character") return renderCharacter(root);
  if (state.page === "ammo") return renderAmmo(root);
  if (state.page === "sdu") return sduPage.render(root, pageCtx);
  if (state.page === "skills") return skillsPage.render(root, pageCtx);
  if (state.page === "world") return worldPage.render(root, pageCtx);
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
  const levelInput = h("input", { type: "number", min: 1, max: 100, value: detail.level, onkeydown: (e) => e.key === "Enter" && commitLevel() });
  const commitLevel = () => applyLevel(parseInt(levelInput.value, 10));
  const nudge = (by) => () => { levelInput.value = Math.max(1, Math.min(100, (parseInt(levelInput.value, 10) || 1) + by)); };

  const banner = h(
    "div",
    { class: "banner", vars: { "--rar": rarityColor(entry.rarity) } },
    h("div", {},
      h("span", { class: `stamp ${detail.location.toLowerCase()}`, text: detail.location }),
      h("h1", { text: cleanItemName(detail.name), title: detail.name }),
      h("div", { class: "facts" },
        mfr && h("span", {}, emblem(mfr, 18), h("b", { text: MANUFACTURERS[mfr].label })),
        h("span", { text: type || classLabel(detail.class) }),
        entry.rarity != null && h("span", { text: `Rarity ${entry.rarity}` }))),
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
  const info = current ? parsePart(current.name, current.group) : null;
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
          foreign && h("span", { class: "foreign" }, icon("alert", 13), `from ${info.weaponType}`))),
      h("span", { class: "swap" }, icon("swap", 18)),
    ),
  );
}

/** A collapsible section. Open/closed state survives re-renders. */
function dropdown(key, title, badge, content) {
  return h(
    "details",
    { class: "drop", open: state.open[key] || null, ontoggle: (event) => { state.open[key] = event.target.open; } },
    h("summary", {}, icon("chevron", 20), h("h3", { text: title }), badge && h("em", { text: badge })),
    h("div", { class: "drop-body" }, content),
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
  return h(
    "div",
    {},
    h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: String(v) })])),
    h("div", { class: "tools" },
      h("button", { class: "btn sm", onclick: () => dump(`/api/debug/item?id=${encodeURIComponent(detail.id)}`) }, "Save item dump"),
      h("button", { class: "btn sm", onclick: () => dump("/api/debug/inventory") }, "Save inventory dump"),
      h("button", { class: "btn sm", onclick: () => dump("/api/debug/gestalts") }, "Save ALL gestalt tables"),
      h("button", { class: "btn sm", onclick: () => dump("/api/debug/coverage") }, "Save mesh coverage report"),
      detail.class === "WillowWeapon" && h("button", { class: "btn sm", onclick: () => dump(`/api/debug/meshes?id=${encodeURIComponent(detail.id)}`) }, "Save mesh probe"),
      detail.class === "WillowWeapon" && h("button", { class: "btn sm", onclick: () => dump(`/api/debug/gestalt?id=${encodeURIComponent(detail.id)}`) }, "Save gestalt probe"),
      h("button", { class: "btn sm", onclick: () => guarded(async () => { await api("POST", "/api/parts/rescan", {}); state.owners = {}; toast("Part list cleared"); }) }, icon("refresh", 13), "Rescan parts")),
  );
}

async function dump(path) {
  const result = await guarded(() => api("GET", path));
  if (result) toast(`Saved ${result.saved_to}`);
}

// ---------- character ----------

const CURRENCY_STYLE = {
  money: { color: "#6fd36f", glyph: "$", step: 1_000_000 },
  eridium: { color: "#b27cff", glyph: "E", step: 100 },
  seraph: { color: "#ff7ab8", glyph: "S", step: 100 },
  torgue: { color: "#ff6a2b", glyph: "T", step: 100 },
};

/** The player's vault hunter: what the game says, else what their class mod says. */
function currentClass() {
  const c = state.character;
  let key = c && c.class_key && CLASS_INFO[c.class_key] ? c.class_key : null;
  if (!key) {
    const mod = state.items.find((item) => item.class === "WillowClassMod");
    key = (mod && classFromText(mod.name)) || null;
  }
  return { key };
}

/** The game's own picture of the character, or a plain tile if we can't tell who they are. */
function portraitEl(size = "") {
  const { key } = currentClass();
  const el = h("div", { class: `avatar ${size}`.trim() });
  if (key) {
    const img = new Image();
    img.alt = CLASS_INFO[key].name;
    img.src = portraitUrl(key);
    el.append(img);
  } else {
    el.append(h("span", { class: "q", text: "?" }));
  }
  return el;
}

function renderPinned() {
  const c = state.character;
  const cls = currentClass();
  const pin = (page, open, icon_, title, sub, extra) => h(
    "button",
    { class: `pin${state.page === page ? " on" : ""}`, onclick: open },
    h("span", { class: "pin-ic" }, icon_),
    h("div", {}, h("div", { class: "t", text: title }), h("div", { class: "s", text: sub })),
    extra);
  fill($("#pinned"),
    pin("character", openCharacter, portraitEl(), "Character",
      cls.key ? `${CLASS_INFO[cls.key].name} / ${CLASS_INFO[cls.key].job}` : (c && c.name) || "Level, skill points, money",
      c && c.level != null && h("div", { class: "lvl" }, h("small", { text: "LV" }), String(c.level))),
    pin("ammo", openAmmo, icon("crosshair", 20), "Ammo", "Every ammo type", null),
    pin("sdu", openSdu, icon("backpack", 20), "Upgrades", "Backpack, bank, ammo SDUs", null),
    pin("skills", openSkills, icon("sliders", 20), "Skills", "Skill tree", null),
    pin("world", openWorld, icon("swap", 20), "World", "Missions, challenges, travel", null));
}

async function openCharacter() {
  state.page = "character";
  closePicker();
  renderPinned();
  renderInventory();
  renderWorkspace();
  await loadCharacter();
}

async function loadCharacter() {
  const data = await guarded(() => api("GET", "/api/character"));
  if (!data) return;
  state.character = data;
  renderPinned();
  if (state.page === "character") renderWorkspace();
}

// ---------- feature pages (own modules: page_sdu.js, page_skills.js, page_world.js) ----------

const pageCtx = { h, fill, icon, api, toast, guarded, dump, dropdown, rerender: () => renderWorkspace() };

async function openPage(page, module) {
  state.page = page;
  closePicker();
  renderPinned();
  renderInventory();
  renderWorkspace();
  await module.load(pageCtx);
}

const openSdu = () => openPage("sdu", sduPage);
const openSkills = () => openPage("skills", skillsPage);
const openWorld = () => openPage("world", worldPage);

// ---------- ammo ----------

async function openAmmo() {
  state.page = "ammo";
  closePicker();
  renderPinned();
  renderInventory();
  renderWorkspace();
  await loadAmmo();
}

async function loadAmmo() {
  try {
    state.ammo = await api("GET", "/api/ammo");
  } catch (error) {
    state.ammo = { pools: [], error: error.message };
  }
  if (state.page === "ammo") renderWorkspace();
}

async function applyAmmo(pool, value) {
  if (value !== "max" && !Number.isInteger(value)) return toast("Enter a whole number", true);
  const result = await guarded(() => api("POST", "/api/ammo/set", { id: pool.id, value }));
  if (!result) return;
  state.ammo = result;
  renderWorkspace();
  const { before, after, requested } = result.applied;
  if (after === requested) toast(`${pool.label}: ${(before ?? 0).toLocaleString()} to ${after.toLocaleString()}`);
  else toast(`${pool.label}: asked for ${requested.toLocaleString()} but the game reports ${after == null ? "nothing" : after.toLocaleString()} (the game may cap it at your maximum)`, true);
}

function ammoCard(pool) {
  const box = h("input", { class: "field", type: "number", min: 0, value: pool.value ?? 0, "aria-label": `${pool.label} ammo`, onkeydown: (e) => e.key === "Enter" && commit() });
  const commit = () => applyAmmo(pool, parseInt(box.value, 10));
  const pct = pool.max ? Math.max(0, Math.min(100, ((pool.value || 0) / pool.max) * 100)) : 0;
  return h(
    "div",
    { class: "cc ammo", vars: { "--c": "#ffd21f" } },
    h("div", { class: "cc-head" }, h("span", { class: "coin", text: pool.label.slice(0, 2).toUpperCase() }),
      h("div", {}, h("div", { class: "k", text: pool.label }), pool.max != null && h("div", { class: "sub", text: `Capacity ${pool.max.toLocaleString()}` }))),
    h("div", { class: "cc-val", text: pool.value == null ? "unavailable" : pool.value.toLocaleString() }),
    pool.max != null && h("div", { class: "bar" }, h("i", { style: { width: `${pct}%` } })),
    pool.value != null && h("div", { class: "cc-ctrl" },
      box,
      h("button", { class: "btn sm primary", onclick: commit }, "Set"),
      pool.max != null && h("button", { class: "btn sm", onclick: () => applyAmmo(pool, "max") }, "Fill"),
      h("button", { class: "btn sm danger", onclick: () => applyAmmo(pool, 0) }, "Empty")),
    sduPage.ammoCapacityControls(pool, { ...pageCtx, rerender: loadAmmo }),
  );
}

function renderAmmo(root) {
  const a = state.ammo;
  if (!a) {
    fill(root, h("div", { class: "empty" }, h("div", {}, h("h2", { text: "Loading..." }))));
    return;
  }
  const fillAll = async () => {
    for (const pool of a.pools) if (pool.max != null) await applyAmmo(pool, "max");
  };
  fill(
    root,
    h("div", { class: "bench" },
      h("div", { class: "banner" },
        h("div", {}, h("span", { class: "stamp equipped", text: "Your ammo" }), h("h1", { text: "Ammo pouch" }),
          h("div", { class: "facts" }, h("span", { text: `${a.pools.length} ammo types` }))),
        h("div", { class: "level" },
          h("button", { class: "btn primary", disabled: !a.pools.some((p) => p.max != null) || null, onclick: fillAll }, icon("check", 15), "Fill everything"),
          h("button", { class: "btn ghost", title: "Re-read from the game", "aria-label": "Refresh", onclick: loadAmmo }, icon("refresh", 15)))),
      h("div", { class: "bench-body" },
        a.error && h("div", { class: "warn-banner" }, icon("alert", 18), `${a.error}. Save the ammo dump below and send it over.`),
        !a.error && a.pools.length === 0 && h("div", { class: "warn-banner" }, icon("alert", 18), "No ammo pools were recognised. Save the ammo dump below and send it over."),
        h("div", { class: "char-grid" }, a.pools.map(ammoCard)))),
    dropdown("ammoinfo", "Ammo dev tools", "", h("div", { class: "tools" },
      h("button", { class: "btn sm", onclick: () => dump("/api/debug/ammo") }, "Save ammo dump"))),
  );
}

async function applyCharacter(field, label, value) {
  if (!Number.isInteger(value)) return toast("Enter a whole number", true);
  const result = await guarded(() => api("POST", "/api/character/set", { field, value }));
  if (!result) return;
  state.character = result;
  renderPinned();
  renderWorkspace();
  const { before, after } = result.applied;
  if (after === value) toast(`${label}: ${(before ?? 0).toLocaleString()} to ${after.toLocaleString()}`);
  else toast(`${label}: asked for ${value.toLocaleString()} but the game reports ${after == null ? "nothing" : after.toLocaleString()}`, true);
}

function numberCard({ title, sub, value, input, quick, glyph, color, field, max }) {
  const box = h("input", { class: "field", type: "number", min: 0, max, value: input ?? value ?? 0, "aria-label": `${title} amount`, onkeydown: (e) => e.key === "Enter" && commit() });
  const commit = () => applyCharacter(field, title, parseInt(box.value, 10));
  const available = value != null;
  return h(
    "div",
    { class: `cc${available ? "" : " off"}`, vars: { "--c": color } },
    h("div", { class: "cc-head" },
      h("span", { class: "coin", text: glyph }),
      h("div", {}, h("div", { class: "k", text: title }), sub && h("div", { class: "sub", text: sub }))),
    h("div", { class: "cc-val", text: available ? value.toLocaleString() : "unavailable" }),
    available && h("div", { class: "cc-ctrl" },
      box,
      h("button", { class: "btn sm primary", onclick: commit }, "Set"),
      quick.map(([text, fn]) => h("button", { class: "btn sm", onclick: () => { box.value = fn(parseInt(box.value, 10) || 0, value); commit(); } }, text))),
  );
}

function renderCharacter(root) {
  const c = state.character;
  if (!c) {
    fill(root, h("div", { class: "empty" }, h("div", {}, h("h2", { text: "Loading..." }))));
    return;
  }
  const [lowLevel, highLevel] = c.limits.level;
  const levelBox = h("input", { type: "number", min: lowLevel, max: highLevel, value: c.level ?? 1, onkeydown: (e) => e.key === "Enter" && setLevel() });
  const setLevel = () => applyCharacter("level", "Level", parseInt(levelBox.value, 10));
  const nudge = (by) => () => { levelBox.value = Math.max(lowLevel, Math.min(highLevel, (parseInt(levelBox.value, 10) || 1) + by)); };
  const cls = currentClass();

  const banner = h(
    "div", { class: "banner" },
    h("div", { class: "who" },
      portraitEl("big"),
      h("div", {},
      h("span", { class: "stamp equipped", text: cls.key ? `${CLASS_INFO[cls.key].job}` : "Your character" }),
      h("h1", { text: c.name || (cls.key && CLASS_INFO[cls.key].name) || "Vault Hunter" }),
      h("div", { class: "facts" },
        c.level != null && h("span", { text: `Level ${c.level}` }),
        c.xp != null && h("span", { text: `${c.xp.toLocaleString()} XP` }),
        c.xp_next_level != null && c.level < highLevel && h("span", { text: `${Math.max(0, c.xp_next_level - (c.xp || 0)).toLocaleString()} to next level` })))),
    c.level != null && h("div", { class: "level" },
      h("span", { class: "big", text: "LVL" }),
      h("div", { class: "stepper" },
        h("button", { title: "Down", "aria-label": "Level down", onclick: nudge(-1) }, icon("minus", 15)),
        levelBox,
        h("button", { title: "Up", "aria-label": "Level up", onclick: nudge(1) }, icon("plus", 15))),
      h("button", { class: "btn primary", onclick: setLevel }, icon("check", 15), "Set"),
      h("button", { class: "btn ghost", title: "Re-read from the game", "aria-label": "Refresh", onclick: loadCharacter }, icon("refresh", 15))),
  );

  const cards = [
    numberCard({
      title: "Skill points", sub: "Unspent", value: c.skill_points, field: "skill_points", glyph: "SP", color: "#ffd21f", max: c.limits.skill_points,
      quick: [["+1", (n, now) => now + 1], ["+5", (n, now) => now + 5], ["+25", (n, now) => now + 25]],
    }),
    ...c.currencies.map((cur) => {
      const style = CURRENCY_STYLE[cur.key] || { color: "#8b93a7", glyph: "?", step: 100 };
      return numberCard({
        title: cur.label, value: cur.value, field: cur.key, glyph: style.glyph, color: style.color, max: c.limits.currency,
        quick: [[`+${style.step.toLocaleString()}`, (n, now) => now + style.step], ["Max", () => c.limits.currency]],
      });
    }),
  ];

  const missing = Object.entries(c.sources).filter(([, from]) => !from).map(([key]) => key);
  fill(
    root,
    h("div", { class: "bench" },
      banner,
      h("div", { class: "bench-body" },
        h("div", { class: "warn-banner" }, icon("info", 18), "Changes apply to your live character. Save in game afterwards to keep them. Cash and tokens are capped at two billion."),
        missing.length > 0 && h("div", { class: "warn-banner" }, icon("alert", 18), `This game build didn't expose: ${missing.join(", ")}. Save the character dump below and send it over.`),
        h("div", { class: "char-grid" }, cards))),
    dropdown("charinfo", "Character dev tools", "", h("div", { class: "tools" },
      h("button", { class: "btn sm", onclick: () => dump("/api/debug/character") }, "Save character dump"))),
  );
}

// ---------- edits ----------

async function applyLevel(level) {
  if (!Number.isInteger(level)) return toast("Enter a whole number", true);
  const result = await guarded(() => api("POST", "/api/item/set_level", { id: state.selected, mode: state.mode, level }));
  if (result) { setDetail(result); toast(`Level ${result.level}`); }
}

async function applyStat(stat, value) {
  const result = await guarded(() => api("POST", "/api/item/set_stat", { id: state.selected, mode: state.mode, stat: stat.name, value }));
  if (!result) return;
  setDetail(result);
  const now = result.stats.find((s) => s.name === stat.name);
  toast(value === null ? `${stat.name} reset` : `${stat.name} ${formatNumber(now && now.value)}`);
}

async function applyPart(slotName, option) {
  const result = await guarded(() => api("POST", "/api/item/set_part", { id: state.selected, mode: state.mode, slot: slotName, part: option ? option.id : null }));
  if (!result) return;
  closePicker();
  setDetail(result);
  toast(option ? `${slotLabel(slotName)}: ${parsePart(option.name, option.group).title}` : `${slotLabel(slotName)} removed`);
}

// ---------- part picker ----------

function openPicker(slotName) {
  state.picker = { slot: slotName, query: "", mfr: null, type: null, shown: PAGE, chosen: null, loading: false, stageHost: null, viewer: null, timer: null, watcher: null, lazy: new WeakMap() };
  renderPicker();
}

function closePicker() {
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
}

const parsedCache = new WeakMap();

function parsedOptions(slot) {
  let parsed = parsedCache.get(slot);
  if (!parsed) {
    parsed = slot.options.map((option) => {
      const info = parsePart(option.name, option.group);
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
  const current = slot.current ? parsePart(slot.current.name, slot.current.group) : null;
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
  const p = state.picker;
  if (p) { p.loading = true; p.chosen = null; renderResults(); renderPreview(); }
  const detail = await guarded(() => api("GET", `/api/item?id=${encodeURIComponent(state.selected)}&mode=${mode}`));
  if (p) p.loading = false;
  if (detail) setDetail(detail);
  else if (p) renderResults();
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
    return h("div", { class: d.tone }, d.tone && icon(d.tone === "up" ? "arrowUp" : "arrowDown", 16), h("span", { text: d.text }), h("b", { text: d.value }));
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
    h("div", { class: "actions" },
      h("button", { class: "btn primary", disabled: isCurrent || null, onclick: () => applyPart(slot.slot, option) },
        icon("check", 16), isCurrent ? "Already on the gun" : `Bolt on this ${slotLabel(slot.slot).toLowerCase()}`),
      slot.current && h("button", { class: "btn danger", onclick: () => applyPart(slot.slot, null) }, icon("trash", 15), "Remove current part"),
      h("button", { class: "btn ghost sm", onclick: () => dump(`/api/debug/part?id=${encodeURIComponent(option.id)}&cls=${encodeURIComponent(option.class)}`) }, "Save part dump")),
  );
}

// ---------- boot ----------

$("#search").addEventListener("input", (event) => { state.search = event.target.value; renderInventory(); });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.picker) closePicker();
  if (event.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { event.preventDefault(); $("#search").focus(); }
});

renderPinned();
renderWorkspace();
renderInventory();
pollInventory();
loadCharacter();
setInterval(pollInventory, 2500);
