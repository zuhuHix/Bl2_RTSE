from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:80]!r} (found {text.count(old)})"
    return text.replace(old, new)


# --- build_assets.py: a part is a LIST of slices ---
b = Path("dev/build_assets.py")
s = b.read_text(encoding="utf-8")
start = s.index("        manifest[key] = {")
end = s.index("        print(f\"{key:14}")
s = s[:start] + '''        grouped: dict[str, dict] = {}
        for p in parts:
            name = p["SkeletalMeshFragmentName"]
            origin = bounds.get(name, {}).get("Origin")
            part = grouped.setdefault(name, {"slices": [], "origin": [round(origin[a], 4) for a in "XYZ"] if origin else None})
            part["slices"].append([p["FirstIndex"], p["NumPrimitives"], p["MaterialIndex"]])  # first index, triangles, material
        manifest[key] = {
            "file": file,
            "parts": grouped,
            "sockets": [
                {k: s[k] for k in ("SocketName", "BoneName", "RelativeLocation", "RelativeRotation", "RelativeScale") if k in s}
                for s in entry.get("mesh_sockets", []) if isinstance(s, dict)
            ],
        }
''' + s[end:]
s = s.replace('{len(parts):3} parts")', '{len(grouped):3} parts ({len(parts)} slices)")')
b.write_text(s, encoding="utf-8")

# --- viewer3d.js: concatenate every slice of a part ---
v = Path("rtse/web/viewer3d.js")
t = v.read_text(encoding="utf-8")
old_start = t.index("    // Which primitive does this slice land in?")
old_end = t.index("    if (hidden.size && source.getAttribute(\"skinIndex\")) {")
t = t[:old_start] + '''    // A part is one or more slices of the shared triangle list. Primitives are laid out one after
    // another, so work out which primitive each slice lands in. Attributes are per primitive.
    const slicesByPrim = new Map();
    for (const [start, tris] of part.slices) {
      let first = start;
      let which = 0;
      while (which < model.prims.length - 1 && first >= model.prims[which].index.count) {
        first -= model.prims[which].index.count;
        which++;
      }
      if (!slicesByPrim.has(which)) slicesByPrim.set(which, []);
      slicesByPrim.get(which).push(model.prims[which].index.array.slice(first, first + tris * 3));
    }
    // One draw per primitive keeps vertex buffers shared; a part spanning primitives is rare.
    const [which, chunks] = [...slicesByPrim.entries()].sort((a, b) => b[1].length - a[1].length)[0];
    const source = model.prims[which];
    const total = chunks.reduce((n, c) => n + c.length, 0);
    let indices = new chunks[0].constructor(total);
    let at = 0;
    for (const chunk of chunks) { indices.set(chunk, at); at += chunk.length; }

''' + t[old_end:]
v.write_text(t, encoding="utf-8")
print("patched")
