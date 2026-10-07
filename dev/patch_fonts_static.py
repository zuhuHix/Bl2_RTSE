from pathlib import Path


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


# app.js: drop the unused import
p = Path("rtse/web/app.js")
s = p.read_text(encoding="utf-8")
s = replace_once(s, "import { GUN_TYPES, SLOT_REGION, gunFor, gunSvg, plateSvg }", "import { SLOT_REGION, gunFor, gunSvg, plateSvg }")
s = replace_once(s, "void GUN_TYPES;\n", "")
p.write_text(s, encoding="utf-8")

# guns.js: drop the unused helper
g = Path("rtse/web/guns.js")
t = g.read_text(encoding="utf-8")
t = replace_once(t, 'const TAG = (name) => name.charAt(0).toUpperCase() + name.slice(1);\n\n', "")
t = replace_once(t, "\nexport { TAG };\n", "")
g.write_text(t, encoding="utf-8")

# real server: whitelist fonts
sv = Path("rtse/server.py")
c = sv.read_text(encoding="utf-8")
c = replace_once(
    c,
    'STATIC_PATH = re.compile(r"^/(?:[a-z0-9_]+\\.(?:js|css)|icons/[A-Za-z0-9_.-]+\\.(?:png|svg|webp))$")',
    'STATIC_PATH = re.compile(\n    r"^/(?:[a-z0-9_]+\\.(?:js|css)|icons/[A-Za-z0-9_.-]+\\.(?:png|svg|webp)|fonts/[a-z0-9-]+\\.woff2)$",\n)',
)
c = replace_once(c, '    ".webp": "image/webp",\n}', '    ".webp": "image/webp",\n    ".woff2": "font/woff2",\n}')
c = replace_once(c, "a flat file name in web/icons/.", "a flat file name in web/icons/ or web/fonts/.")
sv.write_text(c, encoding="utf-8")

# mock server: same
m = Path("dev/mock_server.py")
d = m.read_text(encoding="utf-8")
d = replace_once(
    d,
    r'''re.match(r"^/(?:[a-z0-9_]+\.(?:js|css)|icons/[A-Za-z0-9_.-]+\.(?:png|svg|webp))$", split.path)''',
    r'''re.match(r"^/(?:[a-z0-9_]+\.(?:js|css)|icons/[A-Za-z0-9_.-]+\.(?:png|svg|webp)|fonts/[a-z0-9-]+\.woff2)$", split.path)''',
)
d = replace_once(d, '".webp": "image/webp"}', '".webp": "image/webp", ".woff2": "font/woff2"}')
m.write_text(d, encoding="utf-8")

# smoke test: fonts are served, other font-dir names are not
sm = Path("dev/smoke_server.py")
e = sm.read_text(encoding="utf-8")
e = replace_once(
    e,
    '    check("missing icon -> 404", request("GET", "/icons/nothing.png")[0], 404)\n',
    '    check("missing icon -> 404", request("GET", "/icons/nothing.png")[0], 404)\n'
    '    check("bundled font served", request("GET", "/fonts/bebas-neue-400.woff2")[0], 200)\n'
    '    check("non-font in fonts dir refused", request("GET", "/fonts/readme.txt")[0], 403)\n',
)
sm.write_text(e, encoding="utf-8")
print("patched")
