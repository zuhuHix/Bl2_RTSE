"""Smoke-tests rtse/server.py outside the game, with the game modules stubbed.

    python -I dev/smoke_server.py
"""

from __future__ import annotations

import http.client
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def stub(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class Anything:
    """Stands in for any game class or struct used only in annotations."""


stub(
    "unrealsdk",
    logging=types.SimpleNamespace(info=lambda *_: None, error=print),
    unreal=types.SimpleNamespace(UObject=Anything, WrappedStruct=Anything, BoundFunction=Anything),
    find_all=lambda *_: [],
    find_object=lambda *_: None,
)
stub("unrealsdk.hooks", Type=types.SimpleNamespace(POST=1), add_hook=lambda *_: None, remove_hook=lambda *_: None)
stub(
    "mods_base",
    get_pc=lambda **_: None,
    build_mod=lambda **_: None,
    hook=lambda *_a, **_k: (lambda f: f),
    keybind=lambda *_a, **_k: (lambda f: f),
)

sys.path.insert(0, str(ROOT))
from rtse import server  # noqa: E402

# The real game may be running and listening on the default port; never share it. Use a random one.
server.DEFAULT_PORT = 0
url = server.start()
port, token = server.port, server.token
assert port != 8765, "test server must not use the game's port"
results: list[tuple[str, bool, str]] = []


def request(method: str, path: str, headers: dict | None = None, body: bytes | None = None) -> tuple[int, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest(method, path, skip_host=True)
    merged = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    for key, value in merged.items():
        connection.putheader(key, value)
    if body is not None:
        connection.putheader("Content-Length", str(len(body)))
    connection.endheaders(body)
    response = connection.getresponse()
    text = response.read().decode(errors="replace")
    connection.close()
    return response.status, text


def check(name: str, got: int, expected: int, detail: str = "") -> None:
    results.append((name, got == expected, f"{got} (expected {expected}) {detail}".strip()))


try:
    status, body = request("GET", "/app.js")
    check("static js without token", status, 200, "import" if "import" in body else "NO CONTENT")
    check("static css without token", request("GET", "/app.css")[0], 200)
    check("missing icon -> 404", request("GET", "/icons/nothing.png")[0], 404)
    check("bundled font served", request("GET", "/fonts/bebas-neue-400.woff2")[0], 200)
    check("non-font in fonts dir refused", request("GET", "/fonts/readme.txt")[0], 403)
    check("model served", request("GET", "/models/pistol.glb")[0], 200)
    check("model table served", request("GET", "/models/parts.json")[0], 200)
    check("portrait served", request("GET", "/portraits/siren.webp")[0], 200)
    check("portrait traversal refused", request("GET", "/portraits/../api.py")[0], 403)
    check("three.js served", request("GET", "/vendor/three/three.module.js")[0], 200)
    check("three loader served", request("GET", "/vendor/three/loaders/GLTFLoader.js")[0], 200)
    check("vendor traversal refused", request("GET", "/vendor/three/../../server.py")[0], 403)
    check("vendor dotdot dir refused", request("GET", "/vendor/three/loaders/../../../api.py")[0], 403)
    check("non-js in vendor refused", request("GET", "/vendor/three/readme.txt")[0], 403)
    check("model traversal refused", request("GET", "/models/../api.py")[0], 403)
    check("dotty icon name -> 404", request("GET", "/icons/..png")[0], 404)
    check("traversal via dots", request("GET", "/../api.py")[0], 403)
    check("traversal via encoded slash", request("GET", "/icons/..%2f..%2fapi.py")[0], 403)
    check("traversal with token", request("GET", f"/icons/..%2f..%2fapi.py?t={token}")[0], 404)
    check("python source not served", request("GET", "/api.py")[0], 403)
    check("overrides json not served", request("GET", "/overrides.json")[0], 403)
    check("static with spoofed Host", request("GET", "/app.js", {"Host": "evil.example"})[0], 403)
    check("index without token", request("GET", "/")[0], 403)
    check("index with token", request("GET", f"/?t={token}")[0], 200)
    check("index with wrong token", request("GET", "/?t=nope")[0], 403)
    check("api without token", request("GET", "/api/items")[0], 403)
    check("post without token", request("POST", "/api/item/set_level", {"Content-Type": "application/json"}, b"{}")[0], 403)
    check("post token in url only", request("POST", f"/api/item/set_level?t={token}", {}, b"{}")[0], 403)
    check(
        "post with foreign Origin",
        request("POST", "/api/item/set_level", {"X-RTSE-Token": token, "Origin": "http://evil.example"}, b"{}")[0],
        403,
    )
    check("post bad json", request("POST", "/api/item/set_level", {"X-RTSE-Token": token}, b"not json")[0], 400)
finally:
    server.stop()

failed = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
