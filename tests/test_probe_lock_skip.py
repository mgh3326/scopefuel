"""task #639 — single-probe lock skip is a deferred round, not a failure.

#608 의 single_probe_lock 을 다른 프로브가 쥐고 있으면 provider fetch() 는
"탐침이 이미 실행 중 — 이번 회차 건너뜀" 결과를 돌려준다. 이것은 측정 실패가
아니다 — 같은 호스트의 다른 프로브가 지금 측정 중이라 이번 회차만 생략됐다는
뜻이다. 캐시는 마지막 정상 스냅샷을 유지하고(fresh TTL 안이면 그대로, 밖이면
#576 stale_accepted 규칙), 게이트는 건너뛴 회차 때문에 막지 않는다.

전부 fake fetcher·tmp 캐시·고정 시계로 돌린다 — 실 CLI·네트워크 호출 없음.
provider fetch() 를 직접 부르는 테스트는 lock 을 미리 쥐어 probe 본체가
절대 실행되지 않게 한다(잠금 분기는 자식을 띄우기 전에 반환한다).
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from scopefuel import cache, cli, proctrack, refresh, render
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.providers import devin, grok, kimi, kiro
from scopefuel.recommend import gate_check

TODAY = dt.date(2026, 7, 31)
NOW = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.UTC)
EPOCH = NOW.timestamp()

# pool → (profile, required account-scope windows) — manual.REQUIRED_WINDOWS 와 동기.
POOL_PROFILE = {
    "devin": "devin-swe2",
    "kimi": "kimi-k3",
    "grok": "grok-hi",
    "kiro": "kiro-opus",
}
POOL_WINDOWS = {
    "devin": ("1d",),
    "kimi": ("5h", "7d"),
    "grok": ("7d",),
    "kiro": ("30d",),
}
CLI_PROBES = {
    "devin": devin,
    "kimi": kimi,
    "grok": grok,
    "kiro": kiro,
}

# model.PROBE_IN_PROGRESS 와 같은 계약 문자열 — 잠금 분기 결과의 error_kind.
PROBE_IN_PROGRESS = "probe_in_progress"


def _bucket(window: str, used: float, *, hours_ahead: float = 4.0) -> Bucket:
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=(NOW + dt.timedelta(hours=hours_ahead)).isoformat(),
        scope=Scope("account"),
        horizon="now" if window in ("5h", "1d") else "week",
    )


def _good_result(pool: str) -> ProviderResult:
    hours = {"1d": 20.0, "5h": 4.0, "7d": 160.0, "30d": 700.0}
    return ProviderResult(
        id=pool,
        buckets=[_bucket(w, 11.0, hours_ahead=hours[w]) for w in POOL_WINDOWS[pool]],
        source="test",
        pool_class="spend",
    )


def _seed(pool: str, *, at: float) -> None:
    cache.update_entry(pool, _good_result(pool), at)


def _stale_seed_at(pool: str, *, now: float = EPOCH) -> float:
    """fresh TTL 를 막 지난 시각 — pool마다 TTL이 다르므로(60~1800s) 계산한다."""
    return now - cache.PROVIDER_TTL_S.get(pool, cache.DEFAULT_TTL_S) - 20.0


def _skipped(pool: str) -> ProviderResult:
    """provider fetch() 가 잠금 분기에서 돌려주는 것과 같은 모양의 결과."""
    return ProviderResult(
        id=pool,
        error=f"{pool} 탐침이 이미 실행 중 — 이번 회차 건너뜀",
        error_kind=PROBE_IN_PROGRESS,
        pool_class="spend",
    )


def _skip_fetcher(pool: str):
    def fetch() -> ProviderResult:
        return _skipped(pool)

    fetch.pool_class = "spend"
    return fetch


def _collect_skip(pool: str, *, now: float = EPOCH, use_cache: bool = False) -> ProviderResult:
    return cache.collect({pool: _skip_fetcher(pool)}, [pool], now=now, use_cache=use_cache)[0]


def _read_entries() -> dict:
    return json.loads(cache.cache_path().read_text(encoding="utf-8"))


# ---------------------------------------------------------------- AC1: 캐시 보존


def test_lock_skip_inside_fresh_ttl_passes_as_is():
    """fresh TTL 안의 스냅샷은 그대로 통과 — 건너뜀이 DEGRADED 로 바꾸지 않는다."""
    _seed("devin", at=EPOCH - 30.0)  # devin TTL = 60s
    before = _read_entries()["devin"]

    result = _collect_skip("devin", use_cache=False)

    assert result.error is None
    assert result.stale is False
    assert result.status == "ok"
    assert result.verdict.mark != "degraded"
    assert "탐침 진행 중" in (result.note or "")

    after = _read_entries()["devin"]
    assert after["fetched_at"] == before["fetched_at"]
    assert after["result"] == before["result"]
    assert "last_error" not in after  # 건너뜀은 실패 감사도 남기지 않는다


def test_lock_skip_keeps_last_good_snapshot_beyond_ttl():
    """TTL 밖 스냅샷은 stale 로 유지 — '조회 실패' 가 아니라 '탐침 진행 중'."""
    _seed("devin", at=EPOCH - 80.0)
    result = _collect_skip("devin", use_cache=True)

    assert result.stale is True
    assert result.error is None
    assert result.error_kind == PROBE_IN_PROGRESS
    assert "탐침 진행 중" in (result.note or "")
    assert "80초 전" in (result.note or "")
    assert "조회 실패" not in (result.note or "")


def test_lock_skip_writes_no_cache_update():
    """건너뛴 회차는 캐시 파일을 전혀 갱신하지 않는다 — 진행 중인 프로브가 쓴다."""
    _seed("devin", at=EPOCH - 80.0)
    before = _read_entries()
    _collect_skip("devin", use_cache=True)
    assert _read_entries() == before


def test_refresh_worker_treats_probe_lock_skip_as_deferred(capsys):
    """refresh 워커가 probe 잠금을 만나면 실패 기록 없이 조용히 넘어간다."""
    _seed("devin", at=EPOCH - 80.0)
    before = _read_entries()

    rc = refresh.run_worker({"devin": _skip_fetcher("devin")}, "devin")

    assert rc == 0
    assert _read_entries() == before
    capsys.readouterr()


# ---------------------------------------------------------------- AC2: 게이트


def test_gate_passes_with_lock_held_and_fresh_cache():
    _seed("devin", at=EPOCH - 30.0)
    results = [_collect_skip("devin", use_cache=False)]
    res = gate_check(results, "devin-swe2", today=TODAY, now=NOW)
    assert res.ok is True
    assert res.unmeasurable is False


def test_gate_passes_with_lock_held_and_stale_cache():
    """81초짜리 캐시(인시던트 재현)도 건너뜀이면 stale_accepted 로 통과한다."""
    _seed("devin", at=EPOCH - 80.0)
    results = [_collect_skip("devin", use_cache=True)]
    res = gate_check(results, "devin-swe2", today=TODAY, now=NOW)
    assert res.ok is True
    assert res.stale_accepted is True
    assert "탐침 진행 중" in res.reason


def test_gate_blocked_with_lock_held_and_no_cache():
    """정상 캐시가 없으면 오늘과 같이 막는다 — 단 사유는 '탐침 진행 중'."""
    results = [_collect_skip("devin", use_cache=True)]
    res = gate_check(results, "devin-swe2", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True
    assert "탐침 진행 중" in res.reason


def test_gate_blocked_with_lock_held_and_cache_older_than_6h():
    _seed("devin", at=EPOCH - cache.STALE_MAX_S - 10.0)
    results = [_collect_skip("devin", use_cache=True)]
    assert results[0].error is not None
    res = gate_check(results, "devin-swe2", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True
    assert res.stale_accepted is False


def test_gate_blocked_with_lock_held_and_elapsed_reset():
    """reset 회차가 지난 스냅샷은 #576 과 같이 수용하지 않는다."""
    stale = ProviderResult(
        id="devin",
        buckets=[
            Bucket(
                label="daily",
                window="1d",
                used_pct=11.0,
                resets_at=(NOW - dt.timedelta(hours=1)).isoformat(),
                scope=Scope("account"),
                horizon="now",
            )
        ],
        pool_class="spend",
    )
    cache.update_entry("devin", stale, EPOCH - 80.0)
    results = [_collect_skip("devin", use_cache=True)]
    res = gate_check(results, "devin-swe2", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True


def test_gate_blocks_real_provider_error_even_with_stale_cache():
    """반대 방향 뮤턴트 핀: 진짜 오류(배너 형식 변경)는 여전히 측정 불가."""
    _seed("devin", at=EPOCH - 80.0)

    def real_error() -> ProviderResult:
        return ProviderResult(
            id="devin",
            error="기동 배너에서 쿼타 세그먼트를 찾지 못함(형식 불일치 또는 미출현)",
            pool_class="spend",
        )

    results = cache.collect({"devin": real_error}, ["devin"], now=EPOCH, use_cache=True)
    assert results[0].stale is True
    assert results[0].verdict.mark == "degraded"
    assert "조회 실패" in (results[0].note or "")
    res = gate_check(results, "devin-swe2", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True
    assert "측정 불가" in res.reason


# ---------------------------------------------------------------- AC3: 표시


def test_render_table_distinguishes_probe_in_progress():
    _seed("devin", at=EPOCH - 80.0)
    result = _collect_skip("devin", use_cache=True)
    out = render.table([result], color=False)
    assert "탐침 진행 중" in out
    assert "80초 전" in out
    assert "조회 실패" not in out


def test_render_brief_distinguishes_probe_in_progress():
    _seed("devin", at=EPOCH - 80.0)
    result = _collect_skip("devin", use_cache=True)
    out = render.brief([result], color=False)
    assert "탐침 진행 중" in out


def test_gate_cli_passes_while_probe_in_progress(monkeypatch, capsys):
    """cli 경로: --no-cache 로 강제해도 건너뜀 + 정상 캐시면 통과.

    cli.main 은 실제 시계를 쓰므로 시드도 실제 시각 기준으로 잡는다
    (test_stale_accepted 의 _claude_ok_real 과 같은 패턴).
    """
    now = dt.datetime.now(dt.UTC)
    fresh = ProviderResult(
        id="devin",
        buckets=[
            Bucket(
                label="daily",
                window="1d",
                used_pct=11.0,
                resets_at=(now + dt.timedelta(hours=20)).isoformat(),
                scope=Scope("account"),
                horizon="now",
            )
        ],
        source="test",
        pool_class="spend",
    )
    cache.update_entry("devin", fresh, now.timestamp() - 80.0)
    monkeypatch.setattr(cli, "registry", lambda: {"devin": _skip_fetcher("devin")})
    rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    assert "stale_accepted=true" in out.out
    assert "탐침 진행 중" in out.out


# ---------------------------------------------------------------- AC4: 4개 CLI 풀


@pytest.mark.parametrize("pool", ["devin", "kimi", "grok", "kiro"])
def test_every_cli_probe_marks_lock_skip_not_failure(pool, monkeypatch, tmp_path):
    """각 provider 의 fetch() 는 잠금 보유 시 skip 마커를 단다 — 자식 미실행."""
    module = CLI_PROBES[pool]
    monkeypatch.setattr(module, "BINARY", "/bin/true")
    workdir = module.PROBE_WORKDIR  # conftest 가 tmp 로 돌려 놓은 값
    with proctrack.single_probe_lock(workdir) as acquired:
        assert acquired is True
        result = module.fetch()
    assert result.error and "이미 실행 중" in result.error
    assert result.error_kind == PROBE_IN_PROGRESS


@pytest.mark.parametrize("pool", ["devin", "kimi", "grok", "kiro"])
def test_every_cli_pool_gate_accepts_lock_skip_with_good_cache(pool):
    _seed(pool, at=_stale_seed_at(pool))
    results = [_collect_skip(pool, use_cache=True)]
    assert results[0].stale is True
    assert results[0].error_kind == PROBE_IN_PROGRESS
    res = gate_check(results, POOL_PROFILE[pool], today=TODAY, now=NOW)
    assert res.ok is True, res.reason
    assert res.stale_accepted is True
    assert "탐침 진행 중" in res.reason


@pytest.mark.parametrize("pool", ["devin", "kimi", "grok", "kiro"])
def test_every_cli_pool_gate_blocks_lock_skip_without_cache(pool):
    results = [_collect_skip(pool, use_cache=True)]
    res = gate_check(results, POOL_PROFILE[pool], today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True
