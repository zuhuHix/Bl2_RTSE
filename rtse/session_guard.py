"""Write guard: decides whether a write route may run right now. CONTRACT ONLY (always allows).

The route registry calls this before every POST route, on the game thread, before the route touches the game:

    allowed, reason = session_guard.can_write("/api/item/set_part")
    if not allowed: -> HTTP 423 {"error": reason, "blocked": True}

`can_write(route: str) -> tuple[bool, str]`
    route:  the POST path, e.g. "/api/world/mission".
    returns (True, "") to allow, or (False, "<one sentence the user can act on>") to block, e.g.
    (False, "you are in a multiplayer session; edits are disabled") or (False, "the game is loading a map").
    Must be cheap and must never raise (an internal error should allow and log, not block every write).

A Wave 1 stream implements real checks (loading screens, multiplayer client, cutscenes, ...). Read routes are never
guarded.
"""

from __future__ import annotations

BLOCKED_STATUS = 423  # HTTP "Locked": the request was fine, the game state isn't


def can_write(route: str) -> tuple[bool, str]:
    """Always allows for now."""
    return True, ""
