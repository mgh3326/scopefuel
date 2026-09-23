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
    """실제 홈 디렉터리 캐시/스펙/설정을 건드리지 않는다."""
    from scopefuel import bench

    monkeypatch.setenv("SCOPEFUEL_CACHE", str(tmp_path / "snapshots.json"))
    monkeypatch.setenv("SCOPEFUEL_SPEC_DIR", str(tmp_path / "specs"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # #593: the bench backend now defaults to ``auto`` and discovers handoffkeep
    # credentials from the handoffkeep CLI's own config.env. Point that at a path
    # that does not exist, so a test's backend never depends on whether the
    # developer's machine happens to be provisioned.
    monkeypatch.setenv("HANDOFFKEEP_CONFIG", str(tmp_path / "handoffkeep-absent.env"))
    # The catalog is memoised per process so one command cannot straddle the TTL
    # boundary; that memo must not survive from one test into the next.
    bench.reset_catalog_memo()
    return tmp_path
