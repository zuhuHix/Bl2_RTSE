from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


web = Path("rtse/web")

css = (web / "app.css").read_text(encoding="utf-8")

# sidebar rows: plate (76) + its 14px margin = 90px column
css = replace_once(css, ".row { display: grid; grid-template-columns: 76px 1fr auto;", ".row { display: grid; grid-template-columns: 90px 1fr auto;")

# colour used to carve details out of drawn glyphs; brighter dim colour for plate art
css = replace_once(css, "  --plate-dim: #3a352b;", "  --plate-dim: #5a523f;")
css = replace_once(css, ".plate { width: 76px;", ".plate { --cut: var(--ink); width: 76px;")

# bench: gun across the top, legend underneath
start = css.index(".bench-body {")
end = css.index(".leg {")
css = css[:start] + '''.bench-body { display: block; }
.diagram { padding: 22px 26px 18px; border-bottom: 3px solid #000; background-color: #15130f; background-image: linear-gradient(rgba(255, 210, 31, .05) 1px, transparent 1px), linear-gradient(90deg, rgba(255, 210, 31, .05) 1px, transparent 1px); background-size: 24px 24px; display: flex; flex-direction: column; align-items: center; gap: 12px; }
.diagram .gun { width: 100%; max-width: 660px; height: auto; }
.diagram .item-big { --cut: #0b0a09; width: 190px; height: 190px; margin: 6px auto; color: var(--c, var(--bone)); filter: drop-shadow(5px 6px 0 #000); }
.diagram .cap { font-size: 11.5px; letter-spacing: .2em; text-transform: uppercase; color: var(--muted); font-weight: 700; text-align: center; }
.diagram .cap i { color: var(--hazard); font-style: normal; }
.legend { list-style: none; margin: 0; padding: 18px 18px 20px; display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 10px 14px; align-content: start; }
''' + css[end:]

# effect rows: only the arrow and the number take the colour
css = replace_once(css, ".fx div span { flex: 1; }", ".fx div span { flex: 1; text-align: left; color: var(--bone); }")
css = replace_once(css, ".fx b { font-family: var(--display);", ".fx b { white-space: nowrap; font-family: var(--display);")

# media query that referenced the old two-column layout
css = replace_once(css, "  .bench-body { grid-template-columns: 1fr; }\n  .diagram { border-right: 0; border-bottom: 3px solid #000; }\n", "")

# markers a little smaller (the diagram is drawn larger now)
css = replace_once(css, ".gun .marker text { fill: var(--hazard); font-family: var(--display); font-size: 12px; }", ".gun .marker text { fill: var(--hazard); font-family: var(--display); font-size: 9.5px; }")
(web / "app.css").write_text(css, encoding="utf-8")

guns = (web / "guns.js").read_text(encoding="utf-8")
guns = replace_once(guns, '<circle r="8"/><text y="3.4" text-anchor="middle">', '<circle r="6.5"/><text y="3.1" text-anchor="middle">')
(web / "guns.js").write_text(guns, encoding="utf-8")
print("patched")
