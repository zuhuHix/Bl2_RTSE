from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:80]!r} (found {text.count(old)})"
    return text.replace(old, new)


# ---------------- html ----------------
html = Path("rtse/web/index.html")
t = html.read_text(encoding="utf-8")
t = replace_once(t, '      <div class="search">', '      <div id="pinned" class="pinned"></div>\n      <div class="search">')
html.write_text(t, encoding="utf-8")

# ---------------- js ----------------
js = Path("rtse/web/app.js")
s = js.read_text(encoding="utf-8")

s = replace_once(s, "  picker: null,\n", "  picker: null,\n  page: \"gear\", // \"gear\" (item editor) or \"character\"\n  character: null,\n")

s = replace_once(
    s,
    "async function selectItem(id) {\n  state.selected = id;",
    "async function selectItem(id) {\n  state.page = \"gear\";\n  renderPinned();\n  state.selected = id;",
)

s = replace_once(
    s,
    "function renderWorkspace() {\n  const root = $(\"#workspace\");\n  const detail = state.detail;\n",
    "function renderWorkspace() {\n  const root = $(\"#workspace\");\n  if (state.page === \"character\") return renderCharacter(root);\n  const detail = state.detail;\n",
)

s = replace_once(
    s,
    "// ---------- edits ----------\n",
    '''// ---------- character ----------

const CURRENCY_STYLE = {
  money: { color: "#6fd36f", glyph: "$", step: 1_000_000 },
  eridium: { color: "#b27cff", glyph: "E", step: 100 },
  seraph: { color: "#ff7ab8", glyph: "S", step: 100 },
  torgue: { color: "#ff6a2b", glyph: "T", step: 100 },
};

function renderPinned() {
  const c = state.character;
  fill($("#pinned"), h(
    "button",
    { class: `pin${state.page === "character" ? " on" : ""}`, onclick: openCharacter },
    h("span", { class: "pin-ic" }, icon("crosshair", 20)),
    h("div", {}, h("div", { class: "t", text: "Character" }),
      h("div", { class: "s", text: c && c.name ? c.name : "Level, skill points, money" })),
    c && c.level != null && h("div", { class: "lvl" }, h("small", { text: "LV" }), String(c.level))));
}

async function openCharacter() {
  state.page = "character";
  closePicker();
  renderPinned();
  renderWorkspace();
  await loadCharacter();
}

async function loadCharacter() {
  const data = await guarded(() => api("GET", "/api/character"));
  if (!data) return;
  state.character = data;
  renderPinned();
  if (state.page === "character") renderWorkspace();
}

async function applyCharacter(field, label, value) {
  if (!Number.isInteger(value)) return toast("Enter a whole number", true);
  const result = await guarded(() => api("POST", "/api/character/set", { field, value }));
  if (!result) return;
  state.character = result;
  renderPinned();
  renderWorkspace();
  const { before, after } = result.applied;
  if (after === value) toast(`${label}: ${(before ?? 0).toLocaleString()} to ${after.toLocaleString()}`);
  else toast(`${label}: asked for ${value.toLocaleString()} but the game reports ${after == null ? "nothing" : after.toLocaleString()}`, true);
}

function numberCard({ title, sub, value, input, quick, glyph, color, field, max }) {
  const box = h("input", { class: "field", type: "number", min: 0, max, value: input ?? value ?? 0, "aria-label": `${title} amount`, onkeydown: (e) => e.key === "Enter" && commit() });
  const commit = () => applyCharacter(field, title, parseInt(box.value, 10));
  const available = value != null;
  return h(
    "div",
    { class: `cc${available ? "" : " off"}`, vars: { "--c": color } },
    h("div", { class: "cc-head" },
      h("span", { class: "coin", text: glyph }),
      h("div", {}, h("div", { class: "k", text: title }), sub && h("div", { class: "sub", text: sub }))),
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
  const levelBox = h("input", { type: "number", min: lowLevel, max: highLevel, value: c.level ?? 1, onkeydown: (e) => e.key === "Enter" && setLevel() });
  const setLevel = () => applyCharacter("level", "Level", parseInt(levelBox.value, 10));
  const nudge = (by) => () => { levelBox.value = Math.max(lowLevel, Math.min(highLevel, (parseInt(levelBox.value, 10) || 1) + by)); };
  const money = (cur) => c.currencies.find((x) => x.key === cur);

  const banner = h(
    "div", { class: "banner" },
    h("div", {},
      h("span", { class: "stamp equipped", text: "Your character" }),
      h("h1", { text: c.name || "Vault Hunter" }),
      h("div", { class: "facts" },
        c.level != null && h("span", { text: `Level ${c.level}` }),
        c.xp != null && h("span", { text: `${c.xp.toLocaleString()} XP` }),
        c.xp_next_level != null && c.level < highLevel && h("span", { text: `${Math.max(0, c.xp_next_level - (c.xp || 0)).toLocaleString()} to next level` }))),
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
      quick: [["+1", (n, now) => now + 1], ["+5", (n, now) => now + 5], ["+25", (n, now) => now + 25]],
    }),
    ...c.currencies.map((cur) => {
      const style = CURRENCY_STYLE[cur.key] || { color: "#8b93a7", glyph: "?", step: 100 };
      return numberCard({
        title: cur.label, value: cur.value, field: cur.key, glyph: style.glyph, color: style.color, max: c.limits.currency,
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

// ---------- edits ----------
''',
)

s = replace_once(
    s,
    "renderWorkspace();\nrenderInventory();\npollInventory();",
    "renderPinned();\nrenderWorkspace();\nrenderInventory();\npollInventory();\nloadCharacter();",
)
s = replace_once(s, "  open: { stats: false, info: false },", "  open: { stats: false, info: false, charinfo: false },")
js.write_text(s, encoding="utf-8")

# ---------------- css ----------------
css = Path("rtse/web/app.css")
c = css.read_text(encoding="utf-8")
c += '''
/* ---- character page ---- */
.pinned { padding: 0 16px 12px; }
.pin { width: 100%; display: grid; grid-template-columns: 40px 1fr auto; gap: 12px; align-items: center; text-align: left; padding: 9px 12px; border: 2px solid #000; background: var(--panel-2); box-shadow: 3px 3px 0 #000; border-left: 8px solid var(--hazard); }
.pin:hover { background: #3a3426; }
.pin.on { background: #3a3012; box-shadow: 3px 3px 0 var(--hazard); }
.pin-ic { width: 40px; height: 40px; display: grid; place-items: center; background: var(--ink); border: 2px solid #000; color: var(--hazard); }
.pin .t { font-weight: 700; font-size: 15px; }
.pin .s { font-size: 11.5px; color: var(--muted); font-weight: 700; letter-spacing: .06em; text-transform: uppercase; margin-top: 2px; }
.pin .lvl { font-family: var(--display); font-size: 30px; color: var(--hazard); line-height: 1; text-align: right; }
.pin .lvl small { display: block; font-size: 12px; color: var(--muted); letter-spacing: .1em; }
.char-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; padding: 4px 0 6px; }
.cc { border: 3px solid #000; background: var(--panel-2); box-shadow: 4px 4px 0 #000; padding: 14px 16px 16px; border-top: 8px solid var(--c, var(--hazard)); }
.cc.off { opacity: .55; }
.cc-head { display: flex; align-items: center; gap: 12px; }
.coin { width: 38px; height: 38px; border-radius: 50%; display: grid; place-items: center; background: var(--c); color: var(--ink); font-family: var(--display); font-size: 21px; border: 3px solid #000; box-shadow: 2px 2px 0 #000; flex: none; line-height: 1; padding-top: 2px; }
.cc .k { font-weight: 700; letter-spacing: .1em; text-transform: uppercase; font-size: 13px; }
.cc .sub { color: var(--muted); font-size: 12px; font-weight: 600; }
.cc-val { font-family: var(--display); font-size: 50px; line-height: 1.05; margin: 10px 0 12px; color: var(--bone); text-shadow: 3px 3px 0 #000; overflow-wrap: anywhere; }
.cc-ctrl { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.cc-ctrl .field { width: 132px; }
'''
css.write_text(c, encoding="utf-8")
print("patched")
