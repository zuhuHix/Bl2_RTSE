"""Mock of the part-status, save-check, kept and parts-cache answers (the real side is rtse/gear_edit.py and rtse/gear_parts.py).

Loaded by dev/mock_server.py as a feature mock. The statuses and checks are invented but deterministic (a hash of the
part id), and every result is unverified, as the real ones are. Nothing here reads the game's data.
"""

from __future__ import annotations

import hashlib

DLC_NAMES = [
    "Captain Scarlett and Her Pirate's Booty",
    "Mr. Torgue's Campaign of Carnage",
    "Sir Hammerlock's Big Game Hunt",
    "Tiny Tina's Assault on Dragon Keep",
]
CHECK_NAMES = ["serial round-trip", "SerialNumberMatchesDef", "CheckDefMatch"]
DETAIL = {  # per check, what each outcome says
    True: {"serial round-trip": "encoded and decoded back to the same part", "SerialNumberMatchesDef": "the serial names this part's definition",
           "CheckDefMatch": "the check definition is this part"},
    False: {"serial round-trip": "decoded back to a different part", "SerialNumberMatchesDef": "the serial names a different definition",
            "CheckDefMatch": "no matching check definition"},
    None: {"serial round-trip": "not available on this object", "SerialNumberMatchesDef": "not available on this object",
           "CheckDefMatch": "not available on this object"},
}
CAN_BE_SAVED = {True: "the game reports the item can be saved", False: "the game reports the item cannot be saved"}
DECODER = "UnpackSerialNumber (native)"
_natural: set[str] = set()  # part ids some item lists in its own balance; filled in by decorate()


class MockConflict(Exception):
    """An action the game refuses in this state. mock_server sends it as HTTP 409."""


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def _status(part_id: str, weapon: bool) -> dict:
    """status, status_reason and dlc for one part, the way the server reports them (precedence: natural > unsupported > dlc > custom)."""
    if part_id in _natural:
        return {"status": "natural", "status_reason": "In this item's own balance list for this slot", "dlc": None}
    digest = _digest(part_id)
    if digest[0] % 9 == 0:
        reason = ("A ItemPartDefinition is not a weapon part" if weapon
                  else "A GrenadeModPartDefinition, but this item's own parts here are ShieldPartDefinition")
        return {"status": "unsupported", "status_reason": f"{reason} (not checked in game)", "dlc": None}
    if digest[0] % 5 == 1:
        name = DLC_NAMES[digest[1] % len(DLC_NAMES)]
        licensed = (True, False, None)[digest[2] % 3]
        return {"status": "dlc", "status_reason": f"Only used by DLC gear: {name}",
                "dlc": {"name": name, "licensed": licensed, "source": "DlcItemSet"}}
    return {"status": "custom", "status_reason": "Comes from another balance", "dlc": None}


def item_dlc(item_id: str) -> dict | None:
    """The item's own DLC, for the item info row. About one item in four, deterministic."""
    digest = _digest(item_id)
    if digest[0] % 4 != 0:
        return None
    return {"name": DLC_NAMES[digest[1] % len(DLC_NAMES)], "licensed": (True, False, None)[digest[2] % 3], "source": "DlcItemSet"}


def decorate(slot_name: str, options: list[dict], legal_ids: set[str], weapon: bool = True) -> list[dict]:
    """The options (copies) with status, status_reason and dlc added. legal_ids are the item's own parts for this slot."""
    _natural.update(legal_ids)
    return [{**option, **_status(option["id"], weapon)} for option in options]


def parts_cache(mode: str) -> dict:
    """The part-list scan the backend keeps for the item's kind. Scanned once a non-legal mode has read every part."""
    return {"scanned": mode != "legal", "age_s": 125.0, "map": "Sanctuary_P"}


def _restored(part_id: str) -> bool:
    """One part id in thirteen: the mock's check says it changed the live item and RTSE put it back (for the warning)."""
    return int.from_bytes(_digest(part_id)[:4], "big") % 13 == 0


def _save_check(part_id: str | None, status: str | None, slot: str | None, can_be_saved: bool | None) -> dict:
    """A SaveCheck. slot/part_id are None for the item as it is now. One custom part in about seven cannot be checked (survives null)."""
    restored = slot is not None and part_id is not None and _restored(part_id)
    if restored:
        survives = None
    elif part_id is None:
        survives = True
    elif status == "unsupported":
        survives = False
    elif status == "custom" and _digest(part_id)[3] % 7 == 0:
        survives = None
    else:
        survives = True
    summary = {True: "Survived a serial round-trip (unverified)", False: "Did not survive a serial round-trip (unverified)",
               None: "Could not check: not available on this object"}[survives]
    if restored:
        summary = "Could not check: the check changed the live item, so RTSE put it back"
    names = CHECK_NAMES + (["CanBeSaved"] if can_be_saved is not None else [])
    checks = []
    for name in names:
        if name == "CanBeSaved":
            checks.append({"name": name, "ok": can_be_saved, "detail": CAN_BE_SAVED[can_be_saved]})
        else:
            checks.append({"name": name, "ok": survives, "detail": DETAIL[survives][name]})
    lost = [{"field": f"{slot}PartDefinition", "expected": part_id, "got": None}] if survives is False else []
    return {"survives": survives, "summary": summary, "verified": False, "checks": checks, "lost": lost,
            "restored": restored, "decoder": DECODER}


def save_check_reply(item, params: dict) -> dict:
    """GET /api/item/save_check. Equipped gear is refused (409); with slot the item is checked as it would be with that part."""
    if item.location == "Equipped":
        raise MockConflict("The experimental save check is not run on equipped gear: unequip it first (the check changes the live item while it runs).")
    weapon = item.cls == "WillowWeapon"
    slot = params.get("slot") or None
    part_id = params.get("part") or None
    if slot is None:  # the item as it is now; CanBeSaved is part of this check for weapons
        check = _save_check(None, None, None, can_be_saved=True if weapon else None)
        return {"slot": None, "part": None, "experimental": True, "save_check": check}
    status = _status(part_id, weapon)["status"] if part_id else None
    check = _save_check(part_id, status, slot, can_be_saved=None)
    return {"slot": slot, "part": part_id, "experimental": True, "save_check": check}


def snapshot(item, stats: list) -> dict:
    """The item's name, parts and stat values, taken before an edit so kept() can say what changed. stats: (name, label, base)."""
    return {
        "name": item.name,
        "parts": dict(item.parts),
        "stats": {name: (label, item.overrides.get(name, item.stats[name])) for name, label, _base in stats},
    }


def kept(before: dict, item, stats: list, edited_slot: str | None = None) -> dict:
    """applied.kept: the name, every slot's part (before and after), the stats that changed, the quick checks and whether the save check can run."""
    slots = list(dict.fromkeys([*before["parts"], *item.parts]))
    parts = []
    for slot in slots:
        was, now = before["parts"].get(slot), item.parts.get(slot)
        was_name = was["name"] if was else None
        now_name = now["name"] if now else None
        parts.append({"slot": slot, "before": was_name, "after": now_name,
                      "changed": was_name != now_name, "edited": slot == edited_slot})
    after = snapshot(item, stats)
    changed = []
    for name, (label, was_value) in before["stats"].items():
        now_value = after["stats"][name][1]
        if was_value != now_value:
            changed.append({"name": name, "label": label, "before": was_value, "after": now_value})
    quick = [{"name": "CanBeSaved", "ok": True, "detail": CAN_BE_SAVED[True]}] if item.cls == "WillowWeapon" else []
    return {"name": {"before": before["name"], "after": item.name}, "parts": parts, "stats": changed,
            "quick_checks": quick, "save_check_available": item.location != "Equipped"}


def handle(path: str, params: dict, shared: dict) -> dict | None:
    if path == "/api/item/save_check":
        return save_check_reply(shared["find"](params["id"]), params)
    if path == "/api/debug/validation":
        return {"saved_to": "rtse/debug_validation.json (mock: nothing written)", "id": params.get("id"), "mode": params.get("mode"), "parts": 0}
    return None
