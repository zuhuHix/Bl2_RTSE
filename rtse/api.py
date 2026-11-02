"""API routes. Every route runs on the game thread and returns JSON-serialisable data.

Read routes (GET) and write routes (POST) both take one dict: the query string or the JSON body.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mods_base import get_pc

from . import ammo, character, gear_debug, gear_edit, gear_inventory, gear_parts, inspector, sdu, skills, world
from .routing import ApiError, Params, Route  # noqa: F401 - ApiError is re-exported for server.py and idempotency.py


def ping(_params: Params) -> dict[str, Any]:
    return {"ok": True, "in_game": get_pc(possibly_loading=True) is not None}


def _module_route(action: Callable[[Params], dict[str, Any]]) -> Callable[[Params], dict[str, Any]]:
    def route(params: Params) -> dict[str, Any]:
        try:
            return action(params)
        except (character.CharacterError, ammo.AmmoError, sdu.SduError, skills.SkillsError, world.WorldError, inspector.InspectError) as ex:
            raise ApiError(ex.status, str(ex)) from None

    return route


GET_ROUTES: dict[str, Route] = {
    "/api/ping": ping,
    "/api/items": gear_inventory.items,
    "/api/item": gear_edit.item_detail,
    "/api/debug/item": gear_debug.debug_item,
    "/api/debug/inventory": gear_debug.debug_inventory,
    "/api/part": gear_parts.part_info,
    "/api/part_owners": gear_parts.part_owners,
    "/api/debug/part": gear_debug.debug_part,
    "/api/debug/meshes": gear_debug.debug_meshes,
    "/api/debug/gestalt": gear_debug.debug_gestalt,
    "/api/debug/gestalts": gear_debug.export_gestalts,
    "/api/debug/coverage": gear_debug.export_coverage,
    "/api/character": _module_route(lambda _params: character.read()),
    "/api/debug/character": _module_route(lambda _params: character.debug_dump()),
    "/api/ammo": _module_route(lambda _params: ammo.read()),
    "/api/debug/ammo": _module_route(lambda _params: ammo.debug_dump()),
    "/api/sdu": _module_route(lambda _params: sdu.read()),
    "/api/debug/sdu": _module_route(lambda _params: sdu.debug_dump()),
    "/api/skills": _module_route(lambda _params: skills.read()),
    "/api/debug/skills": _module_route(lambda _params: skills.debug_dump()),
    "/api/world": _module_route(world.read),
    "/api/debug/world": _module_route(lambda _params: world.debug_dump()),
    "/api/world/objectives": _module_route(world.mission_objectives),
    "/api/inspect": _module_route(inspector.look),
}
POST_ROUTES: dict[str, Route] = {
    "/api/item/set_part": gear_edit.set_part,
    "/api/item/set_level": gear_edit.set_level,
    "/api/parts/rescan": gear_parts.rescan_parts,
    "/api/item/set_stat": gear_edit.set_stat,
    "/api/character/set": _module_route(character.set_field),
    "/api/ammo/set": _module_route(ammo.set_value),
    "/api/sdu/set": _module_route(sdu.set_level),
    "/api/sdu/ammo_max": _module_route(sdu.set_ammo_max),
    "/api/skills/set": _module_route(skills.set_level),
    "/api/skills/reset": _module_route(skills.reset),
    "/api/world/mission": _module_route(world.set_mission),
    "/api/world/missions/reset": _module_route(world.reset_missions),
    "/api/world/objective": _module_route(world.set_objective),
    "/api/world/objective_set": _module_route(world.set_objective_set),
    "/api/world/track": _module_route(world.track_mission),
    "/api/world/challenge": _module_route(world.set_challenge),
    "/api/world/stations": _module_route(world.unlock_stations),
    "/api/world/playthrough": _module_route(world.set_playthrough),
    "/api/inspect/set": _module_route(inspector.set_value),
}
