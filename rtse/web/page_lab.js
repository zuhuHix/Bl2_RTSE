// Weapon lab page: A/B compare of one weapon (snapshots, live part swaps, stat diff) and measured damage from a
// recording session. Backed by rtse/lab.py (the lab section of the feature spec). The damage hooks are UNVERIFIED in a
// live game, and every damage number on this page says so. Imports only core.js, for the shared page state and the
// unverified badge.
import { state as shell, unverifiedBadge } from "./core.js";

const HIT_BUFFER = 600; // hits kept client-side for the chart; the tiles still cover every kept hit
const POLL_MS = 1000;
const WEAPON_CLASS = "WillowWeapon";

const st = {
  root: null, // the workspace element we last drew into
  loaded: false,
  items: [], itemsError: null,
  weaponId: null, // the picked weapon; its id can change after a part swap
  detail: null, detailError: null,
  lab: null, labError: null, // the last GET /api/lab reply (STATE)
  targets: [], // target list for the filter; kept while a filter is set
  compare: null, compareError: null,
  hits: [], session: null, lastSeq: -1, // chart buffer, the session it belongs to, the newest seq we hold
  target: "", onlyWeapon: false, cap: 2000,
  timer: null, polling: false, renderedActive: false,
};
let C = null; // the shell's pageCtx, set by load() and render()
let dom = {}; // the damage section's elements; paint() refreshes them every poll

// ---------- small helpers ----------

const num = (value, digits = 1) => (value === null || value === undefined ? "-" : Number(value).toLocaleString(undefined, { maximumFractionDigits: digits }));
const percent = (value) => (value === null || value === undefined ? "-" : `${(value * 100).toFixed(1)}%`);
const signed = (value) => (value === null || value === undefined ? "-" : `${value > 0 ? "+" : ""}${num(value, 3)}`);
const changePct = (value) => (value === null || value === undefined ? "-" : `${value > 0 ? "+" : ""}${value.toFixed(1)}%`);
const alive = () => shell.page === "lab" && Boolean(st.root && st.root.isConnected);
const labQuery = () => (st.target ? `?target=${encodeURIComponent(st.target)}` : "");
const niceMax = (value) => (value > 0 ? value * 1.08 : 1);

function estTag(title = "Estimated by the lab from game values, not read from the game") {
  return C.h("span", { class: "lab-est", title, text: "estimated" });
}

/** One API call that reports its failure as a value instead of throwing, for the loaders below. */
async function tryApi(method, path, body) {
  try {
    return { data: await C.api(method, path, body) };
  } catch (error) {
    return { error: error.message };
  }
}

// ---------- data ----------

async function loadItems() {
  const r = await tryApi("GET", "/api/items");
  if (r.error) {
    st.itemsError = r.error;
    return;
  }
  st.itemsError = null;
  st.items = r.data.items
    .filter((item) => item.class === WEAPON_CLASS)
    .sort((a, b) => Number(b.location === "Equipped") - Number(a.location === "Equipped"));
  if (!st.items.some((item) => item.id === st.weaponId)) st.weaponId = st.items[0]?.id ?? null;
}

async function loadDetail() {
  st.detail = null;
  st.detailError = null;
  if (!st.weaponId) return;
  const r = await tryApi("GET", `/api/item?id=${encodeURIComponent(st.weaponId)}`);
  if (r.error) st.detailError = r.error;
  else st.detail = r.data;
}

async function refreshCompare() {
  st.compare = null;
  st.compareError = null;
  if (!st.lab?.snapshots?.a) return; // nothing to compare until Snapshot A exists
  const r = await tryApi("GET", `/api/lab/compare${st.lab.snapshots.b ? "" : "?live=1"}`);
  if (r.error) st.compareError = r.error;
  else st.compare = r.data;
}

async function loadLab() {
  const r = await tryApi("GET", `/api/lab${labQuery()}`);
  if (r.error) {
    st.labError = r.error;
  } else {
    st.lab = r.data;
    st.labError = null;
    if (!st.target || !st.targets.length) st.targets = r.data.summary.targets || [];
  }
  await refreshCompare();
  if (st.weaponId && (!st.detail || st.detail.id !== st.weaponId)) await loadDetail();
}

/** Hits for the chart. Keeps a buffer of the newest HIT_BUFFER hits; the server's seq is the cursor. */
function absorbHits(reply) {
  if (st.session !== null && reply.session !== st.session) {
    // a new session started (or a reset): drop the buffer and read again from the start next time
    st.session = reply.session;
    st.hits = [];
    st.lastSeq = -1;
    return;
  }
  st.session = reply.session;
  if (reply.hits.length) {
    st.hits.push(...reply.hits);
    const extra = st.hits.length - HIT_BUFFER;
    if (extra > 0) st.hits.splice(0, extra);
    st.lastSeq = reply.hits[reply.hits.length - 1].seq;
  } else {
    st.lastSeq = Math.max(st.lastSeq, reply.last);
  }
}

// ---------- polling ----------

function startPolling() {
  if (st.timer) return;
  st.timer = setInterval(tick, POLL_MS);
  tick();
}

function stopPolling() {
  clearInterval(st.timer);
  st.timer = null;
}

/** One poll: the hits since the last seq, and the summary. Stops itself when the page is left or the session stops. */
async function tick() {
  if (!alive()) return stopPolling();
  if (st.polling) return;
  st.polling = true;
  try {
    const target = st.target ? `&target=${encodeURIComponent(st.target)}` : "";
    // first read of a session: only the newest buffer's worth, so a long session does not start with the oldest hits
    const after = st.lastSeq >= 0 ? st.lastSeq : Math.max(-1, (st.lab?.session?.recorded ?? 0) - HIT_BUFFER - 1);
    const [hits, lab] = await Promise.all([
      C.api("GET", `/api/lab/hits?after=${after}&limit=2000${target}`),
      C.api("GET", `/api/lab${labQuery()}`),
    ]);
    absorbHits(hits);
    st.lab = lab;
    st.labError = null;
    if (!st.target || !st.targets.length) st.targets = lab.summary.targets || [];
  } catch (error) {
    st.labError = error.message; // keep the last good numbers on screen and keep polling
  } finally {
    st.polling = false;
  }
  const active = Boolean(st.lab?.session?.active);
  if (st.lab && !active) stopPolling();
  if (active !== st.renderedActive) C.rerender();
  else paint();
}

// ---------- actions ----------

async function pickWeapon(id) {
  st.weaponId = id;
  st.detail = null;
  st.detailError = null;
  C.rerender();
  await loadDetail();
  C.rerender();
}

async function snapshot(slot) {
  if (!st.weaponId) return;
  const lab = await C.guarded(() => C.api("POST", "/api/lab/snapshot", { id: st.weaponId, slot }));
  if (!lab) return;
  st.lab = lab;
  st.labError = null;
  C.toast(`Snapshot ${slot.toUpperCase()} taken: ${st.detail?.name ?? st.weaponId}`);
  await refreshCompare();
  C.rerender();
}

async function clearSnapshots() {
  const lab = await C.guarded(() => C.api("POST", "/api/lab/snapshot/clear", {}));
  if (!lab) return;
  st.lab = lab;
  st.compare = null;
  C.toast("Snapshots cleared");
  C.rerender();
}

/** A part swap on the game (the existing gear route). The reply carries the item's id, which can change. */
async function setPart(slot, partId) {
  const reply = await C.guarded(() => C.api("POST", "/api/item/set_part", { id: st.weaponId, slot, part: partId || null }));
  if (!reply) return C.rerender(); // redraw so the select shows the part that is really on
  C.toast(`${slot}: ${partId ? "part changed" : "part removed"}`);
  st.weaponId = reply.id || st.weaponId;
  await loadItems();
  await loadDetail();
  await refreshCompare();
  C.rerender();
}

async function startSession() {
  const cap = Number(st.cap);
  if (!Number.isInteger(cap) || cap < 10 || cap > 20000) return C.toast("Cap must be a whole number from 10 to 20000", true);
  const lab = await C.guarded(() => C.api("POST", "/api/lab/session/start", { cap, weapon: st.onlyWeapon ? st.weaponId : null }));
  if (!lab) return;
  st.lab = lab;
  st.session = null;
  st.hits = [];
  st.lastSeq = -1;
  C.toast(`Recording, up to ${num(cap, 0)} hits kept`);
  C.rerender();
  startPolling();
}

async function stopSession() {
  const lab = await C.guarded(() => C.api("POST", "/api/lab/session/stop", {}));
  if (!lab) return;
  st.lab = lab;
  C.toast("Recording stopped");
  await tick(); // one last read of the hits; the poll then stops
}

async function resetSession() {
  const lab = await C.guarded(() => C.api("POST", "/api/lab/session/reset", {}));
  if (!lab) return;
  st.lab = lab;
  st.session = null;
  st.hits = [];
  st.lastSeq = -1;
  C.toast("Session cleared");
  paint();
}

function onTarget(event) {
  st.target = event.target.value;
  st.hits = [];
  st.lastSeq = -1;
  tick();
}

// ---------- module interface ----------

/** Fetches the lab state, the weapon list and the picked weapon, then redraws. Resumes polling if a session runs. */
export async function load(ctx) {
  C = ctx;
  await loadItems();
  await loadLab();
  st.loaded = true;
  ctx.rerender();
  if (st.lab?.session?.active) startPolling();
  else if (st.lab?.session?.recorded) await tick(); // show the last session's hits once
}

/** The sidebar card: says "recording" while a session runs. */
export function pin(ctx) {
  const recording = Boolean(st.lab?.session?.active);
  return { icon: ctx.icon("crosshair", 20), sub: recording ? "Recording damage..." : "A/B parts, measured damage" };
}

export function render(root, ctx) {
  C = ctx;
  st.root = root;
  const { h, fill } = ctx;
  if (!st.loaded) {
    fill(root, h("div", { class: "empty" }, h("div", {}, h("h2", { text: "Loading..." }))));
    return;
  }
  const active = Boolean(st.lab?.session?.active);
  st.renderedActive = active;
  const compareBlock = compareSection();
  const damageBlock = damageSection();
  fill(root,
    h("div", { class: "bench" },
      h("div", { class: "banner" },
        h("div", {},
          h("span", { class: "stamp equipped", text: "Weapon lab" }),
          h("h1", { text: "Weapon lab" }),
          h("div", { class: "facts" },
            h("span", {}, "Weapons ", h("b", { text: st.items.length })),
            h("span", {}, "Snapshot A ", h("b", { text: st.lab?.snapshots?.a?.name ?? "none" })),
            h("span", {}, "Snapshot B ", h("b", { text: st.lab?.snapshots?.b?.name ?? "none" })),
            h("span", {}, "Session ", h("b", { text: active ? "recording" : st.lab?.session?.recorded ? "stopped" : "idle" }))))),
      h("div", { class: "lab-body" }, compareBlock, damageBlock)));
  paint();
}

// ---------- A/B compare ----------

function snapChip(slot, snap) {
  const { h } = C;
  return h("div", { class: `lab-snap ${slot}` },
    h("b", { text: `Snapshot ${slot.toUpperCase()}` }),
    snap
      ? [h("span", { text: snap.name }), h("small", { text: `taken ${new Date(snap.taken_at * 1000).toLocaleTimeString()}` })]
      : h("small", { text: "not taken" }));
}

function partStrip() {
  const { h } = C;
  if (!st.detail) {
    return h("div", { class: "lab-note", text: st.detailError ? `Could not read the weapon: ${st.detailError}` : st.weaponId ? "Loading the weapon..." : "Pick a weapon to see and swap its parts." });
  }
  if (!st.detail.slots?.length) return h("div", { class: "lab-note", text: "This weapon has no swappable parts." });
  return h("div", { class: "lab-strip" }, st.detail.slots.map((slot) => h("label", { class: "lab-slot" },
    h("span", { text: slot.slot }),
    h("select", { class: "field lab-sel", "aria-label": `${slot.slot} part`, onchange: (event) => setPart(slot.slot, event.target.value) },
      h("option", { value: "", text: "(none)", selected: !slot.current || null }),
      slot.options.map((option) => h("option", { value: option.id, text: option.name, selected: slot.current?.id === option.id || null }))))));
}

function partDiffs() {
  const { h } = C;
  if (!st.compare) return null;
  const changed = st.compare.parts.filter((part) => part.changed);
  return h("div", { class: "lab-parts" },
    h("h4", { class: "lab-sub", text: "Parts" }),
    changed.length
      ? changed.map((part) => h("div", { class: "lab-part-row" },
        h("span", { text: part.slot }), h("b", { text: part.a ?? "none" }), h("i", { class: "lab-to", text: "to" }), h("b", { text: part.b ?? "none" })))
      : h("div", { class: "lab-note", text: "No part differences between A and B." }));
}

function compareTable() {
  const { h } = C;
  if (!st.compare) {
    const text = st.compareError ? `Could not compare: ${st.compareError}` : st.lab?.snapshots?.a ? "Loading..." : "Take Snapshot A to start a comparison.";
    return h("div", { class: "lab-note", text });
  }
  const { compare } = st;
  return h("div", { class: "lab-tablewrap" },
    h("table", { class: "lab-table" },
      h("thead", {}, h("tr", {},
        h("th", { text: "Stat" }), h("th", { text: "A" }), h("th", { text: compare.b_live ? "B (live)" : "B" }),
        h("th", { text: "Delta" }), h("th", { text: "Change" }))),
      h("tbody", {}, compare.rows.map((row) => h("tr", { class: row.changed ? "chg" : null },
        h("td", {}, row.label, row.estimated && estTag()),
        h("td", { text: num(row.a, 3) }), h("td", { text: num(row.b, 3) }),
        h("td", { class: "sign", text: signed(row.delta) }), h("td", { text: changePct(row.pct) }))))));
}

function compareSection() {
  const { h, icon } = C;
  const snaps = st.lab?.snapshots || { a: null, b: null };
  const weapons = h("select", {
    class: "field lab-sel", "aria-label": "Weapon", disabled: !st.items.length || null,
    onchange: (event) => pickWeapon(event.target.value),
  }, st.items.map((item) => h("option", { value: item.id, text: `${item.name} (${item.location}, lvl ${item.level})`, selected: item.id === st.weaponId || null })));
  return h("section", { class: "lab-sec" },
    h("h2", { class: "lab-h", text: "A/B compare" }),
    st.itemsError && h("div", { class: "warn-banner" }, icon("alert", 18), `Could not list weapons: ${st.itemsError}`),
    h("p", { class: "lab-lede", text: "Take Snapshot A, swap parts (or pick another weapon), take Snapshot B. Without B, the table compares A with the weapon as it is now." }),
    h("div", { class: "lab-pick" },
      h("label", {}, "Weapon", weapons),
      h("button", { class: "btn sm primary", disabled: !st.weaponId || null, onclick: () => snapshot("a") }, "Snapshot A"),
      h("button", { class: "btn sm primary", disabled: !st.weaponId || null, onclick: () => snapshot("b") }, "Snapshot B"),
      h("button", { class: "btn sm ghost", disabled: (!snaps.a && !snaps.b) || null, onclick: clearSnapshots }, "Clear")),
    h("div", { class: "lab-snaps" }, snapChip("a", snaps.a), snapChip("b", snaps.b)),
    h("div", { class: "lab-sub", text: "Part swaps (these change the weapon in game)" }),
    partStrip(),
    partDiffs(),
    compareTable());
}

// ---------- measured damage ----------

function damageSection() {
  const { h, icon } = C;
  const active = Boolean(st.lab?.session?.active);
  dom = {
    status: h("span", { class: "lab-status" }),
    target: h("select", { class: "field lab-sel", "aria-label": "Target", onchange: onTarget }),
    banner: h("div"),
    note: h("div"),
    tiles: h("div", { class: "lab-tiles" }),
    chart: h("div", { class: "lab-chartbox" }),
  };
  return h("section", { class: "lab-sec" },
    h("h2", { class: "lab-h", text: "Measured damage" }),
    h("div", { class: "lab-session" },
      h("label", {}, "Cap",
        h("input", { class: "field lab-cap", type: "number", min: 10, max: 20000, step: 1, value: st.cap, "aria-label": "Hit cap", oninput: (event) => { st.cap = event.target.value; } })),
      h("label", { class: "lab-check" },
        h("input", { type: "checkbox", checked: st.onlyWeapon || null, disabled: !st.weaponId || null, onchange: (event) => { st.onlyWeapon = event.target.checked; } }),
        "Only this weapon"),
      h("button", { class: "btn sm primary", disabled: active || null, onclick: startSession }, icon("check", 14), "Start"),
      h("button", { class: "btn sm", disabled: !active || null, onclick: stopSession }, "Stop"),
      h("button", { class: "btn sm ghost", onclick: resetSession }, "Reset"),
      dom.status),
    h("div", { class: "lab-target" }, h("label", {}, "Target", dom.target)),
    dom.banner,
    dom.note,
    dom.tiles,
    dom.chart,
    h("div", { class: "lab-foot" },
      h("button", { class: "btn sm ghost", title: "Writes the raw lab state to rtse/debug_lab.json", onclick: () => C.dump("/api/debug/lab") }, "Save lab dump")));
}

/** Everything under the damage controls: status, banner, notes, tiles and chart. Runs on every poll. */
function paint() {
  if (!dom.tiles) return;
  paintStatus();
  paintTargets();
  paintBanner();
  paintNote();
  paintTiles();
  paintChart();
}

function paintStatus() {
  const s = st.lab?.session;
  if (!s) return;
  const head = s.active ? `Recording ${num(s.elapsed)} s` : s.recorded ? `Stopped at ${num(s.elapsed)} s` : "Idle";
  const seen = s.recorded ? `${num(s.recorded, 0)} hits seen, ${num(s.kept, 0)} kept${s.dropped ? `, ${num(s.dropped, 0)} dropped` : ""}, ${num(s.shots, 0)} shots` : "";
  const failed = st.labError ? `last read failed: ${st.labError}` : "";
  dom.status.textContent = [head, seen, failed].filter(Boolean).join(" - ");
  dom.status.classList.toggle("rec", Boolean(s.active));
}

/** The target filter. Rebuilt only when the list of targets changes, so an open dropdown survives polls. */
function paintTargets() {
  const { h } = C;
  const list = [["", "All targets"], ...st.targets.map((target) => [target.target_id, target.target])];
  const key = list.map(([value]) => value).join("|");
  if (dom.target.dataset.key !== key) {
    dom.target.dataset.key = key;
    C.fill(dom.target, list.map(([value, label]) => h("option", { value, text: label })));
  }
  dom.target.value = st.target;
}

function paintBanner() {
  const { h, icon } = C;
  C.fill(dom.banner, st.lab?.session?.via === "take_damage"
    ? h("div", { class: "warn-banner" }, icon("alert", 18), "Only the fallback hook (TakeDamage) is reporting: every damage number below is a pre-mitigation estimate.")
    : null);
}

function paintNote() {
  const { h, icon } = C;
  const u = st.lab?.unverified || {};
  const hooks = st.lab?.hooks;
  const fired = hooks ? Object.entries(hooks.fired || {}).map(([path, count]) => `${path.split(":").pop()} ${num(count, 0)}`).join(", ") : "";
  C.fill(dom.note,
    h("div", { class: "warn-banner" }, icon("alert", 18), "UNVERIFIED: the damage hooks have not been seen working in a live game. Treat these numbers as a dry run.", unverifiedBadge(u.hooks)),
    h("ul", { class: "lab-unv" }, ["shield", "dps_clock", "pellets"].filter((key) => u[key]).map((key) => h("li", {}, u[key]))),
    hooks ? h("div", { class: "lab-note", text: `Hooks: ${(hooks.installed || []).length} installed, ${Object.keys(hooks.failed || {}).length} failed; fired ${fired}; ${hooks.errors} errors${hooks.last_error ? ` (last: ${hooks.last_error})` : ""}` }) : null);
}

function paintTiles() {
  const { h, fill } = C;
  const s = st.lab?.summary;
  if (!s) return fill(dom.tiles, h("div", { class: "lab-note", text: st.labError ? `Could not read the lab: ${st.labError}` : "Loading..." }));
  const estimated = new Set(s.estimated || []);
  const flagged = (fields) => fields.some((field) => estimated.has(field));
  const u = st.lab.unverified || {};
  const tile = (label, value, sub, { fields = [], unv = null, wide = false } = {}) => h("div", { class: "lab-tile" },
    h("span", { class: "lab-tile-l" }, label, flagged(fields) && estTag(), unv && unverifiedBadge(unv)),
    h("b", { class: wide ? "wide" : null, text: value }),
    h("small", { text: sub || "" }));
  fill(dom.tiles,
    tile("Hits", num(s.hits, 0), `of ${num(st.lab.session.recorded, 0)} seen`),
    tile("Crits", num(s.crits, 0), `crit rate ${percent(s.crit_rate)}`, { fields: ["crits", "crit_rate"] }),
    tile("Total damage", num(s.total, 0), `shield absorbed ${num(s.shield, 0)}`, { fields: ["total"] }),
    tile("DPS", num(s.dps), s.duration == null ? "needs hits over 0.5 s" : `over ${num(s.duration)} s`, { fields: ["dps"], unv: u.dps_clock }),
    tile("Avg hit", num(s.avg_hit), s.min_hit == null ? "" : `min ${num(s.min_hit)}`, { fields: ["avg_hit"] }),
    tile("Max hit", num(s.max_hit), "", { fields: ["max_hit"] }),
    tile("Avg crit / non-crit", `${num(s.avg_crit)} / ${num(s.avg_noncrit)}`, "", { fields: ["avg_crit", "avg_noncrit"], wide: true }),
    tile("Crit multiplier", s.crit_multiplier == null ? "-" : `${num(s.crit_multiplier, 2)}x`, "avg crit / avg non-crit", { fields: ["crit_multiplier"] }),
    tile("Shots", num(s.shots, 0), `${num(s.pellets_fired, 0)} pellets fired`),
    tile("Pellets / shot", num(s.pellets_per_shot, 0), "", { unv: u.pellets }),
    tile("Per pellet", num(s.per_pellet), s.per_pellet == null && s.shots ? "all targets only" : "total / pellets fired", { fields: ["per_pellet"] }),
    tile("Hits / shot", num(s.hits_per_shot, 2), s.hits_per_shot == null && s.shots ? "all targets only" : "misses lower it", { fields: ["hits_per_shot"] }),
    tile("Kills", num(s.kills, 0), "killing blows", { fields: ["kills"] }));
}

/** Plain SVG, built by hand. Top panel: damage per hit (crits red). Bottom: damage in the trailing 1 s. */
function chartSvg(hits) {
  const W = 900, H = 318, L = 62, R = 16;
  const top = [26, 150], bottom = [204, 280]; // [top y, baseline y] of each panel
  const t0 = hits[0].t, t1 = Math.max(hits[hits.length - 1].t, t0 + 1);
  const x = (t) => (L + ((t - t0) / (t1 - t0)) * (W - L - R)).toFixed(1);
  // damage in the one-second window ending at each hit (two pointers; hits are in time order)
  const windowDamage = [];
  let lo = 0;
  let sum = 0;
  hits.forEach((hit) => {
    sum += hit.amount;
    while (hits[lo].t <= hit.t - 1) {
      sum -= hits[lo].amount;
      lo += 1;
    }
    windowDamage.push(Math.max(0, sum));
  });
  const hitMax = niceMax(Math.max(...hits.map((hit) => hit.amount)));
  const dpsMax = niceMax(Math.max(...windowDamage));
  const yHit = (v) => (top[1] - (v / hitMax) * (top[1] - top[0])).toFixed(1);
  const yDps = (v) => (bottom[1] - (v / dpsMax) * (bottom[1] - bottom[0])).toFixed(1);
  const axis = (max, [y0, y1]) => [0, 0.5, 1].map((f) => {
    const y = (y1 - f * (y1 - y0)).toFixed(1);
    return `<line class="grid" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/><text x="${L - 8}" y="${y}" text-anchor="end" dominant-baseline="middle">${Math.round(max * f)}</text>`;
  }).join("");
  const xTicks = [0, 1, 2, 3, 4].map((k) => {
    const t = t0 + ((t1 - t0) * k) / 4;
    return `<text x="${x(t)}" y="${H - 6}" text-anchor="middle">${t.toFixed(1)} s</text>`;
  }).join("");
  const dots = hits.map((hit) => (hit.critical
    ? `<circle class="lab-crit" cx="${x(hit.t)}" cy="${yHit(hit.amount)}" r="3.6"/>`
    : `<circle class="lab-dot" cx="${x(hit.t)}" cy="${yHit(hit.amount)}" r="2.4"/>`)).join("");
  const line = hits.map((hit, i) => `${x(hit.t)},${yDps(windowDamage[i])}`).join(" ");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Damage per hit and damage in the last second, over the session">`
    + `${axis(hitMax, top)}${axis(dpsMax, bottom)}`
    + `<text x="${L}" y="14" class="lab-axis">DAMAGE PER HIT (crits in red)</text>`
    + `<text x="${L}" y="${bottom[0] - 10}" class="lab-axis">DAMAGE IN THE LAST 1 S</text>`
    + `${dots}<polyline class="lab-dps" points="${line}"/>${xTicks}</svg>`;
}

function paintChart() {
  const { h, fill } = C;
  const active = Boolean(st.lab?.session?.active);
  if (!st.hits.length) {
    const text = active ? "Recording, no hits yet. Fire the weapon in game."
      : st.lab?.session?.recorded ? "Nothing to chart for this filter."
        : "No hits yet. Pick a weapon, press Start, then fire it in game.";
    return fill(dom.chart, h("div", { class: "lab-empty", text }));
  }
  const legend = h("div", { class: "lab-legend" },
    h("span", {}, h("i", { class: "k-dot" }), "hit"),
    h("span", {}, h("i", { class: "k-crit" }), "crit"),
    h("span", {}, h("i", { class: "k-dps" }), "damage in the last 1 s"));
  fill(dom.chart,
    h("div", { class: "lab-svg", html: chartSvg(st.hits) }),
    legend,
    h("div", { class: "lab-note", text: `The last ${st.hits.length} hits${st.target ? " for this target" : ""}. The tiles above cover every kept hit.` }));
}
