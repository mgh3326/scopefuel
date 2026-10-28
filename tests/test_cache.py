"""캐시 stale 폴백."""

from __future__ import annotations

import threading

from scopefuel import cache
from scopefuel.model import Bucket, ProviderResult, Scope


def test_cache_falls_back_to_stale_snapshot_with_age(monkeypatch):
    """agy 세션을 다 정리해도 마지막 값을 나이와 함께 쓸 수 있어야 한다."""
    good = ProviderResult(
        id="agy",
        buckets=[
            Bucket(
                label="gemini 5h", window="5h", used_pct=7.4, scope=Scope("group", "gemini"), horizon="now"
            )
        ],
    )
    fetchers = {"agy": lambda: good}
    first = cache.collect(fetchers, ["agy"], now=1000.0)
    assert first[0].stale is False

    def boom():
        raise RuntimeError("agy 세션이 실행 중이 아님")

    later = cache.collect({"agy": boom}, ["agy"], now=1000.0 + 600, ttl_s=60)
    assert later[0].stale is True
    assert later[0].age_s == 600
    assert later[0].buckets[0].used_pct == 7.4
    assert "캐시" in (later[0].note or "")
    assert cache.format_age(600) == "10분 전"


def test_cache_refuses_ancient_snapshot(monkeypatch):
    good = ProviderResult(id="x", buckets=[Bucket(label="5h", window="5h", used_pct=1.0)])
    cache.collect({"x": lambda: good}, ["x"], now=0.0)

    def boom():
        raise RuntimeError("dead")

    result = cache.collect({"x": boom}, ["x"], now=cache.STALE_MAX_S + 10)[0]
    assert result.error == "dead"
    assert result.buckets == []


def test_warning_result_is_not_cached(monkeypatch):
    calls = 0

    def warning():
        nonlocal calls
        calls += 1
        return ProviderResult(id="clinepass", warning="인증 실패 (HTTP 401)")

    first = cache.collect({"clinepass": warning}, ["clinepass"], now=1000.0)
    second = cache.collect({"clinepass": warning}, ["clinepass"], now=1001.0)

    assert calls == 2
    assert first[0].status == second[0].status == "warning"


def test_cache_collects_misses_in_parallel_and_preserves_name_order():
    # 벽시계로 병렬성을 재면 CI 부하에 흔들린다 — 직렬 0.35s 대비 임계 0.30s 는 여유가
    # 0.05s 뿐이라 느린 러너에서 0.356s 가 나와 실패했다. 그래서 소요 시간(대리 지표)이
    # 아니라 "동시에 실행 중인 fetcher 가 몇 개였나" 라는 성질을 직접 관측한다.
    names = ["slow", "fast", "middle"]
    barrier = threading.Barrier(len(names), timeout=10)
    running = 0
    peak = 0
    guard = threading.Lock()

    def make_fetcher(name):
        def fetch():
            nonlocal running, peak
            with guard:
                running += 1
                peak = max(peak, running)
            try:
                # 전원이 도달해야 통과한다 — 직렬이면 여기서 타임아웃으로 실패한다.
                barrier.wait()
            finally:
                with guard:
                    running -= 1
            return ProviderResult(id=name)

        return fetch

    results = cache.collect({name: make_fetcher(name) for name in names}, names, use_cache=False)

    assert [result.id for result in results] == names
    assert peak == len(names)  # 전원이 동시에 실행 중이었다


def test_cache_fetch_exception_does_not_cancel_other_providers():
    def boom():
        raise RuntimeError("broken")

    results = cache.collect(
        {"bad": boom, "good": lambda: ProviderResult(id="good")},
        ["bad", "good"],
        use_cache=False,
    )

    assert results[0].id == "bad"
    assert results[0].error == "broken"
    assert results[1].id == "good"
    assert results[1].error is None


def test_cache_hit_does_not_call_fetcher():
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        return ProviderResult(id="claude")

    cache.collect({"claude": fetch}, ["claude"], now=1000.0)
    cached = cache.collect({"claude": fetch}, ["claude"], now=1000.0 + 179.0)

    assert calls == 1
    assert cached[0].stale is False


def test_provider_ttls_and_default_are_used_at_boundaries():
    names = [*cache.PROVIDER_TTL_S, "unknown"]
    calls = {name: 0 for name in names}

    def make_fetcher(name):
        def fetch():
            calls[name] += 1
            return ProviderResult(id=name)

        return fetch

    fetchers = {name: make_fetcher(name) for name in names}
    for name in names:
        ttl = cache.PROVIDER_TTL_S.get(name, cache.DEFAULT_TTL_S)
        cache.collect(fetchers, [name], now=1000.0)
        before = cache.collect(fetchers, [name], now=1000.0 + ttl - 1)
        assert before[0].stale is False
        assert calls[name] == 1
        cache.collect(fetchers, [name], now=1000.0 + ttl + 1)
        assert calls[name] == 2


def test_cache_ttl_override_applies_to_every_provider():
    names = ["claude", "grok", "unknown"]
    calls = {name: 0 for name in names}

    def make_fetcher(name):
        def fetch():
            calls[name] += 1
            return ProviderResult(id=name)

        return fetch

    fetchers = {name: make_fetcher(name) for name in names}
    cache.collect(fetchers, names, now=1000.0)
    cache.collect(fetchers, names, now=1000.0 + 31.0, ttl_s=30.0)

    assert calls == {name: 2 for name in names}
