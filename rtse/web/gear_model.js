// Gear facts shared by the gear list and the gear workbench: what kind of item it is, which 3D model
// draws it, and the small plates and colours that stand for its parts.
import { ELEMENTS, manufacturerColor, parsePart } from "./meta.js";
import { gunFor, plateSvg } from "./guns.js";
import { letterSvg, plateEl } from "./icons.js";
import { state } from "./core.js";

export const STEEL = "#8a8577"; // parts with no recognisable manufacturer

export function weaponTypeFrom(path) {
  const p = (path || "").toLowerCase();
  if (p.includes("pistol")) return "Pistol";
  if (p.includes("smg")) return "SMG";
  if (p.includes("sniper")) return "Sniper Rifle";
  if (p.includes("shotgun")) return "Shotgun";
  if (p.includes("launcher")) return "Launcher";
  if (p.includes("assault") || p.includes("_ar") || p.includes("rifle")) return "Assault Rifle";
  return null;
}

export const entryFor = (id) => state.items.find((item) => item.id === id) || null;
export const hostType = (detail) => weaponTypeFrom((entryFor(detail.id) || {}).type_path);
export const isWeapon = (detail) => detail.class === "WillowWeapon";

// ---------- art ----------

/** A small drawn plate for a part in a slot. */
export function partPlate(part, slotName, detail, cls = "") {
  const info = parsePart(part); // the part object carries the server's in-game label, if any
  if (!isWeapon(detail)) {
    return { info, el: plateEl({ html: letterSvg(slotName), color: "var(--bone)", cls: `${cls} item`, imageName: part.name }) };
  }
  const type = gunFor(info.weaponType || hostType(detail));
  const color = info.element ? ELEMENTS[info.element].color : info.manufacturer ? manufacturerColor(info.manufacturer) : STEEL;
  return { info, el: plateEl({ html: plateSvg(type, slotName, color), color, cls, imageName: part.name }) };
}

/** Which gestalt model draws this item, or null if it has none. */
export function modelKeyFor(detail) {
  if (isWeapon(detail)) return weaponTypeFrom((entryFor(detail.id) || {}).type_path);
  return { WillowShield: "Shield", WillowGrenadeMod: "Grenade", WillowArtifact: "Relic" }[detail.class] || null;
}

function mixHex(a, b, t) {
  const pa = parseInt(a.slice(1), 16);
  const pb = parseInt(b.slice(1), 16);
  const channel = (shift) => Math.round(((pa >> shift) & 255) * (1 - t) + ((pb >> shift) & 255) * t);
  return `#${((1 << 24) | (channel(16) << 16) | (channel(8) << 8) | channel(0)).toString(16).slice(1)}`;
}

/** Paint colour for a part in 3D: its maker's colour, nudged by what kind of part it is. */
export function pieceColor(part, slotName) {
  const info = parsePart(part);
  let base = info.manufacturer ? manufacturerColor(info.manufacturer) : STEEL;
  if (info.element) base = ELEMENTS[info.element].color;
  if (slotName === "Barrel") return mixHex(base, "#aab2bc", 0.5);
  if (slotName === "Grip") return mixHex(base, "#7a5230", 0.55);
  if (slotName === "Stock") return mixHex(base, "#7a5230", 0.4);
  if (slotName === "Sight") return mixHex(base, "#7fb4e6", 0.4);
  if (slotName.startsWith("Accessory")) return mixHex(base, "#f3ecd9", 0.3);
  return base;
}

export function piecesOf(part, slotName) {
  return (part.meshes || []).map((mesh) => ({ slot: slotName, mesh, color: pieceColor(part, slotName) }));
}

/** Everything currently on the item, as 3D pieces. */
export function piecesFor(detail) {
  return detail.slots.flatMap((slot) => (slot.current ? piecesOf(slot.current, slot.slot) : []));
}
