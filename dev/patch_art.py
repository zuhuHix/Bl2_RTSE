from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


web = Path("rtse/web")
g = (web / "guns.js").read_text(encoding="utf-8")

# --- detail lines per gun type (ink strokes drawn over the regions) ---
DETAILS = {
    "Pistol": "M42 33V43M47 33V43M52 33V43M84 33H112V39H84ZM90 54Q90 63 80 63L76 55",
    "SMG": "M70 31H108M70 37H108M70 43H108M44 33H56M84 50V58",
    "Assault Rifle": "M120 31H146M120 38H146M120 45H146M60 26V21M90 50V56",
    "Shotgun": "M122 44V56M130 44V56M138 44V56M52 34H92M70 50V56",
    "Sniper Rifle": "M110 20H114M64 20H68M52 38H100M96 52V58",
    "Launcher": "M120 20V60M150 20V60M40 32H92M60 56V62",
}
for name, path in DETAILS.items():
    marker = f'    anchors: {{'
    # insert a "detail" entry before the first anchors line that follows this gun's key
    key = f'  "{name}": {{' if " " in name else f"  {name}: {{"
    start = g.index(key)
    anchors = g.index(marker, start)
    g = g[:anchors] + f'    detail: "{path}",\n' + g[anchors:]

# --- rewrite gunSvg / plateSvg: shading, details, cropped plates ---
start = g.index("/**\n * Draws a gun.")
g = g[:start] + '''// Slightly different shades per region so single-manufacturer guns still read as separate parts.
const SHADE = { stock: 76, accessory1: 84, grip: 88, body: 100, accessory2: 92, barrel: 94, sight: 100, stripe: 100, elemental: 100 };

/**
 * Draws a gun. `fills` maps region -> colour; a region missing from `fills` is drawn empty (dashed).
 * `markers` adds numbered buttons. `view` crops the board as "x y w h" (used by slot plates).
 */
export function gunSvg(type, { fills = {}, dim = [], solid = false, markers = null, className = "", view = "0 0 200 90" } = {}) {
  const gun = GUNS[gunFor(type)];
  const regions = REGION_ORDER.map((region) => {
    const color = fills[region];
    const classes = ["r", `r-${region}`, color ? "" : "empty", dim.includes(region) ? "dim" : ""].filter(Boolean).join(" ");
    const style = color ? ` style="--fill:color-mix(in srgb, ${color} ${SHADE[region]}%, #000)"` : "";
    return `<path class="${classes}" data-region="${region}" d="${gun[region]}"${style}/>`;
  });
  const dots = markers
    ? markers.map(({ region, label }) => {
        const [x, y] = gun.anchors[region];
        return `<g class="marker" data-region="${region}" transform="translate(${x} ${y})"><circle r="6.5"/><text y="3.1" text-anchor="middle">${label}</text></g>`;
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
'''
(web / "guns.js").write_text(g, encoding="utf-8")

css = (web / "app.css").read_text(encoding="utf-8")
css = replace_once(css, ".gun .r.empty {", ".gun .detail { fill: none; stroke: var(--ink); stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; pointer-events: none; opacity: .7; }\n.gun .r.empty {")
css = replace_once(css, ".fx div span { flex: 1; text-align: left; color: var(--bone); }", ".fx div > span:not(.icon) { flex: 1; text-align: left; color: var(--bone); }")
(web / "app.css").write_text(css, encoding="utf-8")
print("patched")
