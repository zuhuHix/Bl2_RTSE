"""pytest setup: stub the game modules and make `rtse` importable.

    python -I -m pytest dev/tests                 full suite
    python -I -m pytest dev/tests -k <module>     only the tests for one module while iterating
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "dev"
for path in (str(ROOT), str(DEV)):
    if path not in sys.path:
        sys.path.insert(0, path)

import unreal_stub  # noqa: E402

unreal_stub.install()


@pytest.fixture(autouse=True)
def _fresh_stub_state():
    """Every test starts with no player controller, no game objects and an empty log."""
    unreal_stub.STATE.reset()
    yield
    unreal_stub.STATE.reset()


@pytest.fixture
def game():
    """The stub's mutable state: set `game.pc`, `game.objects[(cls, path)]`, read `game.log`."""
    return unreal_stub.STATE
