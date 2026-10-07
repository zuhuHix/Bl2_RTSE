// Names, colours and wording: turns the game's internal identifiers into something readable.

export const MANUFACTURERS = {
  hyperion: { label: "Hyperion", color: "#f2c230" },
  jakobs: { label: "Jakobs", color: "#d98a47" },
  maliwan: { label: "Maliwan", color: "#4aa8ff" },
  tediore: { label: "Tediore", color: "#6fcf5a" },
  torgue: { label: "Torgue", color: "#ff7a1f" },
  vladof: { label: "Vladof", color: "#ec4d44" },
  dahl: { label: "Dahl", color: "#b0c24f" },
  bandit: { label: "Bandit", color: "#c8734d" },
  cov: { label: "COV", color: "#c36bf0" },
  pangolin: { label: "Pangolin", color: "#59c8d8" },
  atlas: { label: "Atlas", color: "#d8d8d8" },
  gearbox: { label: "Gearbox", color: "#e0b050" },
};

export const WEAPON_TYPES = {
  pistol: "Pistol", smg: "SMG", ar: "Assault Rifle", assaultrifle: "Assault Rifle", rifle: "Assault Rifle",
  sg: "Shotgun", shotgun: "Shotgun", sniper: "Sniper Rifle", sniperrifles: "Sniper Rifle",
  l: "Launcher", launcher: "Launcher", launchers: "Launcher",
};

const SLOT_WORDS = {
  body: "body", grip: "grip", barrel: "barrel", sight: "sight", scope: "sight", stock: "stock",
  elemental: "elemental", accessory: "accessory", acc: "accessory", mat: "material", material: "material",
};

export const ELEMENTS = {
  fire: { label: "Incendiary", color: "#ff6a2a" }, incendiary: { label: "Incendiary", color: "#ff6a2a" },
  shock: { label: "Shock", color: "#4ab4ff" }, corrosive: { label: "Corrosive", color: "#8ddc3c" },
  slag: { label: "Slag", color: "#b55cff" }, explosive: { label: "Explosive", color: "#ffd23c" },
};

const RARITY_WORDS = new Set(["common", "uncommon", "rare", "veryrare", "legendary", "pearl", "seraph", "effervescent", "unique", "etech"]);

export const SLOT_LABELS = {
  Accessory1: "Accessory 1", Accessory2: "Accessory 2",
  Alpha: "Alpha", Beta: "Beta", Gamma: "Gamma", Delta: "Delta", Epsilon: "Epsilon", Zeta: "Zeta", Eta: "Eta", Theta: "Theta",
};

export function slotLabel(slot) {
  return SLOT_LABELS[slot] || slot;
}

/** Normalises a slot name to an icon kind: "Accessory1" -> "accessory", "Alpha" -> "item". */
export function slotKind(slot) {
  const key = slot.toLowerCase().replace(/\d+$/, "");
  return SLOT_WORDS[key] || "item";
}

function splitTokens(name) {
  return name
    .split("_")
    .flatMap((token) => token.replace(/([a-z])([A-Z])/g, "$1 $2").split(" "))
    .filter(Boolean);
}

function titleCase(words) {
  return words.map((w) => (/^\d/.test(w) ? w : w[0].toUpperCase() + w.slice(1))).join(" ");
}

/** Reads a part's game name (e.g. Pistol_Barrel_Hyperion_LogansGun) into display pieces. */
export function parsePart(name, group = "") {
  const tokens = splitTokens(name);
  const info = { title: "", weaponType: null, slot: null, manufacturer: null, element: null, rarity: null };
  const rest = [];
  tokens.forEach((token, index) => {
    const t = token.toLowerCase();
    if (index === 0 && WEAPON_TYPES[t]) info.weaponType = WEAPON_TYPES[t];
    else if (SLOT_WORDS[t] && !info.slot) info.slot = SLOT_WORDS[t];
    else if (MANUFACTURERS[t] && !info.manufacturer) info.manufacturer = t;
    else if (ELEMENTS[t] && !info.element) info.element = t;
    else if (RARITY_WORDS.has(t) && !info.rarity) info.rarity = t;
    else rest.push(token);
  });
  if (!info.manufacturer) {
    const fromGroup = group.toLowerCase().split(/[._]/).find((p) => MANUFACTURERS[p]);
    if (fromGroup) info.manufacturer = fromGroup;
  }
  if (!info.weaponType) {
    const fromGroup = group.toLowerCase().split(/[._]/).find((p) => WEAPON_TYPES[p]);
    if (fromGroup) info.weaponType = WEAPON_TYPES[fromGroup];
  }
  const nameless = rest.every((word) => /^\d+$/.test(word)); // nothing but numbers (or nothing)
  if (info.element && nameless) {
    info.title = ELEMENTS[info.element].label;
  } else if (nameless) {
    // e.g. Pistol_Body_Hyperion_4 -> "Hyperion Body 4"
    const words = [info.manufacturer && MANUFACTURERS[info.manufacturer].label, info.slot, ...rest].filter(Boolean);
    info.title = words.length ? titleCase(words.map((w) => w.trim())) : titleCase([info.rarity || "part"]);
  } else {
    info.title = titleCase(rest);
  }
  return info;
}

export function manufacturerColor(key) {
  return key && MANUFACTURERS[key] ? MANUFACTURERS[key].color : "#8b93a7";
}

/** "Hyperion INVALID 13  Earnest Logan's Gun" -> display name without the game's INVALID marker. */
export function cleanItemName(name) {
  return name.replace(/\bINVALID\b/g, "").replace(/\s+/g, " ").trim() || name;
}

export function itemManufacturer(name) {
  const lower = name.toLowerCase();
  return Object.keys(MANUFACTURERS).find((key) => lower.includes(key)) || null;
}

const CLASS_LABELS = {
  WillowWeapon: "Weapon", WillowShield: "Shield", WillowGrenadeMod: "Grenade Mod", WillowClassMod: "Class Mod",
  WillowArtifact: "Relic", WillowUsableItem: "Usable", WillowUsableCustomizationItem: "Customization", WillowMissionItem: "Mission Item",
};

export function classLabel(className) {
  return CLASS_LABELS[className] || className.replace(/^Willow/, "");
}

/**
 * The game only gives a rarity number. Colour it in the familiar white / green / blue / purple /
 * orange / cyan bands (the exact number-to-name mapping is not confirmed, so no names are shown).
 */
export function rarityColor(rarity) {
  if (rarity === null || rarity === undefined) return "#6b6657";
  if (rarity <= 1) return "#e8e2d0";
  if (rarity <= 3) return "#4ad65c";
  if (rarity <= 5) return "#3d8bff";
  if (rarity <= 7) return "#b25cff";
  if (rarity <= 9) return "#ff9a1f";
  return "#32e0d2";
}

// --- Effects ---------------------------------------------------------------------------------

export function prettyAttribute(attribute, labels = {}) {
  if (!attribute) return "Unknown stat";
  if (labels[attribute]) return labels[attribute].replace(/\s*[-(].*$/, "");
  return attribute.replace(/([a-z])([A-Z])/g, "$1 $2");
}

/** Describes one attribute effect as { text, value, tone }, e.g. "Damage per shot", "x1.25 (+25%)". */
export function describeEffect(effect, labels) {
  const text = prettyAttribute(effect.attribute, labels);
  const kind = (effect.type || "").toLowerCase();
  const v = effect.value;
  if (v === null || v === undefined) return { text, value: effect.type || "modifies", tone: "" };
  if (kind.includes("scale")) {
    const pct = Math.round((v - 1) * 100);
    return { text, value: `×${+v.toFixed(2)} (${pct >= 0 ? "+" : ""}${pct}%)`, tone: pct >= 0 ? "up" : "down" };
  }
  if (kind.includes("add")) {
    return { text, value: `${v >= 0 ? "+" : ""}${+v.toFixed(2)}`, tone: v >= 0 ? "up" : "down" };
  }
  return { text, value: `${effect.type || "set"} ${+v.toFixed(2)}`, tone: "" };
}

export const STAT_GROUPS = [
  { title: "Damage", stats: ["InstantHitDamage", "ProjectilesPerShot", "MeleeDamage", "ExtraShotChance", "AdditionalRicochets"] },
  { title: "Ammo & firing", stats: ["ClipSize", "ShotCost", "FireInterval", "AutomaticBurstCount", "ReloadTime"] },
  { title: "Accuracy & range", stats: ["Spread", "AimError", "WeaponRange", "ProjectileSpeedMultiplier"] },
  { title: "Status effects", stats: ["StatusEffectChanceModifier", "StatusEffectDamage"] },
  { title: "Handling", stats: ["EquipTime", "PutDownTime"] },
];

export function formatNumber(value) {
  if (value === null || value === undefined) return "-";
  return Number.isInteger(value) ? String(value) : String(Number(value.toPrecision(5)));
}
