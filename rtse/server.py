"""Local-only HTTP server exposing the editor API and web UI."""

from __future__ import annotations

import json
import re
import secrets
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from unrealsdk import logging

from . import api, gamethread

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WEB_DIR = Path(__file__).parent / "web"
GAME_THREAD_TIMEOUT = 15.0  # the first part scan walks every balance definition
MAX_BODY_BYTES = 64 * 1024

# Plain front-end files. Served without the token (they hold no secrets; every API call needs it),
# but only from this whitelist: a flat file name in web/, or a flat file name in web/icons/, web/fonts/ or web/models/ or web/portraits/, or a .js file under web/vendor/three/.
STATIC_PATH = re.compile(
    r"^/(?:[a-z0-9_]+\.(?:js|css)"
    r"|icons/[A-Za-z0-9_.-]+\.(?:png|svg|webp)"
    r"|fonts/[a-z0-9-]+\.woff2"
    r"|models/[a-z_]+\.(?:glb|json)"
    r"|portraits/[a-z]+\.webp"
    r"|vendor/three/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.js)$",
)
STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".glb": "model/gltf-binary",
    ".json": "application/json",
}

_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None
token: str = ""
port: int = 0


class Handler(BaseHTTPRequestHandler):
    server_version = "RTSE"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep the game console quiet

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _host_ok(self) -> bool:
        # Defeats DNS rebinding: only requests addressed to our own loopback name are served.
        return self.headers.get("Host", "") in {f"{HOST}:{port}", f"localhost:{port}"}

    def _serve_static(self, path: str) -> None:
        file = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR.resolve() not in file.parents or not file.is_file():
            self._json(404, {"error": "not found"})
            return
        self._send(200, file.read_bytes(), STATIC_TYPES[file.suffix.lower()])

    def _authorized(self, *, header_only: bool) -> bool:
        # State-changing requests must carry the token in a custom header (which browsers will not
        # send cross-origin without a preflight we never approve), never in the URL.
        if not self._host_ok():
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{HOST}:{port}", f"http://localhost:{port}"}:
            return False
        supplied = self.headers.get("X-RTSE-Token", "")
        if not supplied and not header_only:
            supplied = parse_qs(urlsplit(self.path).query).get("t", [""])[0]
        return secrets.compare_digest(supplied, token)

    def _dispatch(self, route: api.Route, params: api.Params) -> None:
        try:
            result = gamethread.submit(lambda: route(params)).result(timeout=GAME_THREAD_TIMEOUT)
        except FutureTimeout:
            self._json(503, {"error": "game did not respond (loading or paused?)"})
        except api.ApiError as ex:
            self._json(ex.status, {"error": str(ex)})
        except Exception as ex:  # noqa: BLE001
            logging.error(f"RTSE: {self.path} failed: {ex!r}")
            self._json(500, {"error": repr(ex)})
        else:
            self._json(200, result)

    def do_GET(self) -> None:  # noqa: N802
        split = urlsplit(self.path)
        if STATIC_PATH.match(split.path):
            if self._host_ok():
                self._serve_static(split.path)
            else:
                self._json(403, {"error": "forbidden"})
            return

        if not self._authorized(header_only=False):
            self._json(403, {"error": "forbidden"})
            return

        if split.path == "/":
            self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            return

        route = api.GET_ROUTES.get(split.path)
        if route is None:
            self._json(404, {"error": "not found"})
            return
        params = {k: v[0] for k, v in parse_qs(split.query).items() if k != "t"}
        self._dispatch(route, params)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized(header_only=True):
            self._json(403, {"error": "forbidden"})
            return

        route = api.POST_ROUTES.get(urlsplit(self.path).path)
        if route is None:
            self._json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError
            params = json.loads(self.rfile.read(length))
            if not isinstance(params, dict):
                raise ValueError
        except ValueError:
            self._json(400, {"error": "body must be a JSON object"})
            return
        self._dispatch(route, params)


def start() -> str:
    """Starts the server if needed and returns the URL (including the session token)."""
    global _server, _thread, token, port
    if _server is None:
        token = secrets.token_urlsafe(24)
        try:
            _server = ThreadingHTTPServer((HOST, DEFAULT_PORT), Handler)
        except OSError:
            _server = ThreadingHTTPServer((HOST, 0), Handler)
        _server.daemon_threads = True
        port = _server.server_address[1]
        _thread = threading.Thread(target=_server.serve_forever, name="RTSE-http", daemon=True)
        _thread.start()
    return url()


def stop() -> None:
    global _server, _thread
    if _server is not None:
        _server.shutdown()
        _server.server_close()
    _server = None
    _thread = None


def url() -> str:
    return f"http://{HOST}:{port}/?t={token}"
