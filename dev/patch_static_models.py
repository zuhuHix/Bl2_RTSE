from pathlib import Path
import shutil
import urllib.request

ROOT = Path.cwd()


def replace_once(text, old, new):
    assert text.count(old) == 1, f"expected one match: {old[:70]!r} (found {text.count(old)})"
    return text.replace(old, new)


# --- three.js, served locally (no CDN) ---
vendor = ROOT / "rtse" / "web" / "vendor" / "three"
for sub in ("loaders", "utils", "controls"):
    (vendor / sub).mkdir(parents=True, exist_ok=True)
src = ROOT / "dev" / "vendor" / "three"
shutil.copy(src / "three.module.js", vendor / "three.module.js")
shutil.copy(src / "loaders" / "GLTFLoader.js", vendor / "loaders" / "GLTFLoader.js")
shutil.copy(src / "utils" / "BufferGeometryUtils.js", vendor / "utils" / "BufferGeometryUtils.js")
controls = vendor / "controls" / "OrbitControls.js"
if not controls.exists():
    with urllib.request.urlopen("https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js", timeout=60) as response:
        controls.write_bytes(response.read())
print("three.js files:", sorted(str(p.relative_to(vendor)) for p in vendor.rglob("*.js")))

# --- real server: widen the static whitelist ---
OLD = r'''    r"^/(?:[a-z0-9_]+\.(?:js|css)|icons/[A-Za-z0-9_.-]+\.(?:png|svg|webp)|fonts/[a-z0-9-]+\.woff2)$",'''
NEW = r'''    r"^/(?:[a-z0-9_]+\.(?:js|css)"
    r"|icons/[A-Za-z0-9_.-]+\.(?:png|svg|webp)"
    r"|fonts/[a-z0-9-]+\.woff2"
    r"|models/[a-z_]+\.(?:glb|json)"
    r"|vendor/three/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.js)$",'''
sv = ROOT / "rtse" / "server.py"
s = sv.read_text(encoding="utf-8")
s = replace_once(s, OLD, NEW)
s = replace_once(s, '    ".woff2": "font/woff2",\n}', '    ".woff2": "font/woff2",\n    ".glb": "model/gltf-binary",\n    ".json": "application/json",\n}')
s = replace_once(s, "a flat file name in web/icons/ or web/fonts/.", "a flat file name in web/icons/, web/fonts/ or web/models/, or a .js file under web/vendor/three/.")
sv.write_text(s, encoding="utf-8")

# --- mock server: same rules ---
m = ROOT / "dev" / "mock_server.py"
t = m.read_text(encoding="utf-8")
old_re = r'''re.match(r"^/(?:[a-z0-9_]+\.(?:js|css)|icons/[A-Za-z0-9_.-]+\.(?:png|svg|webp)|fonts/[a-z0-9-]+\.woff2)$", split.path)'''
new_re = r'''re.match(r"^/(?:[a-z0-9_]+\.(?:js|css)|icons/[A-Za-z0-9_.-]+\.(?:png|svg|webp)|fonts/[a-z0-9-]+\.woff2|models/[a-z_]+\.(?:glb|json)|vendor/three/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.js)$", split.path)'''
t = replace_once(t, old_re, new_re)
t = replace_once(t, '".woff2": "font/woff2"}', '".woff2": "font/woff2", ".glb": "model/gltf-binary", ".json": "application/json"}')
m.write_text(t, encoding="utf-8")

# --- smoke tests ---
sm = ROOT / "dev" / "smoke_server.py"
e = sm.read_text(encoding="utf-8")
e = replace_once(
    e,
    '    check("non-font in fonts dir refused", request("GET", "/fonts/readme.txt")[0], 403)\n',
    '    check("non-font in fonts dir refused", request("GET", "/fonts/readme.txt")[0], 403)\n'
    '    check("model served", request("GET", "/models/pistol.glb")[0], 200)\n'
    '    check("model table served", request("GET", "/models/parts.json")[0], 200)\n'
    '    check("three.js served", request("GET", "/vendor/three/three.module.js")[0], 200)\n'
    '    check("three loader served", request("GET", "/vendor/three/loaders/GLTFLoader.js")[0], 200)\n'
    '    check("vendor traversal refused", request("GET", "/vendor/three/../../server.py")[0], 403)\n'
    '    check("vendor dotdot dir refused", request("GET", "/vendor/three/loaders/../../../api.py")[0], 403)\n'
    '    check("non-js in vendor refused", request("GET", "/vendor/three/readme.txt")[0], 403)\n'
    '    check("model traversal refused", request("GET", "/models/../api.py")[0], 403)\n',
)
sm.write_text(e, encoding="utf-8")
print("patched")
