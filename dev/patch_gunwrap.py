from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


js = Path("rtse/web/app.js")
s = js.read_text(encoding="utf-8")
s = replace_once(
    s,
    'h("div", { html: gunSvg(type, { fills: regionFills(detail), markers, className: "interactive" }) }),',
    'h("div", { class: "gunwrap", html: gunSvg(type, { fills: regionFills(detail), markers, className: "interactive" }) }),',
)
js.write_text(s, encoding="utf-8")

css = Path("rtse/web/app.css")
c = css.read_text(encoding="utf-8")
c = replace_once(c, ".diagram .gun { width: 100%; max-width: 660px; height: auto; }", ".gunwrap { width: 100%; display: flex; justify-content: center; }\n.diagram .gun { width: 100%; max-width: 760px; height: auto; }")
css.write_text(c, encoding="utf-8")
print("patched")
