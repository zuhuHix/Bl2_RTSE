from pathlib import Path

web = Path("rtse/web")

# --- CSS arrow: replace the corrupted content value with the real character ---
css = (web / "app.css").read_text(encoding="utf-8")
lines = css.split("\n")
for i, line in enumerate(lines):
    if line.startswith(".drop > summary::before"):
        lines[i] = '.drop > summary::before { content: "▸"; color: var(--accent); transition: transform .15s; display: inline-block; }'
        break
else:
    raise SystemExit("arrow rule not found")
(web / "app.css").write_text("\n".join(lines), encoding="utf-8")

# --- meta.js: fuller titles when the leftover name is empty or just a number ---
meta = (web / "meta.js").read_text(encoding="utf-8")
old = """  info.title = rest.length
    ? titleCase(rest)
    : info.element
      ? ELEMENTS[info.element].label
      : info.rarity
        ? titleCase([info.rarity])
        : titleCase([info.slot || "part"]);
  return info;"""
new = """  const nameless = rest.every((word) => /^\\d+$/.test(word)); // nothing but numbers (or nothing)
  if (info.element && nameless) {
    info.title = ELEMENTS[info.element].label;
  } else if (nameless) {
    // e.g. Pistol_Body_Hyperion_4 -> "Hyperion Body 4"
    const words = [info.manufacturer && MANUFACTURERS[info.manufacturer].label, info.slot, ...rest].filter(Boolean);
    info.title = words.length ? titleCase(words.map((w) => w.trim())) : titleCase([info.rarity || "part"]);
  } else {
    info.title = titleCase(rest);
  }
  return info;"""
assert meta.count(old) == 1, "title block not found"
(web / "meta.js").write_text(meta.replace(old, new), encoding="utf-8")
print("patched")
