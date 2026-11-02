// The shell: boot, the connection pill, the sidebar's pinned pages, routing between pages, and keys.
import { $, fill, h, icon, pageCtx, shell, state } from "./core.js";
import { pollInventory, renderInventory } from "./gear_list.js";
import { closePicker, renderGear } from "./gear_detail.js";
import { currentClass, loadCharacter, openCharacter, portraitEl, renderCharacter } from "./page_character.js";
import { openAmmo, renderAmmo } from "./page_ammo.js";
import { CLASS_INFO } from "./portraits.js";
import * as sduPage from "./page_sdu.js";
import * as skillsPage from "./page_skills.js";
import * as worldPage from "./page_world.js";
import * as inspectPage from "./page_inspect.js";

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
    pin("world", openWorld, icon("swap", 20), "World", "Missions, challenges, travel", null),
    pin("inspect", openInspect, icon("search", 20), "Inspector", "Raw live game data", null));
}

function renderWorkspace() {
  const root = $("#workspace");
  if (state.page === "character") return renderCharacter(root);
  if (state.page === "ammo") return renderAmmo(root);
  if (state.page === "sdu") return sduPage.render(root, pageCtx);
  if (state.page === "skills") return skillsPage.render(root, pageCtx);
  if (state.page === "world") return worldPage.render(root, pageCtx);
  if (state.page === "inspect") return inspectPage.render(root, pageCtx);
  return renderGear(root);
}

// ---------- feature pages (own modules: page_sdu.js, page_skills.js, page_world.js, page_inspect.js) ----------

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
const openInspect = () => openPage("inspect", inspectPage);

// ---------- boot ----------

$("#search").addEventListener("input", (event) => { state.search = event.target.value; renderInventory(); });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.picker) closePicker();
  if (event.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { event.preventDefault(); $("#search").focus(); }
});

shell.renderPinned = renderPinned;
shell.renderWorkspace = renderWorkspace;

renderPinned();
renderWorkspace();
renderInventory();
pollInventory();
loadCharacter();
setInterval(pollInventory, 2500);
