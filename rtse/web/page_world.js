// World & progress page: missions, challenges, fast-travel stations and the playthrough.
// A plain ES module. app.js passes its helpers in as ctx and calls render() whenever it redraws the
// workspace, and load() to fetch. All page state lives here so a redraw never loses the tab, search or scroll.

const TABS = [["missions", "Missions"], ["challenges", "Challenges"], ["stations", "Fast travel"], ["playthrough", "Playthrough"]];
const PT_COLORS = ["#67d983", "#5fa0d6", "#ff5a2c"];
const PT_SHORT = ["Normal", "TVHM", "UVHM"];
const GROUP_FIRST = ["Main story", "Side missions"]; // every other mission group (the DLCs) follows alphabetically

const MISSION_FILTERS = [["all", "All"], ["active", "Active"], ["ready", "Ready"], ["not_started", "Not started"], ["complete", "Complete"], ["failed", "Failed"]];
const CHALLENGE_FILTERS = [["all", "All"], ["progress", "In progress"], ["none", "Not started"], ["done", "Done"]];
const STATION_FILTERS = [["all", "All"], ["locked", "Locked"], ["unlocked", "Unlocked"]];

const S = {
  data: null, // { playthrough, missions, challenges, stations }, each with .available / .error
  error: null,
  tab: "missions",
  pt: null, // which playthrough's missions are shown
  query: { missions: "", challenges: "", stations: "" },
  filter: { missions: "all", challenges: "all", stations: "all" },
  confirm: null, // key of the risky button currently asking "Really?"
  scroll: {}, // list key -> scrollTop, so a redraw keeps your place
};
let C = null; // ctx from the latest render()

const statusClass = (status) => (status === "objectives_done" ? "ready" : status || "unknown");
const statusLabel = (status) => ((S.data && S.data.missions.statuses) || {})[status] || status;
const count = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

// ---------- loading ----------

export async function load(ctx) {
  if (ctx) C = ctx;
  try {
    S.data = await C.api("GET", "/api/world");
    S.error = null;
  } catch (error) {
    S.error = error.message;
  }
  S.confirm = null;
  ensurePlaythrough();
  C.rerender();
}

function ensurePlaythrough() {
  const m = S.data && S.data.missions;
  if (!m || !m.available) return;
  const numbers = m.playthroughs.map((p) => p.number);
  if (!numbers.includes(S.pt)) S.pt = numbers.includes(m.current) ? m.current : numbers[0] ?? null;
}

/** Sends a change, folds the fresh sections it returns into the page state, and says what happened. */
async function write(path, body, summarize) {
  S.confirm = null;
  const result = await C.guarded(() => C.api("POST", path, body));
  if (result) {
    for (const key of ["playthrough", "missions", "challenges", "stations"]) if (result[key]) S.data[key] = result[key];
    ensurePlaythrough();
    const [text, bad] = summarize(result.applied);
    C.toast(text, bad);
  }
  C.rerender();
}

// ---------- small pieces ----------

/** A risky button: the first click turns it into an inline "Really? Yes / Cancel"; only Yes runs the action. */
function ask(key, label, run, { cls = "btn sm", title = null, disabled = false } = {}) {
  const { h } = C;
  if (S.confirm === key) {
    return h("span", { class: "wl-ask" },
      h("b", { text: "Really?" }),
      h("button", { class: "btn sm yes", onclick: run }, "Yes"),
      h("button", { class: "btn sm", onclick: () => { S.confirm = null; C.rerender(); } }, "Cancel"));
  }
  return h("button", { class: cls, title, disabled: disabled || null, onclick: () => { S.confirm = key; C.rerender(); } }, label);
}

function unavailable(section, what) {
  return C.h("div", { class: "warn-banner" }, C.icon("alert", 18),
    `${what} aren't available: ${section.error || "unknown reason"}. Save the world dump below and send it over.`);
}

function chips(key, filters, counts) {
  const { h } = C;
  return h("div", { class: "filters wl-chips" }, filters.map(([id, label]) =>
    h("button", { class: `fchip${S.filter[key] === id ? " on" : ""}`, onclick: () => { S.filter[key] = id; S.confirm = null; C.rerender(); } },
      label, h("em", { class: "wl-n", text: String(counts[id]) }))));
}

/**
 * The shared shape of the three list tabs: a search box and filter chips, a line with the bulk actions,
 * and a scrolling list (grouped, with sticky group headers). Only the list redraws while you type.
 */
function listPanel({ key, scrollKey, rows, filters, test, text, groupOf, order, groupNote, rowEl, actions, bodyClass, noun, after }) {
  const { h, fill, icon } = C;
  const counts = Object.fromEntries(filters.map(([id]) => [id, rows.filter((row) => test(id, row)).length]));
  const shown = () => {
    const query = S.query[key].trim().toLowerCase();
    return rows.filter((row) => test(S.filter[key], row) && (!query || text(row).includes(query)));
  };

  const info = h("div", { class: "wl-actions" });
  const host = h("div", { class: "wl-list", tabindex: "-1" });
  host.addEventListener("scroll", () => { S.scroll[scrollKey] = host.scrollTop; });
  after.push(() => { host.scrollTop = S.scroll[scrollKey] || 0; });

  const draw = () => {
    const list = shown();
    fill(info,
      h("span", { class: "wl-count", text: `Showing ${list.length.toLocaleString()} of ${count(rows.length, noun)}` }),
      h("span", { class: "wl-bulk" }, actions(list)));
    const groups = new Map();
    for (const row of list) {
      const name = groupOf(row);
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(row);
    }
    fill(host, list.length === 0
      ? h("div", { class: "empty-note wl-none", text: rows.length ? "Nothing matches that search or filter." : "Nothing here." })
      : [...groups.keys()].sort(order).map((name) => [
        name && h("div", { class: "wl-group" }, h("span", { text: name }), h("em", { text: groupNote(groups.get(name)) })),
        h("div", { class: bodyClass }, groups.get(name).map(rowEl)),
      ]));
  };

  const box = h("input", {
    class: "field wl-search", type: "search", placeholder: `Search ${noun}s`, value: S.query[key], "aria-label": `Search ${noun}s`, autocomplete: "off",
    oninput: () => { S.query[key] = box.value; S.scroll[scrollKey] = 0; draw(); host.scrollTop = 0; },
  });
  draw();
  return h("div", {},
    h("div", { class: "wl-toolbar" }, h("div", { class: "wl-find" }, icon("search", 15), box), chips(key, filters, counts)),
    info, host);
}

// ---------- missions ----------

function missionsTab(after) {
  const { h, icon } = C;
  const m = S.data.missions;
  if (!m.available) return unavailable(m, "Missions");
  const current = m.playthroughs.find((p) => p.number === S.pt) || m.playthroughs[0];
  if (!current) return h("div", { class: "warn-banner" }, icon("alert", 18), "The game reported no missions. Save the world dump below and send it over.");
  const pt = current.number;
  const test = (id, row) => id === "all" || row.status === id || (id === "ready" && row.status === "objectives_done");

  const ptBar = h("div", { class: "seg wl-pts" }, m.playthroughs.map((p) =>
    h("button", { class: p.number === pt ? "on" : "", onclick: () => { S.pt = p.number; S.confirm = null; C.rerender(); } },
      `PT${p.number} ${(PT_SHORT[p.number - 1]) || ""}`.trim(),
      m.current === p.number && h("i", { class: "wl-now", title: "The playthrough you are in now" }))));

  const bulk = (target, label, verb) => (list) => {
    const todo = list.filter((row) => row.status !== target);
    return ask(`m:${pt}:${target}:bulk`, [icon(target === "complete" ? "check" : "refresh", 13), `${label} shown (${todo.length})`], () => changeMissions(pt, todo, target),
      { cls: `btn sm${target === "complete" ? " primary" : " danger"}`, disabled: todo.length === 0, title: `${verb} every mission in the list below that isn't already ${statusLabel(target).toLowerCase()}` });
  };

  const rowEl = (row) => h("div", { class: "wl-row", vars: { "--st": `var(--st-${statusClass(row.status)})` } },
    h("i", { class: "wl-dot", title: statusLabel(row.status) }),
    h("div", { class: "wl-main" },
      h("div", { class: "wl-name", text: row.name, title: row.id }),
      h("div", { class: "wl-sub" }, h("span", { class: "wl-st", text: statusLabel(row.status) }), h("span", { class: "wl-id", text: row.id }))),
    h("div", { class: "wl-btns" },
      row.status !== "complete" && ask(`m:${pt}:${row.id}:complete`, [icon("check", 13), "Complete"], () => changeMissions(pt, [row], "complete")),
      row.status !== "not_started" && ask(`m:${pt}:${row.id}:reset`, [icon("refresh", 13), "Reset"], () => changeMissions(pt, [row], "not_started"), { cls: "btn sm danger" })));

  return h("div", {},
    ptBar,
    listPanel({
      key: "missions", scrollKey: `missions${pt}`, rows: current.rows, filters: MISSION_FILTERS, test, noun: "mission", after, rowEl,
      text: (row) => `${row.name} ${row.id} ${row.group}`.toLowerCase(),
      groupOf: (row) => row.group,
      order: (a, b) => (GROUP_FIRST.indexOf(a) + 1 || 99) - (GROUP_FIRST.indexOf(b) + 1 || 99) || a.localeCompare(b),
      groupNote: (rows) => `${rows.filter((r) => r.status === "complete").length}/${rows.length} done`,
      bodyClass: "wl-rows",
      actions: (list) => [bulk("complete", "Complete all", "Marks complete")(list), bulk("not_started", "Reset all", "Resets")(list)],
    }));
}

function changeMissions(pt, rows, status) {
  const ids = rows.map((row) => row.id);
  const reset = status === "not_started";
  return write(reset ? "/api/world/missions/reset" : "/api/world/mission", reset ? { ids, playthrough: pt, confirm: true } : { ids, playthrough: pt, status, confirm: true }, (a) => {
    if (a.failed.length) return [`${a.failed.length} of ${count(a.count, "mission")} didn't change - the game kept the old state (see the world dump)`, true];
    if (a.count === 1) return [`${a.name}: ${statusLabel(a.before)} to ${statusLabel(a.after)}`, a.after !== a.requested];
    return [`${a.changed} of ${count(a.count, "mission")} set to ${statusLabel(a.requested).toLowerCase()} (playthrough ${a.playthrough})`, false];
  });
}

// ---------- challenges ----------

function challengeState(row) {
  if (row.done === true) return "done";
  return row.progress ? "progress" : "none";
}

function challengesTab(after) {
  const { h, icon } = C;
  const c = S.data.challenges;
  if (!c.available) return unavailable(c, "Challenges");
  const rowEl = (row) => {
    const state = challengeState(row);
    const pct = row.goal ? Math.max(0, Math.min(100, ((row.progress || 0) / row.goal) * 100)) : state === "done" ? 100 : 0;
    return h("div", { class: "wl-row", vars: { "--st": `var(--st-${state === "done" ? "complete" : state === "progress" ? "active" : "not_started"})` } },
      h("i", { class: "wl-dot", title: state === "done" ? "Done" : state === "progress" ? "In progress" : "Not started" }),
      h("div", { class: "wl-main" },
        h("div", { class: "wl-name", text: row.name, title: row.id }),
        row.desc && h("div", { class: "wl-desc", text: row.desc }),
        h("div", { class: "wl-prog" },
          h("div", { class: "bar" }, h("i", { style: { width: `${pct}%` } })),
          h("span", { text: row.progress == null ? "progress unknown" : `${row.progress.toLocaleString()}${row.goal ? ` / ${row.goal.toLocaleString()}` : ""}` }))),
      h("div", { class: "wl-btns" },
        state !== "done" && h("button", { class: "btn sm", onclick: () => changeChallenges([row], "complete") }, icon("check", 13), "Complete"),
        c.can_reset !== false && state !== "none" && ask(`c:${row.id}:reset`, [icon("refresh", 13), "Reset"], () => changeChallenges([row], "reset"), { cls: "btn sm danger" })));
  };
  return listPanel({
    key: "challenges", scrollKey: "challenges", rows: c.rows, filters: CHALLENGE_FILTERS, noun: "challenge", after, rowEl,
    test: (id, row) => id === "all" || challengeState(row) === id,
    text: (row) => `${row.name} ${row.desc} ${row.group}`.toLowerCase(),
    groupOf: (row) => row.group,
    order: (a, b) => a.localeCompare(b),
    groupNote: (rows) => `${rows.filter((r) => r.done === true).length}/${rows.length} done`,
    bodyClass: "wl-rows",
    actions: (list) => {
      const todo = list.filter((row) => challengeState(row) !== "done");
      return [
        h("button", { class: "btn sm primary", disabled: todo.length === 0 || null, onclick: () => changeChallenges(todo, "complete") }, icon("check", 13), `Complete all shown (${todo.length})`),
        c.can_reset !== false && ask("c:bulk:reset", [icon("refresh", 13), "Reset all shown"], () => changeChallenges(list, "reset"), { cls: "btn sm danger", disabled: list.length === 0 }),
      ];
    },
  });
}

function changeChallenges(rows, action) {
  const body = { ids: rows.map((row) => row.id), action };
  if (action === "reset") body.confirm = true;
  return write("/api/world/challenge", body, (a) => {
    if (a.failed.length) return [`${a.failed.length} of ${count(a.count, "challenge")} didn't change - the game kept the old value (see the world dump)`, true];
    return [a.count === 1 ? `${a.name}: ${action === "complete" ? "completed" : "reset"}` : `${count(a.count, "challenge")} ${action === "complete" ? "completed" : "reset"}`, false];
  });
}

// ---------- fast travel ----------

function stationsTab(after) {
  const { h, icon } = C;
  const s = S.data.stations;
  if (!s.available) return unavailable(s, "Fast-travel stations");
  const stateOf = (row) => (row.visited === true ? "unlocked" : row.visited === false ? "locked" : "unknown");
  const rowEl = (row) => {
    const state = stateOf(row);
    return h("div", { class: `wl-card ${state}` },
      h("div", { class: "wl-main" }, h("div", { class: "wl-name", text: row.name, title: row.id }), row.level && h("div", { class: "wl-id", text: row.level })),
      h("div", { class: "wl-btns" },
        h("span", { class: "wl-stamp", text: state === "unlocked" ? "Unlocked" : state === "locked" ? "Locked" : "Unknown" }),
        state !== "unlocked" && h("button", { class: "btn sm primary", onclick: () => changeStations({ ids: [row.id] }, row.name) }, icon("check", 13), "Unlock"),
        s.can_lock !== false && state === "unlocked" && ask(`s:${row.id}:lock`, "Lock", () => changeStations({ ids: [row.id], visited: false, confirm: true }, row.name), { cls: "btn sm ghost", title: "Mark as not yet visited" })));
  };
  return h("div", {},
    s.warning && h("div", { class: "warn-banner" }, icon("alert", 18), `${s.warning}.`),
    listPanel({
      key: "stations", scrollKey: "stations", rows: s.rows, filters: STATION_FILTERS, noun: "station", after, rowEl,
      test: (id, row) => id === "all" || stateOf(row) === id,
      text: (row) => `${row.name} ${row.level}`.toLowerCase(),
      groupOf: () => "",
      order: () => 0,
      groupNote: () => "",
      bodyClass: "wl-cards",
      actions: (list) => {
        const todo = list.filter((row) => row.visited !== true);
        return h("button", { class: "btn sm primary", disabled: todo.length === 0 || null, onclick: () => changeStations({ ids: todo.map((row) => row.id) }, null) },
          icon("check", 13), `Unlock all shown (${todo.length})`);
      },
    }));
}

function changeStations(body, name) {
  return write("/api/world/stations", body, (a) => {
    const lock = a.requested === "lock";
    if (a.failed.length) return [`${a.failed.length} of ${count(a.count, "station")} didn't change - the game kept the old state (see the world dump)`, true];
    if (a.count === 1) return [`${a.name || name}: ${lock ? "locked" : "unlocked"}`, false];
    return [`${count(a.count, "station")} ${lock ? "locked" : "unlocked"}`, false];
  });
}

// ---------- playthrough ----------

function playthroughTab() {
  const { h, icon } = C;
  const p = S.data.playthrough;
  if (!p.available) return unavailable(p, "The playthrough");
  return h("div", {},
    h("div", { class: "char-grid wl-pt-grid" }, p.options.map((option) => {
      const here = p.current === option.number;
      return h("div", { class: `cc wl-pt${here ? " here" : ""}`, vars: { "--c": PT_COLORS[option.number - 1] } },
        h("div", { class: "cc-head" },
          h("span", { class: "coin", text: String(option.number) }),
          h("div", {}, h("div", { class: "k", text: option.short }), h("div", { class: "sub", text: option.name }))),
        h("div", { class: "cc-val", text: `PT${option.number}` }),
        here
          ? h("span", { class: "stamp equipped", text: "You are here" })
          : ask(`p:${option.number}`, [icon("swap", 13), `Switch to ${option.short}`], () => switchPlaythrough(option), { cls: "btn sm primary" }));
    })),
    h("p", { class: "note", text: "This sets which playthrough the game thinks you are in. Missions, enemy levels and loot follow the next time an area loads, and the game may also keep the value in your save, so save and reload to be sure. Switching mid-playthrough can leave the mission log out of step with the world." }),
    p.current == null && h("p", { class: "note", text: `The game reports playthrough number ${p.raw}, which is outside the normal range.` }));
}

function switchPlaythrough(option) {
  return write("/api/world/playthrough", { playthrough: option.number, confirm: true }, (a) =>
    a.after === a.requested ? [`Playthrough ${a.before} to ${a.after} (${option.short})`, false] : [`Asked for playthrough ${a.requested} but the game reports ${a.after == null ? "nothing" : a.after}`, true]);
}

// ---------- page ----------

function banner() {
  const { h, icon } = C;
  const d = S.data;
  const facts = [];
  const p = d.playthrough;
  if (p.available && p.current != null) facts.push(h("span", {}, h("b", { text: p.options[p.current - 1].name })));
  if (d.missions.available) {
    const cur = d.missions.playthroughs.find((x) => x.number === S.pt);
    if (cur) facts.push(h("span", { text: `${(cur.counts.complete || 0).toLocaleString()}/${cur.rows.length.toLocaleString()} missions done (PT${cur.number})` }));
  }
  if (d.challenges.available) facts.push(h("span", { text: `${d.challenges.rows.filter((r) => r.done === true).length}/${d.challenges.rows.length} challenges done` }));
  if (d.stations.available) facts.push(h("span", { text: `${d.stations.rows.filter((r) => r.visited === true).length}/${d.stations.rows.length} stations unlocked` }));
  return h("div", { class: "banner" },
    h("div", {}, h("span", { class: "stamp equipped", text: "Your progress" }), h("h1", { text: "World & progress" }), h("div", { class: "facts" }, facts)),
    h("div", { class: "level" },
      h("button", { class: "btn ghost", title: "Re-read from the game", "aria-label": "Refresh", onclick: () => load() }, icon("refresh", 15))));
}

export function render(root, ctx) {
  C = ctx;
  const { h, fill, icon } = ctx;
  if (!S.data) {
    fill(root, S.error
      ? h("div", { class: "bench" }, h("div", { class: "banner" }, h("div", {}, h("h1", { text: "World & progress" }))),
        h("div", { class: "warn-banner" }, icon("alert", 18), `${S.error}. Is a save loaded?`),
        h("div", { class: "wl-body" }, h("button", { class: "btn primary", onclick: () => load() }, icon("refresh", 15), "Try again")))
      : h("div", { class: "empty" }, h("div", {}, h("h2", { text: "Loading..." }))));
    return;
  }

  const after = []; // things to do once the page is in the document (restoring scroll positions)
  const tabs = h("div", { class: "seg wl-tabs" }, TABS.map(([id, label]) => {
    const section = S.data[id];
    return h("button", { class: S.tab === id ? "on" : "", onclick: () => { S.tab = id; S.confirm = null; ctx.rerender(); } },
      label, section && !section.available && h("small", { class: "wl-off", text: " (n/a)" }));
  }));
  const body = { missions: missionsTab, challenges: challengesTab, stations: stationsTab, playthrough: playthroughTab }[S.tab](after);

  fill(root,
    h("div", { class: "bench" },
      banner(),
      h("div", { class: "bench-body" },
        h("div", { class: "warn-banner" }, icon("info", 18), "Changes apply to your live game. Save in game afterwards to keep them. None of this has been checked against the real game yet, so each change reports what the game says afterwards."),
        h("div", { class: "wl-body" }, tabs, body))),
    ctx.dropdown("worldinfo", "World dev tools", "", h("div", { class: "tools" },
      h("button", { class: "btn sm", onclick: () => ctx.dump("/api/debug/world") }, "Save world dump"))));
  for (const run of after) run();
}
