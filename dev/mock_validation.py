"""Mock of the part-status, kept and parts-cache answers (the real side is rtse/gear_edit.py and rtse/gear_parts.py).

Loaded by dev/mock_server.py as a feature mock. The statuses are invented but deterministic (a hash of the
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
CAN_BE_SAVED = {True: "the game reports the item can be saved", False: "the game reports the item cannot be saved"}
_natural: set[str] = set()  # part ids some item lists in its own balance; filled in by decorate()


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def _status(part_id: str, weapon: bool, slot: str) -> dict:
    """status, status_reason, dlc and unknown for one part, the way the server reports them.

    Precedence: natural > unsupported > unknown weight > dlc > unknown dlc > custom. Unknown parts say what could not be told.
    """
    digest = _digest(part_id)
    if part_id in _natural:
        if digest[6] % 4 == 0:  # in this item's own list, but the drop weight could not be read
            return {"status": "unknown", "unknown": "weight", "dlc": None,
                    "status_reason": f"In this item's own {slot} list, but its drop weight could not be read (unverified)"}
        return {"status": "natural", "status_reason": "In this item's own balance list for this slot", "dlc": None}
    if digest[0] % 9 == 0:
        reason = ("A ItemPartDefinition is not a weapon part" if weapon
                  else "A GrenadeModPartDefinition, but this item's own parts here are ShieldPartDefinition")
        return {"status": "unsupported", "status_reason": f"{reason} (not checked in game)", "dlc": None}
    if digest[0] % 5 == 1:
        name = DLC_NAMES[digest[1] % len(DLC_NAMES)]
        licensed = (True, False, None)[digest[2] % 3]
        return {"status": "dlc", "status_reason": f"Only used by DLC gear: {name}",
                "dlc": {"name": name, "licensed": licensed, "source": "DlcItemSet"}}
    if digest[5] % 3 == 0:  # not in this item's own list, and whether it is base game or DLC cannot be told
        return {"status": "unknown", "unknown": "dlc", "dlc": None,
                "status_reason": "From another balance; whether it needs a DLC could not be told"}
    return {"status": "custom", "status_reason": "Comes from another balance", "dlc": None}


def item_dlc(item_id: str) -> dict | None:
    """The item's own DLC, for the item info row. About one item in four, deterministic."""
    digest = _digest(item_id)
    if digest[0] % 4 != 0:
        return None
    return {"name": DLC_NAMES[digest[1] % len(DLC_NAMES)], "licensed": (True, False, None)[digest[2] % 3], "source": "DlcItemSet"}


def decorate(slot_name: str, options: list[dict], legal_ids: set[str], weapon: bool = True) -> list[dict]:
    """The options (copies) with status, status_reason, dlc (and unknown) added. legal_ids are the item's own parts for this slot."""
    _natural.update(legal_ids)
    return [{**option, **_status(option["id"], weapon, slot_name)} for option in options]


def parts_cache(mode: str) -> dict:
    """The part-list scan the backend keeps for the item's kind. Scanned once a non-legal mode has read every part."""
    return {"scanned": mode != "legal", "age_s": 125.0, "map": "Sanctuary_P"}


def snapshot(item, stats: list) -> dict:
    """The item's name, parts and stat values, taken before an edit so kept() can say what changed. stats: (name, label, base)."""
    return {
        "name": item.name,
        "parts": dict(item.parts),
        "stats": {name: (label, item.overrides.get(name, item.stats[name])) for name, label, _base in stats},
    }


def kept(before: dict, item, stats: list, edited_slot: str | None = None) -> dict:
    """applied.kept: the name, every slot's part (before and after), the stats that changed and the quick checks (CanBeSaved on weapons)."""
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
    return {"name": {"before": before["name"], "after": item.name}, "parts": parts, "stats": changed, "quick_checks": quick}


def handle(path: str, params: dict, shared: dict) -> dict | None:
    if path == "/api/debug/validation":
        return {"saved_to": "rtse/debug_validation.json (mock: nothing written)", "id": params.get("id"), "mode": params.get("mode"), "parts": 0}
    return None
