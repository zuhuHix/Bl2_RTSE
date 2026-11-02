// The character page: level, skill points, cash and tokens. Also the sidebar portrait helpers.
import { CLASS_INFO, classFromText, portraitUrl } from "./portraits.js";
import { api, dropdown, dump, fill, guarded, h, icon, shell, state, toast, unverifiedBadge } from "./core.js";

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

/** The sidebar card: the game's portrait, the class and the level. */
export function pin() {
  const c = state.character;
  const cls = currentClass();
  return {
    icon: portraitEl(),
    sub: cls.key ? `${CLASS_INFO[cls.key].name} / ${CLASS_INFO[cls.key].job}` : (c && c.name) || "Level, skill points, money",
    badge: c && c.level != null && h("div", { class: "lvl" }, h("small", { text: "LV" }), String(c.level)),
  };
}

async function loadCharacter() {
  const data = await guarded(() => api("GET", "/api/character"));
  if (!data) return;
  state.character = data;
  shell.renderPinned();
  if (state.page === "character") shell.renderWorkspace();
}

async function applyCharacter(field, label, value) {
  if (!Number.isInteger(value)) return toast("Enter a whole number", true);
  const result = await guarded(() => api("POST", "/api/character/set", { field, value }));
  if (!result) return;
  state.character = result;
  shell.renderPinned();
  shell.renderWorkspace();
  const { before, after } = result.applied;
  if (after === value) toast(`${label}: ${(before ?? 0).toLocaleString()} to ${after.toLocaleString()}`);
  else toast(`${label}: asked for ${value.toLocaleString()} but the game reports ${after == null ? "nothing" : after.toLocaleString()}`, true);
}

function numberCard({ title, sub, value, input, quick, glyph, color, field, max, reason }) {
  const box = h("input", { class: "field", type: "number", min: 0, max, value: input ?? value ?? 0, "aria-label": `${title} amount`, onkeydown: (e) => e.key === "Enter" && commit() });
  const commit = () => applyCharacter(field, title, parseInt(box.value, 10));
  const available = value != null;
  return h(
    "div",
    { class: `cc${available ? "" : " off"}`, vars: { "--c": color } },
    h("div", { class: "cc-head" },
      h("span", { class: "coin", text: glyph }),
      h("div", {}, h("div", { class: "k", text: title }, unverifiedBadge(reason)), sub && h("div", { class: "sub", text: sub }))),
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
  // Keys in `unverified` may be dotted (e.g. "limits.level", "xp_next_level", or a currency key such as "torgue").
  const unv = c.unverified || {};
  const levelReason = unv.level || unv["limits.level"];
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
        c.level != null && h("span", { text: `Level ${c.level}` }, unverifiedBadge(levelReason)),
        c.xp != null && h("span", { text: `${c.xp.toLocaleString()} XP` }),
        c.xp_next_level != null && c.level < highLevel && h("span", { text: `${Math.max(0, c.xp_next_level - (c.xp || 0)).toLocaleString()} to next level` }, unverifiedBadge(unv.xp_next_level))))),
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
      quick: [["+1", (n, now) => now + 1], ["+5", (n, now) => now + 5], ["+25", (n, now) => now + 25]], reason: unv.skill_points,
    }),
    ...c.currencies.map((cur) => {
      const style = CURRENCY_STYLE[cur.key] || { color: "#8b93a7", glyph: "?", step: 100 };
      return numberCard({
        title: cur.label, value: cur.value, field: cur.key, glyph: style.glyph, color: style.color, max: c.limits.currency, reason: unv[cur.key],
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

// The page interface (see pages.js): load() fetches the character, render() draws the page.
export { loadCharacter as load, renderCharacter as render };
