// The ammo page: every ammo pool, set to a number or filled, plus the ammo dev tools.
import { api, dump, dropdown, fill, guarded, h, icon, pageCtx, shell, state, toast, unverifiedBadge } from "./core.js";
import * as sduPage from "./page_sdu.js";

// ---------- ammo ----------

async function loadAmmo() {
  try {
    state.ammo = await api("GET", "/api/ammo");
  } catch (error) {
    state.ammo = { pools: [], error: error.message };
  }
  if (state.page === "ammo") shell.renderWorkspace();
}

async function applyAmmo(pool, value) {
  if (value !== "max" && !Number.isInteger(value)) return toast("Enter a whole number", true);
  const result = await guarded(() => api("POST", "/api/ammo/set", { id: pool.id, value }));
  if (!result) return;
  state.ammo = result;
  shell.renderWorkspace();
  const { before, after, requested } = result.applied;
  if (after === requested) toast(`${pool.label}: ${(before ?? 0).toLocaleString()} to ${after.toLocaleString()}`);
  else toast(`${pool.label}: asked for ${requested.toLocaleString()} but the game reports ${after == null ? "nothing" : after.toLocaleString()} (the game may cap it at your maximum)`, true);
}

function ammoCard(pool, reason) {
  const box = h("input", { class: "field", type: "number", min: 0, value: pool.value ?? 0, "aria-label": `${pool.label} ammo`, onkeydown: (e) => e.key === "Enter" && commit() });
  const commit = () => applyAmmo(pool, parseInt(box.value, 10));
  const pct = pool.max ? Math.max(0, Math.min(100, ((pool.value || 0) / pool.max) * 100)) : 0;
  return h(
    "div",
    { class: "cc ammo", vars: { "--c": "#ffd21f" } },
    h("div", { class: "cc-head" }, h("span", { class: "coin", text: pool.label.slice(0, 2).toUpperCase() }),
      h("div", {}, h("div", { class: "k", text: pool.label }, unverifiedBadge(reason)), pool.max != null && h("div", { class: "sub", text: `Capacity ${pool.max.toLocaleString()}` }))),
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
        h("div", { class: "char-grid" }, a.pools.map((pool) => ammoCard(pool, (a.unverified || {})[pool.key]))))),
    dropdown("ammoinfo", "Ammo dev tools", "", h("div", { class: "tools" },
      h("button", { class: "btn sm", onclick: () => dump("/api/debug/ammo") }, "Save ammo dump"))),
  );
}

// The page interface (see pages.js): load() fetches the ammo pools, render() draws the page.
export { loadAmmo as load, renderAmmo as render };
