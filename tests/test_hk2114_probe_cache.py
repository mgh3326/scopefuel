"""hk 2114 — never probe an excluded/unsubscribed pool, and briefly cache failures.

The three invariants this file pins, stated as mutants:

- probe an excluded or unsubscribed pool again -> the zero-calls cases go RED;
- drop the negative cache -> the not-called-within-TTL cases go RED;
- never expire the negative cache -> the called-again-after-TTL cases go RED;
- send rate limits to the negative cache -> the backoff case goes RED.

Every case uses temp HOME/XDG/SCOPEFUEL_CACHE from the suite fixtures and stub
fetchers only; no real provider is reached.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from scopefuel import cache, cli, policy
from scopefuel.model import Bucket, ProviderResult, Scope


def _ok(provider_id: str, used: float = 10.0) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class="spend",
        buckets=[
            Bucket(
                label="7d",
                window="7d",
                used_pct=used,
                resets_at="2099-01-01T00:00:00+00:00",
                scope=Scope("account"),
                horizon="week",
            )
        ],
    )


class _Counter:
    def __init__(self, result: ProviderResult | None = None, exc: Exception | None = None):
        self.calls = 0
        self.result = result
        self.exc = exc
        self.pool_class = "spend"

    def __call__(self) -> ProviderResult:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


def _registry(**extra: object) -> dict[str, object]:
    """A registry with the named providers plus the usual spend pools."""
    fetchers: dict[str, object] = {
        "claude": _Counter(_ok("claude")),
        "codex": _Counter(_ok("codex")),
        "devin": _Counter(_ok("devin")),
        "grok": _Counter(_ok("grok")),
    }
    fetchers.update(extra)
    return fetchers


# --------------------------------------------------------------- never probe


@pytest.mark.parametrize("state", ["exclude", "unsubscribed"])
def test_excluded_and_unsubscribed_pools_make_zero_fetcher_calls(monkeypatch, capsys, state):
    kimi = _Counter(_ok("kimi"))
    fetchers = _registry(kimi=kimi)
    monkeypatch.setattr(cli, "registry", lambda: fetchers)

    if state == "exclude":
        policy.set_policy("kimi", "exclude", until=dt.date(2099, 1, 1), note="hk2114")
        reason_fragment = "policy excluded (kimi 풀 class=exclude"
    else:
        policy.set_subscribed("kimi", False)
        reason_fragment = "unsubscribed ([pools.kimi] subscribed=false"

    # collect: the fetcher is not called and the pool keeps a reason row.
    results = cache.collect(fetchers, list(fetchers))
    assert kimi.calls == 0
    kimi_result = next(item for item in results if item.id == "kimi")
    assert "탐침 안 함" in (kimi_result.error or "")

    # gate: refuses with the existing policy/subscription answer, no probe.
    gate_rc = cli.main(["gate", "-m", "kimi-k3", "--no-cache"])
    gate_out = capsys.readouterr()
    assert kimi.calls == 0
    assert gate_rc == (3 if state == "exclude" else 6)
    assert "정책 제외" in gate_out.err if state == "exclude" else "unsubscribed" in gate_out.err

    # recommend: the pool is folded with its reason, still no probe.
    assert cli.main(["--recommend", "S", "--no-cache"]) == 0
    rec_out = capsys.readouterr()
    assert kimi.calls == 0
    assert reason_fragment in rec_out.out


@pytest.mark.parametrize("state", ["exclude", "unsubscribed"])
def test_agy_excluded_or_unsubscribed_makes_zero_fetcher_calls(monkeypatch, capsys, state):
    """Director-requested pin: agy in either state is never probed."""
    agy = _Counter(_ok("agy"))
    fetchers = _registry(agy=agy)
    monkeypatch.setattr(cli, "registry", lambda: fetchers)

    if state == "exclude":
        policy.set_policy("agy", "exclude", until=dt.date(2099, 1, 1))
    else:
        policy.set_subscribed("agy", False)

    results = cache.collect(fetchers, list(fetchers), use_cache=False)
    assert agy.calls == 0
    assert "탐침 안 함" in (next(item for item in results if item.id == "agy").error or "")

    # oc-oss is a D3 profile on agy/3p; the early exclude check must answer
    # without the measurement the skip row no longer carries.
    rc = cli.main(["gate", "-m", "oc-oss", "--no-cache"])
    out = capsys.readouterr()
    assert agy.calls == 0
    assert rc == (3 if state == "exclude" else 6)
    assert ("정책 제외" in out.err) if state == "exclude" else ("unsubscribed" in out.err)

    assert cli.main(["--recommend", "A+", "--no-cache"]) == 0
    assert agy.calls == 0


@pytest.mark.parametrize("state", ["exclude", "unsubscribed"])
def test_status_shows_the_skip_reason_without_failing_the_exit_code(monkeypatch, capsys, state):
    """The not-probed row is visible, but it is not a measurement failure."""
    kimi = _Counter(_ok("kimi"))
    fetchers = _registry(kimi=kimi)
    monkeypatch.setattr(cli, "registry", lambda: fetchers)
    if state == "exclude":
        policy.set_policy("kimi", "exclude", until=dt.date(2099, 1, 1))
    else:
        policy.set_subscribed("kimi", False)

    assert cli.main([]) == 0
    out = capsys.readouterr()
    assert kimi.calls == 0
    assert "탐침 안 함" in out.out


# --------------------------------------------------------------- negative cache


@pytest.mark.parametrize(
    "failure",
    [
        ProviderResult(id="stub", error="n/a (parse failed)", error_kind="parse_error"),
        TimeoutError("probe timed out"),
    ],
    ids=["n-a", "timeout"],
)
def test_negative_cache_skips_a_failed_probe_until_its_ttl(failure):
    def fetch():
        calls.append(1)
        if isinstance(failure, Exception):
            raise failure
        return failure

    calls: list[int] = []
    fetchers = {"stub": fetch}

    first = cache.collect(fetchers, ["stub"], now=1000.0)[0]
    assert len(calls) == 1
    assert first.error

    inside = cache.collect(fetchers, ["stub"], now=1000.0 + cache.NEGATIVE_TTL_S - 1)[0]
    assert len(calls) == 1  # not probed again inside the window
    assert inside.error == first.error

    after = cache.collect(fetchers, ["stub"], now=1000.0 + cache.NEGATIVE_TTL_S + 1)[0]
    assert len(calls) == 2  # the window expired
    assert after.error == first.error


def test_negative_cache_serves_the_last_good_snapshot_as_stale():
    calls: list[int] = []

    def good():
        calls.append(1)
        return _ok("stub", used=7.0)

    def bad():
        calls.append(1)
        return ProviderResult(id="stub", error="n/a (parse failed)", error_kind="parse_error")

    cache.collect({"stub": good}, ["stub"], now=1000.0)
    failed = cache.collect({"stub": bad}, ["stub"], now=1200.0)[0]
    assert len(calls) == 2
    assert failed.stale is True
    assert failed.buckets[0].used_pct == 7.0
    assert "캐시 사용" in (failed.note or "")

    inside = cache.collect({"stub": bad}, ["stub"], now=1300.0)[0]
    assert len(calls) == 2  # served from the negative cache
    assert inside.stale is True
    assert inside.buckets[0].used_pct == 7.0

    cache.collect({"stub": bad}, ["stub"], now=1200.0 + cache.NEGATIVE_TTL_S + 1)
    assert len(calls) == 3  # expired, probed again


def test_no_cache_does_not_bypass_the_negative_cache():
    calls: list[int] = []

    def bad():
        calls.append(1)
        return ProviderResult(id="stub", error="n/a (parse failed)", error_kind="parse_error")

    cache.collect({"stub": bad}, ["stub"], now=1000.0)
    served = cache.collect({"stub": bad}, ["stub"], now=1010.0, use_cache=False)[0]
    assert len(calls) == 1
    assert served.error == "n/a (parse failed)"


def test_negative_ttl_can_be_overridden_per_pool(monkeypatch):
    monkeypatch.setitem(cache.NEGATIVE_TTL_S_BY_POOL, "stub", 30.0)
    calls: list[int] = []

    def bad():
        calls.append(1)
        return ProviderResult(id="stub", error="n/a", error_kind="parse_error")

    cache.collect({"stub": bad}, ["stub"], now=1000.0)
    cache.collect({"stub": bad}, ["stub"], now=1020.0)
    assert len(calls) == 1
    cache.collect({"stub": bad}, ["stub"], now=1031.0)
    assert len(calls) == 2


def test_rate_limit_keeps_backoff_and_does_not_open_the_negative_cache():
    calls: list[int] = []

    def limited():
        calls.append(1)
        return ProviderResult(id="stub", error="429 too many requests", error_kind="rate_limited")

    cache.collect({"stub": limited}, ["stub"], now=1000.0)
    assert len(calls) == 1
    assert cache.backoff_remaining("stub", 1000.0) > 0
    assert "negative_until" not in (cache._load().get("stub") or {})

    # Inside the backoff window: no fetch (backoff, not negative cache).
    cache.collect({"stub": limited}, ["stub"], now=1030.0)
    assert len(calls) == 1

    # Backoff expired but the negative TTL would still be running if the 429
    # had been sent there — it must probe again here.
    cache.collect({"stub": limited}, ["stub"], now=1061.0)
    assert len(calls) == 2


def test_positive_ttls_are_unchanged():
    assert cache.DEFAULT_TTL_S == 60.0
    assert cache.PROVIDER_TTL_S["claude"] == 180.0
    assert cache.PROVIDER_TTL_S["codex"] == 600.0
    assert cache.PROVIDER_TTL_S["grok"] == 1800.0

    calls: list[int] = []

    def good():
        calls.append(1)
        return _ok("claude")

    cache.collect({"claude": good}, ["claude"], now=1000.0)
    cache.collect({"claude": good}, ["claude"], now=1000.0 + cache.PROVIDER_TTL_S["claude"] - 1)
    assert len(calls) == 1  # fresh entry, no fetch
    cache.collect({"claude": good}, ["claude"], now=1000.0 + cache.PROVIDER_TTL_S["claude"] + 1)
    assert len(calls) == 2  # expired entry, fetch


# --------------------------------------------------------------- warm timing


def _seed_warm_cache(names: list[str], now: float) -> None:
    for name in names:
        cache.update_entry(name, _ok(name), now)


def _slow_failing(seconds: float = 20.0) -> _Counter:
    def fetch() -> ProviderResult:
        time.sleep(seconds)
        return ProviderResult(id="kimi", error="n/a (parse failed)", error_kind="parse_error")

    fetch.pool_class = "spend"  # type: ignore[attr-defined]
    fetch.calls = 0  # type: ignore[attr-defined]

    def counted() -> ProviderResult:
        fetch.calls += 1  # type: ignore[attr-defined]
        return fetch()

    counted.pool_class = "spend"  # type: ignore[attr-defined]
    counted.calls = 0  # type: ignore[attr-defined]
    return counted  # type: ignore[return-value]


def _warm_registry() -> tuple[dict[str, object], object]:
    slow = _slow_failing()
    fetchers: dict[str, object] = {
        "claude": _Counter(_ok("claude")),
        "codex": _Counter(_ok("codex")),
        "devin": _Counter(_ok("devin")),
        "grok": _Counter(_ok("grok")),
        "kimi": slow,
    }
    return fetchers, slow


def test_warm_recommend_and_gate_never_touch_a_negative_cached_slow_probe(monkeypatch):
    fetchers, slow = _warm_registry()
    monkeypatch.setattr(cli, "registry", lambda: fetchers)
    now = time.time()
    _seed_warm_cache([name for name in fetchers if name != "kimi"], now)
    cache.record_failure(
        "kimi", ProviderResult(id="kimi", error="n/a (parse failed)", error_kind="parse_error"), now
    )

    started = time.monotonic()
    assert cli.main(["--recommend", "A+"]) == 0
    assert cli.main(["gate", "-m", "kimi-k3"]) == 4
    elapsed = time.monotonic() - started

    assert slow.calls == 0
    assert elapsed < 3.0


def test_warm_recommend_and_gate_skip_an_excluded_slow_probe(monkeypatch):
    fetchers, slow = _warm_registry()
    monkeypatch.setattr(cli, "registry", lambda: fetchers)
    now = time.time()
    _seed_warm_cache(list(fetchers), now)
    policy.set_policy("kimi", "exclude", until=dt.date(2099, 1, 1))

    started = time.monotonic()
    assert cli.main(["--recommend", "A+"]) == 0
    assert cli.main(["gate", "-m", "kimi-k3"]) == 3
    elapsed = time.monotonic() - started

    assert slow.calls == 0
    assert elapsed < 3.0
