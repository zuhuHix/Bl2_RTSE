// Icons and small art. UI icons follow the Lucide set (inline, so the page works offline).
// Manufacturer emblems are simple monograms of our own, not the real logos.

const UI_ICONS = {
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/>',
  plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  minus: '<path d="M5 12h14"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  swap: '<path d="m16 3 4 4-4 4"/><path d="M20 7H4"/><path d="m8 21-4-4 4-4"/><path d="M4 17h16"/>',
  sliders: '<path d="M21 4h-7"/><path d="M10 4H3"/><path d="M21 12h-9"/><path d="M8 12H3"/><path d="M21 20h-5"/><path d="M12 20H3"/><path d="M14 2v4"/><path d="M8 10v4"/><path d="M16 18v4"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  arrowUp: '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
  arrowDown: '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
  crosshair: '<circle cx="12" cy="12" r="10"/><path d="M22 12h-4"/><path d="M6 12H2"/><path d="M12 6V2"/><path d="M12 22v-4"/>',
  backpack: '<path d="M4 10a4 4 0 0 1 4-4h8a4 4 0 0 1 4 4v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/><path d="M8 21v-5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v5"/><path d="M8 10h8"/>',
};

export function ui(name, size = 16) {
  return `<svg class="ui-icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${UI_ICONS[name]}</svg>`;
}

// 24x24 emblem outlines
const EMBLEM_SHAPES = {
  hyperion: ["M12 2l9 5v10l-9 5-9-5V7z", "H"],
  jakobs: ["M4 3h16v10c0 5-4 8-8 9-4-1-8-4-8-9z", "J"],
  maliwan: ["M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z", "M"],
  tediore: ["M3 3h18v18H3z", "T"],
  torgue: ["M8 2h8l6 6v8l-6 6H8l-6-6V8z", "T"],
  vladof: ["M12 1l11 11-11 11L1 12z", "V"],
  dahl: ["M12 2l10 18H2z", "D"],
  bandit: ["M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z", "B"],
  cov: ["M12 2l10 8-4 12H6L2 10z", "C"],
  pangolin: ["M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z", "P"],
  atlas: ["M12 2l10 18H2z", "A"],
  gearbox: ["M3 3h18v18H3z", "G"],
};

export function emblemSvg(key, color, size = 18) {
  const [shape, letter] = EMBLEM_SHAPES[key] || EMBLEM_SHAPES.gearbox;
  const y = key === "dahl" || key === "atlas" ? 17.5 : 16.4;
  return `<svg class="emblem" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><path d="${shape}" fill="${color}" stroke="var(--ink)" stroke-width="2" stroke-linejoin="round"/><text x="12" y="${y}" text-anchor="middle" font-family="Bebas Neue, Impact, sans-serif" font-size="13" fill="var(--ink)">${letter}</text></svg>`;
}

// Non-weapon items (48x48), filled with currentColor and outlined in ink.
const ITEM_GLYPHS = {
  WillowShield: '<path d="M24 4l16 6v12c0 10-7 17-16 21C15 39 8 32 8 22V10z"/><path d="M24 12v26" stroke="var(--cut)" stroke-width="3"/>',
  WillowClassMod: '<rect x="9" y="9" width="30" height="30" rx="3"/><rect x="16" y="16" width="16" height="16" rx="2" fill="var(--cut)" stroke="none"/><path d="M16 3v6M24 3v6M32 3v6M16 39v6M24 39v6M32 39v6" stroke-width="3.4"/>',
  WillowGrenadeMod: '<circle cx="24" cy="28" r="14"/><rect x="19" y="8" width="10" height="7" rx="1.5"/><path d="M29 11l9-5" fill="none" stroke-width="3.4"/>',
  WillowArtifact: '<path d="M24 3l15 15-15 27L9 18z"/><path d="M9 18h30M24 3l-6 15 6 27 6-27z" stroke="var(--cut)" stroke-width="1.8" fill="none"/>',
  WillowUsableCustomizationItem: '<path d="M24 5C13 5 5 13.5 5 24s8 19 17 19c4.5 0 5.5-3.4 3.4-6.4-2-3 0-6.4 4-6.4H34c5 0 9-3.6 9-8.4C43 12.4 34.5 5 24 5z"/><circle cx="15" cy="22" r="3.2" fill="var(--cut)" stroke="none"/><circle cx="22" cy="14" r="3.2" fill="var(--cut)" stroke="none"/><circle cx="32" cy="16" r="3.2" fill="var(--cut)" stroke="none"/>',
  WillowMissionItem: '<path d="m24 4 6 13 14 2-10 10 3 14-13-7-13 7 3-14L4 19l14-2z"/>',
  generic: '<path d="M24 4l17 10v20L24 44 7 34V14z"/><circle cx="24" cy="24" r="5.5" fill="var(--cut)" stroke="none"/>',
};

export function itemClassSvg(className) {
  const glyph = ITEM_GLYPHS[className] || ITEM_GLYPHS.generic;
  return `<svg class="item-art" viewBox="0 0 48 48" fill="currentColor" stroke="var(--ink)" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true">${glyph}</svg>`;
}

const GREEK = { Alpha: "α", Beta: "β", Gamma: "γ", Delta: "δ", Epsilon: "ε", Zeta: "ζ", Eta: "η", Theta: "θ" };

/** Plate art for non-weapon part slots: the slot's Greek letter, or a palette for Material. */
export function letterSvg(slot) {
  if (slot === "Material") return itemClassSvg("WillowUsableCustomizationItem");
  return `<svg class="letter-art" viewBox="0 0 100 60" aria-hidden="true"><text x="50" y="46" text-anchor="middle" font-size="52" font-weight="700" fill="currentColor" stroke="var(--ink)" stroke-width="2.5" paint-order="stroke" font-family="Barlow, Segoe UI, serif">${GREEK[slot] || "?"}</text></svg>`;
}

const missingImages = new Set();

/**
 * A plate element holding art. If icons/<imageName>.png exists it replaces the drawn art, so real
 * images can be dropped in without touching the code.
 */
export function plateEl({ html, color, cls = "", imageName = null }) {
  const plate = document.createElement("div");
  plate.className = `plate ${cls}`.trim();
  plate.style.setProperty("--c", color);
  plate.innerHTML = html;
  if (imageName && !missingImages.has(imageName)) {
    const img = new Image();
    img.alt = "";
    img.onload = () => plate.replaceChildren(img);
    img.onerror = () => missingImages.add(imageName);
    img.src = `icons/${encodeURIComponent(imageName)}.png`;
  }
  return plate;
}
