// Bank & codes: the bank, the backpack (and equipped gear, read only), importing item codes, and inventory JSON.
// Backed by rtse/bank.py, rtse/codes.py and rtse/gear_inventory.py (dev: dev/mock_inventory.py). Not yet tested in a live game.
import { classLabel, cleanItemName, rarityColor } from "./meta.js";
import { weaponTypeFrom } from "./gear_model.js";
import { gearPlate, pollInventory } from "./gear_list.js";
import { h, icon, unverifiedBadge } from "./core.js";

const BANK_REASON = "Bank access and item import use game calls nobody has tested in a live game yet";
const ARM_MS = 4000; // a destructive button needs its second click within this long
const TABS = [["bank", "Bank"], ["backpack", "Backpack"], ["import", "Import codes"], ["export", "Export / JSON"]];
const CODE_DESTS = [["backpack", "Backpack"], ["bank", "Bank"]];
const JSON_DESTS = [["original", "Original locations"], ["backpack", "Backpack"], ["bank", "Bank"]];
const WHERE = { original: "backpack and bank", backpack: "backpack", bank: "bank" };

const state = {
  tab: "bank",
  bank: null, bankError: null, // GET /api/bank
  inventory: null, inventoryError: null, // GET /api/items: backpack and equipped
  busy: false, // a write is in flight
  armed: null, // { key } while a two-click button waits for its second click
  text: "", // the import box
  preview: null, // the last GET /api/codes/preview result for the import box
  dest: "backpack", // destination for the import box
  results: null, // per-code results of the last code import
  json: null, // { name, items, preview } once an inventory JSON file has been read
  jsonDest: "original", jsonReplace: false,
  jsonResults: null,
  deleted: [], // [{ name, code }] deleted this session, newest first, so a delete can be undone by re-importing
};
let current = null; // the last ctx we were rendered with, for load() calls without one

// ---------- data ----------

/** Fetches the bank and the inventory, then redraws. Called when the page opens and after every write. */
export async function load(ctx = current) {
  if (!ctx) return;
  current = ctx;
  await refresh(ctx);
}

async function refresh(ctx) {
  const [bank, items] = await Promise.allSettled([ctx.api("GET", "/api/bank"), ctx.api("GET", "/api/items")]);
  if (bank.status === "fulfilled") Object.assign(state, { bank: bank.value, bankError: null });
  else Object.assign(state, { bank: null, bankError: bank.reason.message });
  if (items.status === "fulfilled") Object.assign(state, { inventory: items.value.items, inventoryError: null });
  else Object.assign(state, { inventory: null, inventoryError: items.reason.message });
  ctx.rerender();
}

/** One write, then the page reloads and the sidebar polls. `report` gets the reply when the write worked. */
async function write(ctx, path, body, report) {
  if (state.busy) return null;
  state.busy = true;
  state.armed = null;
  const result = await ctx.guarded(() => ctx.api("POST", path, body));
  state.busy = false;
  if (result && report) report(result);
  await refresh(ctx);
  pollInventory();
  return result;
}

// ---------- small helpers ----------

/** Copies text: the async clipboard API, else a hidden textarea and execCommand. Resolves true when it worked. */
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fall through to the older route */ }
  const area = h("textarea", { style: { position: "fixed", opacity: "0", left: "-9999px" } });
  area.value = text;
  document.body.append(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  area.remove();
  return ok;
}

const shorten = (text, max = 60) => (text.length > max ? `${text.slice(0, max)}...` : text);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const button = (label, onclick, cls = "sm") => h("button", { class: `btn ${cls}`.trim(), onclick }, label);
const warnBanner = (text) => h("div", { class: "warn-banner" }, icon("alert", 18), text);
const note = (text) => h("div", { class: "empty-note", text });

/**
 * A button that needs a second click: the first click turns it into `armed` (red) for ARM_MS, the second runs `run`.
 * Used for deletes and for an import that replaces what is already there. No window.confirm, so pages never block.
 */
function confirmButton(ctx, key, { idle, armed, cls = "sm" }, run) {
  const on = state.armed?.key === key;
  return h("button", {
    class: `btn ${cls}${on ? " bk-armed" : ""}`,
    onclick: () => {
      if (on) return run();
      state.armed = { key };
      ctx.rerender();
      setTimeout(() => {
        if (state.armed?.key !== key) return;
        state.armed = null;
        ctx.rerender();
      }, ARM_MS);
      return null;
    },
  }, on ? armed : idle);
}

async function copyCode(ctx, id) {
  const data = await ctx.guarded(() => ctx.api("GET", `/api/codes/export?id=${encodeURIComponent(id)}`));
  if (!data) return;
  const ok = await copyText(data.code);
  ctx.toast(ok ? "Copied" : "Copy failed: select the code by hand", !ok);
}

function destSelect(options, value, onChange) {
  return h("select", { class: "field bk-select", "aria-label": "Destination", onchange: (event) => onChange(event.target.value) },
    options.map(([key, label]) => h("option", { value: key, text: label, selected: key === value })));
}

// ---------- cards ----------

/** The same plate as the sidebar, with the rarity colour, clean name, class and level. `note` is an optional line. */
function itemCard(item, actions = null, extra = null) {
  return h("div", { class: "bk-card", vars: { "--rar": rarityColor(item.rarity) }, title: item.name },
    h("div", { class: "bk-plate" }, gearPlate(item)),
    h("div", { class: "bk-info" },
      h("div", { class: "bk-name", text: cleanItemName(item.name) }),
      h("div", { class: "bk-sub", text: weaponTypeFrom(item.type_path) || classLabel(item.class) }),
      extra,
      actions && h("div", { class: "bk-actions" }, actions)),
    item.level != null && h("div", { class: "bk-lvl" }, h("small", { text: "LV" }), String(item.level)));
}

const grid = (cards) => h("div", { class: "bk-grid" }, cards);

function deleteButton(ctx, item) {
  return confirmButton(ctx, `del:${item.id}`, {
    idle: "Delete", armed: `Click again to delete ${cleanItemName(item.name)}`, cls: "sm danger",
  }, () => write(ctx, "/api/inventory/delete", { id: item.id }, (r) => {
    if (r.deleted.code) state.deleted.unshift({ name: r.deleted.name, code: r.deleted.code });
    ctx.toast(`Deleted ${r.deleted.name}. Its code is kept under Import codes, in case you want it back.`);
  }));
}

function duplicate(ctx, item) {
  return write(ctx, "/api/inventory/duplicate", { id: item.id }, (r) => {
    ctx.toast(`Duplicated ${cleanItemName(item.name)} into the ${r.destination}`);
  });
}

function moveTo(ctx, item, to) {
  return write(ctx, "/api/inventory/move", { id: item.id, to }, () => {
    ctx.toast(`Moved ${cleanItemName(item.name)} to the ${to}`);
  });
}

const bankActions = (ctx, item) => [
  button("To backpack", () => moveTo(ctx, item, "backpack")),
  button("Copy code", () => copyCode(ctx, item.id)),
  button("Duplicate", () => duplicate(ctx, item)),
  deleteButton(ctx, item),
];

const backpackActions = (ctx, item) => [
  button("To bank", () => moveTo(ctx, item, "bank")),
  button("Copy code", () => copyCode(ctx, item.id)),
  button("Duplicate", () => duplicate(ctx, item)),
  deleteButton(ctx, item),
];

const equippedActions = (ctx, item) => [
  button("Copy code", () => copyCode(ctx, item.id)),
  button("Duplicate", () => duplicate(ctx, item)),
];

// ---------- tabs ----------

function meter(slots) {
  if (slots.max == null) return h("div", { class: "bk-count", text: `${slots.used} slots used` });
  const pct = Math.min(100, Math.round((slots.used / slots.max) * 100));
  return h("div", { class: "bk-meter-row" },
    h("span", { class: "bk-count", text: `${slots.used} of ${slots.max} slots used` }),
    h("div", { class: "bk-meter", role: "meter", "aria-valuenow": slots.used, "aria-valuemax": slots.max }, h("i", { style: { width: `${pct}%` } })));
}

function bankTab(ctx) {
  if (state.bankError) return warnBanner(`${state.bankError}`);
  if (!state.bank) return note("Loading...");
  const { bank } = state;
  if (!bank.available) return warnBanner(bank.reason || "The bank is not available right now.");
  return [
    meter(bank.slots),
    bank.warnings.map((text) => warnBanner(text)),
    bank.items.length ? grid(bank.items.map((item) => itemCard(item, bankActions(ctx, item)))) : note("The bank is empty."),
  ];
}

function backpackTab(ctx) {
  if (state.inventoryError) return warnBanner(state.inventoryError);
  if (!state.inventory) return note("Loading...");
  const backpack = state.inventory.filter((item) => item.location === "Backpack");
  const equipped = state.inventory.filter((item) => item.location === "Equipped");
  return [
    h("h4", { class: "bk-h", text: `Backpack (${backpack.length})` }),
    backpack.length ? grid(backpack.map((item) => itemCard(item, backpackActions(ctx, item)))) : note("The backpack is empty."),
    h("h4", { class: "bk-h", text: `Equipped (${equipped.length}, read only)` }),
    equipped.length ? grid(equipped.map((item) => itemCard(item, equippedActions(ctx, item)))) : note("Nothing equipped."),
  ];
}

/** Preview cards: good codes as item cards (a note when the UniqueId is taken), bad codes as an error row. */
function previewGrid(entries) {
  return grid(entries.map((entry) => (entry.ok
    ? itemCard(entry.card, null, entry.uid_clash ? h("div", { class: "bk-note", text: "Will get a fresh UniqueId (this one is taken)" }) : null)
    : h("div", { class: "bk-bad" }, h("b", { text: "Not a valid code" }), h("code", { text: shorten(entry.code) }), h("span", { text: entry.error })))));
}

function summaryLine(found, good) {
  if (!found) return note("No BL2(...) codes in that text.");
  return h("div", { class: "bk-summary", text: `Found ${plural(found, "code")}: ${good} valid, ${found - good} not valid` });
}

function resultBlock(title, results) {
  return [
    h("h4", { class: "bk-h", text: title }),
    h("ul", { class: "bk-results" }, results.map((r) => h("li", { class: r.ok ? "ok" : "bad" },
      r.ok ? `Added ${cleanItemName(r.name)} as UniqueId ${r.unique_id}` : `${shorten(r.code)}: ${r.error}`))),
  ];
}

function importTab(ctx) {
  const area = h("textarea", {
    class: "bk-text", rows: 6, spellcheck: "false", "aria-label": "Item codes",
    oninput: (event) => { state.text = event.target.value; },
  });
  area.value = state.text;
  return [
    h("label", { class: "bk-label", text: "Paste one or many BL2(...) codes; labels and markdown are ignored" }),
    area,
    h("div", { class: "bk-row" }, button("Preview", () => preview(ctx, area.value), "primary")),
    state.preview && codePreview(ctx),
    state.results && resultBlock("Last import", state.results),
    state.deleted.length > 0 && deletedBlock(ctx, area),
  ];
}

/** Items deleted this session, with their codes: "Restore" puts the code in the import box for a normal preview. */
function deletedBlock(ctx, area) {
  return h("div", { class: "bk-deleted" },
    h("h3", { text: "Deleted this session" }),
    state.deleted.map((entry) => h("div", { class: "bk-row" },
      h("span", { text: cleanItemName(entry.name) }),
      h("code", { class: "bk-code", text: entry.code }),
      button("Restore", () => {
        area.value = state.text = entry.code;
        return preview(ctx, entry.code);
      }))));
}

async function preview(ctx, text) {
  state.text = text;
  if (!text.trim()) return ctx.toast("Paste a code first", true);
  const result = await ctx.guarded(() => ctx.api("GET", `/api/codes/preview?text=${encodeURIComponent(text)}`));
  if (!result) return null;
  Object.assign(state, { preview: result, results: null });
  ctx.rerender();
  return result;
}

function codePreview(ctx) {
  const { entries, found } = state.preview;
  const good = entries.filter((e) => e.ok);
  return [
    summaryLine(found, good.length),
    entries.length > 0 && previewGrid(entries),
    good.length > 0 && h("div", { class: "bk-foot" },
      destSelect(CODE_DESTS, state.dest, (value) => { state.dest = value; }),
      h("button", { class: "btn primary", onclick: () => importCodes(ctx, good) }, icon("check", 15), `Import ${plural(good.length, "item")}`),
      unverifiedBadge(BANK_REASON)),
  ];
}

function importCodes(ctx, good) {
  return write(ctx, "/api/codes/import", { codes: good.map((e) => e.code), destination: state.dest }, (r) => {
    Object.assign(state, { results: r.results, preview: null });
    ctx.toast(`Imported ${r.imported}${r.failed ? `, ${r.failed} failed` : ""}`, r.imported === 0);
  });
}

function exportTab(ctx) {
  const fileInput = h("input", {
    type: "file", accept: ".json,application/json", class: "bk-file", "aria-label": "Inventory JSON file",
    onchange: (event) => readJsonFile(ctx, event.target.files[0]),
  });
  return [
    h("div", { class: "bk-tools" },
      button("Download inventory JSON", () => downloadJson(ctx), "")),
    h("div", { class: "bk-tools" },
      button("Copy all codes", () => copyAll(ctx), ""),
      h("label", { class: "btn sm" }, "Import JSON file", fileInput)),
    state.json && jsonPreview(ctx),
    state.jsonResults && resultBlock("Last JSON import", state.jsonResults),
  ];
}

async function downloadJson(ctx) {
  const data = await ctx.guarded(() => ctx.api("GET", "/api/inventory/export"));
  if (!data) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const link = h("a", { href: url, download: "rtse-inventory.json" });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  if (data.warnings.length) ctx.toast(data.warnings.join("; "), true);
  else ctx.toast(`Saved ${plural(data.items.length, "item")} to rtse-inventory.json`);
}

async function copyAll(ctx) {
  const data = await ctx.guarded(() => ctx.api("GET", "/api/inventory/export"));
  if (!data) return;
  const ok = await copyText(data.items.map((item) => `${cleanItemName(item.name)}: ${item.code}`).join("\n"));
  ctx.toast(ok ? "Copied" : "Copy failed: select the text by hand", !ok);
}

async function readJsonFile(ctx, file) {
  if (!file) return;
  let data = null;
  try { data = JSON.parse(await file.text()); } catch { data = null; }
  if (!data) return ctx.toast(`${file.name} is not valid JSON`, true);
  const items = Array.isArray(data.items) ? data.items.filter((item) => item && typeof item.code === "string") : null;
  if (!items) return ctx.toast("This is not an RTSE inventory file", true);
  if (!items.length) return ctx.toast("The file has no codes", true);
  const preview = await ctx.guarded(() => ctx.api("GET", `/api/codes/preview?text=${encodeURIComponent(items.map((i) => i.code).join("\n"))}`));
  if (!preview) return null;
  Object.assign(state, { json: { name: file.name, items, preview }, jsonResults: null });
  ctx.rerender();
  return preview;
}

function jsonPreview(ctx) {
  const { name, items, preview } = state.json;
  const valid = new Set(preview.entries.filter((e) => e.ok).map((e) => e.code));
  const send = items.filter((item) => valid.has(item.code)).map((item) => ({ code: item.code, location: item.location }));
  const where = WHERE[state.jsonDest];
  const label = `Import ${plural(send.length, "item")}`;
  return h("div", { class: "bk-json" },
    h("h4", { class: "bk-h", text: `${name}` }),
    summaryLine(preview.found, valid.size),
    previewGrid(preview.entries),
    h("div", { class: "bk-foot" },
      destSelect(JSON_DESTS, state.jsonDest, (value) => { state.jsonDest = value; ctx.rerender(); }),
      h("label", { class: "bk-check" },
        h("input", { type: "checkbox", checked: state.jsonReplace, onchange: (event) => { state.jsonReplace = event.target.checked; ctx.rerender(); } }),
        `Replace the current ${where} first (its items are deleted)`),
      send.length > 0 && (state.jsonReplace
        ? confirmButton(ctx, "replace-json", { idle: `${label}, replacing the ${where}`, armed: `Click again to replace the ${where}`, cls: "sm danger primary" },
          () => importJson(ctx, send))
        : h("button", { class: "btn primary", onclick: () => importJson(ctx, send) }, icon("check", 15), label)),
      unverifiedBadge(BANK_REASON),
      button("Cancel", () => { state.json = null; ctx.rerender(); }, "sm ghost")));
}

function importJson(ctx, send) {
  return write(ctx, "/api/inventory/import", { items: send, destination: state.jsonDest, replace: state.jsonReplace }, (r) => {
    Object.assign(state, { jsonResults: r.results, json: null, jsonReplace: false });
    const extra = r.removed ? `, removed ${r.removed}` : "";
    ctx.toast(`Imported ${r.imported}${r.failed ? `, ${r.failed} failed` : ""}${extra}`, r.imported === 0);
  });
}

// ---------- page ----------

export function render(root, ctx) {
  const { fill, dropdown } = ctx;
  current = ctx;
  const bank = state.bank;
  const bankFact = bank?.available ? `${bank.slots.used} / ${bank.slots.max} slots` : "-";
  const backpackCount = state.inventory ? state.inventory.filter((i) => i.location === "Backpack").length : "-";
  const tabBody = { bank: bankTab, backpack: backpackTab, import: importTab, export: exportTab }[state.tab](ctx);
  fill(
    root,
    h("div", { class: "bench bk-page" },
      h("div", { class: "banner" },
        h("div", {},
          h("span", { class: "stamp equipped", text: "Bank, codes and import" }),
          h("h1", {}, "Bank & codes", unverifiedBadge(BANK_REASON)),
          h("div", { class: "facts" },
            h("span", {}, "Bank ", h("b", { text: bankFact })),
            h("span", {}, "Backpack ", h("b", { text: String(backpackCount) }))))),
      h("div", { class: "bench-body bk-pane" },
        h("div", { class: "seg bk-seg", role: "tablist" },
          TABS.map(([id, label]) => h("button", {
            class: state.tab === id ? "on" : "", role: "tab", "aria-selected": String(state.tab === id),
            onclick: () => { state.tab = id; ctx.rerender(); },
          }, label))),
        h("div", { class: "bk-tab" }, tabBody))),
    dropdown("bankdev", "Dev tools", "", h("div", {},
      h("p", { class: "note", text: "Saves what the codes and bank calls returned, for a bug report." }),
      h("div", { class: "tools" }, h("button", { class: "btn sm", onclick: () => ctx.dump("/api/debug/codes") }, "Save codes dump")))),
  );
}
