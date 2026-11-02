// The page registry: the one list the shell reads to build the sidebar, open feature pages and link their CSS.
//
// Adding a page is one entry below plus its module (page_<id>.js) and, optionally, its stylesheet. Nothing else
// changes: rtse/server.py already serves page_*.js and page_*.css, and index.html lists no page CSS.
//
// Entry shape (all fields are read by app.js):
//   id      string       the page's name in state.page; unique
//   label   string       the sidebar card's title
//   sub     string       the sidebar card's subtitle, used unless the page's pin() supplies one
//   icon    string|null  an icon name for ui() in icons.js; null for a card that always comes from pin()
//   module  string       the ES module, relative to app.js
//   css     string|null  a stylesheet, relative to the web root; linked once at boot
//   eager   boolean?     true to import the module at boot (the character card shows game data), else on first open
//
// Module interface (every page exports these; ctx is the shell's pageCtx from core.js):
//   load(ctx)        async; fetch the page's data, then ask for a redraw. Called each time the page opens.
//   render(root, ctx) draw the page into the #workspace element `root`. Called on every redraw.
//   pin(ctx)?        optional; returns { icon: Node, sub: string, badge?: Node } for the sidebar card.
//                    Without it the card shows the entry's icon and sub.
//
// Character is special only in being eager: its card shows the level and class, which need its data at boot.

export const PAGES = [
  { id: "character", label: "Character", sub: "Level, skill points, money", icon: null, module: "./page_character.js", css: null, eager: true },
  { id: "ammo", label: "Ammo", sub: "Every ammo type", icon: "crosshair", module: "./page_ammo.js", css: null },
  { id: "sdu", label: "Upgrades", sub: "Backpack, bank, ammo SDUs", icon: "backpack", module: "./page_sdu.js", css: "page_sdu.css" },
  { id: "skills", label: "Skills", sub: "Skill tree", icon: "sliders", module: "./page_skills.js", css: "page_skills.css" },
  { id: "world", label: "World", sub: "Missions, challenges, travel", icon: "swap", module: "./page_world.js", css: "page_world.css" },
  { id: "inspect", label: "Inspector", sub: "Raw live game data", icon: "search", module: "./page_inspect.js", css: "page_inspect.css" },
  { id: "lab", label: "Weapon lab", sub: "A/B parts, measured damage", icon: "crosshair", module: "./page_lab.js", css: "page_lab.css" },
  { id: "bank", label: "Bank & codes", sub: "Bank, item codes, import", icon: "backpack", module: "./page_bank.js", css: "page_bank.css" },
  { id: "farm", label: "Farm guard", sub: "Guard a farm, count drops", icon: "crosshair", module: "./page_farm.js", css: "page_farm.css" },
  { id: "history", label: "History", sub: "Undo and save backups", icon: "swap", module: "./page_history.js", css: "page_history.css" },
  { id: "loadouts", label: "Builds", sub: "Save and apply presets", icon: "check", module: "./page_loadouts.js", css: "page_loadouts.css" },
];
