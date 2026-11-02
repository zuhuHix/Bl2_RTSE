"""Stand-ins for the game-only modules (unrealsdk, unrealsdk.hooks, mods_base), so RTSE's Python can be imported
outside the game. Shared by dev/smoke_server.py, dev/audit_names.py and the pytest suite (dev/tests/conftest.py).

    import unreal_stub; unreal_stub.install()      # before importing anything from rtse

Only what RTSE touches at import time (and a few harmless defaults) is provided. Tests that need a player
controller or game objects set `unreal_stub.STATE.pc` / `find_object` results themselves.
"""

from __future__ import annotations

import sys
import types
from typing import Any


class Anything:
    """Stands in for any game class or struct used only in annotations or isinstance checks."""


class _State:
    """Mutable knobs tests can turn: what get_pc() returns, what find_object()/find_all() find."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.pc: Any = None
        self.objects: dict[tuple[str, str], Any] = {}  # (class name, path) -> object
        self.all: dict[str, list[Any]] = {}  # class name -> objects
        self.hooks: dict[tuple[str, Any, str], Any] = {}  # (function path, type, id) -> callback
        self.log: list[tuple[str, str]] = []  # ("info" | "error" | ..., message)


STATE = _State()


def _get_pc(**_kwargs: Any) -> Any:
    return STATE.pc


def _find_object(class_name: str, path: str) -> Any:
    return STATE.objects.get((class_name, path))


def _find_all(class_name: str, *_args: Any) -> list[Any]:
    return list(STATE.all.get(class_name, ()))


def _add_hook(path: str, kind: Any, hook_id: str, fn: Any) -> None:
    STATE.hooks[(path, kind, hook_id)] = fn


def _remove_hook(path: str, kind: Any, hook_id: str) -> None:
    STATE.hooks.pop((path, kind, hook_id), None)


def _logger(level: str):  # noqa: ANN202
    def log(message: Any, *_args: Any) -> None:
        STATE.log.append((level, str(message)))

    return log


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def install() -> None:
    """Registers the stub modules in sys.modules. Safe to call more than once."""
    if getattr(sys.modules.get("unrealsdk"), "__rtse_stub__", False):
        return
    unreal = types.SimpleNamespace(
        UObject=Anything, WrappedStruct=Anything, WrappedArray=Anything, BoundFunction=Anything,
    )  # fmt: skip
    logging = types.SimpleNamespace(
        info=_logger("info"), warning=_logger("warning"), error=_logger("error"), dev_warning=_logger("dev_warning"),
    )  # fmt: skip
    hooks = _module(
        "unrealsdk.hooks",
        Type=types.SimpleNamespace(PRE=0, POST=1, POST_UNCONDITIONAL=2),
        add_hook=_add_hook,
        remove_hook=_remove_hook,
    )
    _module(
        "unrealsdk",
        __rtse_stub__=True,
        logging=logging,
        unreal=unreal,
        hooks=hooks,
        find_all=_find_all,
        find_object=_find_object,
    )
    _module(
        "mods_base",
        get_pc=_get_pc,
        build_mod=lambda **_: None,
        hook=lambda *_a, **_k: (lambda f: f),
        keybind=lambda *_a, **_k: (lambda f: f),
    )
