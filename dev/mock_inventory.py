"""Mock of the item code, inventory and bank routes (see the stream B contract), for front-end work.

Loaded by dev/mock_server.py, which serves /api/items itself. Backpack and Equipped items live in the server's own
ITEMS list, so imports, copies and moves show up in the sidebar. The bank is a list kept here.

A code is "BL2(" + base64 of a small JSON payload, so export and import round-trip inside the mock. A code whose
base64 is valid but whose payload isn't ours gets a made-up card. A valid code whose body contains NOCARD (for
example BL2(NOCARDAA)) gets no card, only a warning, like a game that could not decode it.
RTSE_MOCK_BANK=closed makes the bank unavailable.

Chunks: preview and import answer CHUNK codes or items per request (the real server budgets by time and by count, so
the page must keep asking). Imports are additive: "replace" is refused, and "equipped" decides what happens to Equipped rows.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sys

BACKPACK_MAX = 39
BANK_MAX = 24
MAX_CODES = 200  # the most codes one preview text may hold
CHUNK = 7  # codes or items per preview/import request; small on purpose, so the page shows several chunks
EQUIPPED_RULES = ("skip", "backpack")
BANK_OPEN = os.environ.get("RTSE_MOCK_BANK") != "closed"
UNVERIFIED = "Bank access and item import use game calls nobody has tested in a live game yet"
CODE = re.compile(r"BL2\(([^()\s]*)\)")
GONE_BANK = "that bank entry is gone"
GUN_PATHS = {  # the same weapon type paths as mock_server.GUN_TYPES (used only when the server isn't loaded)
    "Pistol": "GD_Weap_Pistol.A_Weapons.WeaponType_Pistol",
    "SMG": "GD_Weap_SMG.A_Weapons.WeaponType_SMG",
    "Assault Rifle": "GD_Weap_AssaultRifle.A_Weapons.WeaponType_AssaultRifle",
    "Shotgun": "GD_Weap_Shotgun.A_Weapons.WeaponType_Shotgun",
    "Sniper Rifle": "GD_Weap_SniperRifles.A_Weapons.WeaponType_Sniper",
    "Launcher": "GD_Weap_Launchers.A_Weapons.WeaponType_Launcher",
}
# name, class, kind, weapon key, level, rarity, mock item whose parts (build) to copy
BANK_SEEDS = [
    ("Hyperion 26 Earnest Pistol", "WillowWeapon", "weapon", "Pistol", 26, 4, "Earnest"),
    ("Maliwan 29 Slag Storm", "WillowWeapon", "weapon", "SMG", 29, 2, "Slag Storm"),
    ("Vladof 33 Bunny Gun", "WillowWeapon", "weapon", "Assault Rifle", 33, 6, "Bunny Gun"),
    ("Dahl 20 Scoped Sniper", "WillowWeapon", "weapon", "Sniper Rifle", 20, 1, "Dahl 22"),
    ("Hyperion 24 Sweeper", "WillowWeapon", "weapon", "Shotgun", 24, 4, "Sweeper"),
    ("Torgue 36 Nukem Launcher", "WillowWeapon", "weapon", "Launcher", 36, 7, "Nukem"),
    ("Tediore 28 Rapid Shield", "WillowShield", "shield", "Shield", 28, 3, "Tediore Rapid"),
    ("Gilded Relic", "WillowArtifact", "none", None, 30, 6, None),
]


class _Item:
    """Fallback with the attributes of mock_server.Item, for when this module is imported without the server."""

    def __init__(self, id_, cls, name, location, level, rarity, kind, key=None, parts=None, hide=()):
        self.id, self.cls, self.name, self.location = id_, cls, name, location
        self.level, self.rarity, self.kind, self.key = level, rarity, kind, key
        self.parts = dict(parts or {})
        self.hide = list(hide)
        self.overrides = {}

    @property
    def type_path(self):
        return GUN_PATHS.get(self.key) if self.kind == "weapon" else None


_LOCAL_ITEMS: list = []
_BANK: list | None = None  # made on first use, from BANK_SEEDS
_UIDS: dict[str, int] = {}  # item id -> UniqueId; an item keeps its id and UniqueId when it moves
_last_number = [0]


# ---------- the shared state ----------

def _server():
    """The mock server, when this module runs under it (its item list is then the sidebar's)."""
    module = sys.modules.get("__main__")
    return module if hasattr(module, "ITEMS") and hasattr(module, "Item") else None


def _items() -> list:
    server = _server()
    return server.ITEMS if server else _LOCAL_ITEMS


def _bank() -> list:
    global _BANK
    if _BANK is None:
        _BANK = [_seed_item(spec, "Bank") for spec in BANK_SEEDS]
    return _BANK


def _all_known() -> list:
    return _items() + (_BANK or [])


def _next_number() -> int:
    numbers = [int(m.group(1)) for item in _all_known() if (m := re.search(r"_(\d+)$", item.id))]
    _last_number[0] = max([_last_number[0], *numbers]) + 1
    return _last_number[0]


def _make(cls, name, location, level, rarity, kind, key=None, parts=None, hide=()):
    """A new item with a fresh transient id. Not yet placed anywhere."""
    server = _server()
    factory = server.Item if server else _Item
    return factory(f"Transient.{cls}_{_next_number()}", cls, name, location, level, rarity, kind, key, dict(parts or {}), list(hide))


def _source(fragment):
    """The first mock item whose name contains `fragment`, for copying its build."""
    if not fragment:
        return None
    return next((item for item in _items() if fragment in item.name), None)


def _seed_item(spec, location):
    name, cls, kind, key, level, rarity, source_name = spec
    source = _source(source_name)
    return _make(cls, name, location, level, rarity, kind, key, source.parts if source else None, source.hide if source else ())


def _uid(item) -> int:
    if item.id not in _UIDS:
        _UIDS[item.id] = _fresh_uid()
    return _UIDS[item.id]


def _fresh_uid() -> int:
    """A UniqueId no item or bank entry has had in this mock."""
    return max([4000, *_UIDS.values()]) + 1


def _free(destination: str) -> int:
    if destination == "bank":
        return BANK_MAX - len(_bank())
    return BACKPACK_MAX - sum(1 for item in _items() if item.location == "Backpack")


def _ensure_bank_open() -> None:
    if not BANK_OPEN:
        raise ValueError("the bank is closed: open it in the game first")


def _card_id(item) -> str:
    if item.location == "Bank":
        return f"bank:{_bank().index(item)}:{_uid(item)}"
    return item.id


def _card(item, card_id: str, unique_id: int) -> dict:
    build = [
        {"slot": slot, "name": part["name"], "group": part["group"], "meshes": part["meshes"]}
        for slot, part in item.parts.items() if part
    ]
    return {
        "id": card_id, "class": item.cls, "name": item.name, "location": item.location, "level": item.level,
        "rarity": item.rarity, "type_path": item.type_path, "build": build, "hide_bones": list(item.hide),
        "unique_id": unique_id,
    }


def _locate(item_id: str, write: bool = False):
    """The item behind a backpack, equipped or bank id. LookupError when it is gone."""
    if item_id.startswith("bank:"):
        if write:
            _ensure_bank_open()
        bits = item_id.split(":")
        try:
            index, uid = int(bits[1]), int(bits[2])
        except (IndexError, ValueError):
            raise LookupError(GONE_BANK) from None
        bank = _bank()
        if 0 <= index < len(bank) and _uid(bank[index]) == uid:
            return bank[index]
        raise LookupError(GONE_BANK)
    for item in _items():
        if item.id == item_id:
            return item
    raise LookupError("that item is no longer in your inventory")


def _remove(item) -> None:
    (_bank() if item.location == "Bank" else _items()).remove(item)


def _place(item, destination: str, unique_id: int):
    """Puts an item at the end of the backpack or the bank. Returns (card id, UniqueId)."""
    _UIDS[item.id] = unique_id
    if destination == "bank":
        item.location = "Bank"
        _bank().append(item)
    else:
        item.location = "Backpack"
        _items().append(item)
    return _card_id(item), unique_id


def _clone(item):
    copy = _make(item.cls, item.name, "Backpack", item.level, item.rarity, item.kind, item.key, item.parts, item.hide)
    copy.overrides = dict(getattr(item, "overrides", {}))
    return copy


def _encode(item, unique_id: int) -> str:
    payload = {
        "v": 1, "c": item.cls, "n": item.name, "k": item.kind, "key": item.key, "l": item.level, "r": item.rarity,
        "u": unique_id, "p": {slot: (part["id"] if part else None) for slot, part in item.parts.items()}, "h": list(item.hide),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return "BL2(" + base64.b64encode(raw).decode() + ")"


def _from_code(inner: str):
    """(item, UniqueId, came_from_export) for the base64 body of a code. ValueError when it isn't base64."""
    try:
        raw = base64.b64decode(inner, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("not a valid BL2 code") from None
    if not raw:
        raise ValueError("not a valid BL2 code")
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and all(isinstance(data.get(k), str) for k in ("c", "n", "k")):
        server = _server()
        raw_parts = data.get("p") if isinstance(data.get("p"), dict) else {}
        parts = {
            slot: (server.BY_ID.get(pid) if server and isinstance(pid, str) else None)
            for slot, pid in raw_parts.items()
        }
        level = data.get("l") if isinstance(data.get("l"), int) else None
        rarity = data.get("r") if isinstance(data.get("r"), int) else None
        key = data.get("key") if isinstance(data.get("key"), str) else None
        hide = [h for h in data.get("h", []) if isinstance(h, str)] if isinstance(data.get("h"), list) else []
        uid = data.get("u") if isinstance(data.get("u"), int) else 0
        item = _make(data["c"], data["n"], "Preview", level, rarity, data["k"], key, parts, hide)
        return item, uid, True
    digest = hashlib.sha256(raw).digest()
    item = _seed_item(BANK_SEEDS[digest[0] % len(BANK_SEEDS)], "Preview")
    return item, 100000 + int.from_bytes(digest[1:4], "big") % 900000, False


def _need(params: dict, key: str):
    value = params.get(key)
    if value is None or value == "":
        raise ValueError(f"missing {key}")
    return value


# ---------- routes ----------

def _get_bank(_params):
    if not BANK_OPEN:
        return {
            "available": False, "reason": "The bank is closed (the mock runs with RTSE_MOCK_BANK=closed). Open it in the game first.",
            "slots": {"used": 0, "max": BANK_MAX}, "items": [], "warnings": [], "unverified": UNVERIFIED,
        }
    bank = _bank()
    return {
        "available": True, "reason": None, "slots": {"used": len(bank), "max": BANK_MAX},
        "items": [_card(item, _card_id(item), _uid(item)) for item in bank], "warnings": [], "unverified": UNVERIFIED,
    }


def _get_codes_export(params):
    item = _locate(_need(params, "id"))
    uid = _uid(item)
    return {
        "id": params["id"], "name": item.name, "code": _encode(item, uid),
        "header": {"kind": "weapon" if item.kind == "weapon" else "item", "unique_id": uid, "checksum_ok": True},
        "warnings": [],
    }


def _preview_entry(index: int, token: str, inner: str, used: set) -> dict:
    try:
        item, uid, _ = _from_code(inner)
    except ValueError as error:
        return {"index": index, "code": token, "ok": False, "error": str(error), "kind": None, "unique_id": None,
                "card": None, "uid_clash": False}
    kind = "weapon" if item.kind == "weapon" else "item"
    if "NOCARD" in inner:  # the game decoded the code but could not build a card for it
        return {
            "index": index, "code": token, "ok": True, "error": None, "kind": kind, "unique_id": uid, "card": None,
            "warning": "The game could not read this item's card. The code is valid and can still be imported.",
            "uid_clash": uid in used,
        }
    return {
        "index": index, "code": token, "ok": True, "error": None,
        "kind": kind, "unique_id": uid,
        "card": _card(item, f"preview:{index}", uid), "uid_clash": uid in used,
    }


def _int_param(params: dict, key: str, default: int) -> int:
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a whole number") from None


def _get_codes_preview(params):
    text = params.get("text") or ""
    start = _int_param(params, "start", 0)
    matches = list(CODE.finditer(text))
    if len(matches) > MAX_CODES:
        raise ValueError(f"at most {MAX_CODES} codes at a time")
    if not 0 <= start <= len(matches):
        raise ValueError(f"start must be between 0 and {len(matches)}")
    used = {_uid(item) for item in _all_known()}
    end = min(start + CHUNK, len(matches))
    entries = [_preview_entry(i, matches[i].group(0), matches[i].group(1), used) for i in range(start, end)]
    return {"entries": entries, "found": len(matches), "next": end if end < len(matches) else None}


def _failed(code: str, error: str) -> dict:
    return {"code": code, "ok": False, "error": error, "id": None, "name": None, "unique_id": None}


def _import_one(code: str, destination: str):
    """Imports one code. Returns (result, came_from_export or None when the code was invalid)."""
    match = CODE.search(code)
    if not match:
        return _failed(code, "not a valid BL2 code"), None
    try:
        item, _, from_export = _from_code(match.group(1))
    except ValueError as error:
        return _failed(code, str(error)), None
    if _free(destination) <= 0:
        return _failed(code, f"{destination} is full"), None
    card_id, unique_id = _place(item, destination, _fresh_uid())
    return {"code": code, "ok": True, "error": None, "id": card_id, "name": item.name, "unique_id": unique_id}, from_export


def _summary(results: list, **extra) -> dict:
    imported = sum(1 for r in results if r["ok"])
    skipped = sum(1 for r in results if r.get("skipped"))
    return {"results": results, "imported": imported, "failed": len(results) - imported - skipped, **extra}


def _post_codes_import(body):
    codes = body.get("codes")
    destination = body.get("destination", "backpack")
    if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
        raise ValueError("codes must be a list of strings")
    if destination not in ("backpack", "bank"):
        raise ValueError("destination must be backpack or bank")
    if destination == "bank":
        _ensure_bank_open()
    chunk = codes[:CHUNK]
    if chunk and _free(destination) <= 0:
        raise ValueError(f"{destination} is full")
    results, sources = [], set()
    for code in chunk:
        result, from_export = _import_one(code, destination)
        results.append(result)
        if from_export is not None:
            sources.add(from_export)
    path = "mixed" if len(sources) == 2 else ("create" if sources == {False} else "unpack")
    return _summary(results, path=path, processed=len(chunk), remaining=len(codes) - len(chunk))


def _post_duplicate(body):
    item = _locate(_need(body, "id"), write=True)
    destination = "bank" if item.location == "Bank" else "backpack"
    if _free(destination) <= 0:
        raise ValueError(f"{destination} is full")
    copy = _clone(item)
    card_id, unique_id = _place(copy, destination, _fresh_uid())
    return {"ok": True, "id": card_id, "name": copy.name, "unique_id": unique_id, "destination": destination}


def _post_delete(body):
    item = _locate(_need(body, "id"), write=True)
    if item.location == "Equipped":
        raise ValueError("unequip it first")
    unique_id = _uid(item)
    deleted = {"name": item.name, "unique_id": unique_id, "code": _encode(item, unique_id)}
    _remove(item)
    return {"ok": True, "deleted": deleted}


def _post_move(body):
    item = _locate(_need(body, "id"), write=True)
    to = body.get("to")
    if to not in ("bank", "backpack"):
        raise ValueError("to must be bank or backpack")
    if item.location == "Equipped":
        raise ValueError("unequip it first")
    if item.location == ("Bank" if to == "bank" else "Backpack"):
        raise ValueError(f"it is already in the {to}")
    if _free(to) <= 0:
        raise ValueError(f"{to} is full")
    unique_id = _uid(item)
    _remove(item)
    card_id, _ = _place(item, to, unique_id)
    return {"ok": True, "id": card_id, "name": item.name, "to": to}


def _export_row(item) -> dict:
    unique_id = _uid(item)
    return {"location": item.location, "name": item.name, "class": item.cls, "level": item.level, "rarity": item.rarity,
            "unique_id": unique_id, "code": _encode(item, unique_id)}


def _get_inventory_export(_params):
    rows = [_export_row(i) for i in _items() if i.location == "Equipped"]
    rows += [_export_row(i) for i in _items() if i.location == "Backpack"]
    warnings = []
    if BANK_OPEN:
        rows += [_export_row(i) for i in _bank()]
    else:
        warnings.append("The bank is closed, so its items are not in this export.")
    return {"format": "rtse-inventory", "version": 1, "items": rows, "warnings": warnings}


def _post_inventory_import(body):
    if "replace" in body and body["replace"] is not False:
        raise ValueError("replace was removed: imports only add items; delete items separately")
    entries = body.get("items")
    destination = body.get("destination", "backpack")
    equipped = body.get("equipped", "skip")
    if not isinstance(entries, list):
        raise ValueError("items must be a list")
    if destination not in ("backpack", "bank", "original"):
        raise ValueError("destination must be backpack, bank or original")
    if equipped not in EQUIPPED_RULES:
        raise ValueError("equipped must be skip or backpack")
    chunk = entries[:CHUNK]
    rows = []  # (code, target place, is an Equipped row)
    for entry in chunk:
        code = entry.get("code") if isinstance(entry, dict) else None
        if not isinstance(code, str):
            raise ValueError("each item needs a code")
        location = entry.get("location")
        if destination == "original":
            target = "bank" if location == "Bank" else "backpack"
        else:
            target = destination
        rows.append((code, target, location == "Equipped"))
    # an Equipped row goes to its target only when equipped == "backpack"; otherwise it is skipped
    receivers = {target for _, target, is_equipped in rows if not is_equipped or equipped == "backpack"}
    if "bank" in receivers:
        _ensure_bank_open()
    for target in receivers:
        if _free(target) <= 0:
            raise ValueError(f"{target} is full")
    results, skipped = [], 0
    for code, target, is_equipped in rows:
        if is_equipped and equipped != "backpack":
            skipped += 1
            results.append({"code": code, "ok": False, "error": "equipped item skipped", "skipped": True,
                            "id": None, "name": None, "unique_id": None})
            continue
        results.append(_import_one(code, target)[0])
    warnings = []
    if skipped:
        warnings.append(f"{skipped} equipped {'item was' if skipped == 1 else 'items were'} skipped. "
                        "Tick 'Also add equipped items to the backpack' to add them.")
    return _summary(results, processed=len(chunk), remaining=len(entries) - len(chunk), skipped=skipped, warnings=warnings)


def _get_debug_codes(_params):
    return {"saved_to": "(mock) rtse/debug_codes.json", "items": len(_items()), "bank": len(_bank())}


ROUTES = {
    "/api/bank": _get_bank,
    "/api/codes/export": _get_codes_export,
    "/api/codes/preview": _get_codes_preview,
    "/api/codes/import": _post_codes_import,
    "/api/inventory/duplicate": _post_duplicate,
    "/api/inventory/delete": _post_delete,
    "/api/inventory/move": _post_move,
    "/api/inventory/export": _get_inventory_export,
    "/api/inventory/import": _post_inventory_import,
    "/api/debug/codes": _get_debug_codes,
}


def handle(path, params, shared):
    """Answers the contract routes (GET params are strings, POST params are the JSON body). None = not mine."""
    route = ROUTES.get(path)
    return route(params) if route else None
