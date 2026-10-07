from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


web = Path("rtse/web")
g = (web / "guns.js").read_text(encoding="utf-8")

# One sight per gun, never beside the stock.
g = replace_once(g, 'sight: "M170 26H178V34H170ZM40 22H50V28H40Z"', 'sight: "M166 26H176V34H166Z"')
g = replace_once(g, "sight: [174, 21]", "sight: [171, 21]")

g = replace_once(g, 'sight: "M150 24H158V32H150ZM44 20H56V26H44Z"', 'sight: "M84 18H106V26H84Z"')
g = replace_once(g, 'accessory2: "M60 20H112V26H60Z"', 'accessory2: "M132 42H162V50H132Z"')
g = replace_once(g, "sight: [154, 19]", "sight: [95, 12]")
g = replace_once(g, "accessory2: [86, 14]", "accessory2: [147, 58]")

g = replace_once(g, 'sight: "M178 24H186V34H178ZM44 16H62V26H44Z"', 'sight: "M70 14H102V26H70Z"')
g = replace_once(g, "sight: [182, 19]", "sight: [86, 9]")
g = replace_once(g, 'detail: "M120 31H146M120 38H146M120 45H146M60 26V21M90 50V56"', 'detail: "M120 31H146M120 38H146M120 45H146M90 50V56"')

g = replace_once(g, 'sight: "M176 22H184V32H176ZM60 22H72V28H60Z"', 'sight: "M176 22H184V32H176Z"')

# Hover titles: gunSvg takes { titles: { region: "text" } }.
g = replace_once(g, 'className = "", view = "0 0 200 90" } = {}) {', 'className = "", view = "0 0 200 90", titles = {} } = {}) {')
g = replace_once(
    g,
    '''    return `<path class="${classes}" data-region="${region}" d="${gun[region]}"${style}/>`;''',
    '''    const title = titles[region] ? `<title>${titles[region]}</title>` : "";
    return `<path class="${classes}" data-region="${region}" d="${gun[region]}"${style}>${title}</path>`;''',
)
g = replace_once(
    g,
    '''<g class="marker" data-region="${region}" transform="translate(${x} ${y})"><circle r="6.5"/>''',
    '''<g class="marker" data-region="${region}" transform="translate(${x} ${y})">${titles[region] ? `<title>${titles[region]}</title>` : ""}<circle r="6.5"/>''',
)
(web / "guns.js").write_text(g, encoding="utf-8")

js = (web / "app.js").read_text(encoding="utf-8")
js = replace_once(
    js,
    'h("div", { class: "gunwrap", html: gunSvg(type, { fills: regionFills(detail), markers, className: "interactive" }) }),',
    'h("div", { class: "gunwrap", html: gunSvg(type, { fills: regionFills(detail), markers, className: "interactive", titles: regionTitles(detail) }) }),',
)
js = replace_once(
    js,
    "function rowPlate(item) {",
    '''/** Hover text for each region: which slot it is and what is in it. */
function regionTitles(detail) {
  const titles = {};
  for (const slot of detail.slots) {
    const part = slot.current ? parsePart(slot.current.name, slot.current.group).title : "empty";
    titles[SLOT_REGION[slot.slot]] = `${slotLabel(slot.slot)}: ${part}`.replace(/[<>&]/g, "");
  }
  return titles;
}

function rowPlate(item) {''',
)
(web / "app.js").write_text(js, encoding="utf-8")
print("patched")
