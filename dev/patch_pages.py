from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:80]!r} (found {text.count(old)})"
    return text.replace(old, new)


js = Path("rtse/web/app.js")
s = js.read_text(encoding="utf-8")

s = replace_once(s, 'import { emblemSvg,', 'import { CLASSES, avatarSvg, classFromText } from "./avatars.js";\nimport { emblemSvg,')
s = replace_once(s, "  character: null,\n", "  character: null,\n  ammo: null,\n")
s = replace_once(s, "  open: { stats: false, info: false, charinfo: false },", "  open: { stats: false, info: false, charinfo: false, ammoinfo: false },")
s = replace_once(
    s,
    "  if (state.page === \"character\") return renderCharacter(root);\n",
    "  if (state.page === \"character\") return renderCharacter(root);\n  if (state.page === \"ammo\") return renderAmmo(root);\n",
)

# ---- replace the pinned bar + open/load for character ----
start = s.index("function renderPinned() {")
end = s.index("async function applyCharacter(")
s = s[:start] + '''const FORCED_CLASS = "rtse.class";

/** The player's vault hunter: what the game says, else what their class mod says, unless they picked one. */
function currentClass() {
  const picked = localStorage.getItem(FORCED_CLASS);
  if (picked && CLASSES[picked]) return { key: picked, how: "chosen" };
  const c = state.character;
  if (c && c.class_key && CLASSES[c.class_key]) return { key: c.class_key, how: "detected" };
  const mod = state.items.find((item) => item.class === "WillowClassMod");
  const guess = mod && classFromText(mod.name);
  return guess ? { key: guess, how: "from your class mod" } : { key: null, how: "unknown" };
}

function avatarEl(key, cls = "") {
  return h("div", { class: `avatar ${cls}`.trim(), vars: { "--c": key ? CLASSES[key].color : "#8a8577" }, html: avatarSvg(key) });
}

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
    pin("character", openCharacter, avatarEl(cls.key), "Character",
      cls.key ? `${CLASSES[cls.key].name} / ${CLASSES[cls.key].job}` : (c && c.name) || "Level, skill points, money",
      c && c.level != null && h("div", { class: "lvl" }, h("small", { text: "LV" }), String(c.level))),
    pin("ammo", openAmmo, icon("crosshair", 20), "Ammo", "Every ammo type", null));
}

async function openCharacter() {
  state.page = "character";
  closePicker();
  renderPinned();
  renderInventory();
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

function chooseClass(key) {
  localStorage.setItem(FORCED_CLASS, key);
  renderPinned();
  renderWorkspace();
}

// ---------- ammo ----------

async function openAmmo() {
  state.page = "ammo";
  closePicker();
  renderPinned();
  renderInventory();
  renderWorkspace();
  await loadAmmo();
}

async function loadAmmo() {
  try {
    state.ammo = await api("GET", "/api/ammo");
  } catch (error) {
    state.ammo = { pools: [], error: error.message };
  }
  if (state.page === "ammo") renderWorkspace();
}

async function applyAmmo(pool, value) {
  if (value !== "max" && !Number.isInteger(value)) return toast("Enter a whole number", true);
  const result = await guarded(() => api("POST", "/api/ammo/set", { id: pool.id, value }));
  if (!result) return;
  state.ammo = result;
  renderWorkspace();
  const { before, after, requested } = result.applied;
  if (after === requested) toast(`${pool.label}: ${(before ?? 0).toLocaleString()} to ${after.toLocaleString()}`);
  else toast(`${pool.label}: asked for ${requested.toLocaleString()} but the game reports ${after == null ? "nothing" : after.toLocaleString()} (the game may cap it at your maximum)`, true);
}

function ammoCard(pool) {
  const box = h("input", { class: "field", type: "number", min: 0, value: pool.value ?? 0, "aria-label": `${pool.label} ammo`, onkeydown: (e) => e.key === "Enter" && commit() });
  const commit = () => applyAmmo(pool, parseInt(box.value, 10));
  const pct = pool.max ? Math.max(0, Math.min(100, ((pool.value || 0) / pool.max) * 100)) : 0;
  return h(
    "div",
    { class: "cc ammo", vars: { "--c": "#ffd21f" } },
    h("div", { class: "cc-head" }, h("span", { class: "coin", text: pool.label.slice(0, 2).toUpperCase() }),
      h("div", {}, h("div", { class: "k", text: pool.label }), pool.max != null && h("div", { class: "sub", text: `Capacity ${pool.max.toLocaleString()}` }))),
    h("div", { class: "cc-val", text: pool.value == null ? "unavailable" : pool.value.toLocaleString() }),
    pool.max != null && h("div", { class: "bar" }, h("i", { style: { width: `${pct}%` } })),
    pool.value != null && h("div", { class: "cc-ctrl" },
      box,
      h("button", { class: "btn sm primary", onclick: commit }, "Set"),
      pool.max != null && h("button", { class: "btn sm", onclick: () => applyAmmo(pool, "max") }, "Fill"),
      h("button", { class: "btn sm danger", onclick: () => applyAmmo(pool, 0) }, "Empty")),
  );
}

function renderAmmo(root) {
  const a = state.ammo;
  if (!a) {
    fill(root, h("div", { class: "empty" }, h("div", {}, h("h2", { text: "Loading..." }))));
    return;
  }
  const fillAll = async () => {
    for (const pool of a.pools) if (pool.max != null) await applyAmmo(pool, "max");
  };
  fill(
    root,
    h("div", { class: "bench" },
      h("div", { class: "banner" },
        h("div", {}, h("span", { class: "stamp equipped", text: "Your ammo" }), h("h1", { text: "Ammo pouch" }),
          h("div", { class: "facts" }, h("span", { text: `${a.pools.length} ammo types` }))),
        h("div", { class: "level" },
          h("button", { class: "btn primary", disabled: !a.pools.some((p) => p.max != null) || null, onclick: fillAll }, icon("check", 15), "Fill everything"),
          h("button", { class: "btn ghost", title: "Re-read from the game", "aria-label": "Refresh", onclick: loadAmmo }, icon("refresh", 15)))),
      h("div", { class: "bench-body" },
        a.error && h("div", { class: "warn-banner" }, icon("alert", 18), `${a.error}. Save the ammo dump below and send it over.`),
        !a.error && a.pools.length === 0 && h("div", { class: "warn-banner" }, icon("alert", 18), "No ammo pools were recognised. Save the ammo dump below and send it over."),
        h("div", { class: "char-grid" }, a.pools.map(ammoCard)))),
    dropdown("ammoinfo", "Ammo dev tools", "", h("div", { class: "tools" },
      h("button", { class: "btn sm", onclick: () => dump("/api/debug/ammo") }, "Save ammo dump"))),
  );
}

''' + s[end:]

# ---- portrait on the character banner ----
s = replace_once(
    s,
    '''    "div", { class: "banner" },
    h("div", {},
      h("span", { class: "stamp equipped", text: "Your character" }),
      h("h1", { text: c.name || "Vault Hunter" }),''',
    '''    "div", { class: "banner" },
    h("div", { class: "who" },
      avatarEl(cls.key, "big"),
      h("div", {},
      h("span", { class: "stamp equipped", text: cls.key ? `${CLASSES[cls.key].job}` : "Your character" }),
      h("h1", { text: c.name || (cls.key && CLASSES[cls.key].name) || "Vault Hunter" }),''',
)
s = replace_once(
    s,
    '''        c.xp_next_level != null && c.level < highLevel && h("span", { text: `${Math.max(0, c.xp_next_level - (c.xp || 0)).toLocaleString()} to next level` }))),''',
    '''        c.xp_next_level != null && c.level < highLevel && h("span", { text: `${Math.max(0, c.xp_next_level - (c.xp || 0)).toLocaleString()} to next level` })))),''',
)
s = replace_once(s, "  const money = (cur) => c.currencies.find((x) => x.key === cur);\n", "  const cls = currentClass();\n")

s = replace_once(
    s,
    '''        h("div", { class: "char-grid" }, cards))),
    dropdown("charinfo"''',
    '''        h("div", { class: "classpick" },
          h("span", { class: "k", text: `Vault hunter (${cls.how})` }),
          Object.entries(CLASSES).map(([key, info]) => h("button", {
            class: `cp${cls.key === key ? " on" : ""}`, title: `${info.name} / ${info.job}`, vars: { "--c": info.color }, onclick: () => chooseClass(key),
          }, avatarEl(key, "mini"), h("span", { text: info.name })))),
        h("div", { class: "char-grid" }, cards))),
    dropdown("charinfo"''',
)
js.write_text(s, encoding="utf-8")

# renderInventory should refresh the pins' selected state on page change
css = Path("rtse/web/app.css")
c = css.read_text(encoding="utf-8")
c += '''
.pin-ic .avatar { width: 100%; height: 100%; border: 0; }
.pin + .pin { margin-top: 8px; }
.avatar { background: radial-gradient(circle at 50% 40%, color-mix(in srgb, var(--c) 45%, #15130f), #15130f 80%); display: block; overflow: hidden; }
.avatar svg { width: 100%; height: 100%; display: block; }
.avatar.big { width: 112px; height: 112px; border: 3px solid #000; box-shadow: 4px 4px 0 #000; flex: none; }
.avatar.mini { width: 34px; height: 34px; border: 2px solid #000; flex: none; }
.who { display: flex; gap: 20px; align-items: flex-end; min-width: 0; }
.classpick { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 4px 0 16px; }
.classpick .k { width: 100%; color: var(--muted); font-weight: 700; letter-spacing: .1em; text-transform: uppercase; font-size: 12px; }
.cp { display: inline-flex; align-items: center; gap: 8px; padding: 4px 12px 4px 4px; border: 2px solid #000; background: var(--panel-2); box-shadow: 2px 2px 0 #000; font-weight: 700; }
.cp:hover { background: #3a3426; }
.cp.on { background: var(--c); color: var(--ink); }
.bar { height: 14px; border: 2px solid #000; background: var(--ink); margin: 0 0 12px; }
.bar i { display: block; height: 100%; background: var(--hazard); }
'''
css.write_text(c, encoding="utf-8")
print("patched")
