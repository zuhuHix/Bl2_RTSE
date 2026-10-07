from __future__ import annotations

import webbrowser
from typing import Any

from mods_base import build_mod, hook, keybind
from unrealsdk import logging, unreal

from . import api, gamethread, server, stats

# How often (in game ticks) equipped weapons are checked against their stat overrides. This is the
# safety net for anything that recomputes stats without going through a hooked function.
REAPPLY_EVERY_TICKS = 30
_ticks = 0
_periodic_enabled = True


def _announce() -> str:
    url = server.start()
    logging.info(f"RTSE: editor running at {url}")
    return url


def _enable() -> None:
    stats.load()
    stats.install_hooks()
    _announce()


def _disable() -> None:
    stats.remove_hooks()
    server.stop()


@keybind("Open RTSE in Browser", "F8")
def open_editor() -> None:
    webbrowser.open(_announce())


@hook("WillowGame.WillowGameViewportClient:Tick")
def on_tick(
    _obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    global _ticks, _periodic_enabled  # noqa: PLW0603
    gamethread.pump()

    _ticks += 1
    if _ticks < REAPPLY_EVERY_TICKS:
        return
    _ticks = 0
    if not _periodic_enabled or not stats.has_overrides():
        return
    try:
        stats.reapply_all(api.equipped_weapons(), reason="a periodic check")
    except Exception as ex:  # noqa: BLE001
        # Stop rather than log the same failure 60 times a second
        _periodic_enabled = False
        logging.error(f"RTSE: periodic stat re-apply disabled after error: {ex!r}")


mod = build_mod(
    on_enable=_enable,
    on_disable=_disable,
)
