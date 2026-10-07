// Inspector page: browse the live game's objects and change plain numbers, switches and text in them.
// Draws into the workspace; its state lives here. app.js hands in the helpers it owns as `ctx`:
// { h, fill, icon, api, toast, guarded, dump, dropdown, rerender }.

let ctx = null;
const h = (...args) => ctx.h(...args);
const fill = (...args) => ctx.fill(...args);
const icon = (...args) => ctx.icon(...args);

const state = {
  path: "", // what is open ("" = the list of starting points)
  data: null, // the last /api/inspect reply
  query: "", // the filter box
  busy: false,
  error: null,
};

/** Opens a path (or the starting list), then redraws. */
export async function load(passed, path = state.path, offset = 0) {
  ctx = passed || ctx;
  if (!ctx) return;
  state.busy = true;
  try {
    const query = path ? `?path=${encodeURIComponent(path)}${offset ? `&offset=${offset}` : ""}` : "";
    state.data = await ctx.api("GET", `/api/inspect${query}`);
    state.path = path;
    state.error = null;
  } catch (error) {
    state.error = error.message;
  }
  state.busy = false;
  ctx.rerender();
}

const open = (path, offset = 0) => { state.query = ""; return load(null, path, offset); };

async function write(row, value) {
  const result = await ctx.guarded(() => ctx.api("POST", "/api/inspect/set", { path: row.path, value, confirm: true, offset: state.data.offset || 0 }));
  if (!result) return;
  state.data = result;
  state.path = result.path;
  const a = result.applied;
  const same = String(a.after) === String(a.requested);
  ctx.toast(same ? `${row.name}: ${a.before} to ${a.after}` : `${row.name}: asked for ${a.requested} but the game reports ${a.after}`, !same);
  ctx.rerender();
}

// ---------- pieces ----------

function valueCell(row) {
  if (!row.editable) {
    return h("span", { class: `in-val ${row.kind}`, title: String(row.value ?? ""), text: row.value === null ? "none" : String(row.value) });
  }
  let read;
  let box;
  if (row.kind === "bool") {
    box = h("select", { class: "field in-box", "aria-label": `${row.name} value` },
      h("option", { value: "true", selected: row.value === true || null }, "true"),
      h("option", { value: "false", selected: row.value === false || null }, "false"));
    read = () => box.value === "true";
  } else {
    const isNumber = row.kind === "int" || row.kind === "float";
    const start = row.kind === "enum" ? (String(row.value).match(/\((-?\d+)\)$/) || [])[1] ?? "" : row.value;
    box = h("input", { class: "field in-box", type: isNumber ? "number" : "text", step: row.kind === "float" ? "any" : null, value: start, "aria-label": `${row.name} value`, title: row.kind === "enum" ? `Currently ${row.value}. Enter its number.` : null });
    read = () => (isNumber ? Number(box.value) : box.value);
  }
  const commit = () => {
    if (row.kind === "int" && !Number.isInteger(read())) return ctx.toast("Enter a whole number", true);
    if (row.kind === "float" && Number.isNaN(read())) return ctx.toast("Enter a number", true);
    if (row.kind === "enum" && !/^-?\d+$/.test(box.value.trim())) return ctx.toast("Enter the value's number", true);
    return write(row, row.kind === "enum" ? parseInt(box.value, 10) : read());
  };
  box.addEventListener("keydown", (e) => e.key === "Enter" && commit());
  return h("span", { class: "in-edit" }, box, row.kind === "enum" && h("em", { class: "in-enum", text: String(row.value).replace(/\s*\(.*$/, "") }),
    h("button", { class: "btn sm primary", onclick: commit }, "Set"));
}

function rowEl(row) {
  return h("div", { class: `in-row${row.openable ? " openable" : ""}` },
    h("div", { class: "in-name" },
      row.openable
        ? h("button", { class: "in-link", onclick: () => open(row.path) }, icon("chevron", 13), row.name)
        : h("span", { text: row.name })),
    h("div", { class: "in-type", text: row.type }),
    h("div", { class: "in-value" }, valueCell(row)));
}

function crumbs(data) {
  const parts = [h("button", { class: "in-crumb", onclick: () => open("") }, "Start")];
  for (const c of data.crumbs || []) parts.push(h("span", { class: "in-sep", text: "/" }), h("button", { class: "in-crumb", onclick: () => open(c.path) }, c.label));
  return h("div", { class: "in-crumbs" }, parts);
}

function pager(data) {
  if (data.kind !== "array" || (data.offset === 0 && data.total <= data.rows.length)) return null;
  const step = data.rows.length || 100;
  const from = data.offset + 1;
  const to = data.offset + data.rows.length;
  return h("div", { class: "in-pager" },
    h("button", { class: "btn sm", disabled: data.offset === 0 || null, onclick: () => open(data.path, Math.max(0, data.offset - 100)) }, "Previous"),
    h("span", { text: `${from.toLocaleString()}-${to.toLocaleString()} of ${data.total.toLocaleString()}` }),
    h("button", { class: "btn sm", disabled: to >= data.total || null, onclick: () => open(data.path, data.offset + step) }, "Next"));
}

export function render(root, passed) {
  ctx = passed;
  const data = state.data;
  if (!data && !state.error) {
    fill(root, h("div", { class: "empty" }, h("div", {}, h("h2", { text: "Loading..." }))));
    return;
  }
  const search = h("input", {
    class: "field wl-search", type: "search", placeholder: "Filter these fields", value: state.query, "aria-label": "Filter fields", autocomplete: "off",
    oninput: () => { state.query = search.value; redrawList(); },
  });
  const list = h("div", { class: "in-list" });
  const count = h("span", { class: "in-count" });
  const redrawList = () => {
    const q = state.query.trim().toLowerCase();
    const shown = data ? data.rows.filter((r) => !q || `${r.name} ${r.type} ${r.value}`.toLowerCase().includes(q)) : [];
    count.textContent = data ? `${shown.length.toLocaleString()} of ${data.rows.length.toLocaleString()} shown` : "";
    fill(list, shown.length ? shown.map(rowEl) : h("div", { class: "empty-note", text: data && data.rows.length ? "Nothing matches that filter." : "Nothing here." }));
  };
  redrawList();

  fill(
    root,
    h("div", { class: "bench" },
      h("div", { class: "banner" },
        h("div", {},
          h("span", { class: "stamp equipped", text: "Raw" }),
          h("h1", { text: "Inspector" }),
          h("div", { class: "facts" },
            data && data.type && h("span", { text: data.type }),
            data && data.kind !== "roots" && h("span", { text: `${(data.total || 0).toLocaleString()} ${data.kind === "array" ? "entries" : "fields"}` }))),
        h("button", { class: "btn ghost", title: "Read again from the game", "aria-label": "Refresh", onclick: () => load(null, state.path, data ? data.offset : 0) }, icon("refresh", 15))),
      h("div", { class: "warn-banner" }, icon("alert", 18),
        "This shows the game's own objects exactly as they are, and changing a value skips every check. A wrong value can confuse the game or crash it, and some changes do nothing. Back up your saves first."),
      h("div", { class: "bench-body" },
        state.error && h("div", { class: "warn-banner" }, icon("alert", 18), `${state.error}.`),
        data && crumbs(data),
        data && h("div", { class: "in-bar" }, h("div", { class: "wl-find" }, icon("search", 15), search), count),
        pager(data || { kind: "none" }),
        list,
        pager(data || { kind: "none" }))));
}
