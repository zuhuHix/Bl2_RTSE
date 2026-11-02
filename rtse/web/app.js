// The shell: boot, the connection pill, the sidebar, routing between pages, and the keyboard shortcuts.
// Which pages exist is listed in pages.js; this file builds the sidebar from that list and opens pages.
import { $, fill, h, icon, pageCtx, shell, state } from "./core.js";
import { pollInventory, renderInventory } from "./gear_list.js";
import { closePicker, renderGear } from "./gear_detail.js";
import { PAGES } from "./pages.js";

const modules = new Map(); // page id -> its module, once it has been imported

/** Imports a page's module the first time it is needed. */
function importPage(page) {
  if (modules.has(page.id)) return Promise.resolve(modules.get(page.id));
  return import(page.module).then((mod) => {
    modules.set(page.id, mod);
    return mod;
  });
}

/** The sidebar: one card per page, showing the page's own pin() if it has one, else the registry's text. */
function renderPinned() {
  fill($("#pinned"), PAGES.map((page) => {
    const mod = modules.get(page.id);
    const card = mod && mod.pin ? mod.pin(pageCtx) : { icon: page.icon && icon(page.icon, 20), sub: page.sub };
    return h(
      "button",
      { class: `pin${state.page === page.id ? " on" : ""}`, onclick: () => openPage(page.id) },
      h("span", { class: "pin-ic" }, card.icon),
      h("div", {}, h("div", { class: "t", text: page.label }), h("div", { class: "s", text: card.sub })),
      card.badge,
    );
  }));
}

function renderWorkspace() {
  const root = $("#workspace");
  const mod = modules.get(state.page);
  if (mod) return mod.render(root, pageCtx);
  if (state.page !== "gear") return fill(root, h("div", { class: "empty" }, h("div", {}, h("h2", { text: "Loading..." }))));
  return renderGear(root);
}

/** Opens a page: draws it at once, then loads its data (importing its module the first time). */
async function openPage(id) {
  const page = PAGES.find((p) => p.id === id);
  state.page = id;
  closePicker();
  renderPinned();
  renderInventory();
  renderWorkspace();
  const mod = await importPage(page);
  if (state.page !== id) return; // the user has moved on while the module loaded
  renderWorkspace();
  await mod.load(pageCtx);
}

// ---------- boot ----------

for (const page of PAGES) {
  if (page.css) document.head.append(h("link", { rel: "stylesheet", href: page.css }));
}
shell.renderPinned = renderPinned;
shell.renderWorkspace = renderWorkspace;

$("#search").addEventListener("input", (event) => { state.search = event.target.value; renderInventory(); });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.picker) closePicker();
  if (event.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { event.preventDefault(); $("#search").focus(); }
});

renderPinned();
renderWorkspace();
renderInventory();
pollInventory();
for (const page of PAGES.filter((p) => p.eager)) {
  importPage(page).then((mod) => {
    renderPinned();
    return mod.load(pageCtx);
  });
}
setInterval(pollInventory, 2500);
