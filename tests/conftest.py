from __future__ import annotations

import json
import os
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
    # task #654: quota_share publishes sanitized snapshots through the same
    # credential resolution. An inherited HANDOFFKEEP_URL/TOKEN pair would let a
    # test write to production hk — delete it; tests opt in with their own fake.
    monkeypatch.delenv("HANDOFFKEEP_URL", raising=False)
    monkeypatch.delenv("HANDOFFKEEP_TOKEN", raising=False)
    # #608: the CLI providers' probes write probe-calls.log and hold
    # .probe.lock under PROBE_WORKDIR — a test that reaches fetch() without
    # redirecting it would pollute the incident-audit log in the real
    # ~/.local/share/scopefuel and contend with a real probe's lock.
    from scopefuel.providers import devin, grok, kimi, kiro

    for module, name in ((devin, "devin"), (grok, "grok"), (kimi, "kimi"), (kiro, "kiro")):
        monkeypatch.setattr(module, "PROBE_WORKDIR", tmp_path / f"{name}-probe-workdir")
    # The catalog is memoised per process so one command cannot straddle the TTL
    # boundary; that memo must not survive from one test into the next.
    bench.reset_catalog_memo()
    return tmp_path


@pytest.fixture
def _panewire_bin_plant(monkeypatch):
    """외부에서 상속된 ``PANEWIRE_BIN``(wrk 공용 노브)을 흉내 낸다.

    아래 autouse 차단 픽스처가 이것에 의존하므로 매 테스트마다 심어진 뒤
    지워져야 한다 — delenv 를 제거하는 뮤턴트(Ge)는 이 심은 값이 남아
    테스트가 RED 되도록 하기 위함이다.
    """
    monkeypatch.setenv("PANEWIRE_BIN", "/nonexistent-inherited-panewire")


@pytest.fixture(autouse=True)
def _block_real_operator_emit(monkeypatch, _panewire_bin_plant):
    """task #638 사고 방지 — 모든 테스트에서 실 operator-desk 발송을 차단한다.

    2026-09-24 14:53 KST 에 뮤턴트 스윕(M26)이 싱크 미스텁 테스트를 통해 실
    panewire 를 호출해 실 lane.event 1건이 operator-desk 에 도달했다
    (events-lane/00067). 이 픽스처는 두 경로를 전부 막는다:

    - ``exhaust.emit_lane_event`` — argv 를 만드는 호출부. 시도는
      ``exhaust._BLOCKED_EMIT_CALLS`` 에 기록된다. ``PANEWIRE_BIN`` 을 명시한
      테스트(스텁 바이너리 계약 검증)만 실 함수에 위임한다.
    - ``exhaust.subprocess`` — exhaust.py 안의 subprocess 사용 전부.
      ``PANEWIRE_BIN`` 미지정 상태의 run 호출은 AssertionError 로 실패한다.

    실 발송은 pytest 바깥의 설치 후 witness 절차에서만 허용된다.

    상속된 ``PANEWIRE_BIN`` 은 opt-in 이 아니다 — wrk 가 읽는 공용 노브라
    외부 export 만으로 pytest 안의 실 발송이 열렸다(R3-1). 테스트가 opt-in
    하려면 본문에서 ``monkeypatch.setenv("PANEWIRE_BIN", <stub>)`` 해야 한다.
    """
    import subprocess as _subprocess

    from scopefuel import exhaust

    monkeypatch.delenv("PANEWIRE_BIN", raising=False)
    real_emit = exhaust.emit_lane_event

    def _blocked_emit(text: str, event_id: str = "") -> bool:
        exhaust._BLOCKED_EMIT_CALLS.append((text, event_id))
        if os.environ.get("PANEWIRE_BIN"):
            return real_emit(text, event_id)
        return False

    _blocked_emit._test_stub = True  # type: ignore[attr-defined]
    monkeypatch.setattr(exhaust, "emit_lane_event", _blocked_emit)

    class _GuardedSubprocess:
        DEVNULL = _subprocess.DEVNULL
        PIPE = _subprocess.PIPE
        TimeoutExpired = _subprocess.TimeoutExpired

        @staticmethod
        def run(argv, *args, **kwargs):
            if not os.environ.get("PANEWIRE_BIN"):
                raise AssertionError(f"tests: real panewire subprocess blocked: {argv}")
            return _subprocess.run(argv, *args, **kwargs)

    monkeypatch.setattr(exhaust, "subprocess", _GuardedSubprocess)
    yield
    exhaust._BLOCKED_EMIT_CALLS.clear()
