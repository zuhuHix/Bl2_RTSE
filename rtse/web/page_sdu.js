// Save Deposit Upgrades page: backpack, bank and per-ammo-type capacity.
// Also exports ammoCapacityControls(), a small strip the Ammo page can embed in each ammo card.
// Backed by rtse/sdu.py; none of its game-side field names are confirmed in a running game.

const state = { data: null, error: null, loading: false, failed: false, busy: false };
let current = null; // the last ctx we were rendered with, for load() calls without one

// per-kind card styling: coin colour and two-letter glyph
const LOOK = {
  backpack: { color: "#5fa0d6", glyph: "BP" },
  bank: { color: "#ffd21f", glyph: "BK" },
  ammo_pistol: { color: "#ffd21f", glyph: "PI" },
  ammo_smg: { color: "#ff9f2b", glyph: "SM" },
  ammo_rifle: { color: "#67d983", glyph: "AR" },
  ammo_shotgun: { color: "#ff5a2c", glyph: "SG" },
  ammo_sniper: { color: "#5fa0d6", glyph: "SN" },
  ammo_launcher: { color: "#ff7ab8", glyph: "RL" },
  ammo_grenade: { color: "#b27cff", glyph: "GR" },
};
const FALLBACK_LOOK = { color: "#8b93a7", glyph: "?" };
const MAX_CAPACITY = 100000;

// ---------- data ----------

/** Fetches the SDU state, then asks the host to re-render. Safe to call before render(). */
export async function load(ctx = current) {
  if (!ctx) return;
  current = ctx;
  state.loading = true;
  try {
    state.data = await ctx.api("GET", "/api/sdu");
    state.error = null;
    state.failed = false;
  } catch (error) {
    state.data = null;
    state.error = error.message;
    state.failed = true;
  }
  state.loading = false;
  ctx.rerender();
}

const entryFor = (kind) => (state.data ? state.data.kinds.find((k) => k.key === kind) || null : null);

/** Which SDU entry an ammo pool from /api/ammo belongs to: by pool id first, else by its name. */
function entryForPool(pool) {
  if (!state.data) return null;
  const byId = state.data.kinds.find((k) => k.group === "ammo" && k.pool_id !== null && k.pool_id === pool.id);
  if (byId) return byId;
  const name = `${pool.key || ""} ${pool.label || ""}`.toLowerCase();
  const kind = /sniper/.test(name) ? "ammo_sniper" : /pistol|revolver/.test(name) ? "ammo_pistol" : /smg|submachine/.test(name) ? "ammo_smg"
    : /shotgun/.test(name) ? "ammo_shotgun" : /launcher|rocket/.test(name) ? "ammo_launcher" : /grenade/.test(name) ? "ammo_grenade"
    : /rifle|assault|combat|repeater/.test(name) ? "ammo_rifle" : null;
  return kind && entryFor(kind);
}

const unitOf = (entry, n) => `${n.toLocaleString()} ${n === 1 && entry.unit === "slots" ? "slot" : entry.unit}`;

/** Runs one write, stores the reply and reports what the game says now. Returns the reply or null. */
async function write(ctx, path, body, describe) {
  if (state.busy) return null;
  state.busy = true;
  const result = await ctx.guarded(() => ctx.api("POST", path, body));
  state.busy = false;
  if (!result) return null;
  state.data = result;
  state.error = null;
  const { applied } = result;
  const message = describe(applied);
  if (message.ok) ctx.toast(message.text);
  else ctx.toast(message.text, true);
  ctx.rerender();
  return result;
}

function setLevel(ctx, entry, level) {
  if (!Number.isInteger(level)) return ctx.toast("Enter a whole number", true);
  if (level < 0 || level > entry.max_level) return ctx.toast(`${entry.label}: level must be 0-${entry.max_level}`, true);
  return write(ctx, "/api/sdu/set", { kind: entry.key, level }, (a) => {
    const now = a.after.capacity;
    const text = `${entry.label}: SDU level ${a.before.level ?? "?"} to ${a.requested}`;
    // the game must report both the level we asked for and the capacity that level should give
    if (a.after.level === a.requested && now === a.expected_capacity) return { ok: true, text: `${text} (${unitOf(entry, now)})` };
    return { ok: false, text: `${entry.label}: asked for level ${a.requested} (${unitOf(entry, a.expected_capacity)}) but the game reports level ${a.after.level ?? "nothing"}, ${now == null ? "no capacity" : unitOf(entry, now)}` };
  });
}

function setCapacity(ctx, entry, value) {
  if (!Number.isInteger(value)) return ctx.toast("Enter a whole number", true);
  if (value < 1 || value > MAX_CAPACITY) return ctx.toast(`Capacity must be 1-${MAX_CAPACITY.toLocaleString()}`, true);
  const body = entry.pool_id !== null ? { id: entry.pool_id, max: value } : { kind: entry.key, max: value };
  return write(ctx, "/api/sdu/ammo_max", body, (a) => {
    if (a.after.capacity === a.requested) return { ok: true, text: `${entry.label}: capacity ${(a.before.capacity ?? 0).toLocaleString()} to ${a.requested.toLocaleString()}` };
    return { ok: false, text: `${entry.label}: asked for capacity ${a.requested.toLocaleString()} but the game reports ${a.after.capacity == null ? "nothing" : a.after.capacity.toLocaleString()} (it may recompute from your upgrades)` };
  });
}

// ---------- shared controls ----------

/** A "-  [n]  +" level stepper plus Set; the number is clamped to the entry's levels. */
function levelStepper(ctx, entry) {
  const { h, icon } = ctx;
  const box = h("input", { class: "field", type: "number", min: 0, max: entry.max_level, value: entry.level ?? 0, "aria-label": `${entry.label} SDU level`, onkeydown: (e) => e.key === "Enter" && commit() });
  const commit = () => setLevel(ctx, entry, parseInt(box.value, 10));
  const nudge = (by) => () => { box.value = Math.max(0, Math.min(entry.max_level, (parseInt(box.value, 10) || 0) + by)); };
  return [
    h("div", { class: "stepper sdu-step" },
      h("button", { title: "Down", "aria-label": `${entry.label} level down`, onclick: nudge(-1) }, icon("minus", 14)),
      box,
      h("button", { title: "Up", "aria-label": `${entry.label} level up`, onclick: nudge(1) }, icon("plus", 14))),
    h("button", { class: "btn sm primary", onclick: commit }, "Set"),
    entry.level !== entry.max_level && h("button", { class: "btn sm", onclick: () => setLevel(ctx, entry, entry.max_level) }, "Max"),
  ];
}

/** "Capacity [n] Set": writes the pool's maximum directly, whatever the level table says. */
function capacityEditor(ctx, entry, label = "Capacity") {
  const { h } = ctx;
  const box = h("input", { class: "field", type: "number", min: 1, max: MAX_CAPACITY, value: entry.capacity ?? "", "aria-label": `${entry.label} capacity`, onkeydown: (e) => e.key === "Enter" && commit() });
  const commit = () => setCapacity(ctx, entry, parseInt(box.value, 10));
  return h("div", { class: "sdu-cap" }, h("span", { class: "sdu-lbl", text: label }), box, h("button", { class: "btn sm primary", onclick: commit }, "Set"));
}

/**
 * A compact strip for the Ammo page's cards: "Capacity [n] Set  |  SDU lvl - 4/7 +".
 * `pool` is an entry from /api/ammo; `ctx.rerender` is called after any change (pass a function that
 * reloads the ammo page's data). Loads the SDU data itself the first time it is needed.
 */
export function ammoCapacityControls(pool, ctx) {
  const { h, icon } = ctx;
  current = current || ctx;
  if (!state.data && !state.loading && !state.failed) load(ctx);
  const entry = entryForPool(pool);
  const strip = h("div", { class: "sdu-strip" });
  if (!entry || entry.capacity === null) {
    strip.append(h("span", { class: "sdu-lbl", text: state.failed ? "SDU data unavailable" : entry ? "Capacity unavailable" : "Loading SDUs..." }));
    return strip;
  }
  const level = entry.level;
  const step = (by) => () => setLevel(ctx, entry, Math.max(0, Math.min(entry.max_level, (level ?? 0) + by)));
  strip.append(
    capacityEditor(ctx, entry),
    h("div", { class: "sdu-lvl", title: entry.level_inferred ? "Guessed from the capacity: the game's own level field wasn't found" : "Save Deposit Upgrade level" },
      h("span", { class: "sdu-lbl", text: `SDU lvl${entry.level_inferred ? " ~" : ""}` }),
      h("button", { class: "btn sm", "aria-label": `${entry.label} SDU level down`, disabled: !level || null, onclick: step(-1) }, icon("minus", 12)),
      h("b", { text: level == null ? "?" : `${level}/${entry.max_level}` }),
      h("button", { class: "btn sm", "aria-label": `${entry.label} SDU level up`, disabled: level === entry.max_level || null, onclick: step(1) }, icon("plus", 12))));
  return strip;
}

// ---------- cards ----------

/** One upgrade: capacity big, level pips (click one to jump to that level), then the controls. */
function sduCard(ctx, entry) {
  const { h } = ctx;
  const look = LOOK[entry.key] || FALLBACK_LOOK;
  const available = entry.level !== null || entry.capacity !== null;
  const level = entry.level;
  const drift = entry.expected !== null && entry.capacity !== null && entry.expected !== entry.capacity;
  const pips = Array.from({ length: entry.max_level }, (_, i) => h("button", {
    class: `sdu-pip${level !== null && i < level ? " on" : ""}`,
    title: `Level ${i + 1}: ${unitOf(entry, entry.table[i + 1])}`,
    "aria-label": `${entry.label} level ${i + 1}`,
    onclick: () => setLevel(ctx, entry, i + 1),
  }));
  const src = entry.sources;
  return h(
    "div",
    { class: `cc sdu-card${available ? "" : " off"}`, vars: { "--c": look.color } },
    h("div", { class: "cc-head" },
      h("span", { class: "coin", text: look.glyph }),
      h("div", {},
        h("div", { class: "k", text: entry.label }),
        h("div", { class: "sub", text: level === null ? "Level unavailable" : `SDU level ${level} of ${entry.max_level}${entry.level_inferred ? " (guessed)" : ""}` }))),
    h("div", { class: "cc-val", text: entry.capacity === null ? "unavailable" : entry.capacity.toLocaleString() },
      entry.capacity !== null && h("small", { text: entry.unit })),
    available && level !== null && h("div", { class: "sdu-pips" }, pips),
    drift && h("div", { class: "sdu-drift", text: `Level ${level} should give ${unitOf(entry, entry.expected)} (from the game's base and per-upgrade numbers)` }),
    available && h("div", { class: "cc-ctrl" }, levelStepper(ctx, entry)),
    available && entry.group === "ammo" && capacityEditor(ctx, entry),
    h("div", { class: "sdu-src", text: available ? `level: ${src.level || "none"} / capacity: ${src.capacity || "none"}` : "No field found. Save the SDU dump below and send it over." }),
  );
}

async function maxEverything(ctx) {
  const todo = state.data.kinds.filter((k) => k.level !== null && k.level < k.max_level);
  let done = 0;
  for (const entry of todo) {
    const result = await ctx.guarded(() => ctx.api("POST", "/api/sdu/set", { kind: entry.key, level: entry.max_level }));
    if (!result) break;
    state.data = result;
    done += 1;
  }
  ctx.toast(`Maxed ${done} of ${todo.length} upgrades`, done < todo.length);
  ctx.rerender();
}

// ---------- page ----------

export function render(root, ctx) {
  const { h, fill, icon } = ctx;
  current = ctx;
  const d = state.data;
  if (!d && !state.error) {
    fill(root, h("div", { class: "empty" }, h("div", {}, h("h2", { text: "Loading..." }))));
    return;
  }
  const kinds = d ? d.kinds : [];
  const storage = kinds.filter((k) => k.group === "storage");
  const ammo = kinds.filter((k) => k.group === "ammo");
  const facts = storage.filter((k) => k.capacity !== null).map((k) => h("span", {}, `${k.label} `, h("b", { text: unitOf(k, k.capacity) })));
  const sources = d ? Object.entries(d.sources).flatMap(([kind, s]) => [h("dt", { text: kind }), h("dd", { text: `level: ${s.level || "-"} / capacity: ${s.capacity || "-"}` })]) : [];

  fill(
    root,
    h("div", { class: "bench" },
      h("div", { class: "banner" },
        h("div", {}, h("span", { class: "stamp equipped", text: "Save Deposit Upgrades" }), h("h1", { text: "Upgrades" }),
          h("div", { class: "facts" }, facts)),
        h("div", { class: "level" },
          h("button", { class: "btn primary", disabled: !d || !kinds.some((k) => k.level !== null && k.level < k.max_level) || null, onclick: () => maxEverything(ctx) }, icon("check", 15), "Max everything"),
          h("button", { class: "btn ghost", title: "Re-read from the game", "aria-label": "Refresh", onclick: () => load(ctx) }, icon("refresh", 15)))),
      h("div", { class: "bench-body" },
        h("div", { class: "warn-banner" }, icon("info", 18), "Changes apply to your live game. Save in game afterwards to keep them. The capacity tables use the base and per-upgrade numbers from the game's data; what the cards show is what the game reports."),
        state.error && h("div", { class: "warn-banner" }, icon("alert", 18), `${state.error}. Save the SDU dump below and send it over.`),
        d && d.warnings.map((w) => h("div", { class: "warn-banner" }, icon("alert", 18), `${w}. Save the SDU dump below and send it over.`)),
        d && h("div", { class: "sdu-body" },
          h("h4", { class: "sdu-h", text: "Storage" }),
          h("div", { class: "char-grid" }, storage.map((k) => sduCard(ctx, k))),
          h("h4", { class: "sdu-h", text: "Ammo capacity" }),
          h("div", { class: "char-grid" }, ammo.map((k) => sduCard(ctx, k)))))),
    ctx.dropdown("sduinfo", "SDU dev tools", "", h("div", {},
      d && h("dl", { class: "kv" }, sources),
      h("div", { class: "tools" }, h("button", { class: "btn sm", onclick: () => ctx.dump("/api/debug/sdu") }, "Save SDU dump")))),
  );
}
