// Bank & codes: the bank, the backpack (and equipped gear, read only), importing item codes, and inventory JSON.
// Backed by rtse/bank.py, rtse/codes.py and rtse/gear_inventory.py (dev: dev/mock_inventory.py). Not yet tested in a live game.
import { classLabel, cleanItemName, rarityColor } from "./meta.js";
import { weaponTypeFrom } from "./gear_model.js";
import { gearPlate, pollInventory } from "./gear_list.js";
import { h, icon, unverifiedBadge } from "./core.js";

const BANK_REASON = "Bank access and item import use game calls nobody has tested in a live game yet";
const SEND_MAX = 200; // the server refuses more codes than this in one request (codes.MAX_CODES)
const ARM_MS = 4000; // a delete needs its second click within this long
const TABS = [["bank", "Bank"], ["backpack", "Backpack"], ["import", "Import codes"], ["export", "Export / JSON"]];
const CODE_DESTS = [["backpack", "Backpack"], ["bank", "Bank"]];
const JSON_DESTS = [["original", "Original locations"], ["backpack", "Backpack"], ["bank", "Bank"]];

const state = {
  tab: "bank",
  bank: null, bankError: null, // GET /api/bank
  inventory: null, inventoryError: null, // GET /api/items: backpack and equipped
  busy: false, // a write or a chunked run is in flight
  armed: null, // { key } while a two-click button waits for its second click
  text: "", // the import box
  preview: null, // { entries, found } from the last decode of the import box
  dest: "backpack", // destination for the import box
  results: null, // report of the last code import
  json: null, // { name, items, preview: { entries, found } } once an inventory JSON file has been read
  jsonDest: "original",
  jsonEquipped: false, // also add the Equipped rows to the backpack (else the server skips them)
  jsonResults: null, // report of the last JSON import
  progress: null, // "Decoding 20 / 120..." while a chunked call runs
  stopped: null, // "Stopped after N of M: <error>" when a chunked call failed part way
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

/** After a chunked run: reload the lists and the sidebar, whatever happened. */
async function afterRun(ctx) {
  await refresh(ctx);
  pollInventory();
}

/**
 * Runs one chunked call to the end. `send(done)` makes one request, where `done` is how many codes or items the
 * earlier replies processed; each reply has `processed` and `remaining`. Stops at the first failure and keeps the
 * replies that came back. Progress shows as it goes, and the tabs' buttons are disabled meanwhile.
 */
async function inChunks(ctx, verb, total, send) {
  const replies = [];
  let done = 0;
  state.busy = true;
  state.stopped = null;
  setProgress(ctx, `${verb} 0 / ${total}...`);
  try {
    for (;;) {
      const reply = await send(done);
      replies.push(reply);
      const processed = reply.processed || 0;
      done += processed;
      if (done >= total) break; // each request carries at most SEND_MAX codes, so `remaining` is per request
      if (!(processed > 0)) throw new Error("the game took no codes from the last request");
      setProgress(ctx, `${verb} ${done} / ${total}...`);
    }
  } catch (error) {
    state.stopped = `Stopped after ${done} of ${total}: ${error.message}`;
  } finally {
    state.busy = false;
    state.progress = null;
  }
  return { replies, done, stopped: state.stopped };
}

/** Sums the replies of a chunked import into one report: every result in order, and the totals. */
function reportOf(replies) {
  const sum = (key) => replies.reduce((n, reply) => n + (reply[key] || 0), 0);
  return {
    results: replies.flatMap((reply) => reply.results),
    imported: sum("imported"), failed: sum("failed"), skipped: sum("skipped"),
    warnings: replies.flatMap((reply) => reply.warnings || []),
  };
}

const reportText = (report) => {
  const parts = [`Imported ${report.imported}`];
  if (report.failed) parts.push(`${report.failed} failed`);
  if (report.skipped) parts.push(`${report.skipped} equipped skipped`);
  return parts.join(", ");
};

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
const button = (label, onclick, cls = "sm") => h("button", { class: `btn ${cls}`.trim(), onclick, disabled: state.busy }, label);
const warnBanner = (text) => h("div", { class: "warn-banner" }, icon("alert", 18), text);
const note = (text) => h("div", { class: "empty-note", text });
const setProgress = (ctx, text) => { state.progress = text; ctx.rerender(); };
const progressLine = () => state.progress && h("div", { class: "bk-progress", role: "status", text: state.progress });
const stoppedLine = () => state.stopped && warnBanner(state.stopped);

/**
 * A button that needs a second click: the first click turns it into `armed` (red) for ARM_MS, the second runs `run`.
 * Used for deletes. No window.confirm, so pages never block.
 */
function confirmButton(ctx, key, { idle, armed, cls = "sm" }, run) {
  const on = state.armed?.key === key;
  return h("button", {
    class: `btn ${cls}${on ? " bk-armed" : ""}`,
    disabled: state.busy,
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
  return h("select", {
    class: "field bk-select", "aria-label": "Destination", disabled: state.busy,
    onchange: (event) => onChange(event.target.value),
  }, options.map(([key, label]) => h("option", { value: key, text: label, selected: key === value })));
}

// ---------- cards ----------

/** The same plate as the sidebar, with the rarity colour, clean name, class and level. `extra` is optional lines. */
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

/** A code the game decoded but gave no card for: a neutral box, the kind, the UniqueId and the warning. */
function fallbackCard(entry) {
  const kind = entry.kind === "weapon" ? "Weapon" : "Item";
  const uid = entry.unique_id != null ? `UniqueId ${entry.unique_id}` : "UniqueId unknown";
  return h("div", { class: "bk-card bk-fallback", vars: { "--rar": "var(--muted)" }, title: entry.code },
    h("div", { class: "bk-plate" }, h("div", { class: "bk-blank", "aria-hidden": "true" })),
    h("div", { class: "bk-info" },
      h("div", { class: "bk-name", text: kind }),
      h("div", { class: "bk-sub", text: uid }),
      h("div", { class: "bk-note warn", text: entry.warning || "The game could not read this item's card." }),
      entry.uid_clash && h("div", { class: "bk-note", text: "Will get a fresh UniqueId (this one is taken)" })));
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

/** Preview cards: good codes as item cards (a fallback card when the game gave none), bad codes as an error row. */
function previewGrid(entries) {
  return grid(entries.map((entry) => {
    if (!entry.ok) return h("div", { class: "bk-bad" }, h("b", { text: "Not a valid code" }), h("code", { text: shorten(entry.code) }), h("span", { text: entry.error }));
    if (!entry.card) return fallbackCard(entry);
    return itemCard(entry.card, null, [
      entry.uid_clash && h("div", { class: "bk-note", text: "Will get a fresh UniqueId (this one is taken)" }),
      entry.warning && h("div", { class: "bk-note warn", text: entry.warning }),
    ]);
  }));
}

/** `decoded` is how many codes have been decoded so far; less than `found` while a decode is stopped part way. */
function summaryLine(found, good, decoded = found) {
  if (!found) return note("No BL2(...) codes in that text.");
  const undecoded = found - decoded;
  const tail = undecoded > 0 ? `, ${undecoded} not decoded` : "";
  return h("div", { class: "bk-summary", text: `Found ${plural(found, "code")}: ${good} valid, ${decoded - good} not valid${tail}` });
}

function resultRow(r) {
  if (r.ok) return h("li", { class: "ok" }, `Added ${cleanItemName(r.name)} as UniqueId ${r.unique_id}`);
  if (r.skipped) return h("li", { class: "skip" }, `Skipped ${shorten(r.code)}: ${r.error}`);
  return h("li", { class: "bad" }, `${shorten(r.code)}: ${r.error}`);
}

function reportBlock(title, report) {
  return [
    h("h4", { class: "bk-h", text: title }),
    h("div", { class: "bk-summary", text: reportText(report) }),
    report.warnings.map((text) => warnBanner(text)),
    report.results.length > 0 && h("ul", { class: "bk-results" }, report.results.map(resultRow)),
  ];
}

function importTab(ctx) {
  const area = h("textarea", {
    class: "bk-text", rows: 6, spellcheck: "false", "aria-label": "Item codes",
    oninput: (event) => { state.text = event.target.value; },
  });
  area.value = state.text;
  return [
    progressLine(),
    stoppedLine(),
    h("label", { class: "bk-label", text: "Paste one or many BL2(...) codes; labels and markdown are ignored" }),
    area,
    h("div", { class: "bk-row" }, button("Preview", () => preview(ctx, area.value), "primary")),
    state.preview && codePreview(ctx),
    state.results && reportBlock("Last import", state.results),
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

/**
 * Decodes a whole text: GET /api/codes/preview from start 0, then from each `next`, until `next` is null.
 * Stops at the first failure and keeps the entries that came back.
 */
async function decodeAll(ctx, text) {
  const entries = [];
  let found = null;
  let start = 0;
  state.busy = true;
  state.stopped = null;
  setProgress(ctx, "Decoding...");
  try {
    for (;;) {
      const reply = await ctx.api("GET", `/api/codes/preview?text=${encodeURIComponent(text)}&start=${start}`);
      found = reply.found;
      entries.push(...reply.entries);
      if (reply.next == null) break;
      if (!(reply.next > start)) throw new Error("the game did not move on to the next codes");
      start = reply.next;
      setProgress(ctx, `Decoding ${start} / ${found}...`);
    }
  } catch (error) {
    state.stopped = found == null ? `Stopped: ${error.message}` : `Stopped after ${entries.length} of ${found}: ${error.message}`;
  } finally {
    state.busy = false;
    state.progress = null;
  }
  return { entries, found, stopped: state.stopped };
}

async function preview(ctx, text) {
  state.text = text;
  if (!text.trim()) return ctx.toast("Paste a code first", true);
  if (state.busy) return null;
  const decoded = await decodeAll(ctx, text);
  Object.assign(state, {
    preview: decoded.found == null ? null : { entries: decoded.entries, found: decoded.found },
    results: null,
  });
  ctx.rerender();
  return decoded;
}

function codePreview(ctx) {
  const { entries, found } = state.preview;
  const good = entries.filter((e) => e.ok);
  return [
    summaryLine(found, good.length, entries.length),
    entries.length > 0 && previewGrid(entries),
    good.length > 0 && h("div", { class: "bk-foot" },
      destSelect(CODE_DESTS, state.dest, (value) => { state.dest = value; }),
      h("button", { class: "btn primary", disabled: state.busy, onclick: () => importCodes(ctx, good) },
        icon("check", 15), `Import ${plural(good.length, "item")}`),
      unverifiedBadge(BANK_REASON)),
  ];
}

async function importCodes(ctx, good) {
  if (state.busy) return null;
  const codes = good.map((e) => e.code);
  const destination = state.dest;
  const run = await inChunks(ctx, "Importing", codes.length, (done) =>
    ctx.api("POST", "/api/codes/import", { codes: codes.slice(done, done + SEND_MAX), destination }));
  const report = reportOf(run.replies);
  // a stop after some codes went in clears the preview: the box holds what is still to do
  Object.assign(state, {
    results: run.replies.length ? report : null,
    preview: run.done > 0 || !run.stopped ? null : state.preview,
  });
  await afterRun(ctx);
  if (!run.stopped) ctx.toast(reportText(report), report.imported === 0);
  return report;
}

function exportTab(ctx) {
  const fileInput = h("input", {
    type: "file", accept: ".json,application/json", class: "bk-file", "aria-label": "Inventory JSON file",
    onchange: (event) => readJsonFile(ctx, event.target.files[0]),
  });
  return [
    progressLine(),
    stoppedLine(),
    h("div", { class: "bk-tools" },
      button("Download inventory JSON", () => downloadJson(ctx), "")),
    h("div", { class: "bk-tools" },
      button("Copy all codes", () => copyAll(ctx), ""),
      h("label", { class: "btn sm" }, "Import JSON file", fileInput)),
    state.json && jsonPreview(ctx),
    state.jsonResults && reportBlock("Last JSON import", state.jsonResults),
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
  if (state.busy) return ctx.toast("Wait for the current import to finish", true);
  let data = null;
  try { data = JSON.parse(await file.text()); } catch { data = null; }
  if (!data) return ctx.toast(`${file.name} is not valid JSON`, true);
  const items = Array.isArray(data.items) ? data.items.filter((item) => item && typeof item.code === "string") : null;
  if (!items) return ctx.toast("This is not an RTSE inventory file", true);
  if (!items.length) return ctx.toast("The file has no codes", true);
  const decoded = await decodeAll(ctx, items.map((i) => i.code).join("\n"));
  Object.assign(state, {
    json: decoded.found == null ? null : { name: file.name, items, preview: { entries: decoded.entries, found: decoded.found } },
    jsonResults: null,
  });
  ctx.rerender();
  return decoded;
}

function jsonPreview(ctx) {
  const { name, items, preview } = state.json;
  const valid = new Set(preview.entries.filter((e) => e.ok).map((e) => e.code));
  const send = items.filter((item) => valid.has(item.code)).map((item) => ({ code: item.code, location: item.location }));
  // the server skips Equipped rows unless the box is ticked, so the button counts only what will be added
  const addable = state.jsonEquipped ? send : send.filter((item) => item.location !== "Equipped");
  return h("div", { class: "bk-json" },
    h("h4", { class: "bk-h", text: name }),
    summaryLine(preview.found, valid.size, preview.entries.length),
    previewGrid(preview.entries),
    h("div", { class: "bk-foot" },
      destSelect(JSON_DESTS, state.jsonDest, (value) => { state.jsonDest = value; ctx.rerender(); }),
      h("label", { class: "bk-check" },
        h("input", {
          type: "checkbox", checked: state.jsonEquipped, disabled: state.busy,
          onchange: (event) => { state.jsonEquipped = event.target.checked; ctx.rerender(); },
        }),
        "Also add equipped items to the backpack"),
      addable.length > 0 && h("button", { class: "btn primary", disabled: state.busy, onclick: () => importJson(ctx, send) },
        icon("check", 15), `Import ${plural(addable.length, "item")}`),
      unverifiedBadge(BANK_REASON),
      button("Cancel", () => { state.json = null; ctx.rerender(); }, "sm ghost")),
    h("p", { class: "bk-hint", text: "Only adds items. Delete items separately." }));
}

async function importJson(ctx, send) {
  if (state.busy) return null;
  const destination = state.jsonDest;
  const equipped = state.jsonEquipped ? "backpack" : "skip";
  const run = await inChunks(ctx, "Importing", send.length, (done) =>
    ctx.api("POST", "/api/inventory/import", { items: send.slice(done, done + SEND_MAX), destination, equipped }));
  const report = reportOf(run.replies);
  Object.assign(state, {
    jsonResults: run.replies.length ? report : null,
    json: run.done > 0 || !run.stopped ? null : state.json,
  });
  await afterRun(ctx);
  if (!run.stopped) ctx.toast(reportText(report), report.imported === 0);
  return report;
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
