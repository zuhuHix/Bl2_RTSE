from pathlib import Path

web = Path("rtse/web")


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


js = (web / "app.js").read_text(encoding="utf-8")
js = replace_once(
    js,
    "  const cards = list.slice(0, p.shown).map(({ option, info }) => {\n    const { tile } = partTile(option, slot.slot, \"sm\");\n    return h(\n      \"button\",\n      {\n        class: `card${p.chosen && p.chosen.id === option.id ? \" sel\" : \"\"}${option.id === currentId ? \" cur\" : \"\"}`,\n        title: option.id,",
    "  const itemType = itemTypeOf(state.detail);\n  const cards = list.slice(0, p.shown).map(({ option, info }) => {\n    const { tile } = partTile(option, slot.slot, \"sm\");\n    const foreign = itemType && info.weaponType && info.weaponType !== itemType;\n    return h(\n      \"button\",\n      {\n        class: `card${p.chosen && p.chosen.id === option.id ? \" sel\" : \"\"}${option.id === currentId ? \" cur\" : \"\"}${foreign ? \" foreign\" : \"\"}`,\n        title: foreign ? `${option.id}\\n(from a ${info.weaponType}, not a ${itemType})` : option.id,",
)
(web / "app.js").write_text(js, encoding="utf-8")

css = (web / "app.css").read_text(encoding="utf-8")
css = replace_once(
    css,
    ".card.cur { border-color: var(--equipped); }",
    ".card.cur { border-color: var(--equipped); }\n.card { position: relative; }\n.card.foreign::after { content: \"\"; position: absolute; top: 6px; right: 6px; width: 7px; height: 7px; border-radius: 50%; background: var(--warn); box-shadow: 0 0 6px var(--warn); }",
)
(web / "app.css").write_text(css, encoding="utf-8")
print("patched")
