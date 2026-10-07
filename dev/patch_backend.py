"""One-off patch: static file serving + part-info API. Run from the rtse folder."""

from pathlib import Path

# ---------------- server.py: static files ----------------
sp = Path("server.py")
s = sp.read_text(encoding="utf-8")


def replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"expected exactly one match for: {old[:60]!r}"
    return text.replace(old, new)


s = replace_once(s, "import json\nimport secrets\n", "import json\nimport re\nimport secrets\n")
s = replace_once(
    s,
    "_server: ThreadingHTTPServer | None = None",
    '''# Plain front-end files. Served without the token (they hold no secrets; every API call needs it),
# but only from this whitelist: a flat file name in web/, or a flat file name in web/icons/.
STATIC_PATH = re.compile(r"^/(?:[a-z0-9_]+\\.(?:js|css)|icons/[A-Za-z0-9_.-]+\\.(?:png|svg|webp))$")
STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}

_server: ThreadingHTTPServer | None = None''',
)
s = replace_once(
    s,
    '''    def _authorized(self, *, header_only: bool) -> bool:
        # The Host check defeats DNS rebinding; the token stops other pages/processes guessing.
        # State-changing requests must carry the token in a custom header (which browsers will not
        # send cross-origin without a preflight we never approve), never in the URL.
        host = self.headers.get("Host", "")
        if host not in {f"{HOST}:{port}", f"localhost:{port}"}:
            return False
''',
    '''    def _host_ok(self) -> bool:
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
''',
)
s = replace_once(
    s,
    '''    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized(header_only=False):
            self._json(403, {"error": "forbidden"})
            return

        split = urlsplit(self.path)
''',
    '''    def do_GET(self) -> None:  # noqa: N802
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

''',
)
sp.write_text(s, encoding="utf-8")

# ---------------- api.py ----------------
p = Path("api.py")
a = p.read_text(encoding="utf-8")

a = replace_once(
    a,
    '    return {"id": ref.id, "name": name or ref.id, "group": package}',
    '    return {"id": ref.id, "name": name or ref.id, "group": package, "class": ref.class_name}',
)

a = replace_once(
    a,
    '''            {"id": _item_id(item), "class": item.Class.Name, "name": item.GetHumanReadableName(), "location": location}
            for item, location in found''',
    '''            {
                "id": _item_id(item),
                "class": item.Class.Name,
                "name": item.GetHumanReadableName(),
                "location": location,
                "level": getattr(item, "ExpLevel", None),
                "rarity": getattr(item, "RarityLevel", None),
            }
            for item, location in found''',
)

a = replace_once(
    a,
    "def rescan_parts(",
    '''MAX_EFFECTS = 40


def _struct_summary(struct: Any) -> dict[str, Any]:
    """Flattens the simple fields of a struct, e.g. one attribute effect of a part."""
    out: dict[str, Any] = {}
    for prop in struct._type._fields():  # noqa: SLF001
        try:
            value = getattr(struct, prop.Name)
        except Exception:  # noqa: BLE001, S112
            continue
        if isinstance(value, unreal.UObject):
            out[prop.Name] = value.Name
        elif isinstance(value, Enum):
            out[prop.Name] = value.name
        elif value is None or isinstance(value, (bool, int, float, str)):
            out[prop.Name] = value
    return out


def _effect_view(summary: dict[str, Any]) -> dict[str, Any]:
    """Picks out the attribute, how it is modified and by how much; keeps the rest for display."""
    attribute = next((v for k, v in summary.items() if "Attribute" in k and isinstance(v, str)), None)
    kind = next((v for k, v in summary.items() if "Type" in k and isinstance(v, str)), None)
    value = next(
        (v for k, v in summary.items() if "Value" in k and isinstance(v, (int, float)) and not isinstance(v, bool)),
        None,
    )
    return {"attribute": attribute, "type": kind, "value": value, "raw": summary}


def _part_object(params: Params) -> unreal.UObject:
    part_id, class_name = params.get("id"), params.get("cls")
    if not isinstance(part_id, str) or not isinstance(class_name, str):
        raise ApiError(400, "'id' and 'cls' are required")
    try:
        part = find_object(class_name, part_id)
    except Exception:  # noqa: BLE001
        part = None
    if part is None:
        raise ApiError(404, "that part is not loaded right now")
    return part


def _part_effects(part: unreal.UObject) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    for prop in part.Class._fields():  # noqa: SLF001
        if "Effect" not in prop.Name:
            continue
        try:
            elements = list(getattr(part, prop.Name))
        except Exception:  # noqa: BLE001, S112
            continue  # Not an array
        for element in elements[:MAX_EFFECTS]:
            try:
                effects.append({"source": prop.Name, **_effect_view(_struct_summary(element))})
            except Exception:  # noqa: BLE001, S112
                continue
    return effects


def part_info(params: Params) -> dict[str, Any]:
    """What a part does, read from the part's own attribute-effect lists."""
    part = _part_object(params)
    return {"id": params["id"], "effects": _part_effects(part)}


def debug_part(params: Params) -> dict[str, Any]:
    """Dumps a part's simple fields and raw effects, to refine how effects are read."""
    part = _part_object(params)
    simple: dict[str, Any] = {}
    for prop in part.Class._fields():  # noqa: SLF001
        try:
            value = getattr(part, prop.Name)
        except Exception:  # noqa: BLE001, S112
            continue
        if isinstance(value, unreal.UObject):
            simple[prop.Name] = value._path_name()  # noqa: SLF001
        elif value is None or isinstance(value, (bool, int, float, str)):
            simple[prop.Name] = value
        elif isinstance(value, Enum):
            simple[prop.Name] = value.name
        else:
            simple[prop.Name] = f"<{type(value).__name__}>"
    out = {"id": params["id"], "fields": simple, "effects": _part_effects(part)}
    DEBUG_PART_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return {"saved_to": str(DEBUG_PART_FILE), "effects": len(out["effects"])}


def rescan_parts(''',
)
a = replace_once(a, "import json\nfrom pathlib import Path\n", "import json\nfrom enum import Enum\nfrom pathlib import Path\n")
a = replace_once(
    a,
    'DEBUG_INVENTORY_FILE = Path(__file__).parent / "debug_inventory.json"',
    'DEBUG_INVENTORY_FILE = Path(__file__).parent / "debug_inventory.json"\nDEBUG_PART_FILE = Path(__file__).parent / "debug_part.json"',
)
a = replace_once(
    a,
    '    "/api/debug/inventory": debug_inventory,\n',
    '    "/api/debug/inventory": debug_inventory,\n    "/api/part": part_info,\n    "/api/debug/part": debug_part,\n',
)
p.write_text(a, encoding="utf-8")
print("patched server.py and api.py")
