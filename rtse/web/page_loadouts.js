// Builds page (page_loadouts.js): save the open build as a preset; import one from a file, a link or pasted JSON;
// dry-run it (the preview shows what applying would change); apply it in chunks and show each step's result.
// Backed by rtse/loadouts.py. Nothing here has been run against a live game, and every result says so.

const LINK_PREFIX = "RTSE-L1:";
const LINK_LIMIT = 62000; // the server accepts import links up to this many characters
const MAX_IMPORT_BYTES = 256 * 1024; // a file bigger than this is refused before it is read
const SECTIONS = ["character", "skills", "gear"];
const SECTION_LABEL = { character: "Character", skills: "Skills", gear: "Gear" };
const SAVE_PARTS = [
  ["equipped", "Equipped gear", true],
  ["backpack", "Backpack gear", false],
  ["skills", "Skills", true],
  ["character", "Level & skill points", true],
  ["currencies", "Currencies", false],
];
const STATUS_TEXT = { ok: "ok", failed: "failed", rolled_back: "rolled back", rollback_failed: "rollback failed", not_run: "not run" };
const CHUNK_TEXT = { applied: "applied", rolled_back: "rolled back", kept_partial: "kept, partly applied" };
// a part the game finds outside the item's own part list: say where, and that it is unverified
const MODE_TEXT = {
  same_slot: ["same slot", "Not in this item's own part list. RTSE takes it from the same slot. Unverified in game."],
  everything: ["any slot", "Not in the item's list or its slot. RTSE takes it from any slot. Unverified in game."],
};

const state = {
  list: null, // GET /api/loadouts
  error: null,
  busy: false,
  save: { name: "", include: Object.fromEntries(SAVE_PARTS.map(([key, , on]) => [key, on])) },
  imp: { text: "", rename: "", file: null }, // file: { name, size, text } once a .json is chosen
  preview: null, // see openPreview()
  run: null, // while an apply runs: { name, reply } where reply is the last one the server sent
  report: null, // the last finished apply, with its preset name
  notes: null, // { title, lines }: the warnings from the last save or import
  dialog: null, // the in-page confirm; see ask()
  shownLink: null, // { name, link } when the browser refused the clipboard
  scrollTo: null, // a selector to bring into view on the next render
};
let current = null; // the last ctx we were rendered with, for load() calls without one

// ---------- data ----------

/** Fetches the saved builds, then asks the host to re-render. Safe to call before render(). */
export async function load(ctx = current) {
  if (!ctx) return;
  current = ctx;
  try {
    state.list = await ctx.api("GET", "/api/loadouts");
    state.error = null;
  } catch (error) {
    state.list = null;
    state.error = error.message;
  }
  ctx.rerender();
}

// ---------- small helpers ----------

const cap = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : "No class");
const short = (text) => String(text).slice(String(text).lastIndexOf(".") + 1);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const enc = encodeURIComponent;
const count = (n) => n.toLocaleString();

/** A value for the step table: numbers with separators, paths shortened to their last part. */
function show(value) {
  if (value === null || value === undefined) return "none";
  if (typeof value === "number") return value.toLocaleString();
  if (typeof value === "string") return short(value);
  return JSON.stringify(value);
}

function downloadJson(name, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `${name.replace(/[^A-Za-z0-9 _.-]+/g, "_").trim() || "build"}.json`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

function base64Url(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** The RTSE-L1 link for a preset, deflated in this browser so it fits the server's 64 KB POST limit. */
async function linkFor(preset) {
  const raw = new TextEncoder().encode(JSON.stringify(preset));
  const packed = new Response(new Blob([raw]).stream().pipeThrough(new CompressionStream("deflate")));
  return LINK_PREFIX + base64Url(new Uint8Array(await packed.arrayBuffer()));
}

/** The message for a link (or JSON text) longer than the server takes, or null when it fits. */
function tooBig(what, length) {
  return length > LINK_LIMIT
    ? `This build is too big to import: its ${what} is ${count(length)} characters; the limit is ${count(LINK_LIMIT)}.`
    : null;
}

/** What the import POST carries: the text as a link, or the preset itself when this browser cannot make one. */
async function importBody(text) {
  if (text.startsWith(LINK_PREFIX)) {
    const problem = tooBig("link", text.length);
    if (problem) throw new Error(problem);
    return { link: text };
  }
  let preset;
  try {
    preset = JSON.parse(text);
  } catch {
    throw new Error("That is neither an RTSE-L1 link nor valid JSON");
  }
  if (!globalThis.CompressionStream) {
    const problem = tooBig("JSON", text.length);
    if (problem) throw new Error(problem);
    return { preset };
  }
  const link = await linkFor(preset);
  const problem = tooBig("link", link.length);
  if (problem) throw new Error(problem);
  return { link };
}

/**
 * An in-page confirm: resolves true for the yes button, false for cancel or a click on the dimmed page.
 * (A native confirm() box cannot list the warnings, so the page draws its own.)
 */
function ask(ctx, { title, text, items = [], after = "", yes, no = "Cancel" }) {
  return new Promise((resolve) => {
    state.dialog = {
      title, text, items, after, yes, no,
      resolve: (answer) => { state.dialog = null; ctx.rerender(); resolve(answer); },
    };
    ctx.rerender();
  });
}

/**
 * POSTs a save or import. If the name is taken, asks before replacing it. If the server holds back a partial
 * build (saved: false), asks whether to save it anyway, marked partial. Returns the reply, or null when refused.
 */
async function postAsking(ctx, path, body, replaceText) {
  if (state.busy) return null;
  state.busy = true;
  try {
    let send = { ...body };
    for (let round = 0; round < 3; round += 1) {
      try {
        const reply = await ctx.api("POST", path, send);
        if (reply.saved === false) {
          const yes = await ask(ctx, {
            title: "Partial build", text: "Some of the build couldn't be read:", items: reply.warnings || [],
            after: "Save it anyway, marked partial?", yes: "Save anyway",
          });
          if (!yes) return null;
          send = { ...send, allow_partial: true };
          continue;
        }
        return reply;
      } catch (error) {
        if (!/already exists/i.test(error.message)) throw error;
        const yes = await ask(ctx, { title: "Replace build?", text: replaceText, yes: "Replace" });
        if (!yes) return null;
        send = { ...send, overwrite: true };
      }
    }
    throw new Error("The build could not be saved: try again");
  } catch (error) {
    ctx.toast(error.message, true);
    return null;
  } finally {
    state.busy = false;
  }
}

// ---------- save / import / export / list actions ----------

async function savePreset(ctx) {
  const name = state.save.name.trim();
  if (!name) return ctx.toast("Give the build a name first", true);
  const reply = await postAsking(ctx, "/api/loadouts/save", { name, include: { ...state.save.include } },
    `A build named "${name}" already exists. Replace it with the current build?`);
  if (!reply) return;
  state.list = { ...state.list, presets: reply.presets };
  state.save.name = "";
  const warnings = reply.warnings || [];
  state.notes = warnings.length ? { title: `Saved "${reply.preset.name}" with warnings`, lines: warnings } : null;
  ctx.toast(reply.preset.partial ? `Saved "${reply.preset.name}" as partial` : `Saved "${reply.preset.name}"`);
  ctx.rerender();
}

async function chooseFile(ctx, input) {
  const file = input.files && input.files[0];
  if (!file) return;
  if (file.size > MAX_IMPORT_BYTES) {
    input.value = "";
    return ctx.toast(`That file is ${Math.ceil(file.size / 1024)} KB; the limit is 256 KB`, true);
  }
  state.imp.file = { name: file.name, size: file.size, text: await file.text() };
  ctx.rerender();
}

async function importPreset(ctx) {
  if (state.busy) return;
  const source = state.imp.file ? state.imp.file.text : state.imp.text;
  const text = source.trim();
  if (!text) return ctx.toast("Choose a file, or paste a link or JSON", true);
  if (text.length > MAX_IMPORT_BYTES) return ctx.toast("Too big: the limit is 256 KB", true);
  let body;
  try {
    body = await importBody(text);
  } catch (error) {
    return ctx.toast(error.message, true);
  }
  if (state.imp.rename.trim()) body.rename = state.imp.rename.trim();
  const reply = await postAsking(ctx, "/api/loadouts/import", body, "A build with that name already exists. Replace it?");
  if (!reply) return;
  state.list = { ...state.list, presets: reply.presets };
  state.imp = { text: "", rename: "", file: null };
  const warnings = reply.warnings || [];
  state.notes = warnings.length ? { title: `Imported "${reply.preset.name}" with warnings`, lines: warnings } : null;
  ctx.toast(`Imported "${reply.preset.name}"`);
  ctx.rerender();
}

async function exportCharacter(ctx) {
  const reply = await ctx.guarded(() => ctx.api("GET", "/api/loadouts/export_character"));
  if (!reply) return;
  const warnings = reply.warnings || [];
  if (warnings.length > 0) {
    const yes = await ask(ctx, {
      title: "Partial export", text: "This export is partial:", items: warnings, after: "Download it anyway?", yes: "Download",
    });
    if (!yes) return;
  }
  downloadJson(warnings.length > 0 ? `${reply.preset.name}-partial` : reply.preset.name, reply.preset);
  ctx.toast(`Downloaded ${reply.preset.name} (not saved in Builds)`);
}

async function downloadPreset(ctx, name) {
  const reply = await ctx.guarded(() => ctx.api("GET", `/api/loadouts/get?name=${enc(name)}`));
  if (reply) downloadJson(name, reply.preset);
}

async function copyLink(ctx, name) {
  const reply = await ctx.guarded(() => ctx.api("GET", `/api/loadouts/link?name=${enc(name)}`));
  if (!reply) return;
  try {
    await navigator.clipboard.writeText(reply.link);
    state.shownLink = null;
    ctx.toast(`Copied the link for "${name}"`);
  } catch {
    state.shownLink = { name, link: reply.link };
    ctx.toast("The browser blocked the clipboard: copy the link from the box", true);
  }
  ctx.rerender();
}

async function deletePreset(ctx, name) {
  const yes = await ask(ctx, { title: "Delete build?", text: `Delete the saved build "${name}"? This cannot be undone.`, yes: "Delete" });
  if (!yes) return;
  const reply = await ctx.guarded(() => ctx.api("POST", "/api/loadouts/delete", { name }));
  if (!reply) return;
  state.list = { ...state.list, presets: reply.presets };
  if (state.preview && state.preview.name === name) state.preview = null;
  if (state.shownLink && state.shownLink.name === name) state.shownLink = null;
  ctx.toast(`Deleted "${name}"`);
  ctx.rerender();
}

// ---------- preview (dry run) ----------

/** The preview state: the preset, the options the user chose, and the last plan the server returned. */
function openPreview(ctx, name) {
  state.preview = { name, preset: null, available: null, sections: null, overMax: false, skip: [], onFailure: "rollback", plan: null, error: null, loading: true };
  state.scrollTo = "#lo-preview";
  return fetchPreset(ctx).then(() => replan(ctx));
}

async function fetchPreset(ctx) {
  const p = state.preview;
  try {
    p.preset = (await ctx.api("GET", `/api/loadouts/get?name=${enc(p.name)}`)).preset;
  } catch (error) {
    p.error = error.message;
  }
}

/** Asks the server for a fresh plan with the current options. */
async function replan(ctx) {
  const p = state.preview;
  if (!p) return;
  p.loading = true;
  if (p.sections && p.sections.length === 0) {
    p.plan = null;
    p.error = null;
    p.loading = false;
    return ctx.rerender();
  }
  ctx.rerender();
  const query = new URLSearchParams({ name: p.name });
  if (p.sections) query.set("sections", p.sections.join(","));
  if (p.overMax) query.set("over_max", "1");
  if (p.skip.length) query.set("skip", p.skip.join(","));
  let plan = null;
  let error = null;
  try {
    plan = await ctx.api("GET", `/api/loadouts/plan?${query}`);
  } catch (e) {
    error = e.message;
  }
  if (state.preview !== p) return; // closed or replaced while planning
  p.loading = false;
  p.plan = plan;
  p.error = error;
  if (plan && !p.available) {
    p.available = plan.sections;
    p.sections = [...plan.sections];
  }
  ctx.rerender();
}

function setSection(ctx, key, on) {
  const p = state.preview;
  const chosen = new Set(p.sections || []);
  if (on) chosen.add(key);
  else chosen.delete(key);
  p.sections = SECTIONS.filter((s) => chosen.has(s));
  replan(ctx);
}

function toggleSkip(ctx, index, include) {
  const p = state.preview;
  p.skip = include ? p.skip.filter((i) => i !== index) : [...p.skip, index].sort((a, b) => a - b);
  replan(ctx);
}

/**
 * Applies the planned build. The server runs it in chunks: the first request starts the run and runs chunk 1, and
 * each request after it (with the run's id and cursor) runs the next chunk, until the reply says the run is over.
 */
async function applyPreview(ctx) {
  const p = state.preview;
  if (!p || !p.plan || state.busy) return;
  state.busy = true;
  state.report = null;
  state.run = { name: p.name, reply: null };
  state.scrollTo = "#lo-run";
  ctx.rerender();
  let reply = null;
  let error = null;
  let notice = null;
  try {
    reply = await ctx.api("POST", "/api/loadouts/apply", {
      name: p.name, plan_id: p.plan.plan_id, sections: p.sections, over_max: p.overMax, skip: p.skip, on_failure: p.onFailure,
    });
  } catch (e) {
    error = e.message;
  }
  while (!error && reply.state === "running") {
    state.run.reply = reply;
    ctx.rerender();
    try {
      reply = await ctx.api("POST", "/api/loadouts/apply", { run_id: reply.run_id, cursor: reply.cursor });
    } catch (e) {
      // the request failed, so the server may or may not have run the chunk: read the run back before going on
      const found = await ctx.api("GET", `/api/loadouts/run?run_id=${enc(reply.run_id)}`).catch(() => null);
      if (!found) {
        error = `${e.message}. The result is unknown: check the save in game. If it looks wrong, save the debug dump from Build dev tools.`;
        break;
      }
      notice = `The request failed mid-run (${e.message}). The run below is what the server holds now.`;
      reply = found;
    }
  }
  state.busy = false;
  state.run = null;
  if (error) {
    ctx.toast(error, true);
    if (/changed since the preview/i.test(error)) {
      ctx.toast("Re-planned against the game as it is now. Check the steps, then apply again.");
      return replan(ctx);
    }
    p.error = error;
    return ctx.rerender();
  }
  state.report = { name: p.name, ...reply, notice };
  state.scrollTo = "#lo-report";
  ctx.toast(reportToast(reply).text, !reply.ok);
  await replan(ctx); // what the game holds now is the new starting point
}

function reportToast(r) {
  if (r.ok) return { text: `Applied ${plural(r.summary.ok, "step")}. Not tested in a live game.` };
  const failed = r.steps.find((s) => s.status === "failed");
  const where = failed ? `Step ${failed.n} failed` : "A step failed";
  const rest = r.rolled_back ? "the steps in that chunk were rolled back" : "the steps that worked were kept";
  return { text: `${where}: ${rest}.${r.summary.rollback_failed ? " Reload the save before playing." : ""}` };
}

async function importMissing(ctx, name) {
  const reply = await ctx.guarded(() => ctx.api("POST", "/api/loadouts/import_missing", { name }));
  if (!reply) return;
  const failed = reply.results.filter((r) => !r.ok);
  ctx.toast(failed.length ? `${failed.length} item(s) not imported: ${failed[0].error || "unknown error"}` : "Imported the missing items", failed.length > 0);
  replan(ctx);
}

// ---------- building blocks ----------

function saveCard(ctx) {
  const { h, icon } = ctx;
  const s = state.save;
  return h("section", { class: "lo-card" },
    h("h2", { class: "lo-h", text: "Save current build" }),
    h("p", { class: "note", text: "Captures the gear, skills and level of the open save. Nothing is applied." }),
    h("label", { class: "lo-field" },
      h("span", { class: "lo-label", text: "Name" }),
      h("input", {
        class: "field lo-name", type: "text", maxlength: 64, placeholder: "e.g. Siren crit build", value: s.name, "aria-label": "Build name",
        oninput: (e) => { s.name = e.target.value; },
        onkeydown: (e) => { if (e.key === "Enter") savePreset(ctx); },
      })),
    h("div", { class: "lo-checks" }, SAVE_PARTS.map(([key, label]) => h("label", { class: "lo-check" },
      h("input", { type: "checkbox", checked: s.include[key] || null, onchange: (e) => { s.include[key] = e.target.checked; } }),
      h("span", { text: label })))),
    h("div", { class: "tools" },
      h("button", { class: "btn primary", disabled: state.busy || null, onclick: () => savePreset(ctx) }, icon("check", 15), "Save build"),
      h("button", {
        class: "btn ghost", title: "Downloads every equipped and backpack item, the skills and every character value as one .json. Not saved in Builds.",
        onclick: () => exportCharacter(ctx),
      }, "Export whole character")));
}

function importCard(ctx) {
  const { h, icon } = ctx;
  const imp = state.imp;
  return h("section", { class: "lo-card" },
    h("h2", { class: "lo-h", text: "Import" }),
    h("p", { class: "note", text: `A .json preset, an RTSE-L1 link, or the JSON itself. Links and JSON up to ${count(LINK_LIMIT)} characters. Pasted JSON is turned into a link here, so large builds still fit the server's limit.` }),
    imp.file
      ? h("div", { class: "lo-file" },
        h("span", { text: `${imp.file.name} (${Math.ceil(imp.file.size / 1024)} KB)` }),
        h("button", { class: "btn sm ghost", onclick: () => { imp.file = null; ctx.rerender(); } }, "Clear file"))
      : h("label", { class: "lo-field" },
        h("span", { class: "lo-label", text: "JSON file" }),
        h("input", { type: "file", accept: ".json,application/json", onchange: (e) => chooseFile(ctx, e.target) })),
    h("label", { class: "lo-field" },
      h("span", { class: "lo-label", text: "Or paste a link or JSON" }),
      h("textarea", {
        class: "field lo-paste", rows: 4, spellcheck: "false", "aria-label": "Paste a link or preset JSON", text: imp.text,
        oninput: (e) => { imp.text = e.target.value; },
      })),
    h("label", { class: "lo-field" },
      h("span", { class: "lo-label", text: "Save as (optional new name)" }),
      h("input", {
        class: "field lo-name", type: "text", maxlength: 64, value: imp.rename, "aria-label": "New name",
        oninput: (e) => { imp.rename = e.target.value; },
      })),
    h("div", { class: "tools" },
      h("button", { class: "btn primary", disabled: state.busy || null, onclick: () => importPreset(ctx) }, icon("plus", 15), "Import")));
}

function presetRow(ctx, p, currentClass) {
  const { h, icon } = ctx;
  const c = p.counts || {};
  const other = Boolean(p.class_key && currentClass && p.class_key !== currentClass);
  const shown = state.shownLink && state.shownLink.name === p.name ? state.shownLink.link : null;
  const leftOut = p.left_out || [];
  const facts = [
    c.gear !== undefined && `${c.gear} gear (${c.equipped ?? 0} equipped, ${c.backpack ?? 0} backpack)`,
    c.skills !== undefined && plural(c.skills, "skill"),
    c.character && (typeof c.character === "number" ? plural(c.character, "character value") : "character values"),
  ].filter(Boolean);
  const created = typeof p.created === "string" ? p.created.slice(0, 10) : "";
  return h("li", { class: `lo-row${other ? " other" : ""}` },
    h("div", { class: "lo-main" },
      h("div", { class: "lo-name" }, p.name,
        p.kind === "character" && h("span", { class: "lo-tag", text: "whole character" }),
        p.partial && h("span", { class: "lo-tag warn", title: leftOut.join("\n"), text: "partial" }),
        other && h("span", { class: "lo-tag warn", text: "other class" })),
      h("div", { class: "lo-meta", text: [p.class_key ? cap(p.class_key) : "No class", created && `saved ${created}`].filter(Boolean).join(" · ") }),
      p.partial && h("div", { class: "lo-why", text: `Left out: ${leftOut.join("; ")}` }),
      h("div", { class: "lo-chips" }, facts.map((text) => h("span", { class: "lo-chip", text })))),
    h("div", { class: "lo-row-actions" },
      h("button", { class: "btn sm primary", title: "Dry run: what applying this build would change", onclick: () => openPreview(ctx, p.name) }, icon("search", 13), "Preview"),
      h("button", { class: "btn sm", onclick: () => downloadPreset(ctx, p.name) }, "Download JSON"),
      h("button", { class: "btn sm", onclick: () => copyLink(ctx, p.name) }, "Copy link"),
      h("button", { class: "btn sm danger", onclick: () => deletePreset(ctx, p.name) }, icon("trash", 13), "Delete")),
    shown && h("textarea", { class: "field lo-link", readonly: true, rows: 2, "aria-label": `Link for ${p.name}`, text: shown }));
}

function listCard(ctx, d) {
  const { h } = ctx;
  const presets = d.presets;
  return h("section", { class: "lo-card lo-wide" },
    h("div", { class: "lo-head" },
      h("h2", { class: "lo-h", text: "Saved builds" }),
      presets.length > 0 && h("span", { class: "note", text: `${plural(presets.length, "build")}. Preview shows what applying one would change.` })),
    presets.length === 0
      ? h("p", { class: "note", text: "No saved builds yet. Save the open build on the left, or import one." })
      : h("ul", { class: "lo-list" }, presets.map((p) => presetRow(ctx, p, d.class_key))));
}

function noticeBanner(ctx, notes) {
  const { h, icon } = ctx;
  return h("section", { class: "lo-notice", role: "status" },
    icon("info", 18),
    h("div", { class: "lo-notice-body" },
      h("b", { text: notes.title }),
      h("ul", {}, notes.lines.map((line) => h("li", { text: line })))),
    h("button", { class: "btn sm ghost", onclick: () => { state.notes = null; ctx.rerender(); } }, "Dismiss"));
}

function previewOptions(ctx, p) {
  const { h } = ctx;
  const selected = p.sections || [];
  const gear = p.preset ? p.preset.gear || [] : [];
  return h("div", { class: "lo-opts" },
    h("div", { class: "lo-opt" },
      h("div", { class: "lo-opt-title", text: "Sections" }),
      SECTIONS.map((key) => {
        const has = p.available.includes(key);
        return h("label", { class: `lo-check${has ? "" : " dim"}` },
          h("input", { type: "checkbox", checked: selected.includes(key) || null, disabled: !has || null, onchange: (e) => setSection(ctx, key, e.target.checked) }),
          h("span", { text: SECTION_LABEL[key] }),
          !has && h("em", { text: "not in preset" }));
      })),
    h("div", { class: "lo-opt" },
      h("div", { class: "lo-opt-title", text: "Skills" }),
      h("label", { class: "lo-check" },
        h("input", { type: "checkbox", checked: p.overMax || null, onchange: (e) => { p.overMax = e.target.checked; replan(ctx); } }),
        h("span", { text: "Allow above max" })),
      h("p", { class: "lo-hint", text: "Lets a skill go above its normal maximum. Off by default." })),
    h("div", { class: "lo-opt" },
      h("div", { class: "lo-opt-title", text: "If a step fails" }),
      [["rollback", "Undo the chunk that failed"], ["keep", "Keep the steps that worked"]].map(([value, label]) =>
        h("label", { class: "lo-check" },
          h("input", { type: "radio", name: "lo-on-failure", value, checked: p.onFailure === value || null, onchange: () => { p.onFailure = value; } }),
          h("span", { text: label })))),
    gear.length > 0 && h("div", { class: "lo-opt lo-wide" },
      h("div", { class: "lo-opt-title", text: "Gear to apply (untick to skip)" }),
      gear.map((entry, index) => h("label", { class: `lo-check${selected.includes("gear") ? "" : " dim"}` },
        h("input", {
          type: "checkbox", checked: !p.skip.includes(index) || null, disabled: !selected.includes("gear") || null,
          onchange: (e) => toggleSkip(ctx, index, e.target.checked),
        }),
        h("span", { text: entry.name || short(entry.class) }),
        h("em", { text: `${entry.kind} · ${entry.location} · L${entry.level}` })))));
}

function classBanner(ctx, info) {
  const { h } = ctx;
  const { preset, current: now, match } = info;
  if (match === true) return h("div", { class: "lo-class good", text: `Same class: ${cap(now)}.` });
  if (match === false) {
    return h("div", { class: "lo-class bad", text: `This build is for ${cap(preset)}, but your save is ${cap(now)}. Its skills are blocked; gear and character still apply.` });
  }
  return h("div", { class: "lo-class unknown", text: `Class not checked: the build is for ${preset ? cap(preset) : "no class"}, and the open save is ${now ? cap(now) : "not loaded"}.` });
}

function modeBadge(ctx, mode) {
  const info = MODE_TEXT[mode];
  return info ? ctx.h("span", { class: "lo-mode", title: info[1], text: info[0] }) : null;
}

function stepsTable(ctx, steps) {
  const { h } = ctx;
  return h("div", { class: "lo-tablewrap" },
    h("table", { class: "lo-table" },
      h("thead", {}, h("tr", {}, h("th", { text: "#" }), h("th", { text: "Step" }), h("th", { text: "Now" }), h("th", { text: "Becomes" }))),
      h("tbody", {}, steps.map((step) => h("tr", {},
        h("td", { class: "lo-num", text: String(step.n) }),
        h("td", {},
          step.label, modeBadge(ctx, step.params && step.params.mode),
          step.side_effect && h("div", { class: "lo-side", text: step.side_effect })),
        h("td", { class: "lo-mono", text: show(step.before) }),
        h("td", { class: "lo-mono", text: show(step.after) }))))));
}

function foldBlock(ctx, rows) {
  const { h } = ctx;
  return h("details", { class: "lo-fold" },
    h("summary", { text: `Unchanged (${rows.length})` }),
    h("ul", {}, rows.map((row) => h("li", {}, h("b", { text: row.label }), h("span", { class: "lo-why", text: row.reason })))));
}

function missingBlock(ctx, rows, name) {
  const { h, icon } = ctx;
  const canImport = rows.some((m) => m.can_import);
  return h("div", { class: "lo-block" },
    h("h3", { class: "lo-sub", text: `Missing from the game (${rows.length})` }),
    h("p", { class: "note", text: "These items are not in your inventory, and RTSE does not create items. Their steps are left out." }),
    h("ul", { class: "lo-rows" }, rows.map((m) => h("li", { class: "lo-row-flat" },
      h("div", {}, h("b", { text: m.name }), h("span", { class: "lo-meta", text: ` ${m.kind} · ${m.location} · L${m.level}` }))))),
    h("div", { class: "tools" },
      h("button", {
        class: "btn sm", disabled: !canImport || null, title: canImport ? "" : "Coming with item codes",
        onclick: () => importMissing(ctx, name),
      }, icon("plus", 13), "Import from item code"),
      !canImport && h("span", { class: "lo-why", text: "Coming with item codes: not available yet." })));
}

function blockedBlock(ctx, rows) {
  const { h } = ctx;
  return h("div", { class: "lo-block" },
    h("h3", { class: "lo-sub", text: `Can't apply (${rows.length})` }),
    h("ul", { class: "lo-rows" }, rows.map((b) => h("li", { class: "lo-row-flat" },
      h("div", {}, h("b", { text: b.label }), h("span", { class: "lo-tag", text: b.kind })),
      h("div", { class: "lo-why", text: b.reason })))));
}

function planView(ctx, p, plan) {
  const { h, icon } = ctx;
  const s = plan.summary;
  const steps = plan.steps;
  return h("div", { class: `lo-plan${p.loading ? " stale" : ""}` },
    classBanner(ctx, plan.class),
    h("div", { class: "lo-chips" },
      h("span", { class: "lo-chip" }, "changes ", h("b", { text: String(s.changes) })),
      h("span", { class: "lo-chip" }, "unchanged ", h("b", { text: String(s.unchanged) })),
      h("span", { class: "lo-chip" }, "missing ", h("b", { text: String(s.missing) })),
      h("span", { class: "lo-chip" }, "can't apply ", h("b", { text: String(s.blocked) }))),
    steps.length > 0
      ? stepsTable(ctx, steps)
      : h("div", { class: "note", text: "Nothing to change: the save already matches this build, or every step is blocked or skipped." }),
    plan.unchanged.length > 0 && foldBlock(ctx, plan.unchanged),
    plan.missing.length > 0 && missingBlock(ctx, plan.missing, p.name),
    plan.blocked.length > 0 && blockedBlock(ctx, plan.blocked),
    h("div", { class: "lo-apply" },
      h("button", {
        class: "btn primary lo-big", disabled: !steps.length || state.busy || null, onclick: () => applyPreview(ctx),
      }, icon("check", 16), steps.length ? `Apply ${plural(steps.length, "step")}` : "Nothing to apply"),
      h("span", { class: "note", text: plan.unverified })));
}

function previewCard(ctx) {
  const { h, icon } = ctx;
  const p = state.preview;
  return h("section", { class: "lo-card lo-preview", id: "lo-preview" },
    h("div", { class: "lo-head" },
      h("h2", { class: "lo-h", text: `Preview: ${p.name}` }),
      h("button", { class: "btn sm ghost", onclick: () => { state.preview = null; ctx.rerender(); } }, "Close")),
    h("p", { class: "note", text: "A dry run: nothing is written until you press Apply. Changing an option re-plans." }),
    p.error && h("div", { class: "warn-banner" }, icon("alert", 18), p.error),
    p.available && previewOptions(ctx, p),
    p.loading && h("div", { class: "note", text: "Planning..." }),
    p.plan && planView(ctx, p, p.plan),
    !p.loading && !p.plan && !p.error && p.sections && p.sections.length === 0 && h("div", { class: "note", text: "Tick a section to plan it." }));
}

function runCard(ctx) {
  const { h } = ctx;
  const run = state.run;
  const r = run.reply;
  const total = r ? r.total : 0;
  const cursor = r ? r.cursor : 0;
  const pct = total ? Math.round((cursor / total) * 100) : 0;
  return h("section", { class: "lo-card lo-wide lo-run", id: "lo-run", "aria-live": "polite" },
    h("div", { class: "lo-head" }, h("h2", { class: "lo-h", text: `Applying: ${run.name}` })),
    h("p", { class: "note", text: "The game is changed one chunk of up to 4 steps at a time. The result is final when the run ends." }),
    h("div", { class: "lo-bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(total), "aria-valuenow": String(cursor) },
      h("i", { style: { width: `${pct}%` } })),
    h("div", { class: "lo-run-text", text: r ? `Applying step ${cursor} of ${total} (chunk ${r.chunk})` : "Starting..." }),
    r && r.chunks.length > 0 && chunkList(ctx, r.chunks));
}

/** One pill per chunk the run has finished: applied, rolled back, or kept partly applied. */
function chunkList(ctx, chunks) {
  const { h } = ctx;
  return h("div", { class: "lo-chips lo-chunks" }, chunks.map((c) => h("span", { class: `lo-pill ${c.status}`, text: `Chunk ${c.chunk}: steps ${c.from}-${c.to}, ${CHUNK_TEXT[c.status] || c.status}` })));
}

function reportCard(ctx) {
  const { h, icon } = ctx;
  const r = state.report;
  const failed = r.steps.find((s) => s.status === "failed");
  const fallback = r.ok
    ? `All ${plural(r.summary.ok, "step")} ran and read back. Not tested in a live game: check the save in game.`
    : `${failed ? `Step ${failed.n} failed` : "A step failed"}. ${r.rolled_back ? "The steps that worked were rolled back" : "The steps that worked were kept"}, and the steps after it did not run.`;
  const earlier = !r.ok && r.chunks.some((c) => c.status === "applied");
  // The History pointer only when the server found the earlier chunks in the journal; otherwise the table is the record.
  const hint = !earlier ? null : r.history === true
    ? "Earlier chunks stay applied and History lists them: you can undo them there."
    : "Earlier chunks stay applied. History doesn't list them, so the Before and After columns are the record of what changed.";
  return h("section", { class: `lo-card lo-report${r.ok ? "" : " bad"}`, id: "lo-report" },
    h("div", { class: "lo-head" }, h("h2", { class: "lo-h", text: `Result: ${r.name}` })),
    r.notice && h("div", { class: "warn-banner" }, icon("alert", 18), r.notice),
    h("p", { class: "lo-msg", text: r.message || fallback }),
    hint && h("p", { class: "lo-earlier", text: hint }),
    r.summary.rollback_failed > 0 && h("div", { class: "warn-banner" }, icon("alert", 18),
      "An undo step failed. Reload the save before playing: the game may not match the build."),
    r.chunks.length > 0 && chunkList(ctx, r.chunks),
    h("div", { class: "lo-chips" }, Object.keys(STATUS_TEXT).map((key) => h("span", { class: "lo-chip" }, STATUS_TEXT[key], h("b", { text: String(r.summary[key]) })))),
    h("div", { class: "lo-tablewrap" },
      h("table", { class: "lo-table" },
        h("thead", {}, h("tr", {}, h("th", { text: "#" }), h("th", { text: "Step" }), h("th", { text: "Before" }), h("th", { text: "After" }), h("th", { text: "Result" }), h("th", { text: "Detail" }))),
        h("tbody", {}, r.steps.map((s) => h("tr", {},
          h("td", { class: "lo-num", text: String(s.n) }),
          h("td", { text: s.label }),
          h("td", { class: "lo-mono", text: show(s.before) }),
          // after = the game's readback once the step ran; empty for steps that never ran
          h("td", { class: "lo-mono", text: s.status === "ok" || s.status === "rolled_back" ? show(s.after) : "" }),
          h("td", {}, h("span", { class: `lo-pill ${s.status}`, text: STATUS_TEXT[s.status] || s.status })),
          h("td", { class: "lo-why", text: s.error || "" })))))));
}

function dialogView(ctx) {
  const { h } = ctx;
  const d = state.dialog;
  return h("div", { class: "lo-dialog-wrap" },
    h("div", { class: "scrim", onclick: () => d.resolve(false) }),
    h("section", { class: "lo-dialog", role: "alertdialog", "aria-modal": "true", "aria-labelledby": "lo-dialog-title" },
      h("h2", { class: "lo-h", id: "lo-dialog-title", text: d.title }),
      h("p", { class: "lo-dialog-text", text: d.text }),
      d.items.length > 0 && h("ul", { class: "lo-dialog-list" }, d.items.map((line) => h("li", { text: line }))),
      d.after && h("p", { class: "lo-dialog-text", text: d.after }),
      h("div", { class: "tools" },
        h("button", { class: "btn ghost", onclick: () => d.resolve(false) }, d.no),
        h("button", { class: "btn primary", onclick: () => d.resolve(true) }, d.yes))));
}

// ---------- page ----------

export function render(root, ctx) {
  const { h, fill, icon } = ctx;
  current = ctx;
  const d = state.list;
  const presets = d ? d.presets : [];
  const warn = (text) => h("div", { class: "warn-banner" }, icon("alert", 18), text);
  fill(
    root,
    h("div", { class: "bench" },
      h("div", { class: "banner" },
        h("div", {},
          h("span", { class: "stamp backpack", text: "Build presets" }),
          h("h1", { text: "Builds" }),
          d && h("div", { class: "facts" },
            h("span", {}, "Your class ", h("b", { text: d.class_key ? cap(d.class_key) : "not in a save" })),
            h("span", {}, "Saved ", h("b", { text: String(presets.length) })))),
        h("div", { class: "level" },
          h("button", { class: "btn ghost", title: "Re-read the saved builds", "aria-label": "Refresh", onclick: () => load(ctx) }, icon("refresh", 15)))),
      h("div", { class: "bench-body lo-body" },
        h("div", { class: "warn-banner lo-warn" }, icon("info", 18),
          h("span", { text: "Applying presets is untested in a live game. Every change goes through the editor's own checks; the preview shows what will change." }),
          h("span", { class: "unv", title: "Not checked against a running game", text: "unverified" })),
        state.error && warn(state.error),
        !d && !state.error && h("div", { class: "note", text: "Loading builds..." }),
        d && d.warnings.map((w) => warn(w)),
        state.notes && noticeBanner(ctx, state.notes),
        d && h("div", { class: "lo-grid" }, saveCard(ctx), importCard(ctx)),
        d && listCard(ctx, d),
        state.run && runCard(ctx),
        state.preview && previewCard(ctx),
        state.report && reportCard(ctx))),
    ctx.dropdown("loadoutsdev", "Build dev tools", "", h("div", {},
      h("div", { class: "tools" }, h("button", { class: "btn sm", onclick: () => ctx.dump("/api/debug/loadouts") }, "Save build dump")))),
    state.dialog && dialogView(ctx),
  );
  // scroll once the plan has settled: a re-plan renders twice, and the first draw would move the view
  if (state.scrollTo && !(state.preview && state.preview.loading)) {
    const target = root.querySelector(state.scrollTo);
    state.scrollTo = null;
    if (target) target.scrollIntoView({ block: "start" });
  }
}
