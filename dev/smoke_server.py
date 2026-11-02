"""Smoke-tests rtse/server.py outside the game, with the game modules stubbed.

    python -I dev/smoke_server.py
"""

from __future__ import annotations

import http.client
import json
import socket
import time
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT / "dev"))
import unreal_stub  # noqa: E402

unreal_stub.install()

sys.path.insert(0, str(ROOT))
from rtse import server  # noqa: E402

# The real game may be running and listening on the default port; never share it. Use a random one.
server.DEFAULT_PORT = 0
server.Handler.timeout = 1.0  # a short socket timeout, so the stalled-body checks finish quickly
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
    # Every page the shell can open is registered in rtse/web/pages.js: check each one, not a fixed list.
    pages_js = (ROOT / "rtse" / "web" / "pages.js").read_text(encoding="utf-8")
    page_modules = re.findall(r'module:\s*"\./(page_[a-z0-9_]+)\.js"', pages_js)
    page_styles = re.findall(r'css:\s*"(page_[a-z0-9_]+)\.css"', pages_js)
    check("pages.js registers pages", int(len(page_modules) >= 6), 1, f"({len(page_modules)} modules found)")
    check("pages.js served", request("GET", "/pages.js")[0], 200)
    for module in page_modules:
        page = module.removeprefix("page_")
        check(f"{module}.js served", request("GET", f"/{module}.js")[0], 200)
        check(f"{module}.js with spoofed Host", request("GET", f"/{module}.js", {"Host": "evil.example"})[0], 403)
        check(f"{module} traversal refused", request("GET", f"/{module}.js/../../api.py")[0], 403)
        check(f"{module} encoded traversal refused", request("GET", f"/icons/..%2f{module}.js")[0], 403)
        check(f"{page} python source not served", request("GET", f"/{module}.py")[0], 403)
        check(f"{page} api without token", request("GET", f"/api/{page}")[0], 403)
    for style in page_styles:
        check(f"{style}.css served", request("GET", f"/{style}.css")[0], 200)
        check(f"{style}.css with spoofed Host", request("GET", f"/{style}.css", {"Host": "evil.example"})[0], 403)
    # Every flat .js in web/ is a page, a shell file or a helper; a new one must be served without a server edit.
    for script in sorted((ROOT / "rtse" / "web").glob("*.js")):
        check(f"{script.name} served", request("GET", f"/{script.name}")[0], 200)
    check("gear_list.js served", request("GET", "/gear_list.js")[0], 200)
    check("page_character.js served", request("GET", "/page_character.js")[0], 200)
    status, _ = request("GET", "/page_..%2fapi.py")
    results.append(("page_..%2fapi.py not served", status in (403, 404), f"{status} (expected 403 or 404)"))
    check("page_x.py refused", request("GET", "/page_x.py")[0], 403)
    check("uppercase page name refused", request("GET", "/Page_SDU.js")[0], 403)
    check("debug dump json not served", request("GET", "/debug_sdu.json")[0], 403)
    check("skill tree data not served", request("GET", "/skilltrees.json")[0], 403)
    check("skill tree data traversal refused", request("GET", "/models/..%2fskilltrees.json")[0], 403)
    check("skilltree_data.py not served", request("GET", "/skilltree_data.py")[0], 403)
    check("missing page module -> 404", request("GET", "/page_nothing.js")[0], 404)
    check("sdu post without token", request("POST", "/api/sdu/set", {"Content-Type": "application/json"}, b"{}")[0], 403)
    check("world post without token", request("POST", "/api/world/mission", {"Content-Type": "application/json"}, b"{}")[0], 403)
    check("skills reset post without token", request("POST", "/api/skills/reset", {"Content-Type": "application/json"}, b"{}")[0], 403)
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
    # Authenticated routes go through the game thread, which doesn't exist here; call them directly.
    for route in ("/api/sdu", "/api/skills", "/api/world"):
        try:
            server.api.GET_ROUTES[route]({})
            check(f"{route} outside a save -> 409", 0, 409, "no error raised")
        except server.api.ApiError as ex:
            check(f"{route} outside a save -> 409", ex.status, 409)
    for route in ("/api/skills/reset", "/api/world/missions/reset", "/api/world/playthrough"):
        try:
            server.api.POST_ROUTES[route]({})
            check(f"{route} refuses without confirm", 0, 400, "no error raised")
        except server.api.ApiError as ex:
            check(f"{route} refuses without confirm", ex.status, 400)
    # Idempotency-Key: a malformed key is refused before anything is queued.
    key_headers = {"X-RTSE-Token": token, "Idempotency-Key": "bad key!"}
    check("post with invalid idempotency key -> 400", request("POST", "/api/item/set_level", key_headers, b"{}")[0], 400)
    check(
        "post with idempotency key but no token -> 403",
        request("POST", "/api/item/set_level", {"Idempotency-Key": "abcdefgh-01"}, b"{}")[0],
        403,
    )
    # This script never pumps the game thread, so a keyed POST is still queued when the wait times out:
    # it must be cancelled (503, applied false), not left to run later.
    server.GAME_THREAD_TIMEOUT = 0.2
    status, body = request(
        "POST", "/api/item/set_level", {"X-RTSE-Token": token, "Idempotency-Key": "abcdefgh-02"}, b"{}"
    )
    check("queued keyed post times out -> 503", status, 503, body)
    results.append(("queued keyed post reports applied false", json.loads(body).get("applied") is False, body))

    def stalled_post(headers: str) -> tuple[float, bytes]:
        """Sends a POST announcing 1000 body bytes but only 10 of them, then waits. Returns (seconds until the
        server closed the connection, whatever it answered)."""
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        head = f"POST /api/item/set_level HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{headers}Content-Length: 1000\r\n\r\n"
        sock.sendall(head.encode() + b"0123456789")
        started = time.monotonic()
        answer = b""
        try:
            while chunk := sock.recv(4096):
                answer += chunk
        except OSError:
            pass  # a reset counts as closed too
        sock.close()
        return time.monotonic() - started, answer

    waited, answer = stalled_post("")
    results.append(("stalled unauthorized body: closed within the socket timeout", waited < 5, f"{waited:.1f}s"))
    results.append(("stalled unauthorized body: no answer sent", answer == b"", repr(answer[:60])))
    waited, answer = stalled_post(f"X-RTSE-Token: {token}\r\n")
    results.append(("stalled authorized body: closed within the socket timeout", waited < 5, f"{waited:.1f}s"))
    check("server still answers after stalled bodies", request("GET", "/app.js")[0], 200)
finally:
    server.stop()

failed = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
