// History page: what RTSE changed in the live game (the timeline, with undo where that is safe) and the save
// backups (back up now, restore staged for the next load, how many to keep). Shapes follow the Stream A API
// contract and its review round 1 delta: GET /api/history, POST /api/history/undo, GET /api/checkpoints and the
// /api/checkpoints/* writes (create, restore, cancel_restore, settings, skip_backup, unpin).
// A plain ES module: app.js passes its helpers in as ctx, calls load() each time the page opens and render() on
// every redraw. Page state lives here, so a redraw keeps the tab, the typed note and an open confirm panel.

const TABS = [["timeline", "Timeline"], ["backups", "Save backups"]];
const SOURCE_WORDS = {
  game: "read from the game",
  game_id: "file name from the game, folder guessed",
  guessed: "guessed: newest save in the default folder - check it's the right character",
};
const STAGE_WORDS = {
  staged: "Staged. Not applied yet.",
  waiting: "At the main menu. Waiting for the save file to stop changing.",
  applying: "Copying the backup into your save folder now.",
  verifying: "Copied. Re-checking the files a few seconds later.",
  failed: "Failed. The restore did not apply.",
};
const KIND_WORDS = {
  "character.level": "Level",
  "character.xp": "Experience",
  "character.skill_points": "Skill points",
  "character.money": "Cash",
  "character.eridium": "Eridium",
  "character.seraph": "Seraph crystals",
  "character.torgue": "Torgue tokens",
  "sdu.backpack": "Backpack SDU",
  "sdu.bank": "Bank SDU",
  "skills.reset": "Skill tree reset",
  "gear.level": "Gear level",
  "world.playthrough": "Playthrough",
  "world.stations": "Fast travel",
  "world.challenge": "Challenges",
  "inventory.add": "Item added",
  "inventory.delete": "Item deleted",
  "inventory.move": "Item moved",
  "checkpoint.create": "Checkpoint created",
  "checkpoint.restore": "Checkpoint restore staged",
};
// kind -> [title, where the detail comes from]: a record param (gear names its slot or stat, not its long item id)
// or the record's target
const KIND_PREFIX = {
  "ammo.value": ["Ammo", "target"],
  "gear.part": ["Gear part", "slot"],
  "gear.stat": ["Gear stat", "stat"],
  "sdu.ammo_max": ["Ammo capacity", "target"],
  "skills.level": ["Skill", "target"],
  "world.mission": ["Mission", "target"],
  "world.objective": ["Objective", "target"],
  "world.objective_set": ["Mission stage", "target"],
  "world.track": ["Tracked mission", "target"],
  "inspect.value": ["Inspector", "target"],
};

const S = {
  history: null, // the last GET /api/history (an undo's reply carries the same fields)
  historyError: null,
  checkpoints: null, // the last GET /api/checkpoints (a checkpoint write's reply carries the same fields)
  checkpointsError: null,
  tab: "timeline",
  busy: false, // a write is in flight; its buttons ignore clicks until it lands
  undoError: {}, // entry id -> the refusal sentence from the last undo of that entry
  note: "", // the backup note being typed
  retentionDraft: null, // the retention box while it differs from the saved value
  confirm: null, // { id, profile } while one checkpoint's restore is being confirmed
  skipConfirm: false, // the "continue without a backup" confirm in the gate banner is open
};
let C = null; // the ctx from the latest render() or load()
const h = (...args) => C.h(...args);
const icon = (...args) => C.icon(...args);

// ---------- data ----------

/** Fetches the timeline and the backups, then asks the host to redraw. Safe to call before render(). */
export async function load(ctx = C) {
  if (!ctx) return;
  C = ctx;
  const [history, checkpoints] = await Promise.allSettled([ctx.api("GET", "/api/history"), ctx.api("GET", "/api/checkpoints")]);
  if (history.status === "fulfilled") {
    S.history = history.value;
    S.historyError = null;
  } else {
    S.history = null;
    S.historyError = history.reason.message;
  }
  if (checkpoints.status === "fulfilled") {
    S.checkpoints = checkpoints.value;
    S.checkpointsError = null;
  } else {
    S.checkpoints = null;
    S.checkpointsError = checkpoints.reason.message;
  }
  ctx.rerender();
}

/** One write. Resolves to { reply } or { error }; null when another write is still in flight. */
async function post(path, body) {
  if (S.busy) return null;
  S.busy = true;
  try {
    return { reply: await C.api("POST", path, body) };
  } catch (error) {
    return { error: error.message };
  } finally {
    S.busy = false;
  }
}

/** A checkpoint write: stores the reply's backup list, says what happened, redraws. Returns the result or null. */
async function checkpointWrite(path, body, describe) {
  const result = await post(path, body);
  if (!result) return null;
  if (result.error) {
    C.toast(result.error, true);
  } else {
    S.checkpoints = result.reply;
    S.checkpointsError = null;
    C.toast(describe(result.reply.applied));
  }
  C.rerender();
  return result;
}

async function undo(entry) {
  const result = await post("/api/history/undo", { id: entry.id });
  if (!result) return;
  if (result.error) {
    S.undoError[entry.id] = result.error;
    C.toast(result.error, true);
  } else {
    delete S.undoError[entry.id];
    S.history = result.reply;
    C.toast(`Undone: ${titleFor(entry)}`);
  }
  C.rerender();
}

async function backUp() {
  const note = S.note.trim();
  const result = await checkpointWrite("/api/checkpoints/create", { note }, (a) => `Backed up as ${a.checkpoint.id}`);
  if (result && !result.error) S.note = "";
}

async function skipBackup() {
  S.skipConfirm = false;
  await checkpointWrite("/api/checkpoints/skip_backup", { confirm: true }, () =>
    "Continuing without a backup this session. Game writes are allowed; nothing was backed up first.");
}

async function unpin(cp) {
  await checkpointWrite("/api/checkpoints/unpin", { id: cp.id }, (a) => `Unpinned ${a.unpinned}; it can be pruned again`);
}

async function stageRestore(cp, profile) {
  S.confirm = null;
  await checkpointWrite("/api/checkpoints/restore", { id: cp.id, confirm: true, include_profile: profile }, () =>
    `Restore of ${cp.id} staged. Nothing has changed yet: quit to the main menu and load your character.`);
}

async function cancelRestore() {
  await checkpointWrite("/api/checkpoints/cancel_restore", {}, (a) =>
    (a.cancelled ? `Cancelled the staged restore of ${a.cancelled}` : "No restore was staged"));
}

async function saveRetention() {
  const d = S.checkpoints;
  const [low, high] = d.retention_range || [1, 200];
  const value = Number(S.retentionDraft ?? d.retention);
  if (!Number.isInteger(value) || value < low || value > high) return C.toast(`Keep between ${low} and ${high} backups`, true);
  const result = await checkpointWrite("/api/checkpoints/settings", { retention: value }, (a) =>
    `Keeping ${a.retention} backups${a.pruned && a.pruned.length ? `; removed ${a.pruned.length} of the oldest` : ""}`);
  if (result && !result.error) S.retentionDraft = null;
}

// ---------- words and values ----------

const pad = (n) => String(n).padStart(2, "0");

/** A plain value as one short line: objects as "k: v" pairs (a gear part shows its name), lists joined, null as "-". */
function show(value) {
  if (value === null || value === undefined) return "-";
  if (typeof value === "number") return value.toLocaleString();
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "string") return value || "-";
  if (Array.isArray(value)) return value.map(show).join(", ") || "none";
  if (typeof value === "object") {
    if (typeof value.name === "string") return value.name;
    return Object.entries(value).map(([k, v]) => `${k}: ${show(v)}`).join(", ") || "-";
  }
  return String(value);
}

/** Whether what the game kept is not what was asked. A gear part compares by its id; numbers and text loosely. */
function differs(requested, kept) {
  if (requested === null || requested === undefined) return false;
  const a = keyOf(requested);
  const b = keyOf(kept);
  if ((a !== null && typeof a === "object") || (b !== null && typeof b === "object")) return JSON.stringify(a) !== JSON.stringify(b);
  return String(a ?? "") !== String(b ?? "");
}
const keyOf = (v) => (v && typeof v === "object" && !Array.isArray(v) && "id" in v ? v.id : v);

/** yes / no / unknown for a tri-state value (true, false, null). */
const yn = (v) => (v === true ? "yes" : v === false ? "no" : "unknown");

function when(epoch) {
  if (typeof epoch !== "number") return "unknown time";
  return new Date(epoch * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

const fullTime = (epoch) => (typeof epoch === "number" ? new Date(epoch * 1000).toLocaleString() : "");

const bytes = (n) => (typeof n !== "number" ? "" : n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`);

/** The last part of a path, whichever slash it uses. */
const baseName = (path) => (path || "").split(/[\\/]/).pop();

/** The profile.bin that sits next to a backup's character file: a restore writes it there when the box is ticked. */
const profilePathFor = (characterFile) => (characterFile ? `${characterFile.replace(/[^\\/]*$/, "")}profile.bin` : "profile.bin (location unknown)");

/** The warning for a backup whose character file is not the save RTSE located now. */
function mismatchText(cp, save) {
  const found = save.character_file || "none";
  return `This backup is of ${baseName(cp.character_file)}, not the save RTSE found now (${found}). Restoring it overwrites ${cp.character_file}.`;
}

function recordTitle(record) {
  const { kind } = record;
  if (KIND_WORDS[kind]) return KIND_WORDS[kind];
  if (KIND_PREFIX[kind]) {
    const [title, from] = KIND_PREFIX[kind];
    const detail = show(from === "target" ? record.target : (record.params || {})[from]);
    return detail === "-" ? title : `${title}: ${detail}`;
  }
  if (kind && kind.startsWith("sdu.ammo_")) return `Ammo SDU: ${kind.slice(9)}`;
  if (kind && kind.startsWith("sdu.")) return `SDU: ${kind.slice(4)}`;
  if (kind && kind.startsWith("world.")) return `World: ${kind.slice(6)}`;
  return kind || "Change";
}

/** The entry's human title: its records' titles, joined. */
function titleFor(entry) {
  const titles = [...new Set((entry.records || []).map(recordTitle))];
  return titles.length ? titles.join(" + ") : entry.label || "Change";
}

const autoText = (auto) => {
  if (!auto) return "unknown";
  if (auto.skipped) return "skipped for this session at your request";
  if (auto.error) return `failed: ${auto.error}`;
  if (auto.done) return auto.checkpoint ? `made as ${auto.checkpoint}` : "done, nothing to back up";
  return "not made yet: it is made before your first change this session";
};

// ---------- drawing: shared bits ----------

const warning = (text) => h("div", { class: "warn-banner" }, icon("alert", 18), text);
const loadingNote = () => h("p", { class: "note", text: "Loading..." });

/** A small link inside a badge that scrolls the timeline to that entry. */
function jumpLink(id, text) {
  return h("button", { class: "hi-link", text, onclick: () => jump(id) });
}

function jump(id) {
  const el = document.querySelector(`[data-entry="${id}"]`);
  if (!el) return C.toast(`#${id} is not on the timeline`, true);
  el.scrollIntoView({ block: "center" });
  el.classList.add("hi-flash");
  setTimeout(() => el.classList.remove("hi-flash"), 1400);
}

function banner() {
  const entries = S.history ? (S.history.entries || []).length : null;
  const backups = S.checkpoints ? (S.checkpoints.checkpoints || []).length : null;
  return h("div", { class: "banner" },
    h("div", {},
      h("span", { class: "stamp equipped", text: "Undo and backups" }),
      h("h1", { text: "History" }),
      h("div", { class: "facts" },
        entries !== null && h("span", {}, h("b", { text: String(entries) }), "changes this session"),
        backups !== null && h("span", {}, h("b", { text: String(backups) }), "save backups"),
        S.checkpoints && h("span", {}, "keeps", h("b", { text: String(S.checkpoints.retention ?? "?") }), "backups"))),
    h("div", { class: "level" },
      h("button", { class: "btn ghost", title: "Re-read from the game", "aria-label": "Refresh", onclick: () => load() }, icon("refresh", 15))));
}

/**
 * The backup gate: while auto.blocking, every game write is refused until the user backs up or skips the backup.
 * Shown above both tabs. Nothing when the gate is open.
 */
function gateBanner() {
  const auto = S.checkpoints && S.checkpoints.auto;
  if (!auto || !auto.blocking) return null;
  return h("div", { class: "hi-gate", role: "alert" },
    icon("alert", 26),
    h("div", { class: "hi-gate-body" },
      h("h2", { text: "Game writes are refused until you back up" }),
      h("p", { class: "hi-gate-err", text: auto.error || "RTSE couldn't back up your save before its first change this session." }),
      h("p", { text: "Every change on every page is refused until you either back up now, or continue without a backup this session." }),
      S.skipConfirm
        ? h("div", { class: "hi-confirm" },
          h("p", { text: "Continue without a backup? RTSE won't copy your save first, so this session's changes have no backup to go back to." }),
          h("div", { class: "hi-row" },
            h("button", { class: "btn sm primary", disabled: S.busy || null, onclick: skipBackup }, "Continue without a backup"),
            h("button", { class: "btn sm", onclick: () => { S.skipConfirm = false; C.rerender(); } }, "Cancel")))
        : h("div", { class: "hi-row" },
          h("button", { class: "btn primary", disabled: S.busy || null, onclick: backUp }, icon("check", 15), "Back up now"),
          h("button", { class: "btn", disabled: S.busy || null, onclick: () => { S.skipConfirm = true; C.rerender(); } },
            "Continue without a backup this session"))));
}

// ---------- timeline ----------

/**
 * One recorded change: requested, kept and before, with a highlight when the game kept something else.
 * `reason` is the entry's own undo reason; a record note that repeats it is left out (the footer shows it).
 */
function recordRow(record, reason) {
  const requested = record.requested !== null && record.requested !== undefined;
  const mismatch = differs(record.requested, record.after);
  return h("div", { class: `hi-rec${mismatch ? " diff" : ""}` },
    h("div", { class: "hi-what" }, h("b", { text: recordTitle(record) }), record.route && h("small", { text: record.route })),
    h("div", { class: "hi-vals" },
      h("div", { class: "hi-pair" }, h("dt", { text: "before" }), h("dd", { text: show(record.before) })),
      requested && h("div", { class: "hi-pair" }, h("dt", { text: "asked" }), h("dd", { text: show(record.requested) })),
      h("div", { class: "hi-pair" }, h("dt", { text: "kept" }), h("dd", { class: mismatch ? "hi-kept" : "", text: show(record.after) }))),
    mismatch && h("p", { class: "hi-warn", text: `asked ${show(record.requested)}, the game kept ${show(record.after)}` }),
    !record.undoable && record.why_not && record.why_not !== reason && h("p", { class: "hi-note", text: record.why_not }));
}

/**
 * The Undo button when it is offered; otherwise the reason, muted. Nothing once the entry is undone.
 * A partial entry never gets an Undo button: its reason says why.
 */
function undoArea(entry) {
  if (entry.undone_by !== null && entry.undone_by !== undefined) return null;
  if (entry.partial) {
    return h("div", { class: "hi-undo" },
      h("span", { class: "hi-why", text: (entry.undo && entry.undo.reason) || "This undo stopped halfway, so it can't be undone." }));
  }
  const err = S.undoError[entry.id];
  const possible = Boolean(entry.undo && entry.undo.possible);
  return h("div", { class: "hi-undo" },
    possible
      ? h("button", { class: "btn sm primary", disabled: S.busy || null, onclick: () => undo(entry) }, "Undo")
      : entry.undo && entry.undo.reason && h("span", { class: "hi-why", text: entry.undo.reason }),
    err && h("p", { class: "hi-err", role: "alert", text: err }));
}

function entryCard(entry) {
  const undone = entry.undone_by !== null && entry.undone_by !== undefined;
  const partial = entry.partial === true;
  const badges = [];
  if (partial) badges.push(h("span", { class: "hi-badge partial", text: "partial" }));
  if (undone) badges.push(h("span", { class: "hi-badge bad" }, "undone ", jumpLink(entry.undone_by, `by #${entry.undone_by}`)));
  if (entry.undo_of !== null && entry.undo_of !== undefined) badges.push(h("span", { class: "hi-badge" }, "undo of ", jumpLink(entry.undo_of, `#${entry.undo_of}`)));
  if (entry.attempted_undo_of !== null && entry.attempted_undo_of !== undefined) badges.push(h("span", { class: "hi-badge" }, "tried to undo ", jumpLink(entry.attempted_undo_of, `#${entry.attempted_undo_of}`)));
  if (entry.session === "earlier") badges.push(h("span", { class: "hi-badge" }, "earlier session"));
  return h("article", { class: `hi-entry${undone ? " undone" : ""}${partial ? " partial" : ""}`, "data-entry": entry.id },
    h("header", { class: "hi-entry-head" },
      h("time", { class: "hi-when", text: when(entry.time), title: fullTime(entry.time) }),
      h("h3", { text: titleFor(entry) }),
      h("span", { class: "hi-id", text: `#${entry.id}` }),
      badges),
    h("div", { class: "hi-recs" }, (entry.records || []).map((record) => recordRow(record, entry.undo && entry.undo.reason))),
    entry.error && h("p", { class: "hi-err-line", role: "alert", text: entry.error }),
    h("footer", { class: "hi-entry-foot" },
      h("span", { class: "hi-from", text: `from ${entry.label || "an unknown action"}` }),
      undoArea(entry)));
}

function timeline() {
  const d = S.history;
  if (!d) return S.historyError ? warning(`The history can't be read: ${S.historyError}`) : loadingNote();
  const entries = d.entries || [];
  return h("section", { class: "hi-section" },
    d.log_error && warning(`History isn't being saved to disk: ${d.log_error}`),
    h("div", { class: "hi-meta" },
      h("span", {}, "Keeps the last ", h("b", { text: String(d.limit ?? "?") }), " changes this session"),
      h("span", {}, "Dropped by that cap: ", h("b", { text: String(d.dropped ?? 0) })),
      h("span", {}, "Log file: ", h("span", { class: "hi-path", text: d.log_file || "not saved (in memory only)" }))),
    entries.length
      ? h("div", { class: "hi-list" }, entries.map(entryCard))
      : h("p", { class: "note", text: "Nothing recorded yet. Changes you make on the other pages show up here." }));
}

// ---------- save backups ----------

/** The staged-restore banner: status in words, what it waits for, the safety copy, and the error when it failed. */
function stagedBanner(p) {
  const failed = p.status === "failed";
  const files = (p.files || []).map((f) => f.name).join(", ");
  const overwrites = (p.files || []).map((f) => f.to).filter(Boolean);
  return h("div", { class: `hi-staged${failed ? " failed" : ""}`, role: failed ? "alert" : "status" },
    icon("alert", 26),
    h("div", {},
      h("h2", { text: failed ? `Restore failed: ${p.id}` : `Restore staged: ${p.id}.` }),
      h("p", { class: "hi-status", text: STAGE_WORDS[p.status] || (p.status ? `Status: ${p.status}` : "Staged.") }),
      p.detail && h("p", { class: "hi-sub", text: p.detail }),
      p.status === "staged" && h("p", { text: "Nothing has been changed yet. Quit to the main menu; once you're there RTSE copies the backup into your save folder (after first backing up what's there), then load your character." }),
      p.safety_checkpoint && h("p", { class: "hi-sub", text: `What was in the save folder was backed up first as ${p.safety_checkpoint}.` }),
      p.error && h("p", { class: "hi-sub hi-err-text", text: `Error: ${p.error}` }),
      failed && h("p", { class: "hi-sub", text: "This stays until you stage it again or cancel it." }),
      h("p", { class: "hi-sub", text: `Files: ${files || "none"}${p.include_profile ? " (profile.bin too)" : ""}. Restores apply on the next load. This has not been verified in a live game yet (unverified).` }),
      overwrites.length > 0 && h("p", { class: "hi-sub", text: `Overwrites: ${overwrites.join(", ")}` })),
    h("button", { class: "btn", disabled: S.busy || null, onclick: cancelRestore }, "Cancel restore"));
}

/** The last restore's outcome, in words: ok, rolled back, overwritten by the game, or failed. */
function lastRestore(r) {
  const status = r.status || (r.ok ? "ok" : "failed");
  const id = r.id ? ` (${r.id})` : "";
  const at = when(r.time);
  const safety = r.safety_checkpoint;
  if (status === "ok") {
    return h("p", { class: "hi-ok" }, icon("check", 15),
      `Last restore${id} at ${at} copied ${(r.files || []).length} file(s).${safety ? ` What was there was backed up first as ${safety}.` : ""}`);
  }
  if (status === "rolled_back") {
    return warning(`Last restore${id} at ${at} rolled back: a copy failed, so the original files were put back from the safety copy${safety ? ` ${safety}` : ""}.${r.error ? ` ${r.error}` : ""}`);
  }
  if (status === "overwritten") {
    return warning(`Last restore${id} at ${at} was written, but the game wrote over the save within a few seconds. The safety copy${safety ? ` ${safety}` : ""} is kept (pinned).${r.error ? ` ${r.error}` : ""}`);
  }
  return warning(`Last restore${id} at ${at} failed: ${r.error || "no reason given"}.${safety ? ` What was there was backed up as ${safety}.` : ""}`);
}

/** Exactly when a restore is applied, the live gate values, and the note that none of it is verified in a game. */
function gatePanel(d) {
  const g = d.restore_gate || {};
  const menu = g.at_main_menu ?? d.at_main_menu;
  return h("div", { class: "hi-panel" },
    h("h3", { text: "When a restore is applied" }),
    h("p", { class: "hi-rule", text: g.rule || "RTSE doesn't report when a restore is applied in this build." }),
    h("div", { class: "hi-row" },
      h("span", { class: "hi-chip" }, "At main menu ", h("b", { text: yn(menu) })),
      h("span", { class: "hi-chip" }, "Character loaded ", h("b", { text: yn(g.character_loaded) }))),
    h("p", { class: "note", text: "Unverified in a live game: these rules are from the code, not yet checked in a running game." }));
}

/** The inline "Stage restore" step under one checkpoint. It shows the files the restore overwrites. */
function restoreConfirm(cp, hasProfile, save) {
  const withProfile = hasProfile && Boolean(S.confirm.profile);
  return h("div", { class: "hi-confirm" },
    cp.matches_current === false && warning(mismatchText(cp, save)),
    cp.matches_current === null && h("p", { class: "hi-note", text: "No current save was found to compare with." }),
    h("p", { text: `Stage a restore of ${cp.id}? Nothing changes now. The files are copied the next time the game is at the main menu, and then you load your character.` }),
    h("div", { class: "hi-target" },
      h("span", { class: "hi-lbl", text: "This overwrites" }),
      h("code", { class: "hi-path", text: cp.character_file || "unknown file" })),
    withProfile && h("div", { class: "hi-target" },
      h("span", { class: "hi-lbl", text: "and" }),
      h("code", { class: "hi-path", text: profilePathFor(cp.character_file) })),
    h("label", { class: "hi-check" },
      h("input", {
        type: "checkbox",
        checked: S.confirm.profile || null,
        disabled: !hasProfile || null,
        onchange: (e) => { S.confirm.profile = e.target.checked; C.rerender(); },
      }),
      hasProfile ? "Also restore profile.bin" : "Also restore profile.bin (this backup has none)"),
    h("div", { class: "hi-row" },
      h("button", { class: "btn sm primary", disabled: S.busy || null, onclick: () => stageRestore(cp, withProfile) }, "Stage restore"),
      h("button", { class: "btn sm", onclick: () => { S.confirm = null; C.rerender(); } }, "Cancel")));
}

function checkpointRow(cp, save) {
  const files = cp.files || [];
  const hasProfile = files.some((f) => f.kind === "profile");
  const confirming = S.confirm && S.confirm.id === cp.id;
  return h("li", { class: `hi-cp${cp.pending ? " pending" : ""}${cp.matches_current === false ? " mismatch" : ""}`, "data-cp": cp.id },
    h("div", { class: "hi-cp-head" },
      h("b", { text: when(cp.time), title: fullTime(cp.time) }),
      h("span", { class: "hi-badge", text: cp.reason || "backup" }),
      cp.pending && h("span", { class: "hi-badge hot", text: "restore staged" }),
      cp.note && h("span", { class: "hi-cp-note", text: cp.note }),
      h("code", { class: "hi-cp-id", text: cp.id }),
      h("button", {
        class: "btn sm hi-restore",
        disabled: S.busy || null,
        onclick: () => { S.confirm = { id: cp.id, profile: false }; C.rerender(); },
      }, "Restore…")),
    cp.pinned && h("div", { class: "hi-pin" },
      h("span", { class: "hi-badge good", text: "kept" }),
      h("span", { class: "hi-pin-why", text: cp.pinned }),
      h("button", { class: "btn sm", disabled: S.busy || null, onclick: () => unpin(cp) }, "Unpin")),
    h("div", { class: "hi-target" },
      h("span", { class: "hi-lbl", text: "Restoring overwrites" }),
      h("code", { class: "hi-path", text: cp.character_file || "unknown file" })),
    cp.matches_current === false && warning(mismatchText(cp, save)),
    cp.matches_current === null && h("p", { class: "hi-note", text: "No current save was found to compare with." }),
    h("ul", { class: "hi-files" }, files.map((f) => h("li", {}, f.name, h("small", { text: bytes(f.size) })))),
    confirming && restoreConfirm(cp, hasProfile, save));
}

function backups() {
  const d = S.checkpoints;
  if (!d) return S.checkpointsError ? warning(`Save backups can't be read: ${S.checkpointsError}`) : loadingNote();
  const save = d.save || {};
  const cps = d.checkpoints || [];
  const [low, high] = d.retention_range || [1, 200];
  const hasSave = Boolean(save.character_file);
  return h("section", { class: "hi-section" },
    d.pending_restore && stagedBanner(d.pending_restore),
    d.last_restore && lastRestore(d.last_restore),
    h("div", { class: "hi-panel" },
      h("h3", { text: "Your save" }),
      h("dl", { class: "kv" },
        h("dt", { text: "Character save" }), h("dd", { text: save.character_file || "not found" }),
        h("dt", { text: "Where it was found" }), h("dd", { text: SOURCE_WORDS[save.source] || "not found" }),
        h("dt", { text: "Verified" }), h("dd", { text: save.verified === true ? "yes" : save.verified === false ? "no" : "unknown" }),
        h("dt", { text: "Backups folder" }), h("dd", { text: d.backups_dir || "-" }),
        h("dt", { text: "Automatic backup" }), h("dd", { text: autoText(d.auto) }),
        h("dt", { text: "Game at main menu" }), h("dd", { text: yn(d.at_main_menu) })),
      (save.notes || []).map((n) => h("p", { class: "note", text: n })),
      save.source === "guessed" && warning("This save was guessed. Check it's the right character before you restore anything.")),
    h("div", { class: "hi-panel" },
      h("h3", { text: "Back up now" }),
      h("div", { class: "hi-row" },
        h("input", {
          class: "field hi-wide",
          type: "text",
          placeholder: "Note (optional)",
          maxlength: 200,
          "aria-label": "Backup note",
          value: S.note,
          oninput: (e) => { S.note = e.target.value; },
        }),
        h("button", {
          class: "btn primary",
          disabled: !hasSave || S.busy || null,
          title: hasSave ? "" : "No save file was found to back up",
          onclick: backUp,
        }, icon("check", 15), "Back up now")),
      h("div", { class: "hi-row" },
        h("span", { class: "hi-lbl", text: "Keep" }),
        h("input", {
          class: "field hi-num",
          type: "number",
          min: low,
          max: high,
          "aria-label": "Backups to keep",
          value: S.retentionDraft ?? d.retention,
          oninput: (e) => { S.retentionDraft = e.target.value; },
        }),
        h("span", { class: "note", text: `backups (${low}-${high}); the oldest unpinned go when there are more` }),
        h("button", { class: "btn sm", onclick: saveRetention }, "Save"))),
    gatePanel(d),
    h("div", { class: "hi-panel" },
      h("h3", { text: `Backups (${cps.length})` }),
      h("p", { class: "note", text: "A restore is staged, not applied: it takes effect the next time you load the game from the main menu. This has not been verified in a live game yet (unverified)." }),
      cps.length
        ? h("ol", { class: "hi-cps" }, cps.map((cp) => checkpointRow(cp, save)))
        : h("p", { class: "note", text: "No backups yet. Back up now, or one is made before your first change this session." })));
}

// ---------- page ----------

export function render(root, ctx) {
  C = ctx;
  const { fill } = ctx;
  if (!S.history && !S.checkpoints) {
    const error = S.historyError || S.checkpointsError;
    fill(root, error
      ? h("div", { class: "bench" },
        h("div", { class: "banner" }, h("div", {}, h("span", { class: "stamp equipped", text: "Undo and backups" }), h("h1", { text: "History" }))),
        h("div", { class: "bench-body" }, h("div", { class: "hi-body" },
          warning(`${error}. Is the mod running?`),
          h("button", { class: "btn primary", onclick: () => load() }, icon("refresh", 15), "Try again"))))
      : h("div", { class: "empty" }, h("div", {}, h("h2", { text: "Loading..." }))));
    return;
  }
  if (S.confirm && !(S.checkpoints && (S.checkpoints.checkpoints || []).some((c) => c.id === S.confirm.id))) S.confirm = null;

  const tabs = h("div", { class: "seg hi-tabs" }, TABS.map(([id, label]) => h("button", {
    class: S.tab === id ? "on" : "",
    onclick: () => { S.tab = id; S.confirm = null; S.skipConfirm = false; ctx.rerender(); },
  }, label)));
  const body = S.tab === "timeline" ? timeline() : backups();

  fill(root,
    h("div", { class: "bench" },
      banner(),
      h("div", { class: "bench-body" },
        h("div", { class: "hi-body" },
          gateBanner(),
          h("div", { class: "warn-banner" }, icon("info", 18), "Undo puts a value back only when the game still holds what that change left. Changes apply to your live game. Save in game afterwards to keep them."),
          tabs,
          body))),
    ctx.dropdown("historyinfo", "History dev tools", "", h("div", { class: "tools" },
      h("button", { class: "btn sm", onclick: () => ctx.dump("/api/debug/history") }, "Save history dump"))));
}
