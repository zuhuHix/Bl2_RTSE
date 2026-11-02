"""dev/mock_server.py loads every dev/mock_<feature>.py automatically."""

from __future__ import annotations

import importlib.util
from pathlib import Path

DEV = Path(__file__).resolve().parents[1]


def _mock_server():
    spec = importlib.util.spec_from_file_location("mock_server_under_test", DEV / "mock_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # does not start the HTTP server (that is under __main__)
    return module


def test_every_feature_mock_file_is_loaded():
    server = _mock_server()
    expected = sorted(p.stem for p in DEV.glob("mock_*.py") if p.name != "mock_server.py")
    assert sorted(m.__name__ for m in server.FEATURE_MOCKS) == expected
    assert {"mock_sdu", "mock_skills", "mock_world", "mock_inspect"} <= set(expected)


def test_a_new_feature_mock_needs_no_registration(tmp_path):
    server = _mock_server()
    (tmp_path / "mock_server.py").write_text("raise AssertionError('never imported')", encoding="utf-8")
    (tmp_path / "mock_zeta.py").write_text(
        "def handle(path, params, shared):\n    return {'zeta': True} if path == '/api/zeta' else None\n",
        encoding="utf-8",
    )
    (tmp_path / "mock_alpha.py").write_text("NOT_A_HANDLER = 1\n", encoding="utf-8")
    found = server.discover_feature_mocks(tmp_path)
    assert [m.__name__ for m in found] == ["mock_zeta"]
    assert found[0].handle("/api/zeta", {}, {}) == {"zeta": True}
