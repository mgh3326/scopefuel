from __future__ import annotations

import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_json():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES / f"{name}.json").read_text())

    return _load


@pytest.fixture
def fixture_text():
    def _load(name: str) -> str:
        return (FIXTURES / f"{name}.txt").read_text()

    return _load


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """실제 홈 디렉터리 캐시/스펙을 건드리지 않는다."""
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(tmp_path / "snapshots.json"))
    monkeypatch.setenv("SCOPEFUEL_SPEC_DIR", str(tmp_path / "specs"))
    return tmp_path
