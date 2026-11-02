// Farm guard page: guards a farm's mission (an alert when the game changes it, undo, snapshot) and counts the
// attempts and drops of a farm session. A plain ES module: app.js calls load() each time the page opens and
// render() on every redraw. While this page is open GET /api/farm is polled, so new alerts appear without a reload.
// Nothing here has been checked in a live game yet.
import { shell, state } from "./core.js";

const POLL_MS = 2000;
const TICK_MS = 1000; // the one interval: the clock ticks every second, the state is read every POLL_MS
const RARITY = { 1: ["White", "#e8e4d8"], 2: ["Green", "#67d983"], 3: ["Blue", "#5fa0d6"], 4: ["Purple", "#b87cff"], 5: ["Orange", "#ff8a2c"] };
const UNKNOWN_RARITY = ["Unknown", "#6b6657"];
const PT_NAMES = ["Normal", "TVHM", "UVHM"];
const STATUS_TEXT = { active: "Active", complete: "Complete", not_started: "Not started", ready: "Ready to turn in", objectives_done: "Ready to turn in", failed: "Failed", unknown: "Unknown" };
const KIND_TEXT = { weapon: "Weapon", item: "Item", mission_item: "Mission item", ammo: "Ammo", money: "Money", usable: "Usable" };

const S = {
  data: null, // the last reply of GET /api/farm, or of a write (without its "applied")
  error: null, // the last poll's error (shown as a banner)
  print: "", // what was last drawn from: a redraw happens only when this changes (the clock is left out)
  profile: null, // the farm the tracker is set to (not necessarily the one it is counting)
  clock: null, // { base, at }: the session's elapsed seconds when read, and the local time then
  confirm: null, // key of the risky button that is asking "Really?"
  pending: false, // a write is in flight: the write buttons are off
  fetching: false,
  timer: null,
  ticks: 0,
};
let C = null; // ctx from the latest load() or render()

// ---------- small helpers ----------

const statusText = (status) => STATUS_TEXT[status] || (status ? String(status).replace(/_/g, " ") : "-");
const progressText = (progress) => (Array.isArray(progress) ? progress.join(" / ") : progress == null ? "-" : String(progress));
const pad = (n) => String(n).padStart(2, "0");
const clock = (seconds) => {
  const total = Math.floor(seconds);
  return `${Math.floor(total / 3600)}:${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)}`;
};
const rarityOf = (rarity) => RARITY[rarity] || UNKNOWN_RARITY;
const playthroughText = (n) => (n ? `playthrough ${n}${PT_NAMES[n - 1] ? ` (${PT_NAMES[n - 1]})` : ""}` : null);

/** "3 min ago" from an epoch-seconds time the backend gives. */
function ago(epochSeconds) {
  if (!epochSeconds) return "";
  const seconds = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(epochSeconds * 1000).toLocaleString();
}

const nameOf = (id) => {
  const profile = S.data && S.data.profiles.find((p) => p.id === id);
  return profile ? profile.name : id;
};

/** What the page draws from: the reply minus its write receipt, and the clock (which moves on every read). */
function fingerprint(data) {
  return JSON.stringify({ ...data, applied: undefined, session: { ...data.session, elapsed: 0 } });
}

function elapsedNow() {
  const session = S.data.session;
  if (!S.clock) return session.elapsed;
  return session.running ? S.clock.base + (Date.now() - S.clock.at) / 1000 : S.clock.base;
}

function ensureProfile() {
  if (!S.profile || !S.data.profiles.some((p) => p.id === S.profile)) {
    S.profile = S.data.session.profile || (S.data.profiles[0] && S.data.profiles[0].id) || null;
  }
}

/** Takes a reply as the page's state (the clock restarts from its elapsed time). */
function accept(data) {
  S.data = data;
  S.error = null;
  S.clock = { base: data.session.elapsed, at: Date.now() };
  ensureProfile();
}

/** Redraws the page while it is open, and the sidebar card (its badge counts open alerts) always. */
function redraw() {
  if (!C) return;
  if (state.page === "farm") C.rerender();
  shell.renderPinned();
}

// ---------- loading and polling ----------

export async function load(ctx) {
  if (ctx) C = ctx;
  startTimer();
  await refresh();
}

/** Reads GET /api/farm. Redraws only when something other than the clock changed. */
async function refresh() {
  if (S.fetching || !C) return;
  S.fetching = true;
  try {
    const data = await C.api("GET", "/api/farm");
    const recovered = S.error !== null;
    const print = fingerprint(data);
    const changed = print !== S.print;
    accept(data);
    S.print = print;
    if (changed || recovered) redraw();
  } catch (error) {
    if (S.error !== error.message) {
      S.error = error.message;
      redraw();
    }
  } finally {
    S.fetching = false;
  }
}

/** One interval for the page: it stops itself once the user leaves the page, and load() starts it again. */
function startTimer() {
  if (S.timer) return;
  S.timer = setInterval(() => {
    if (state.page !== "farm") {
      clearInterval(S.timer);
      S.timer = null;
      return;
    }
    tickClock();
    S.ticks += 1;
    if (S.ticks % (POLL_MS / TICK_MS) === 0) refresh();
  }, TICK_MS);
}

function tickClock() {
  const el = document.querySelector("[data-farm-clock]");
  if (el && S.data) el.textContent = clock(elapsedNow());
}

/** Sends a change, takes the reply as the state, and says what happened. A failed one re-reads the state. */
async function write(path, body, say) {
  S.pending = true;
  S.confirm = null;
  redraw();
  const result = await C.guarded(() => C.api("POST", path, body));
  S.pending = false;
  if (result) {
    accept(result);
    S.print = fingerprint(result);
    if (say) say(result.applied);
    redraw();
  } else {
    await refresh();
    redraw();
  }
}

// ---------- actions ----------

const setEnabled = (profile, on) => write("/api/farm/enable", { profile: profile.id, enabled: on }, (a) => {
  C.toast(a.after ? `Guarding ${profile.name}${a.snapshot === "taken" ? ": snapshot taken" : ""}` : `Guard off: ${profile.name}`);
  if (a.warning) C.toast(a.warning, true);
});

const retakeSnapshot = (profile) => write("/api/farm/snapshot", { profile: profile.id }, (a) => {
  C.toast(`Snapshot retaken for ${profile.name}: ${statusText(a.after && a.after.status)}, progress ${progressText(a.after && a.after.progress)}`);
});

const setAutoBlock = (profile, on) => write("/api/farm/settings", { profile: profile.id, auto_block: on }, () => {
  C.toast(`Auto-block ${on ? "on" : "off"}: ${profile.name}`);
});

const setQuickReload = (on) => write("/api/farm/settings", { experimental_reload: on }, () => {
  C.toast(`Quick reload ${on ? "on" : "off"}`);
});

const resolve = (alert, action) => write("/api/farm/resolve", { alert: alert.id, action }, (a) => {
  const texts = {
    keep: "Kept: the snapshot is retaken from the new state",
    undo: `Put back the snapshot: ${alert.mission_name}`,
    undo_reload: "Put back the snapshot. Quick reload queued (the game saves and quits to the title screen)",
    dismiss: "Alert dismissed",
  };
  C.toast(texts[action] || action);
});

const sessionAction = (action, extra = {}) => write("/api/farm/session", { action, ...extra }, (a) => {
  const after = a.after || {};
  const texts = {
    start: `Counting ${nameOf(after.profile)}`,
    stop: `Stopped at ${clock(after.elapsed || 0)}`,
    reset: "Count reset",
    attempt: `Attempt ${after.attempts}`,
  };
  C.toast(texts[action] || "Done");
});

// ---------- small pieces ----------

function btn(label, onclick, { disabled = false, title = null, cls = "" } = {}) {
  return C.h("button", { class: `btn sm ${cls}`.trim(), disabled: disabled || null, title, onclick }, label);
}

/** A risky button: the first click turns it into an inline "Really?" with Yes and Cancel. */
function ask(key, label, question, run, { disabled = false, title = null } = {}) {
  const { h } = C;
  if (S.confirm === key) {
    return h("span", { class: "fg-ask" },
      h("b", { text: question }),
      h("button", { class: "btn sm yes", onclick: run }, "Yes"),
      h("button", { class: "btn sm", onclick: () => { S.confirm = null; redraw(); } }, "Cancel"));
  }
  return btn(label, () => { S.confirm = key; redraw(); }, { disabled, title });
}

/** A switch: a checkbox that reports its new value. */
function toggle(label, checked, disabled, onChange, title = null) {
  const { h } = C;
  return h("label", { class: `fg-switch${disabled ? " dis" : ""}`, title },
    h("input", { type: "checkbox", checked: checked || null, disabled: disabled || null, onchange: (event) => onChange(event.target.checked) }),
    h("span", { class: "fg-track-sw" }),
    h("span", { class: "fg-sw-text", text: label }));
}

const stat = (label, value) => C.h("div", { class: "fg-stat" }, C.h("span", { class: "fg-lbl", text: label }), C.h("b", { text: String(value) }));

// ---------- the page ----------

export function render(root, ctx) {
  C = ctx;
  const { h, fill, icon } = ctx;
  if (!S.data) {
    fill(root, S.error
      ? h("div", { class: "bench" }, banner(0, 0),
        h("div", { class: "fg-body" },
          h("div", { class: "warn-banner" }, icon("alert", 18), `Farm guard isn't reachable: ${S.error}.`),
          h("button", { class: "btn primary", onclick: () => refresh() }, icon("refresh", 15), "Try again")))
      : h("div", { class: "empty" }, h("div", {}, h("h2", { text: "Loading..." }))));
    return;
  }

  const d = S.data;
  const locked = !d.writes.allowed || S.pending;
  const guarded = d.profiles.filter((p) => p.enabled).length;
  fill(root,
    h("div", { class: "bench fg" },
      banner(guarded, d.alerts.length),
      h("div", { class: "fg-body" },
        !d.writes.allowed && h("div", { class: "warn-banner fg-stop" }, icon("alert", 18),
          `Farm guard is off here: ${d.writes.reason || "not in a save"}. Nothing on this page can be changed.`),
        h("div", { class: "warn-banner" }, icon("info", 18),
          "Nothing here has been checked in a live game yet. Alerts and drops come from the game's own hooks; what each step does to a farm comes from community notes."),
        alertsSection(d, locked),
        h("section", { class: "fg-section" },
          h("div", { class: "fg-h2row" }, h("h2", { class: "fg-h2", text: "Farms" }), h("span", { class: "fg-meta", text: "Guard a mission, then count the drops of the farm you run." })),
          h("div", { class: "fg-cards" }, d.profiles.map((p) => profileCard(p, d, locked)))),
        h("div", { class: "fg-lower" }, trackerPanel(d, locked), settingsPanel(d, locked)))),
    ctx.dropdown("farmtools", "Farm dev tools", "", devTools(d)));
}

function banner(guarded, open) {
  const { h } = C;
  return h("div", { class: "banner fg-banner" },
    h("div", {},
      h("h1", { text: "Farm guard" }),
      h("p", { class: "fg-sub", text: "Keeps a farm's mission from changing under you, and counts what drops while you farm." })),
    h("div", { class: "fg-chips" },
      h("span", { class: "fg-chip", text: `${guarded} guarded` }),
      h("span", { class: `fg-chip${open ? " hot" : ""}`, text: `${open} open ${open === 1 ? "alert" : "alerts"}` })));
}

function alertsSection(d, locked) {
  const { h } = C;
  const head = h("div", { class: "fg-h2row" }, h("h2", { class: "fg-h2", text: "Alerts" }),
    h("span", { class: "fg-meta", text: d.alerts.length ? `${d.alerts.length} open` : "none open" }));
  if (!d.alerts.length) {
    return h("section", { class: "fg-section" }, head,
      h("div", { class: "fg-empty", text: "Nothing has changed a guarded farm. An alert appears here the moment the game changes one." }));
  }
  return h("section", { class: "fg-section" }, head, h("div", { class: "fg-alerts" }, d.alerts.map((a) => alertCard(a, d, locked))));
}

function alertCard(a, d, locked) {
  const { h, icon } = C;
  const can = (action) => a.actions.includes(action);
  const exp = d.settings.experimental_reload;
  const statusChange = a.before && a.after && a.before.status !== a.after.status
    ? `${statusText(a.before.status)} → ${statusText(a.after.status)}` : null;
  const delta = a.blocked
    ? h("div", { class: "fg-delta blocked" }, icon("alert", 16), h("span", {}, "Blocked before it happened: the game never made this change, so there is nothing to undo."))
    : h("div", { class: "fg-delta" },
      h("span", { class: "fg-lbl", text: "Progress" }),
      h("b", { text: progressText(a.before && a.before.progress) }),
      h("span", { class: "fg-arrow", text: "→" }),
      h("b", { text: progressText(a.after && a.after.progress) }),
      statusChange && h("span", { class: "fg-meta", text: statusChange }));
  const buttons = [];
  if (can("keep")) buttons.push(btn("Keep", () => resolve(a, "keep"), { disabled: locked, title: "Accept the change: the snapshot is retaken from the new state" }));
  if (can("undo")) buttons.push(btn("Undo", () => resolve(a, "undo"), { disabled: locked, title: "Put the mission back to the snapshot" }));
  if (can("undo_reload")) {
    buttons.push(ask(`alert:${a.id}`, "Undo & quick reload", "Save, quit to the title screen and reload?", () => resolve(a, "undo_reload"), {
      disabled: locked || !exp,
      title: exp ? "Put the snapshot back, then save and reload the game" : "Experimental: turn on Quick reload in Settings first",
    }));
  }
  if (can("dismiss")) buttons.push(btn("Dismiss", () => resolve(a, "dismiss"), { disabled: locked, title: "Close this alert (there is nothing to undo)" }));
  const reloadOff = can("undo_reload") && !exp
    ? h("span", { class: "fg-meta", text: "Quick reload is off: turn it on in Settings to use Undo & quick reload." }) : null;

  return h("article", { class: `fg-alert${a.blocked ? " blocked" : ""}` },
    h("div", { class: "fg-alert-head" },
      h("div", {},
        h("h3", { class: "fg-alert-title", text: a.mission_name || a.mission }),
        h("div", { class: "fg-meta", text: [a.profile_name, a.objective_name && `objective: ${a.objective_name}`, a.item && `item: ${a.item}`].filter(Boolean).join(" · ") })),
      h("div", { class: "fg-alert-tags" },
        a.blocked && h("span", { class: "fg-blocked-tag", text: "Blocked" }),
        a.count > 1 && h("span", { class: "fg-count", text: `x ${a.count}` }),
        h("span", { class: "fg-meta", text: ago(a.at) }))),
    h("p", { class: "fg-msg", text: a.message }),
    delta,
    h("div", { class: "fg-alert-actions" }, buttons, reloadOff));
}

function profileCard(p, d, locked) {
  const { h } = C;
  const canBlock = d.hooks.can_block;
  const place = [p.area, p.dlc && p.dlc !== p.area ? `DLC: ${p.dlc}` : null, p.map].filter(Boolean).join(" · ");
  const blockHint = !canBlock
    ? "This SDK can't block game functions, so auto-block is unavailable."
    : p.can_block_pickups
      ? "Stops the change before it happens, pickups included. Nothing to undo. Needs the guard on."
      : "Stops the change before it happens. Nothing to undo. Needs the guard on.";
  return h("article", { class: `fg-card${p.enabled ? " on" : ""}` },
    h("header", { class: "fg-card-head" },
      h("div", { class: "fg-card-title" }, h("h3", { text: p.name }), place && h("div", { class: "fg-meta", text: place })),
      toggle(p.enabled ? "Guarding" : "Guard off", p.enabled, locked, (on) => setEnabled(p, on))),
    h("div", { class: "fg-mission" }, h("span", { class: "fg-lbl", text: "Mission" }), h("b", { text: p.mission_name || p.mission })),
    h("p", { class: "fg-desc", text: p.description }),
    p.consequence && h("p", { class: "fg-cons" }, h("span", { class: "fg-lbl", text: "If it changes" }), p.consequence),
    h("div", { class: "fg-two" }, snapshotBlock(p, locked), liveBlock(p)),
    h("div", { class: "fg-autoblock" },
      toggle("Auto-block (experimental)", p.auto_block, locked || !canBlock, (on) => setAutoBlock(p, on)),
      h("span", { class: "fg-meta", text: blockHint })),
    unverifiedBlock(p));
}

function snapshotBlock(p, locked) {
  const { h } = C;
  const snap = p.snapshot;
  const facts = snap ? [`taken ${ago(snap.taken_at)}`, snap.character, playthroughText(snap.playthrough)].filter(Boolean).join(" · ") : null;
  return h("div", { class: "fg-block" },
    h("span", { class: "fg-lbl", text: "Snapshot" }),
    snap
      ? [h("div", { class: "fg-val", text: `${statusText(snap.status)} · progress ${progressText(snap.progress)}` }), h("div", { class: "fg-meta", text: facts })]
      : h("div", { class: "fg-meta", text: p.enabled ? "No snapshot yet." : "Turn the guard on to snapshot this mission." }),
    btn("Retake snapshot", () => retakeSnapshot(p), {
      disabled: locked || !p.enabled,
      title: "Accept the mission's current state as the one to guard",
    }));
}

function liveBlock(p) {
  const { h } = C;
  let note = "Not in a save";
  if (p.found === false) note = "Mission isn't in this playthrough";
  return h("div", { class: "fg-block" },
    h("span", { class: "fg-lbl", text: "Live now" }),
    p.live
      ? h("div", { class: "fg-val", text: `${statusText(p.live.status)} · progress ${progressText(p.live.progress)}` })
      : h("div", { class: "fg-meta", text: note }),
    p.changed === true && h("span", { class: "fg-chg", text: "Changed since snapshot" }),
    p.changed === false && h("span", { class: "fg-ok", text: "Matches snapshot" }));
}

function unverifiedBlock(p) {
  const { h } = C;
  if (!p.unverified || !p.unverified.length) return null;
  return h("div", { class: "fg-unv" },
    h("div", { class: "fg-lbl", text: "Not checked in game yet" }),
    h("ul", {}, p.unverified.map((text) => h("li", { text }))));
}

function trackerPanel(d, locked) {
  const { h, icon } = C;
  const s = d.session;
  const counting = d.profiles.find((p) => p.id === s.profile);
  const rule = (counting && counting.drops) || { kinds: ["weapon", "item"], min_rarity: 3 };
  const ruleText = `Counts ${rule.kinds.map((k) => `${(KIND_TEXT[k] || k).toLowerCase()}s`).join(" and ")} of rarity ${rule.min_rarity} `
    + `(${rarityOf(rule.min_rarity)[0]}) and up. The rarity numbers aren't checked in game yet.`;
  const other = s.profile && s.profile !== S.profile;
  const shown = s.drops;
  return h("section", { class: "fg-panel" },
    h("div", { class: "fg-panel-head" }, h("h2", { text: "Farm tracker" }), h("span", { class: "fg-meta", text: s.running ? "Counting" : "Stopped" })),
    h("div", { class: "fg-track-top" },
      h("label", { class: "fg-field" },
        h("span", { class: "fg-lbl", text: "Farm" }),
        h("select", { class: "fg-select", disabled: S.pending || null, onchange: (event) => { S.profile = event.target.value; redraw(); } },
          d.profiles.map((p) => h("option", { value: p.id, selected: p.id === S.profile || null }, p.name)))),
      h("div", { class: "fg-stats" },
        stat("Attempts", s.attempts), stat("Drops", s.drop_count), stat("Pickups", s.pickups),
        h("div", { class: "fg-stat" }, h("span", { class: "fg-lbl", text: "Time" }), h("b", { class: "fg-clock", "data-farm-clock": true, text: clock(elapsedNow()) })))),
    h("div", { class: "fg-btns" },
      s.running
        ? btn("Stop", () => sessionAction("stop"), { disabled: locked })
        : btn("Start", () => sessionAction("start", { profile: S.profile }), { disabled: locked || !S.profile, cls: "primary", title: "Start counting this farm" }),
      btn("+1 attempt", () => sessionAction("attempt"), { disabled: locked, title: "Count a try by hand" }),
      ask("reset", "Reset", "Clear the count?", () => sessionAction("reset", { profile: S.profile }), { disabled: locked, title: "Clear attempts and drops" })),
    other && h("p", { class: "fg-warnline" }, icon("alert", 16),
      `The count is for ${nameOf(s.profile)}. Starting ${nameOf(S.profile)} starts a new count from zero.`),
    h("p", { class: "fg-meta", text: ruleText }),
    h("div", { class: "fg-listhead" },
      h("span", { class: "fg-lbl", text: "Drops, newest first" }),
      s.drop_count > shown.length && h("span", { class: "fg-meta", text: `last ${shown.length} of ${s.drop_count}` })),
    shown.length
      ? h("ul", { class: "fg-droplist" }, shown.map(dropRow))
      : h("div", { class: "fg-empty", text: s.running ? "No drops yet." : "Start the count to list the drops you pick up." }));
}

function dropRow(drop) {
  const { h } = C;
  const [name, color] = rarityOf(drop.rarity);
  return h("li", { class: "fg-drop", vars: { "--r": color } },
    h("span", { class: "fg-swatch" }),
    h("div", { class: "fg-drop-main" },
      h("div", { class: "fg-drop-name", text: drop.item }),
      h("div", { class: "fg-meta", text: `${KIND_TEXT[drop.kind] || drop.kind} · ${name}${drop.rarity ? ` (${drop.rarity})` : ""} · attempt ${drop.attempt}` })),
    h("span", { class: "fg-time", text: ago(drop.at) }));
}

function settingsPanel(d, locked) {
  const { h, icon } = C;
  const reload = d.reload;
  return h("section", { class: "fg-panel fg-settings" },
    h("div", { class: "fg-panel-head" }, h("h2", { text: "Settings" })),
    h("label", { class: `fg-check${locked ? " dis" : ""}` },
      h("input", { type: "checkbox", checked: d.settings.experimental_reload || null, disabled: locked || null, onchange: (event) => setQuickReload(event.target.checked) }),
      h("span", {},
        h("b", { text: "Quick reload (experimental)" }),
        h("span", { class: "fg-meta", text: "Off until it has been checked in a live game." }))),
    h("p", { class: "fg-warnline" }, icon("alert", 16),
      "Undo & quick reload saves the game, quits to the title screen and loads the same playthrough straight back in. If it doesn't relaunch, press Continue yourself."),
    reload.pending && h("p", { class: "fg-meta", text: "A quick reload is pending." }),
    reload.last && h("p", { class: "fg-meta", text: `Last quick reload: ${reload.last}` }));
}

function devTools(d) {
  const { h } = C;
  const hooks = d.hooks;
  return h("div", { class: "tools fg-tools" },
    h("div", { class: "fg-meta", text: `Hooks: ${hooks.installed} of ${hooks.total} installed · ${hooks.can_block ? "can block changes" : "can't block changes"}` }),
    hooks.failed.length > 0 && h("div", { class: "fg-warnline", text: `Failed: ${hooks.failed.join(", ")}` }),
    h("button", { class: "btn sm", onclick: () => C.dump("/api/debug/farm") }, "Save farm dump"));
}

/** The sidebar card: a badge with the open alerts, and how many farms are guarded. */
export function pin(ctx) {
  const { h, icon } = ctx;
  const d = S.data;
  const open = d ? d.alerts.length : 0;
  const guarded = d ? d.profiles.filter((p) => p.enabled).length : 0;
  return {
    icon: icon("crosshair", 20),
    sub: d ? `${guarded} guarded${d.session.running ? " · counting" : ""}` : "Guard a farm, count drops",
    badge: open ? h("span", { class: "fg-badge", text: String(open) }) : undefined,
  };
}
