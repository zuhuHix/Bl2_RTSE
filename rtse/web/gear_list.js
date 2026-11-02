// The loadout list in the sidebar: rows, thumbnails, search, polling, and selecting an item.
import { cleanItemName, classLabel, itemManufacturer, manufacturerColor, rarityColor } from "./meta.js";
import { gunFor, gunSvg } from "./guns.js";
import { itemClassSvg, plateEl } from "./icons.js";
import { $, api, ensureViewerModule, fill, h, shell, state, v3d } from "./core.js";
import { modelKeyFor, piecesOf, weaponTypeFrom } from "./gear_model.js";
import { closePicker, loadDetail } from "./gear_detail.js";

const rowThumbs = new Map(); // picture signature -> data URL, so a re-render never flashes back to the sketch

/** Swaps a plate's sketch for a render of the real gun once one is ready. */
function showRender(plate, item) {
  const key = modelKeyFor(item);
  const pieces = (item.build || []).flatMap((part) => piecesOf(part, part.slot));
  if (!key || !pieces.length || v3d.failed) return;
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

export function renderInventory() {
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

export async function pollInventory() {
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

export async function selectItem(id) {
  state.page = "gear";
  shell.renderPinned();
  state.selected = id;
  state.detail = null;
  closePicker();
  renderInventory();
  shell.renderWorkspace();
  await loadDetail();
}
