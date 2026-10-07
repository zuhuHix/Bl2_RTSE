// Skill tree page: every skill of the class with its points, set any skill to any level, reset the tree.
// Draws into the workspace; its state lives here, not in app.js. app.js hands in the helpers it owns as `ctx`:
// { h, fill, icon, api, toast, guarded, dump, dropdown, rerender }.

let ctx = null;
const h = (...args) => ctx.h(...args);
const fill = (...args) => ctx.fill(...args);
const icon = (...args) => ctx.icon(...args);

const state = {
  data: null, // the last /api/skills reply
  busy: false, // a write is in flight; clicks are ignored until it lands
  confirmReset: false, // the inline "Really reset?" step is showing
};

/** Fetches the tree, then redraws. A failed fetch shows as an unavailable tree rather than an empty page. */
export async function load(passed) {
  ctx = passed || ctx;
  if (!ctx) return;
  try {
    state.data = await ctx.api("GET", "/api/skills");
  } catch (error) {
    state.data = { available: false, reason: error.message, skills: [], branches: [], skill_points: null, limits: { skill_points: 999, tier_unlock: 5 }, sources: {} };
  }
  ctx.rerender();
}

async function write(path, body) {
  if (state.busy) return null;
  state.busy = true;
  const result = await ctx.guarded(() => ctx.api("POST", path, body));
  state.busy = false;
  if (result) state.data = result;
  ctx.rerender();
  return result;
}

async function setLevel(skill, level) {
  if (level === skill.level) return;
  const result = await write("/api/skills/set", { id: skill.id, level });
  if (!result) return;
  const { before, after, requested } = result.applied;
  if (after === requested) ctx.toast(`${skill.name}: ${before ?? 0} to ${after}`);
  else ctx.toast(`${skill.name}: asked for ${requested} but the game reports ${after ?? "nothing"}`, true);
}

async function setPoints(value) {
  if (!Number.isInteger(value)) return ctx.toast("Enter a whole number", true);
  if (state.busy) return;
  state.busy = true;
  const result = await ctx.guarded(() => ctx.api("POST", "/api/character/set", { field: "skill_points", value }));
  state.busy = false;
  if (!result) return;
  // The character reply has no skills in it; keep the tree we already have and take only the new total.
  state.data = { ...state.data, skill_points: result.skill_points };
  ctx.rerender();
  const { before, after } = result.applied;
  if (after === value) ctx.toast(`Skill points: ${(before ?? 0).toLocaleString()} to ${after.toLocaleString()}`);
  else ctx.toast(`Skill points: asked for ${value.toLocaleString()} but the game reports ${after ?? "nothing"}`, true);
}

async function resetTree() {
  state.confirmReset = false;
  const result = await write("/api/skills/reset", { confirm: true });
  if (!result) return;
  const { before, refunded, refund_via: via } = result.applied;
  if (before > 0 && !via) ctx.toast("Tree cleared, but the points could not be handed back. Set them yourself above.", true);
  else ctx.toast(`Tree reset: ${refunded ? `${refunded} points added to your unspent total` : `${before} points refunded by the game`}`);
}

// ---------- drawing ----------

function pips(skill) {
  const max = skill.max ?? 5;
  return h("div", { class: "sk-pips", role: "group", "aria-label": `${skill.name} points` },
    Array.from({ length: max }, (_, i) => h("button", {
      class: `sk-pip${i < (skill.level || 0) ? " on" : ""}`,
      title: `Set to ${i + 1 === skill.level ? i : i + 1}`,
      "aria-label": `Set ${skill.name} to ${i + 1 === skill.level ? i : i + 1}`,
      disabled: skill.level == null || null,
      onclick: () => setLevel(skill, i + 1 === skill.level ? i : i + 1),
    })));
}

function tile(skill) {
  const known = skill.level != null;
  const maxed = known && skill.max != null && skill.level >= skill.max;
  const top = skill.max ?? 5;
  const cls = ["sk", skill.locked && "locked", maxed && "maxed", known && skill.level > 0 && "has"].filter(Boolean).join(" ");
  return h("div", { class: cls, title: skill.path || skill.name },
    h("div", { class: "sk-top" },
      h("div", { class: "sk-name", text: skill.name }),
      h("div", { class: "sk-num" }, h("b", { text: known ? String(skill.level) : "?" }), h("small", { text: `/${skill.max ?? "?"}` }))),
    h("div", { class: "sk-bot" },
      pips(skill),
      h("div", { class: "sk-btns" },
        h("button", { class: "btn sm", title: "One point less", "aria-label": `${skill.name} down`, disabled: !known || skill.level <= 0 || null, onclick: () => setLevel(skill, skill.level - 1) }, icon("minus", 13)),
        h("button", { class: "btn sm", title: "One point more", "aria-label": `${skill.name} up`, disabled: !known || skill.level >= top || null, onclick: () => setLevel(skill, skill.level + 1) }, icon("plus", 13)),
        h("button", { class: "btn sm primary", title: "Set to its maximum", disabled: !known || maxed || null, onclick: () => setLevel(skill, top) }, "Max"))));
}

/** One branch's tiers, top to bottom. A skill whose tier is unknown goes in a single untitled group. */
function branchColumn(branch, skills) {
  const mine = skills.filter((s) => s.branch === branch.key);
  const rows = [...new Set(mine.map((s) => s.row))].sort((a, b) => (a ?? 1e9) - (b ?? 1e9));
  return h("section", { class: "sk-branch" },
    h("header", {}, h("h2", { text: branch.label }), h("div", { class: "sk-pts" }, h("b", { text: String(branch.points) }), h("small", { text: "pts" }))),
    rows.map((row) => {
      const group = mine.filter((s) => s.row === row);
      const locked = row != null && group.every((s) => s.locked);
      return h("div", { class: `sk-tier${locked ? " locked" : ""}` },
        row != null && h("div", { class: "sk-tier-head" },
          h("span", { text: `Tier ${row + 1}` }),
          h("i", { text: row === 0 ? "open" : `needs ${group[0].requires} pts above` }),
          locked && h("em", { text: "locked", title: "Locked in the game's menu. RTSE can still set these." })),
        group.map(tile));
    }));
}

function pointsBox(data) {
  const limit = data.limits.skill_points;
  const box = h("input", { type: "number", min: 0, max: limit, value: data.skill_points ?? 0, "aria-label": "Unspent skill points", onkeydown: (e) => e.key === "Enter" && commit() });
  const commit = () => setPoints(parseInt(box.value, 10));
  const nudge = (by) => () => { box.value = Math.max(0, Math.min(limit, (parseInt(box.value, 10) || 0) + by)); };
  return h("div", { class: "level" },
    h("span", { class: "big", text: "UNSPENT" }),
    h("div", { class: "stepper" },
      h("button", { title: "Down", "aria-label": "Unspent points down", onclick: nudge(-1) }, icon("minus", 15)),
      box,
      h("button", { title: "Up", "aria-label": "Unspent points up", onclick: nudge(1) }, icon("plus", 15))),
    h("button", { class: "btn primary", onclick: commit }, icon("check", 15), "Set"),
    h("button", { class: "btn ghost", title: "Re-read from the game", "aria-label": "Refresh", onclick: () => ctx.guarded(load) }, icon("refresh", 15)));
}

function resetBar(data) {
  const any = data.skills.some((s) => s.level);
  const ask = h("span", { class: "sk-confirm" },
    h("b", { text: "Really reset?" }),
    h("span", { text: `All ${data.spent} spent points go back to your unspent total.` }),
    h("button", { class: "btn sm danger", onclick: resetTree }, "Yes"),
    h("button", { class: "btn sm", onclick: () => { state.confirmReset = false; ctx.rerender(); } }, "Cancel"));
  return h("div", { class: "sk-bar" },
    h("span", { class: "note", text: "Setting a skill ignores tier locks and does not spend skill points. Save in game afterwards to keep changes." }),
    state.confirmReset
      ? ask
      : h("button", { class: "btn danger", disabled: !any || null, title: "Zero every skill and refund the points", onclick: () => { state.confirmReset = true; ctx.rerender(); } }, icon("trash", 15), "Reset tree"));
}

function devTools(data) {
  const rows = [
    ["Skill tree", data.sources.tree || "not found"], ["Skill list", data.sources.list || (data.sources.tree ? "(the tree itself)" : "not found")],
    ["Skill points", data.sources.skill_points || "not found"], ["Class", data.class_key || "unknown"],
  ];
  return ctx.dropdown("skillsinfo", "Skills dev tools", "", h("div", {},
    h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: String(v) })])),
    h("div", { class: "tools" }, h("button", { class: "btn sm", onclick: () => ctx.dump("/api/debug/skills") }, "Save skills dump"))));
}

export function render(root, passed) {
  ctx = passed;
  const data = state.data;
  if (!data) {
    fill(root, h("div", { class: "empty" }, h("div", {}, h("h2", { text: "Loading..." }))));
    return;
  }
  const ok = data.available;
  const maxed = data.skills.filter((s) => s.max != null && s.level >= s.max).length;
  fill(
    root,
    h("div", { class: "bench" },
      h("div", { class: "banner" },
        h("div", {},
          h("span", { class: "stamp equipped", text: "Your skills" }),
          h("h1", { text: "Skill tree" }),
          h("div", { class: "facts" },
            ok && h("span", {}, h("b", { text: String(data.spent) }), "points spent"),
            ok && h("span", {}, h("b", { text: `${maxed}/${data.skills.length}` }), "skills maxed"),
            ok && h("span", { text: `${data.branches.length} branches` }),
            !ok && h("span", { text: "tree unavailable" }))),
        pointsBox(data)),
      h("div", { class: "bench-body" },
        !ok && h("div", { class: "warn-banner" }, icon("alert", 18), `${data.reason || "The skill tree can't be read"}. Save the skills dump below and send it over.`),
        ok && data.reason && h("div", { class: "warn-banner" }, icon("alert", 18), `${data.reason}.`),
        ok && resetBar(data),
        ok && h("div", { class: "sk-tree" }, data.branches.map((branch) => branchColumn(branch, data.skills))))),
    devTools(data),
  );
}
