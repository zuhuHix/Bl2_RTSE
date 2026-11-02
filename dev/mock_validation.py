"""Mock of the part-status and save-check endpoints (rtse/gear_edit.py: check_part, _kept_view, debug_validation).

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
SAVED_CHECK_NAMES = CHECK_NAMES + ["CanBeSaved"]  # CanBeSaved only appears in the check made after an apply
DETAIL = {  # per check, what each outcome says
    True: {"serial round-trip": "encoded and decoded back to the same part", "SerialNumberMatchesDef": "the serial names this part's definition",
           "CheckDefMatch": "the check definition is this part", "CanBeSaved": "the game reports the item can be saved"},
    False: {"serial round-trip": "decoded back to a different part", "SerialNumberMatchesDef": "the serial names a different definition",
            "CheckDefMatch": "no matching check definition", "CanBeSaved": "the game reports the item cannot be saved"},
    None: {name: "not available on this object" for name in SAVED_CHECK_NAMES},
}
_natural: set[str] = set()  # part ids some item lists in its own balance; filled in by decorate()


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


def _save_check(part_id: str | None, status: str | None, slot: str, post_apply: bool) -> dict:
    """A SaveCheck. One part in about seven custom parts cannot be checked (survives null), like the real game sometimes."""
    names = SAVED_CHECK_NAMES if post_apply else CHECK_NAMES
    if part_id is None:
        survives = True
    elif status == "unsupported":
        survives = False
    elif status == "custom" and _digest(part_id)[3] % 7 == 0:
        survives = None
    else:
        survives = True
    summary = {True: "Survived a serial round-trip (unverified)", False: "Did not survive a serial round-trip (unverified)",
               None: "Could not check: not available on this object"}[survives]
    lost = [{"field": f"{slot}PartDefinition", "expected": part_id, "got": None}] if survives is False else []
    checks = [{"name": name, "ok": survives, "detail": DETAIL[survives][name]} for name in names]
    return {"survives": survives, "summary": summary, "verified": False, "checks": checks, "lost": lost}


def _check_part(params: dict) -> dict:
    slot = params["slot"]
    part_id = params.get("part") or None
    if part_id is None:  # taking the part off
        return {"slot": slot, "part": None, "status": None, "status_reason": None, "dlc": None,
                "save_check": _save_check(None, None, slot, post_apply=False)}
    weapon = "WillowWeapon" in params.get("id", "")
    info = _status(part_id, weapon)
    return {"slot": slot, "part": part_id, **info, "save_check": _save_check(part_id, info["status"], slot, post_apply=False)}


def snapshot(item, stats: list) -> dict:
    """The item's name, parts and stat values, taken before an edit so kept() can say what changed. stats: (name, label, base)."""
    return {
        "name": item.name,
        "parts": dict(item.parts),
        "stats": {name: (label, item.overrides.get(name, item.stats[name])) for name, label, _base in stats},
    }


def kept(before: dict, item, stats: list, edited_slot: str | None = None) -> dict:
    """applied.kept: the name, every slot's part (before and after), the stats that changed, and a save check after the edit."""
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
    edited_part = item.parts.get(edited_slot) if edited_slot else None
    status = _status(edited_part["id"], item.cls == "WillowWeapon")["status"] if edited_part else None
    save = _save_check(edited_part["id"] if edited_part else None, status, edited_slot or "", post_apply=True)
    return {"name": {"before": before["name"], "after": item.name}, "parts": parts, "stats": changed, "save_check": save}


def handle(path: str, params: dict, shared: dict) -> dict | None:
    if path == "/api/item/check_part":
        return _check_part(params)
    if path == "/api/debug/validation":
        return {"saved_to": "rtse/debug_validation.json (mock: nothing written)", "id": params.get("id"), "mode": params.get("mode"), "parts": 0}
    return None
