// Gun diagrams. Each weapon type is drawn as separate part regions on a 200x90 board, muzzle to the
// right, so a diagram can colour every part by its manufacturer, highlight a single part for a slot
// plate, and make regions clickable. These are drawings, not game art: the game has no part images.

const REGION_ORDER = ["stock", "accessory1", "grip", "body", "accessory2", "barrel", "sight", "stripe", "elemental"];

// Slot name -> the region that shows it.
export const SLOT_REGION = {
  Body: "body", Grip: "grip", Barrel: "barrel", Sight: "sight", Stock: "stock", Elemental: "elemental",
  Accessory1: "accessory1", Accessory2: "accessory2", Material: "stripe",
};

const GUNS = {
  Pistol: {
    stock: "M26 30H36V46H26Z",
    body: "M36 28H130Q134 28 134 32V48H98L92 54H36Z",
    grip: "M46 50H82L66 86H38Z",
    barrel: "M134 34H182V44H134ZM176 30H186V48H176Z",
    sight: "M166 26H176V34H166Z",
    accessory1: "M104 52H128V60H104Z",
    accessory2: "M70 20H110V28H70Z",
    stripe: "M56 28L72 28L64 48L48 48Z",
    elemental: "M188 39L200 30L196 39L200 48Z",
    detail: "M42 33V43M47 33V43M52 33V43M84 33H112V39H84ZM90 54Q90 63 80 63L76 55",
    anchors: { stock: [31, 38], body: [100, 41], grip: [58, 72], barrel: [158, 39], sight: [171, 21], accessory1: [116, 67], accessory2: [90, 14], stripe: [60, 38], elemental: [194, 22] },
  },
  SMG: {
    stock: "M8 30H40V38H8ZM4 28H12V42H4Z",
    body: "M40 26H120Q124 26 124 30V50H40Z",
    grip: "M66 50H88L82 82H60Z",
    barrel: "M124 32H172V42H124ZM166 28H178V46H166Z",
    sight: "M84 18H106V26H84Z",
    accessory1: "M98 50H116V88H98Z",
    accessory2: "M132 42H162V50H132Z",
    stripe: "M52 26L68 26L60 50L44 50Z",
    elemental: "M180 37L194 29L190 37L194 45Z",
    detail: "M70 31H108M70 37H108M70 43H108M44 33H56M84 50V58",
    anchors: { stock: [20, 35], body: [96, 38], grip: [74, 68], barrel: [146, 37], sight: [95, 12], accessory1: [107, 72], accessory2: [147, 58], stripe: [56, 38], elemental: [187, 22] },
  },
  "Assault Rifle": {
    stock: "M2 28H34L40 34V50L34 54H2Z",
    body: "M40 26H150V50H40Z",
    grip: "M80 50H100L92 82H72Z",
    barrel: "M150 34H188V42H150ZM182 30H192V46H182Z",
    sight: "M70 14H102V26H70Z",
    accessory1: "M104 50H122L128 84H110Z",
    accessory2: "M126 50H148V58H126Z",
    stripe: "M52 26L70 26L62 50L44 50Z",
    elemental: "M194 38L200 30L198 38L200 46Z",
    detail: "M120 31H146M120 38H146M120 45H146M90 50V56",
    anchors: { stock: [20, 41], body: [112, 38], grip: [86, 68], barrel: [168, 38], sight: [86, 9], accessory1: [116, 70], accessory2: [137, 66], stripe: [57, 38], elemental: [195, 22] },
  },
  Shotgun: {
    stock: "M2 26H38L44 34V52L36 58H2Z",
    body: "M44 28H100V50H44Z",
    grip: "M60 50H82L74 82H54Z",
    barrel: "M100 32H186V40H100ZM180 28H190V44H180Z",
    sight: "M176 22H184V32H176Z",
    accessory1: "M116 42H150V58H116Z",
    accessory2: "M100 44H182V49H100Z",
    stripe: "M52 28L66 28L60 50L46 50Z",
    elemental: "M192 36L200 28L198 36L200 44Z",
    detail: "M122 44V56M130 44V56M138 44V56M52 34H92M70 50V56",
    anchors: { stock: [22, 42], body: [82, 39], grip: [68, 70], barrel: [140, 33], sight: [180, 17], accessory1: [133, 51], accessory2: [168, 58], stripe: [56, 39], elemental: [195, 22] },
  },
  "Sniper Rifle": {
    stock: "M2 30H46L50 36V58L44 64H2Z",
    body: "M50 34H104V52H50Z",
    grip: "M62 52H82L76 82H56Z",
    barrel: "M104 39H186V45H104ZM178 36H190V48H178Z",
    sight: "M64 14H116V26H64ZM72 26H78V34H72ZM100 26H106V34H100Z",
    accessory1: "M128 45H134L128 70H122ZM150 45H156L162 70H156Z",
    accessory2: "M84 52H96V66H84Z",
    stripe: "M58 34L72 34L66 52L52 52Z",
    elemental: "M192 42L200 34L198 42L200 50Z",
    detail: "M110 20H114M64 20H68M52 38H100M96 52V58",
    anchors: { stock: [24, 47], body: [90, 43], grip: [68, 70], barrel: [146, 42], sight: [90, 20], accessory1: [142, 62], accessory2: [90, 66], stripe: [62, 43], elemental: [195, 28] },
  },
  Launcher: {
    stock: "M6 28H34V52H6Z",
    body: "M34 24H100V56H34Z",
    grip: "M50 56H72L66 86H46Z",
    barrel: "M100 20H176Q182 20 182 26V54Q182 60 176 60H100ZM176 16H192V64H176Z",
    sight: "M120 10H146V20H120Z",
    accessory1: "M112 60H134V78H112Z",
    accessory2: "M60 14H92V24H60Z",
    stripe: "M44 24L60 24L52 56L38 56Z",
    elemental: "M194 40L200 32L198 40L200 48Z",
    detail: "M120 20V60M150 20V60M40 32H92M60 56V62",
    anchors: { stock: [20, 40], body: [78, 40], grip: [58, 74], barrel: [140, 40], sight: [133, 15], accessory1: [123, 70], accessory2: [76, 19], stripe: [48, 40], elemental: [195, 24] },
  },
};

export const GUN_TYPES = Object.keys(GUNS);

export function gunFor(type) {
  return GUNS[type] ? type : "Pistol";
}

// Slightly different shades per region so single-manufacturer guns still read as separate parts.
const SHADE = { stock: 76, accessory1: 84, grip: 88, body: 100, accessory2: 92, barrel: 94, sight: 100, stripe: 100, elemental: 100 };

/**
 * Draws a gun. `fills` maps region -> colour; a region missing from `fills` is drawn empty (dashed).
 * `markers` adds numbered buttons. `view` crops the board as "x y w h" (used by slot plates).
 */
export function gunSvg(type, { fills = {}, dim = [], solid = false, markers = null, className = "", view = "0 0 200 90", titles = {} } = {}) {
  const gun = GUNS[gunFor(type)];
  const regions = REGION_ORDER.map((region) => {
    const color = fills[region];
    const classes = ["r", `r-${region}`, color ? "" : "empty", dim.includes(region) ? "dim" : ""].filter(Boolean).join(" ");
    const style = color ? ` style="--fill:color-mix(in srgb, ${color} ${SHADE[region]}%, #000)"` : "";
    const title = titles[region] ? `<title>${titles[region]}</title>` : "";
    return `<path class="${classes}" data-region="${region}" d="${gun[region]}"${style}>${title}</path>`;
  });
  const dots = markers
    ? markers.map(({ region, label }) => {
        const [x, y] = gun.anchors[region];
        return `<g class="marker" data-region="${region}" transform="translate(${x} ${y})">${titles[region] ? `<title>${titles[region]}</title>` : ""}<circle r="6.5"/><text y="3.1" text-anchor="middle">${label}</text></g>`;
      }).join("")
    : "";
  const lines = `<path class="detail" d="${gun.detail}"/>`;
  return `<svg class="gun ${className}${solid ? " solid" : ""}" viewBox="${view}" aria-hidden="true">${regions.join("")}${lines}${dots}</svg>`;
}

/** A slot plate: the gun in muted ink with only the part's region lit in `color`, zoomed on that part. */
export function plateSvg(type, slot, color) {
  const gun = GUNS[gunFor(type)];
  const region = SLOT_REGION[slot] || "body";
  const dim = REGION_ORDER.filter((r) => r !== region && r !== "stripe" && r !== "elemental");
  const fills = Object.fromEntries(dim.map((r) => [r, "var(--plate-dim)"]));
  fills[region] = color;
  const [ax, ay] = gun.anchors[region];
  const w = 96;
  const h = 50;
  const x = Math.max(0, Math.min(200 - w, ax - w / 2));
  const y = Math.max(0, Math.min(90 - h, ay - h / 2));
  return gunSvg(type, { fills, dim, solid: true, className: "plate-art", view: `${x} ${y} ${w} ${h}` });
}
